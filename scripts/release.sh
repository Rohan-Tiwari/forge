#!/usr/bin/env bash
# scripts/release.sh — automate version bump + tag + push
#
# Usage:
#   ./scripts/release.sh patch    # 0.2.0 → 0.2.1
#   ./scripts/release.sh minor    # 0.2.x → 0.3.0
#   ./scripts/release.sh major    # 0.x.y → 1.0.0
#   ./scripts/release.sh rc       # 0.2.0 → 0.2.1-rc.1 (Test PyPI only)
#
# Run from repo root.

set -euo pipefail

BUMP_TYPE="${1:-}"
if [[ -z "$BUMP_TYPE" ]]; then
    echo "Usage: $0 [patch|minor|major|rc]" >&2
    exit 2
fi

# Sanity: must be on main with a clean working tree.
if [[ "$(git symbolic-ref --short HEAD)" != "main" ]]; then
    echo "error: must be on main branch" >&2
    exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
    echo "error: working tree is not clean. Commit or stash first." >&2
    git status --short
    exit 1
fi

# Read current version from pyproject.toml
current=$(grep '^version = ' pyproject.toml | head -1 | sed 's/version = "\(.*\)"/\1/')
echo "current version: $current"

# Compute new version
IFS='.-' read -ra parts <<< "$current"
major="${parts[0]}"
minor="${parts[1]}"
patch="${parts[2]}"

case "$BUMP_TYPE" in
    patch)
        new="$major.$minor.$((patch + 1))"
        ;;
    minor)
        new="$major.$((minor + 1)).0"
        ;;
    major)
        new="$((major + 1)).0.0"
        ;;
    rc)
        # rc bumps: 0.2.0 → 0.2.1-rc.1, 0.2.1-rc.1 → 0.2.1-rc.2
        if [[ "$current" == *-rc.* ]]; then
            rc_num="${current##*-rc.}"
            base="${current%-rc.*}"
            new="$base-rc.$((rc_num + 1))"
        else
            new="$major.$minor.$((patch + 1))-rc.1"
        fi
        ;;
    *)
        echo "error: unknown bump type '$BUMP_TYPE' (use patch/minor/major/rc)" >&2
        exit 2
        ;;
esac

echo "new version:     $new"
echo ""

# Confirm
read -r -p "Bump $current → $new and tag? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "aborted"
    exit 1
fi

# Update version in pyproject.toml
sed -i.bak "s/^version = \".*\"/version = \"$new\"/" pyproject.toml
rm -f pyproject.toml.bak

# Update version in src/forge/__init__.py
sed -i.bak "s/^__version__ = \".*\"/__version__ = \"$new\"/" src/forge/__init__.py
rm -f src/forge/__init__.py.bak

# Generate a CHANGELOG section from git log since the last tag.
last_tag=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
if [[ -n "$last_tag" ]]; then
    echo "commits since $last_tag:"
    git log "${last_tag}..HEAD" --pretty=format:"  - %s" --no-merges | head -30
fi
echo ""

# Commit + tag
git add pyproject.toml src/forge/__init__.py
git commit -m "chore: bump version to $new"
git tag -a "v$new" -m "v$new"

echo ""
echo "✓ tagged v$new"
echo ""
echo "To push:"
echo "  git push origin main"
echo "  git push origin v$new"
echo ""
if [[ "$BUMP_TYPE" == "rc" ]]; then
    echo "(RC tags publish to Test PyPI via .github/workflows/publish.yml)"
else
    echo "(Stable tags publish to real PyPI via .github/workflows/publish.yml)"
fi

# Contributing to Forge

Thanks for considering a contribution. This document covers how to set up
a development environment, the conventions we follow, and how to ship a
release.

## Development setup

```bash
git clone https://github.com/Rohan-Tiwari/forge.git
cd forge
pip install -e ".[dev]"

# Install pre-commit hooks (runs ruff + leak-scan on every commit)
pip install pre-commit
pre-commit install
```

Verify:
```bash
forge doctor
pytest                # ~10s, no live integration
ruff check src/ tests/
```

## Code conventions

- **Type annotations on every public function.** mypy isn't strict yet but
  we're moving that way.
- **Docstrings on every module + public class + non-trivial function.**
  Explain *why*, not just *what* — the diff already shows what.
- **Error types from `forge.errors`.** Anything user-facing raises a
  `ForgeError` subclass so the CLI can render it cleanly.
- **Tests for safety-critical paths.** Every new tool/skill/exec path
  needs a test that proves the safety boundary fires.
- **No `print()` in src/forge/.** Use the module's logger (`forge.log.get_logger`)
  for diagnostics, or `rich.console` in `cli.py` for user-facing output.

## Adding a new feature

1. Open an issue describing what + why.
2. Branch from `main`, prefix the branch `feat/`, `fix/`, or `docs/`.
3. Add tests first (or at least alongside).
4. Run `ruff check src/ tests/ && pytest` before pushing.
5. Update `CHANGELOG.md` under `[Unreleased]`.
6. Open a PR. CI runs on macOS + Ubuntu across Python 3.11/3.12/3.13.

## Release flow

We use semantic versioning. Bumps are triggered by a single command:

```bash
./scripts/release.sh patch     # 0.2.0 → 0.2.1
./scripts/release.sh minor     # 0.2.x → 0.3.0
./scripts/release.sh major     # 0.x.y → 1.0.0
./scripts/release.sh rc        # 0.2.0 → 0.2.1-rc.1 (Test PyPI only)
```

The script:
1. Updates the version in `pyproject.toml` + `src/forge/__init__.py`.
2. Generates a CHANGELOG section from git log since the last tag.
3. Creates an annotated tag.
4. Pushes commits + tag.

The tag push triggers `.github/workflows/publish.yml`, which:
- Builds wheel + sdist.
- Publishes to **Test PyPI** for `-rc` tags.
- Publishes to **real PyPI** for stable tags.

Both use PyPI Trusted Publishing (no API tokens stored in GitHub).

## Reporting security issues

See [SECURITY.md](SECURITY.md). Don't open public issues for security
vulnerabilities — use the contact in that file.

## License

By contributing you agree your contributions are licensed under the
Apache-2.0 License (same as the project).

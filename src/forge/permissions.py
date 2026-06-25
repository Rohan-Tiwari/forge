"""forge.permissions — remember what the user has approved.

Two scopes:
  * **Session** — in-memory, lasts for one Session lifetime. "always allow this
    cell-pattern in this session" without writing to disk. Cleared on exit.
  * **Persistent** — `~/.forge/permissions.toml`. "always allow Bash(git:*)"
    saved across sessions. Edit by hand or via `forge skill permit`.

The matching is pattern-based, modeled after Claude Code's allow rules:

  Bash(git:*)       — any Bash whose first token is `git`
  Bash(rg:*)        — any Bash whose first token is `rg`
  Write(./out/**)   — any Write to a path matching this glob
  Network(api.github.com)  — exact host match
  Skill(pdf-extract)       — auto-allow this skill's cells
  *                  — match anything (only for explicit blanket allow-this-session)

A grant is `(scope, pattern, kind)`. Kind is "allow" or "always_confirm" — the
latter is a soft "remember to ask again next time" that only matters if we
later add silent denial.
"""
from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomli_w  # for writing
    _HAVE_TOML_W = True
except ImportError:
    _HAVE_TOML_W = False


PERMISSIONS_PATH = Path(
    os.environ.get("FORGE_PERMISSIONS",
                   str(Path.home() / ".forge" / "permissions.toml"))
).expanduser()


@dataclass
class PermissionGrant:
    """A single 'always allow this' rule."""

    pattern: str
    kind: str = "allow"   # "allow" | "always_confirm"

    def matches(self, action: Action) -> bool:
        """Does this grant cover the given action?"""
        return _action_matches_pattern(action, self.pattern)


@dataclass
class Action:
    """A single action a cell wants to perform.

    The Session derives one Action per side-effect from a Preview. The
    permission store is asked: "is this Action covered by an existing grant?"
    If yes → allow. If no → prompt the user, optionally remember the answer.
    """

    kind: str           # "Bash" | "Write" | "Edit" | "Network" | "Skill"
    target: str         # the path / hostname / skill name / first cmd token

    def to_pattern(self) -> str:
        """Suggest a default 'always allow' pattern for this action.

        Bash with first token 'git' → 'Bash(git:*)'
        Write to ./out/foo.csv → 'Write(./out/**)'  (parent dir + **)
        Network api.github.com → 'Network(api.github.com)'
        """
        if self.kind == "Bash":
            tok = self.target.split()[0] if self.target else ""
            if tok:
                return f"Bash({tok}:*)"
            return "Bash(*)"
        if self.kind in {"Write", "Edit"}:
            # Use parent dir + **.
            p = Path(self.target)
            parent = p.parent if p.parent != Path() else Path(".")
            return f"{self.kind}({parent}/**)"
        if self.kind == "Network":
            return f"Network({self.target})"
        if self.kind == "Skill":
            return f"Skill({self.target})"
        return f"{self.kind}({self.target})"


def _action_matches_pattern(action: Action, pattern: str) -> bool:
    """Implement the matcher for grants.

    Pattern shapes:
      Kind(args)  — the whole thing
      *           — wildcard everything
    """
    if pattern == "*":
        return True
    if "(" not in pattern or not pattern.endswith(")"):
        return False
    kind, _, rest = pattern.partition("(")
    args = rest[:-1]  # strip closing paren

    if kind != action.kind:
        return False

    if action.kind == "Bash":
        # `git:*` matches first token = "git". `*` matches anything.
        if args == "*":
            return True
        if ":" in args:
            tok, _, suffix = args.partition(":")
            first = action.target.split()[0] if action.target else ""
            if tok != first:
                return False
            # suffix is currently always '*' — we don't constrain further.
            return True
        # Plain string match
        return action.target.startswith(args)

    if action.kind in {"Write", "Edit"}:
        # Glob on the path. Standard fnmatch.* doesn't recurse on `**`, so
        # we expand `**` to fnmatch's `*` for path-component matching, plus
        # a parent-directory containment check to handle nested paths.
        if "**" in args:
            # Treat **/foo as "any descendant", and prefix/** as "any descendant of prefix".
            # We split on "**" and check that each non-empty piece appears in order.
            normalized = args.replace("/**", "*").replace("**/", "*").replace("**", "*")
            target_str = str(Path(action.target))
            target_resolved = str(Path(action.target).resolve())
            if fnmatch.fnmatch(target_str, normalized) or fnmatch.fnmatch(target_resolved, normalized):
                return True
            # Also: pattern "./out/**" should cover anything under ./out/
            base = args.split("**", 1)[0].rstrip("/")
            if base:
                base_path = Path(base).expanduser()
                try:
                    Path(action.target).expanduser().resolve().relative_to(base_path.resolve())
                    return True
                except (ValueError, OSError):
                    pass
            return False
        return fnmatch.fnmatch(str(Path(action.target)), args) or \
               fnmatch.fnmatch(str(Path(action.target).resolve()), args)

    if action.kind == "Network":
        return args == "*" or args == action.target

    if action.kind == "Skill":
        return args == action.target or args == "*"

    return False


# =============================================================================
# Permission store
# =============================================================================


@dataclass
class PermissionStore:
    """Holds session + persistent grants, makes is_allowed() decisions."""

    session_grants: list[PermissionGrant] = field(default_factory=list)
    persistent_grants: list[PermissionGrant] = field(default_factory=list)

    @classmethod
    def load(cls) -> PermissionStore:
        """Load persistent grants from ~/.forge/permissions.toml."""
        store = cls()
        if not PERMISSIONS_PATH.exists():
            return store
        try:
            with PERMISSIONS_PATH.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return store
        for entry in data.get("allow", []):
            if isinstance(entry, str):
                store.persistent_grants.append(PermissionGrant(pattern=entry))
            elif isinstance(entry, dict) and "pattern" in entry:
                store.persistent_grants.append(PermissionGrant(
                    pattern=entry["pattern"],
                    kind=entry.get("kind", "allow"),
                ))
        return store

    def save(self) -> None:
        """Persist `persistent_grants` back to disk. Session grants stay in mem."""
        if not _HAVE_TOML_W:
            return  # silently skip — install tomli_w to enable write
        PERMISSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "allow": [
                {"pattern": g.pattern, "kind": g.kind}
                for g in self.persistent_grants
            ],
        }
        with PERMISSIONS_PATH.open("wb") as f:
            tomli_w.dump(data, f)

    def is_allowed(self, action: Action) -> bool:
        """Is this action pre-approved by any grant?"""
        for g in self.session_grants:
            if g.matches(action):
                return True
        for g in self.persistent_grants:
            if g.matches(action):
                return True
        return False

    def grant_session(self, pattern: str) -> None:
        """Add a session-only grant (forgotten on Session.close)."""
        self.session_grants.append(PermissionGrant(pattern=pattern))

    def grant_persistent(self, pattern: str) -> None:
        """Add a persistent grant and save to disk."""
        self.persistent_grants.append(PermissionGrant(pattern=pattern))
        self.save()


# =============================================================================
# Action extraction from a Preview
# =============================================================================


def actions_for_preview(preview) -> list[Action]:  # type: ignore[no-untyped-def]
    """Extract the list of Actions a Preview implies."""
    actions: list[Action] = []
    for fc in preview.file_changes:
        kind = "Edit" if fc.kind == "modify" else "Write"
        actions.append(Action(kind=kind, target=fc.path))
    for n in preview.network_calls:
        actions.append(Action(kind="Network", target=n))
    for b in preview.bash_commands:
        actions.append(Action(kind="Bash", target=b))
    return actions

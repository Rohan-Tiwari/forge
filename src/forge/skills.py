"""forge.skills — Anthropic-style SKILL.md folder registry.

A skill is a folder containing:
    SKILL.md          — required, YAML frontmatter + markdown body
    helpers.py        — optional Python module (importable as skills.<name>)
    references/       — optional, files loaded only on read
    scripts/          — optional, AST-scanned at install time
    assets/           — optional data files

Progressive disclosure (the [agentskills.io](https://agentskills.io/) pattern,
mirroring the v10 harness's skill-resolution):

    Catalog (always) — every skill's name + one-line description is injected
                       into the system prompt at session start, byte-capped.
                       This is cheap: enough for the model to know what's on
                       hand without paying the full-text context cost.
    Full text (lazy) — a skill's complete SKILL.md body is loaded ONLY when
                       selected, via `read_skill(name)`. The agent can also
                       search mid-task with `find_skill(query)`.

Skill search (`find`) ranks the catalog against a query. It uses an LLM ranker
(the `skillsearch` router role) when a router is wired — matching by meaning,
not keywords — and falls back to token-overlap scoring offline or when the
catalog is small.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from forge.config import SKILLS_HOME

# =============================================================================
# Schema
# =============================================================================


class SkillFrontmatter(BaseModel):
    """The YAML block at the top of a SKILL.md."""

    name: str
    description: str = Field(..., max_length=2000)
    when_to_use: str = ""
    model: str = "inherit"          # inherit | sonnet | opus | gpt-5 | gpt-oss:20b | ...
    effort: str = "medium"          # low | medium | high
    allowed_tools: list[str] = Field(default_factory=list)
    requires_mcp: list[str] = Field(default_factory=list)
    requires_env: list[str] = Field(default_factory=list)
    license: str = "Unspecified"
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass
class Skill:
    """An installed, parsed skill ready for activation."""

    path: Path
    frontmatter: SkillFrontmatter
    body: str

    @property
    def name(self) -> str:
        return self.frontmatter.name

    @property
    def description(self) -> str:
        return self.frontmatter.description

    @property
    def helpers_path(self) -> Path | None:
        p = self.path / "helpers.py"
        return p if p.exists() else None

    @property
    def references_dir(self) -> Path | None:
        p = self.path / "references"
        return p if p.is_dir() else None

    def render_summary(self) -> str:
        """One-liner injected into the system prompt for Tier 1."""
        return f"- **{self.name}**: {self.description}"


# =============================================================================
# Loading
# =============================================================================


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_skill_md(text: str) -> tuple[SkillFrontmatter, str]:
    """Split SKILL.md into validated frontmatter and body."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("SKILL.md missing YAML frontmatter (must start with '---')")
    yaml_text, body = m.group(1), m.group(2)
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"frontmatter YAML parse error: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("frontmatter must be a YAML mapping")
    return SkillFrontmatter.model_validate(data), body.strip()


def load_skill(path: Path) -> Skill:
    """Read and validate one skill folder."""
    skill_md = path / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"no SKILL.md in {path}")
    fm, body = parse_skill_md(skill_md.read_text(encoding="utf-8"))
    return Skill(path=path, frontmatter=fm, body=body)


def discover_skills(roots: list[Path] | None = None) -> list[Skill]:
    """Scan standard skill locations.

    Searched, in order:
      <each `roots` entry>
      ./skills/                    — project-local skills checked into the repo
      ~/.skills/                   — user-installed skills
      ~/.skills/installed/<source>/ — pinned skill installs from the web
    """
    seen_names: set[str] = set()
    skills: list[Skill] = []

    candidates: list[Path] = list(roots or [])
    candidates.append(Path.cwd() / "skills")
    candidates.append(SKILLS_HOME)

    for root in candidates:
        if not root.exists():
            continue
        # Either each subdir is a skill, or there's a SKILL.md right here.
        if (root / "SKILL.md").exists():
            try:
                s = load_skill(root)
            except (FileNotFoundError, ValueError):
                continue
            if s.name not in seen_names:
                seen_names.add(s.name)
                skills.append(s)
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            md = child / "SKILL.md"
            if md.exists():
                try:
                    s = load_skill(child)
                except (FileNotFoundError, ValueError):
                    continue
                if s.name not in seen_names:
                    seen_names.add(s.name)
                    skills.append(s)
            else:
                # Could be a `<source>/<skill@sha>/` layout; recurse one level
                for grandchild in sorted(child.iterdir()):
                    if grandchild.is_dir() and (grandchild / "SKILL.md").exists():
                        try:
                            s = load_skill(grandchild)
                        except (FileNotFoundError, ValueError):
                            continue
                        if s.name not in seen_names:
                            seen_names.add(s.name)
                            skills.append(s)
    return skills


# =============================================================================
# Registry — what the Session uses.
# =============================================================================


@dataclass
class SkillRegistry:
    skills: list[Skill] = field(default_factory=list)
    eager_token_cap: int = 5000
    # Optional ModelRouter for LLM-based skill ranking. When None, find()
    # falls back to token-overlap scoring. Wired by Session.start().
    router: Any = None
    # Catalogs at or below this size skip the LLM ranker entirely (overlap
    # scoring is fine and free for a handful of skills).
    llm_search_threshold: int = 8

    @classmethod
    def scan(cls, roots: list[Path] | None = None) -> SkillRegistry:
        return cls(skills=discover_skills(roots))

    def get(self, name: str) -> Skill | None:
        for s in self.skills:
            if s.name == name:
                return s
        return None

    def read_full(self, name: str) -> str | None:
        """Progressive disclosure: return a skill's FULL SKILL.md body on
        demand. Returns None if no such skill. This is what `read_skill(name)`
        calls — the catalog carries only descriptions, so the full text is
        paid for in context only when a task actually selects the skill.
        """
        s = self.get(name)
        if s is None:
            return None
        return s.body

    def render_for_system_prompt(self) -> str:
        """Catalog — eager metadata (name + description) only, never full
        bodies. Byte-capped at `eager_token_cap` (~chars*4). Tells the model
        to `read_skill(name)` for a skill's full instructions and
        `find_skill(query)` to search by meaning.
        """
        if not self.skills:
            return ""
        lines = ["## Available skills (catalog)", "",
                 "These are the skills installed on this machine — names and "
                 "one-line descriptions only. Call `read_skill(name)` to load a "
                 "skill's full instructions when you decide to use it, or "
                 "`find_skill(query)` to search by meaning.", ""]
        used = sum(len(line) for line in lines)
        for i, s in enumerate(self.skills):
            line = s.render_summary()
            # Naive char-as-tokens estimate. Conservative — tokens are ~4 chars.
            if used + len(line) > self.eager_token_cap * 4:
                lines.append(f"- ... and {len(self.skills) - i} more "
                             f"(use find_skill())")
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def find(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        """Search the catalog for skills relevant to `query`.

        Uses the LLM ranker (via the `skillsearch` router role) when a router
        is wired AND the catalog is larger than `llm_search_threshold` —
        matching by meaning so an oddly-phrased request still finds the right
        skill. Falls back to token-overlap scoring when there's no router, the
        catalog is small, or the LLM call fails. Always returns
        [{name, description, score}] ranked best-first.
        """
        if not self.skills:
            return []
        if self.router is not None and len(self.skills) > self.llm_search_threshold:
            try:
                ranked = self._find_llm(query, top_k=top_k)
                if ranked:
                    return ranked
            except Exception:  # noqa: BLE001 — never let ranking break a tool call
                pass  # fall through to overlap scoring
        return self._find_overlap(query, top_k=top_k)

    def _find_overlap(self, query: str, *, top_k: int) -> list[dict[str, Any]]:
        """Token-overlap fallback ranker (no model needed)."""
        q_terms = {t.lower() for t in re.findall(r"\w+", query) if len(t) > 2}
        if not q_terms:
            return []
        scored: list[tuple[float, Skill]] = []
        for s in self.skills:
            haystack = (s.name + " " + s.description + " "
                        + s.frontmatter.when_to_use).lower()
            haystack_terms = set(re.findall(r"\w+", haystack))
            overlap = len(q_terms & haystack_terms)
            if overlap == 0:
                continue
            # Boost name matches
            if any(t in s.name.lower() for t in q_terms):
                overlap += 2
            scored.append((overlap, s))
        scored.sort(key=lambda x: -x[0])
        return [
            {"name": s.name, "description": s.description, "score": float(score)}
            for score, s in scored[:top_k]
        ]

    def _find_llm(self, query: str, *, top_k: int) -> list[dict[str, Any]]:
        """LLM ranker — ask the `skillsearch` role to pick the relevant skills
        by meaning. Returns [] if the model names nothing usable (caller then
        falls back to overlap). Raises on transport errors (caught by find()).
        """
        catalog = "\n".join(
            f"- {s.name}: {s.description}" for s in self.skills
        )
        system = (
            "You match a user request to the most relevant skills from a "
            "catalog. Return ONLY the names of skills that genuinely fit, most "
            "relevant first, one per line. If none fit, return nothing. Do not "
            "invent names — copy them exactly from the catalog."
        )
        user = f"Request:\n{query}\n\nSkill catalog:\n{catalog}"
        completion = self.router.complete(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            role="skillsearch",
        )
        # Parse names from the response; keep only real catalog entries, in the
        # model's order, deduplicated.
        by_name = {s.name: s for s in self.skills}
        picked: list[Skill] = []
        seen: set[str] = set()
        for raw in completion.content.splitlines():
            token = raw.strip().lstrip("-*0123456789. ").strip("`\"' ")
            if token in by_name and token not in seen:
                seen.add(token)
                picked.append(by_name[token])
        return [
            {"name": s.name, "description": s.description,
             "score": float(len(picked) - i)}
            for i, s in enumerate(picked[:top_k])
        ]

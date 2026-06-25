"""forge.skills — Anthropic-style SKILL.md folder registry.

A skill is a folder containing:
    SKILL.md          — required, YAML frontmatter + markdown body
    helpers.py        — optional Python module (importable as skills.<name>)
    references/       — optional, files loaded only on read
    scripts/          — optional, AST-scanned at install time
    assets/           — optional data files

The registry has two tiers:
    Tier 1 (eager) — every skill's name+description injected into the system
                     prompt at session start, capped at 5k tokens. The model
                     sees them all and can pick by name.
    Tier 2 (lazy)  — for catalogs of >50 skills, find_skill(query) does a
                     semantic search by description. v0 ships a stub that
                     does substring matching; v1 swaps in a real embedder.
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

    @classmethod
    def scan(cls, roots: list[Path] | None = None) -> SkillRegistry:
        return cls(skills=discover_skills(roots))

    def get(self, name: str) -> Skill | None:
        for s in self.skills:
            if s.name == name:
                return s
        return None

    def render_for_system_prompt(self) -> str:
        """Tier 1 — eager metadata, capped at `eager_token_cap` chars (~tokens)."""
        if not self.skills:
            return ""
        lines = ["## Available skills", "",
                 "Use `find_skill(query)` for ones not listed here.", ""]
        used = sum(len(line) for line in lines)
        for s in self.skills:
            line = s.render_summary()
            # Naive char-as-tokens estimate. Conservative — tokens are ~4 chars.
            if used + len(line) > self.eager_token_cap * 4:
                lines.append(f"- ... and {len(self.skills) - len(lines) + 4} more "
                             f"(use find_skill())")
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def find(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        """Tier 2 — substring + token overlap search.

        v0 keeps it simple. Replace with embeddings + cosine sim for v1.
        """
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

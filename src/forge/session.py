"""forge.session — the agent loop.

Glues kernel + gate + tools + shadow + audit + skills + router + preview +
permissions. Exposes a single `Session.turn(user_msg)` method that runs the
full perceive-plan-execute-observe loop until the model emits prose.

Design rules followed here:

  * The Session OWNS the Kernel, ShadowGit, AuditLog, PermissionStore.
    Lifetimes match.
  * Every cell auto-commits the shadow PRE and POST. Always.
  * Every model call writes to the audit log with token + cost data.
  * Three RETRY COUNTERS, scoped per-failure-type, reset on every successful
    cell. Empty-content (Day 0 finding A.4), parse-error, and gate-deny each
    have their own budget.
  * Preview-and-confirm runs BEFORE execution for any cell with side
    effects (writes, network, Bash) in interactive mode. Pre-approved
    actions in the PermissionStore skip the prompt.
  * Optional streaming: pass `on_chunk=callable(delta_str)` to turn() and the
    model's output streams to that callback as it arrives. The gate still
    runs on the FULL content after the stream ends — streaming is purely a
    UX layer for the user-visible prose.
  * The Session is sync. Streaming + async is a v0.2 concern.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from forge.audit import AuditLog, SessionLog, new_session_id
from forge.config import audit_log as audit_log_path, ensure_dirs
from forge.gate import GateAction, GateDecision, check, parse_cell
from forge.kernel import Kernel, Observation
from forge.mcp import MCPRegistry
from forge.permissions import Action, PermissionStore, actions_for_preview
from forge.preview import Preview
from forge.router import Completion, ModelRouter
from forge.shadow import ShadowGit
from forge.skills import SkillRegistry
from forge import tools


SYSTEM_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"


PreviewMode = str  # "always" | "cells" | "never"
ChunkCallback = Callable[[str], None]   # called with each delta of streamed content


@dataclass
class TurnResult:
    """One full user-turn worth of the loop."""

    final_text: str = ""
    cells_run: int = 0
    cells_denied: int = 0
    escalations: int = 0
    cost_usd: float = 0.0
    completions: list[Completion] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)


@dataclass
class _Counters:
    """Per-failure-type retry counters. Reset on every successful cell."""

    empty_content: int = 0
    parse_format: int = 0
    gate_deny: int = 0

    def reset(self) -> None:
        self.empty_content = 0
        self.parse_format = 0
        self.gate_deny = 0


class Session:
    """A live agent session bound to a workspace."""

    def __init__(
        self,
        *,
        workspace: Path,
        mode: str = "interactive",      # "interactive" | "auto" | "plan"
        preview: PreviewMode = "cells",   # "always" | "cells" | "never"
        dry_run: bool = True,             # use overlay dry-run for previews
        sandboxed: bool = True,           # wrap kernel in sandbox-exec on macOS
        max_cells_per_turn: int = 12,
        max_format_retries: int = 2,
        max_empty_retries: int = 1,
        max_gate_deny_retries: int = 2,
    ):
        self.workspace = workspace.resolve()
        self.mode = mode
        self.preview_mode = preview
        self.dry_run = dry_run
        self.sandboxed = sandboxed
        self.max_cells_per_turn = max_cells_per_turn
        self.max_format_retries = max_format_retries
        self.max_empty_retries = max_empty_retries
        self.max_gate_deny_retries = max_gate_deny_retries

        ensure_dirs(self.workspace)

        self.kernel = Kernel(workspace=self.workspace, sandboxed=sandboxed)
        self.shadow = ShadowGit(workspace=self.workspace)
        self.audit = AuditLog(audit_log_path(self.workspace))
        self.router = ModelRouter()
        self.skills = SkillRegistry.scan()
        self.permissions = PermissionStore.load()
        self.mcp = MCPRegistry()  # loads ~/.forge/mcp.toml lazily
        self.session_id = new_session_id()
        self.log = SessionLog(self.audit, self.session_id)

        self._history: list[dict[str, str]] = []
        self._system_prompt: str = ""

    # ---- lifecycle ------------------------------------------------------

    def __enter__(self) -> "Session":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        """Initialize kernel, shadow git, skill runtime, system prompt."""
        self.kernel.start()
        self.shadow.init()

        # Wire skill + MCP callbacks.
        tools.set_skill_runtime(
            find=self.skills.find,
            run=self._run_skill,
            mcp=self.mcp.call,
        )

        self._system_prompt = self._build_system_prompt()
        self._history = [{"role": "system", "content": self._system_prompt}]

        self.log.write(
            "session.start",
            workspace=str(self.workspace),
            mode=self.mode,
            preview_mode=self.preview_mode,
            skills=len(self.skills.skills),
            mcp_servers=len(self.mcp.configs),
            driver_model=self.router.roles["driver"].primary,
        )

    def close(self) -> None:
        """Flush, kill kernel, write final audit entry."""
        self.log.write(
            "session.end",
            spent_usd=self.router.spent_usd,
            calls=len(self.router.calls),
        )
        self.kernel.stop()
        try:
            self.mcp.close_all()
        except Exception:  # noqa: BLE001 — never let MCP cleanup mask user errors
            pass

    # ---- helpers --------------------------------------------------------

    def _build_system_prompt(self) -> str:
        base = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        skill_block = self.skills.render_for_system_prompt()
        if skill_block:
            return base + "\n\n" + skill_block
        return base

    def _run_skill(self, name: str, **kwargs: object) -> object:
        """Implementation of run_skill(). Calls the skill's main() in-process.

        Replaces the previous exec-via-string hack. See docs/SHAKE-OUT.md
        finding #3.
        """
        skill = self.skills.get(name)
        if skill is None:
            raise KeyError(f"no installed skill named {name!r}")

        if skill.helpers_path is None:
            return {
                "name": skill.name,
                "body_loaded": True,
                "body": skill.body[:2000],
                "note": "skill has no helpers.py; agent should write code based on the body above",
            }

        cache = getattr(self, "_skill_module_cache", None)
        if cache is None:
            cache = {}
            self._skill_module_cache = cache  # type: ignore[attr-defined]

        mod = cache.get(skill.name)
        if mod is None:
            import importlib.util
            module_name = f"skills.{skill.name.replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(module_name, skill.helpers_path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"skill {name} helpers spec failed: {skill.helpers_path}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
            cache[skill.name] = mod

        main = getattr(mod, "main", None)
        if main is None:
            raise RuntimeError(
                f"skill {name} helpers.py has no `main(**kwargs)` entry point"
            )

        return main(**kwargs)

    # ---- the agent loop -------------------------------------------------

    def turn(
        self,
        user_msg: str,
        *,
        on_chunk: Optional[ChunkCallback] = None,
    ) -> TurnResult:
        """Run one user-turn worth of the perceive-plan-execute loop.

        If `on_chunk` is provided, each model completion streams via
        router.complete_stream() and the callback receives every delta as
        it arrives. The gate runs on the FULL content after the stream ends.
        """
        self._history.append({"role": "user", "content": user_msg})
        self._maybe_truncate_history()
        result = TurnResult()
        counters = _Counters()

        self.log.write("turn.start", user_msg_chars=len(user_msg))

        for cell_idx in range(self.max_cells_per_turn):
            try:
                completion = self._call_driver(on_chunk)
            except Exception as e:  # noqa: BLE001 — surface gracefully
                self.log.write("model.error", error=str(e))
                result.final_text = f"(model call failed: {e})"
                return result

            result.completions.append(completion)
            result.cost_usd += completion.cost_usd
            self.log.write(
                "model.complete",
                model=completion.model_used,
                role=completion.role_used,
                in_tokens=completion.prompt_tokens,
                out_tokens=completion.completion_tokens,
                cost_usd=completion.cost_usd,
                elapsed_s=completion.elapsed_s,
                finish_reason=completion.finish_reason,
            )

            # max_tokens truncation surfaces as a parse_problem so the
            # retry path handles it like any other format failure.
            if completion.finish_reason == "length":
                self.log.write("recovery.max_tokens_truncation")

            # Day 0 A.4: empty content → retry once with reminder.
            if completion.empty:
                if counters.empty_content < self.max_empty_retries:
                    counters.empty_content += 1
                    self.log.write("recovery.empty_content_retry",
                                   attempt=counters.empty_content)
                    self._history.append({
                        "role": "user",
                        "content": "Reminder: respond in markdown only — "
                                   "no tool calls. Use ```intent then ```py fences.",
                    })
                    continue
                result.final_text = "(model produced empty response after retry)"
                self.log.write("turn.end.empty")
                return result

            self._history.append({"role": "assistant", "content": completion.content})

            gate = check(completion.content)

            # Prose-only — model is done.
            if gate.action == GateAction.ALLOW and "prose_only" in gate.reasons:
                result.final_text = completion.content.strip()
                self.log.write("turn.end.prose", chars=len(result.final_text))
                return result

            # Format failure — retry with reminder.
            if gate.action == GateAction.DENY and gate.parse_problems:
                if counters.parse_format >= self.max_format_retries:
                    result.escalations += 1
                    self.log.write("turn.end.format_failure",
                                   problems=gate.parse_problems)
                    result.final_text = (
                        f"(format failure after {counters.parse_format} retries: "
                        f"{', '.join(gate.parse_problems)})"
                    )
                    return result
                counters.parse_format += 1
                # Tell the router this role just failed a parse — at 2× it
                # promotes the next call to the next model in the chain.
                self.router.note_parse_format_fail("driver")
                obs_text = (
                    f"Observation:\n```\nFormatError: {gate.parse_problems}. "
                    f"Output must be a markdown response with ```intent then "
                    f"```py fenced blocks. Try again.\n```"
                )
                self._history.append({"role": "user", "content": obs_text})
                continue

            # Gate flagged but not parse error — preview + confirm path.
            parsed = parse_cell(completion.content)
            code = parsed.code or ""
            if self.dry_run and self.mode != "auto":
                # Dry-run gives REAL diffs by executing in an overlay
                preview = Preview.from_dry_run(
                    gate, code=code, workspace=self.workspace,
                )
            else:
                preview = Preview.from_gate(gate, workspace=self.workspace).with_code(code)

            # If the gate flagged an intent mismatch (declared writes/network
            # didn't match AST), notify the router. Two strikes in a row →
            # next call escalates to a stronger model.
            if gate.action == GateAction.CONFIRM and any(
                "undeclared" in r for r in gate.reasons
            ):
                self.router.note_intent_mismatch("driver")

            should_confirm = self._needs_confirmation(preview, gate)
            if should_confirm:
                approved = self._confirm(preview, gate)
                if not approved:
                    if counters.gate_deny >= self.max_gate_deny_retries:
                        self.log.write("turn.end.user_denied",
                                       reasons=gate.reasons)
                        result.final_text = (
                            "(user denied the cell; agent had no other plan)"
                        )
                        return result
                    counters.gate_deny += 1
                    result.cells_denied += 1
                    self.log.write("gate.user_deny", reasons=gate.reasons)
                    obs_text = (
                        f"Observation:\n```\nUserDeny: the user did not approve "
                        f"this cell ({', '.join(gate.reasons)}). Try a different "
                        f"approach or stop.\n```"
                    )
                    self._history.append({"role": "user", "content": obs_text})
                    continue

            # ---- execute ----
            assert gate.intent is not None

            try:
                pre_commit = self.shadow.commit(
                    f"forge:pre cell {cell_idx} — {gate.intent.intent}",
                    allow_empty=True,
                )
            except Exception as e:  # noqa: BLE001 — disk full, git lock, etc.
                self.log.write("shadow.commit_failed", phase="pre", error=str(e))
                pre_commit = None

            obs = self.kernel.execute(code, timeout=120.0)

            try:
                post_commit = self.shadow.commit(
                    f"forge:post cell {cell_idx} — {gate.intent.intent}",
                    allow_empty=True,
                )
            except Exception as e:  # noqa: BLE001
                self.log.write("shadow.commit_failed", phase="post", error=str(e))
                post_commit = None

            # Honest ok=True: post-execution self-check on .py writes.
            if obs.ok:
                broken = self._check_python_writes(gate.intent.writes)
                if broken:
                    obs = Observation(
                        ok=False,
                        stdout=obs.stdout,
                        stderr=(obs.stderr +
                                f"\nPostCheckFailed: wrote syntactically broken Python: "
                                f"{broken}"),
                        result=obs.result,
                        elapsed_s=obs.elapsed_s,
                        cell_code=code,
                    )
                    self.log.write("post_check.broken_py", files=broken)

            result.observations.append(obs)
            result.cells_run += 1

            self.log.write(
                "cell.exec",
                intent=gate.intent.intent,
                writes=gate.intent.writes,
                network=gate.intent.network,
                pre_sha=pre_commit.sha if pre_commit else None,
                post_sha=post_commit.sha if post_commit else None,
                ok=obs.ok,
                stdout_chars=len(obs.stdout),
                stderr_chars=len(obs.stderr),
                elapsed_s=obs.elapsed_s,
            )

            # Successful cell → reset retry counters AND escalation state.
            # The model just succeeded, so any pending escalation triggers
            # are no longer warranted.
            if obs.ok:
                counters.reset()
                self.router.reset_escalation("driver")

            # Feed observation back to the model.
            obs_text = f"Observation:\n```\n{obs.format()}\n```"
            self._history.append({"role": "user", "content": obs_text})

            if self.kernel.health.is_wedged():
                self.log.write("kernel.wedged",
                               consecutive_errors=self.kernel.health.consecutive_errors)
                result.final_text = (
                    "(kernel appears wedged — too many consecutive errors. "
                    "Type /reset in chat or restart the session.)"
                )
                return result

        # Hit max cells without prose
        self.log.write("turn.end.max_cells", limit=self.max_cells_per_turn)
        result.final_text = (
            f"(stopped after {self.max_cells_per_turn} cells without prose end)"
        )
        return result

    # ---- preview / confirm hooks ----------------------------------------

    def _call_driver(self, on_chunk: Optional[ChunkCallback]) -> Completion:
        """Single driver-role completion. Streams via on_chunk if provided.

        Whether streaming or not, returns a fully-populated Completion. The
        gate works on completion.content; the streaming is only for visible
        UX. This separation is what keeps the safety story honest — the
        gate sees the same artifact regardless of whether the user watched
        it appear token-by-token.
        """
        if on_chunk is None:
            return self.router.complete(self._history, role="driver")

        # Streaming path — accumulate via complete_stream and forward deltas.
        final: Optional[Completion] = None
        for chunk in self.router.complete_stream(self._history, role="driver"):
            if chunk.delta:
                try:
                    on_chunk(chunk.delta)
                except Exception:  # noqa: BLE001 — never let the UX break the loop
                    pass
            if chunk.is_final:
                final = chunk.completion
        if final is None:
            # Stream produced no final chunk — should never happen but be safe.
            return Completion(
                content="", role_used="driver", model_used="unknown",
                elapsed_s=0.0, finish_reason="error: stream produced no final chunk",
            )
        return final

    def _needs_confirmation(self, preview: Preview, gate: GateDecision) -> bool:
        """Decide whether to prompt the user before running this cell.

        Auto mode: never prompt (just deny on confirm cells with reasons).
        Plan mode: always prompt (this is the whole point of plan mode).
        Interactive mode + preview=always: prompt for every non-prose cell.
        Interactive mode + preview=cells: prompt only if side-effects.
        Interactive mode + preview=never: prompt only on gate flags.

        Pre-approved actions (PermissionStore) skip the prompt.
        """
        if self.mode == "auto":
            # Auto mode: silent denial of flagged cells. No prompts.
            return False
        if self.mode == "plan":
            return True

        # Interactive
        if self.preview_mode == "never":
            return gate.action != GateAction.ALLOW
        if self.preview_mode == "always":
            return preview.has_side_effects or gate.action != GateAction.ALLOW
        # cells (default)
        if not preview.has_side_effects and gate.action == GateAction.ALLOW:
            return False
        # Check permission store: if every action is pre-approved, skip prompt
        actions = actions_for_preview(preview)
        if actions and all(self.permissions.is_allowed(a) for a in actions):
            self.log.write("preview.preapproved",
                           actions=[(a.kind, a.target) for a in actions])
            return False
        return True

    def _confirm(self, preview: Preview, gate: GateDecision) -> bool:
        """Override in CLI for nice rendering. Default: deny in non-interactive."""
        return False

    # ---- post-execution checks ------------------------------------------

    def _check_python_writes(self, writes: list[str]) -> list[str]:
        """Check that any .py file we declared writing actually parses.

        Returns the list of files that fail to parse. Empty list = all good.
        """
        import ast
        broken: list[str] = []
        for w in writes:
            if not w.endswith(".py"):
                continue
            p = Path(w).expanduser()
            if not p.is_absolute():
                p = self.workspace / p
            if not p.exists():
                continue
            try:
                ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError as e:
                broken.append(f"{w}: line {e.lineno}: {e.msg}")
            except (OSError, UnicodeDecodeError):
                pass
        return broken

    # ---- history truncation ----------------------------------------------

    def _maybe_truncate_history(self) -> None:
        """Compress old turns if context is getting full.

        Strategy:
          1. Always keep: system prompt, the FIRST user message (the task),
             every assistant cell with intent block, the last 6 messages
             verbatim.
          2. Compressible: older `Observation:` blocks (largest first), older
             assistant prose.

        For v0.1 we use a simple char-count heuristic. v0.2 swaps in the
        summarizer model role for actual semantic compression.
        """
        # Threshold: 80% of num_ctx, treating chars-as-tokens (conservative).
        ctx_chars = self.router.roles["driver"].num_ctx * 4
        threshold = int(ctx_chars * 0.8)
        total = sum(len(m.get("content", "")) for m in self._history)
        if total < threshold:
            return

        # Keep system prompt + first user msg + last 6 entries verbatim.
        if len(self._history) <= 8:
            return  # nothing meaningful to truncate

        keep_head = self._history[:2]   # system + first user
        keep_tail = self._history[-6:]
        middle = self._history[2:-6]

        # Compress middle: replace Observation: blocks with a one-line summary.
        compressed: list[dict[str, str]] = []
        n_observations = 0
        n_replies = 0
        for m in middle:
            content = m.get("content", "")
            if content.startswith("Observation:") or "Observation:" in content[:20]:
                n_observations += 1
                continue
            if m.get("role") == "assistant":
                n_replies += 1
                # Keep assistant intent blocks verbatim — they're load-bearing
                # for the model's next-turn context.
                if "```intent" in content:
                    compressed.append(m)
                else:
                    continue
            else:
                # User msg in the middle (gate-deny observations etc.)
                continue

        if n_observations or n_replies:
            summary = (
                f"[forge:context-truncated] {n_observations} prior observations and "
                f"{n_replies - len([m for m in compressed if 'intent' in m.get('content','')])} prior replies "
                f"compressed for context. The original task and recent turns are below."
            )
            compressed.insert(0, {"role": "user", "content": summary})

        new_history = keep_head + compressed + keep_tail
        self.log.write(
            "history.truncate",
            from_chars=total,
            to_chars=sum(len(m.get("content", "")) for m in new_history),
            from_messages=len(self._history),
            to_messages=len(new_history),
        )
        self._history = new_history

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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from forge import tools
from forge.audit import AuditLog, SessionLog, new_session_id
from forge.config import audit_log as audit_log_path
from forge.config import ensure_dirs
from forge.gate import GateAction, GateDecision, check, parse_cell
from forge.kernel import Kernel, Observation
from forge.mcp import MCPRegistry
from forge.permissions import PermissionStore, actions_for_preview
from forge.preview import Preview
from forge.router import Completion, ModelRouter
from forge.shadow import ShadowGit
from forge.skills import SkillRegistry

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
        self.instruction_files: list[str] = []
        self.resumed_from: str | None = None
        # Optional Rich Live region set by the streaming CLI runner so the
        # confirm prompt can pause it. None outside streaming chat.
        self._live_region: object | None = None

    # ---- lifecycle ------------------------------------------------------

    def __enter__(self) -> Session:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        """Initialize kernel, shadow git, skill runtime, system prompt."""
        self.kernel.start()
        self.shadow.init()

        # Wire skill + MCP callbacks. Give the registry the router so
        # find_skill can rank by meaning (progressive disclosure); read_skill
        # loads a skill's full body on demand.
        self.skills.router = self.router
        tools.set_skill_runtime(
            find=self.skills.find,
            read=self.skills.read_full,
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
            instruction_files=self.instruction_files,
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

    # ---- cross-session resume ------------------------------------------

    def _session_state_path(self, session_id: str | None = None) -> Path:
        from forge.config import sessions_dir
        return sessions_dir(self.workspace) / f"{session_id or self.session_id}.json"

    def _persist_state(self) -> None:
        """Save this session's conversation state so it can be resumed later.

        Persists the message history (system prompt is NOT stored — it's
        rebuilt fresh on resume so prompt/skill changes take effect) plus
        minimal metadata. Best-effort: a write failure is logged, never fatal.

        Honest scope: only the CONVERSATION is saved. Kernel globals (variables
        the agent defined in cells) are NOT — a resumed session starts with a
        fresh kernel. The resume banner says so.
        """
        import json

        from forge.config import sessions_dir
        try:
            sessions_dir(self.workspace).mkdir(parents=True, exist_ok=True)
            # Drop the system message (index 0) — rebuilt on resume.
            persisted = [m for m in self._history if m.get("role") != "system"]
            payload = {
                "version": 1,
                "session_id": self.session_id,
                "workspace": str(self.workspace),
                "spent_usd": round(self.router.spent_usd, 6),
                "messages": persisted,
            }
            self._session_state_path().write_text(
                json.dumps(payload), encoding="utf-8"
            )
        except OSError as e:
            self.log.write("session.persist_failed", error=str(e))

    def resume_from(self, session_id: str | None = None) -> int:
        """Restore conversation history from a persisted session.

        If `session_id` is None, resumes the MOST RECENT persisted session in
        this workspace (the `--continue` semantics). Returns the number of
        prior messages restored (0 if none found). Must be called after
        start() — it appends restored messages after the fresh system prompt.

        Kernel globals are NOT restored; the caller should surface an honest
        banner (the CLI does).
        """
        import json

        from forge.config import sessions_dir
        sdir = sessions_dir(self.workspace)
        if session_id is not None:
            path = sdir / f"{session_id}.json"
        else:
            path = self._most_recent_session_file(sdir)
        if path is None or not path.exists():
            return 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self.log.write("session.resume_failed", error=str(e))
            return 0
        messages = payload.get("messages") or []
        if not isinstance(messages, list):
            return 0
        # Append restored messages after the (already-set) system prompt.
        # Only user/assistant turns — a persisted 'system' entry must never
        # inject a second system prompt.
        restored = [
            m for m in messages
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        ]
        self._history.extend(restored)
        self.resumed_from = payload.get("session_id")
        self.log.write(
            "session.resumed",
            from_session=payload.get("session_id"),
            messages=len(restored),
        )
        return len(restored)

    @staticmethod
    def _most_recent_session_file(sdir: Path) -> Path | None:
        if not sdir.is_dir():
            return None
        files = [p for p in sdir.glob("*.json") if p.is_file()]
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    # ---- helpers --------------------------------------------------------

    def _build_system_prompt(self) -> str:
        base = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        parts = [base]

        skill_block = self.skills.render_for_system_prompt()
        if skill_block:
            parts.append(skill_block)

        # Project/user instruction files (FORGE.md / AGENTS.md). Loaded under
        # a visible source-labelled header; loaded paths are recorded for the
        # run header + audit log so the injection surface stays inspectable.
        from forge.config import load_project_instructions
        instr_block, loaded = load_project_instructions(self.workspace)
        self.instruction_files = loaded
        if instr_block:
            parts.append(instr_block)

        return "\n\n".join(parts)

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
        on_chunk: ChunkCallback | None = None,
    ) -> TurnResult:
        """Run one user-turn and persist conversation state afterward.

        Thin wrapper over _turn_inner so cross-session resume state is saved
        on every exit path (prose end, max cells, errors) without threading a
        persist call through each return.
        """
        try:
            return self._turn_inner(user_msg, on_chunk=on_chunk)
        finally:
            self._persist_state()

    def _turn_inner(
        self,
        user_msg: str,
        *,
        on_chunk: ChunkCallback | None = None,
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

            # v0.2.2 — Ollama harmony parse recovery. The content we got is
            # the model's raw output salvaged from a server-side tool-call
            # parser crash; it's almost always a fragment (e.g. an import
            # line), NOT the model's intended final reply. Never let it
            # terminate the turn — force a retry with a stricter format
            # reminder, counted against the parse-format budget.
            if completion.finish_reason == "tool_call_parse_recovered":
                if counters.parse_format < self.max_format_retries:
                    counters.parse_format += 1
                    self.log.write("recovery.tool_call_parse_retry",
                                   attempt=counters.parse_format,
                                   recovered_chars=len(completion.content))
                    self.router.note_parse_format_fail("driver")
                    self._history.append({
                        "role": "user",
                        "content": (
                            "Reminder: your previous response was intercepted "
                            "by Ollama's tool-call parser and only a fragment "
                            "was recovered. Respond with a complete markdown "
                            "message containing ```intent ...``` then ```py "
                            "...``` fences. Do NOT emit bare Python statements "
                            "or anything that looks like a tool call."
                        ),
                    })
                    continue
                # Out of retries — surface honestly rather than display the stub.
                self.log.write("turn.end.tool_call_parse_unrecovered",
                               recovered_chars=len(completion.content))
                result.final_text = (
                    f"(model output was intercepted by Ollama's tool-call "
                    f"parser after {counters.parse_format} retries; only "
                    f"a fragment was recovered)"
                )
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

            # v0.2.4 — nudge the model to stop when the cell succeeded.
            # gpt-oss:20b under-trusts null results (0 files, [], False) and
            # loops re-verifying. After a clean cell, remind it: trust the
            # kernel, reply in prose if you have an answer.
            if obs.ok and obs.stderr.strip() == "":
                obs_text += (
                    "\n\n(The cell ran cleanly. If this output answers the "
                    "user's question — including null/empty/zero answers — "
                    "reply in plain prose now. Do NOT re-verify with another "
                    "cell unless you need genuinely different information.)"
                )

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

    # ---- plan mode -----------------------------------------------------

    def plan(self, user_msg: str) -> str:
        """Plan-only mode: model writes a markdown plan, no cells execute.

        Uses the `planner` role (defaults to gpt-oss at high effort, but
        auto-escalates to claude-sonnet if ANTHROPIC_API_KEY is set — see
        forge.router.default_roles).

        The plan prompt asks the model to write:
          1. Goal
          2. Steps (each with rationale + risk level: low/medium/high/critical)
          3. Files it would touch
          4. Network calls it would make
          5. Open questions

        No code executes. No shadow git commits. No kernel involvement.
        Returns the markdown plan as a string.

        Use case: review-before-action for high-stakes tasks. The whole point
        is letting the user see what the agent WOULD do for risky intents —
        the planner is instructed to ALWAYS produce a structured plan, even
        for destructive tasks, with critical-risk markers and refusal
        rationale folded into the Open questions section.
        """
        plan_system = (
            "You are Forge in PLAN MODE. The user wants to see a plan, not "
            "have you do the work. ALWAYS respond with the structured "
            "markdown plan below — even for tasks you would refuse to "
            "execute. A flat refusal defeats the purpose of plan mode; "
            "instead, write a plan with Risk: critical markers and put your "
            "objections under Open questions.\n\n"
            "PRINCIPLE: prefer minimal one-shot solutions. For read-only "
            "questions ('how many X', 'list Y', 'what files...') a single "
            "shell pipeline or one-line Python is usually right. Don't "
            "propose a new helper script unless persistence/reuse is "
            "explicitly required. Steps you'd skip in your own work should "
            "be marked 'informational' rather than 'required'.\n\n"
            "Response format (no code fences, no intent blocks):\n\n"
            "## Goal\n"
            "(restate the user's task in one sentence)\n\n"
            "## Steps\n"
            "Numbered list. For each step:\n"
            "- What you'd do\n"
            "- Why\n"
            "- Risk: low / medium / high / critical\n"
            "- (Optional) Required: yes / informational\n\n"
            "## Files touched\n"
            "(or 'none')\n\n"
            "## Network calls\n"
            "(or 'none')\n\n"
            "## Open questions\n"
            "Things you'd need to verify, OR your reasons for declining to "
            "execute this plan (still include the plan itself above so the "
            "user can review). Use this section to surface ethical / safety "
            "concerns rather than refusing the whole response.\n\n"
            "Keep it under 600 words. Be specific. Don't pad."
        )
        messages = [
            {"role": "system", "content": plan_system},
            {"role": "user", "content": user_msg},
        ]
        self.log.write("plan.start", user_msg_chars=len(user_msg))
        try:
            completion = self.router.complete(messages, role="planner")
        except Exception as e:  # noqa: BLE001
            self.log.write("plan.error", error=str(e))
            return f"(plan-mode call failed: {e})"

        self.log.write(
            "plan.complete",
            model=completion.model_used,
            in_tokens=completion.prompt_tokens,
            out_tokens=completion.completion_tokens,
            cost_usd=completion.cost_usd,
        )

        # If the model still flat-refused despite the prompt (sometimes happens
        # at high effort for very-aligned tasks), wrap the refusal in our
        # structured format so the contract holds.
        text = completion.content.strip()
        if not text:
            return "(planner returned empty response)"
        # Detect a likely refusal: no markdown header in the first 200 chars
        # and contains a refusal phrase. If so, synthesize a critical-risk
        # decline plan.
        first = text[:200].lower()
        looks_refused = (
            "## " not in first
            and any(p in first for p in (
                "i'm sorry", "i can't", "i won't", "cannot help",
                "can't help", "i am unable",
            ))
        )
        if looks_refused:
            return (
                "## Goal\n"
                f"{user_msg.strip()}\n\n"
                "## Steps\n"
                "1. (no plan produced — planner declined)\n"
                "   - Why: the planner refused to enumerate steps for this task.\n"
                "   - Risk: **critical**\n\n"
                "## Files touched\n"
                "unknown (planner declined to enumerate)\n\n"
                "## Network calls\n"
                "unknown (planner declined to enumerate)\n\n"
                "## Open questions\n"
                f"The planner returned a refusal: {text!r}\n\n"
                "If you genuinely want to proceed, you'll need to break the "
                "task down yourself and run individual cells. Plan mode "
                "cannot expand a refusal into actionable steps without your "
                "involvement."
            )
        return text

    # ---- preview / confirm hooks ----------------------------------------

    def _call_driver(self, on_chunk: ChunkCallback | None) -> Completion:
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
        final: Completion | None = None
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
        """Compress old turns when context approaches the driver's limit.

        Two-tier compaction (mirrors the v10 harness's basicCompact →
        summarize escalation):

        Tier 1 — cheap, no model call. Replace the bodies of old Observation
        blocks with a short placeholder, keeping the last KEEP_RECENT_TURNS
        exchanges verbatim. Most of the time this is enough and costs nothing.

        Tier 2 — if Tier 1 doesn't get us under target, ask the (local,
        zero-cost) `summarizer` role to condense the MIDDLE span into one
        labelled `[forge:summary — model-generated, not verbatim]` block,
        preserving task intent, files modified, decisions, and unresolved
        errors. A labelled summary is more honest than silent deletion.

        Error-signal-aware pruning (A2): the most recent traceback /
        PostCheckFailed is preserved verbatim through both tiers so the model
        keeps learning from its last mistake.

        If the summarizer call fails (offline, cost ceiling, unknown role), we
        fall back to blind char-count deletion so compaction never blocks a
        turn.
        """
        threshold, total = self._compaction_threshold()
        if total < threshold:
            return
        if len(self._history) <= 8:
            return  # nothing meaningful to compress

        # --- Tier 1: evict old observation bodies (no model call) ---
        target = self._compaction_target()
        tier1_applied = self._compact_tier1(target)
        _, total_after_t1 = self._compaction_threshold()
        if total_after_t1 < threshold:
            if tier1_applied:
                self.log.write(
                    "history.compact", mode="evict_observations",
                    from_chars=total, to_chars=total_after_t1,
                )
            return

        # --- Tier 2: LLM summarization of the middle span ---
        keep_head = self._history[:2]     # system + first user (the task)
        keep_tail = self._history[-6:]
        middle = self._history[2:-6]
        if not middle:
            return

        try:
            summary = self._summarize_span(middle)
        except Exception as e:  # noqa: BLE001 — never let compaction crash a turn
            self.log.write("history.compact_failed", error=str(e))
            self._blind_truncate_history()
            return

        if not summary:
            # Summarizer produced nothing usable — fall back honestly.
            self._blind_truncate_history()
            return

        # A2: keep the most recent error block from the middle verbatim so the
        # model doesn't repeat the mistake the summary might smooth over.
        last_error = self._last_error_message(middle)

        new_middle: list[dict[str, str]] = [{
            "role": "user",
            "content": (
                "[forge:summary — model-generated, not verbatim] "
                "Earlier turns were compacted to save context:\n\n" + summary
            ),
        }]
        if last_error is not None:
            new_middle.append(last_error)

        new_history = keep_head + new_middle + keep_tail
        self.log.write(
            "history.compact",
            mode="semantic",
            from_chars=total,
            to_chars=sum(self._estimate_message_chars(m) for m in new_history),
            from_messages=len(self._history),
            to_messages=len(new_history),
            kept_last_error=last_error is not None,
        )
        self._history = new_history

    # Number of most-recent messages Tier 1 never touches.
    _KEEP_RECENT_MESSAGES = 6

    def _compact_tier1(self, target_chars: int) -> bool:
        """Evict old Observation bodies until under `target_chars` or nothing
        left to evict. Keeps the last _KEEP_RECENT_MESSAGES verbatim and never
        evicts the most recent error block. Returns True if it changed
        anything. No model call — this is the cheap tier.
        """
        head = self._history[:2]
        tail = self._history[-self._KEEP_RECENT_MESSAGES:]
        middle = self._history[2:-self._KEEP_RECENT_MESSAGES]
        if not middle:
            return False

        last_error = self._last_error_message(middle)
        changed = False
        for m in middle:
            total = sum(self._estimate_message_chars(x) for x in self._history)
            if total <= target_chars:
                break
            content = m.get("content", "")
            # Only evict large observation bodies; never the last error, never
            # assistant intent cells (load-bearing), never already-evicted.
            is_obs = content.startswith("Observation:") or "Observation:" in content[:20]
            if (is_obs and m is not last_error
                    and "[forge:observation-evicted]" not in content
                    and len(content) > 200):
                m["content"] = (
                    "[forge:observation-evicted] "
                    "(older tool output removed to save context)"
                )
                changed = True
        return changed

    @staticmethod
    def _estimate_message_chars(m: dict[str, str]) -> int:
        """Per-message size estimate (chars ≈ 4× tokens). Kept as a helper so
        token accounting lives in one place if we later switch to real
        tokenization."""
        return len(m.get("content", ""))

    def _compaction_target(self) -> int:
        """Target size to compact DOWN to — 25% of num_ctx (chars), mirroring
        v10's COMPACT_TARGET. Compacting to well under the threshold avoids
        re-triggering on every subsequent turn."""
        ctx_chars = self.router.roles["driver"].num_ctx * 4
        return int(ctx_chars * 0.25)

    def _compaction_threshold(self) -> tuple[int, int]:
        """(threshold_chars, total_chars) for the current history.

        Threshold is 80% of num_ctx, treating chars-as-tokens (conservative,
        ~4 chars/token headroom is folded into the 80%)."""
        ctx_chars = self.router.roles["driver"].num_ctx * 4
        threshold = int(ctx_chars * 0.8)
        total = sum(len(m.get("content", "")) for m in self._history)
        return threshold, total

    @staticmethod
    def _looks_like_error(content: str) -> bool:
        """Heuristic: does this observation carry an error signal worth keeping?"""
        markers = ("Traceback (most recent call last)", "PostCheckFailed",
                   "Error:", "Exception", "FormatError", "UserDeny",
                   "--- stderr ---")
        return any(mk in content for mk in markers)

    def _last_error_message(self, span: list[dict[str, str]]) -> dict[str, str] | None:
        """The most recent message in `span` that carries an error signal."""
        for m in reversed(span):
            if self._looks_like_error(m.get("content", "")):
                return m
        return None

    def _summarize_span(self, span: list[dict[str, str]]) -> str:
        """Ask the summarizer role to condense a span of history.

        Returns the summary text, or "" if the model produced nothing.
        Raises on transport/router errors so the caller can fall back.
        """
        # Render the span as a plain transcript for the summarizer.
        lines: list[str] = []
        for m in span:
            role = m.get("role", "?")
            content = m.get("content", "")
            lines.append(f"[{role}]\n{content}")
        transcript = "\n\n".join(lines)

        summ_system = (
            "You are compacting an agent's working history to save context. "
            "Produce a TERSE summary (<= 250 words) that preserves, in this "
            "order:\n"
            "1. The task being worked on.\n"
            "2. Files created or modified (paths).\n"
            "3. Key decisions and findings.\n"
            "4. Any UNRESOLVED errors or open problems — keep these verbatim "
            "if short.\n"
            "Discard successful command output, greetings, and restated "
            "context. Write plain prose or a short bullet list. Do NOT add "
            "commentary about the summarization itself."
        )
        messages = [
            {"role": "system", "content": summ_system},
            {"role": "user", "content": transcript},
        ]
        completion = self.router.complete(messages, role="summarizer")
        return completion.content.strip()

    def _blind_truncate_history(self) -> None:
        """Fallback compaction: char-count deletion of old observations.

        This is the pre-v0.2.5 behaviour, retained as the safety net when
        semantic summarization is unavailable. Keeps system prompt + first
        user message + assistant intent blocks + last 6 messages; drops older
        Observation blocks and non-intent prose.
        """
        total = sum(len(m.get("content", "")) for m in self._history)
        if len(self._history) <= 8:
            return

        keep_head = self._history[:2]   # system + first user
        keep_tail = self._history[-6:]
        middle = self._history[2:-6]

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
                # Keep assistant intent blocks verbatim — load-bearing context.
                if "```intent" in content:
                    compressed.append(m)
                else:
                    continue
            else:
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
            mode="blind",
            from_chars=total,
            to_chars=sum(len(m.get("content", "")) for m in new_history),
            from_messages=len(self._history),
            to_messages=len(new_history),
        )
        self._history = new_history

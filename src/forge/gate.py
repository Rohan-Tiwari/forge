"""forge.gate — intent block parsing + AST safety lint + gate decisions.

This is the safety perimeter. Every cell the model produces is parsed here
and its declared intent is compared against what its code actually does. The
gate emits one of three decisions: allow, confirm (require user OK), or deny.

The lint is intentionally a HEURISTIC. The real authority for "this can't run"
lives at the tool layer (forge.tools), where wrapped open()/subprocess/etc.
enforce protected-path and protected-action checks that the agent's emitted
code cannot route around.
"""
from __future__ import annotations

import ast
import fnmatch
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator


# =============================================================================
# Cell parsing — extract the intent fence and the python fence.
# =============================================================================

# Match ` ```intent ` ... ` ``` ` and ` ```py ` (or ```python) ... ` ``` `.
# We accept both fence languages because the model sometimes drifts.
_INTENT_RE = re.compile(r"```intent\s*\n(.*?)\n```", re.DOTALL)
_PY_RE = re.compile(r"```(?:py|python)\s*\n(.*?)\n```", re.DOTALL)


class IntentBlock(BaseModel):
    """The structured declaration that precedes every code cell.

    The model fills this in. The gate verifies it against the AST. The user
    sees it before the cell runs (in interactive mode).
    """

    intent: str = Field(..., description="One sentence describing what this cell does.")
    writes: list[str] = Field(default_factory=list)
    network: list[str] = Field(default_factory=list)
    reversible: bool = True

    @field_validator("writes", "network", mode="before")
    @classmethod
    def _coerce_list(cls, v: object) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return [str(x) for x in v]
        raise ValueError(f"expected list, got {type(v).__name__}")


@dataclass
class ParsedCell:
    """The result of parsing a model response."""

    intent: Optional[IntentBlock]
    code: Optional[str]
    intent_first: bool = False
    parse_problems: list[str] = field(default_factory=list)


def parse_cell(text: str) -> ParsedCell:
    """Extract the first intent+python pair from a model response.

    Returns ParsedCell with `parse_problems` populated for any issues.
    A response without a python fence is valid (= prose-only turn end);
    callers distinguish that case via `code is None and not parse_problems`.
    """
    intent_match = _INTENT_RE.search(text)
    py_match = _PY_RE.search(text)

    if py_match is None and intent_match is None:
        return ParsedCell(intent=None, code=None)  # prose-only — turn end

    problems: list[str] = []
    intent: Optional[IntentBlock] = None
    intent_first = False

    if py_match is None:
        problems.append("no_python_fence")
        return ParsedCell(intent=None, code=None, parse_problems=problems)

    code = py_match.group(1)

    if intent_match is None:
        problems.append("no_intent_fence")
    else:
        intent_first = intent_match.start() < py_match.start()
        if not intent_first:
            problems.append("intent_after_python")
        try:
            parsed_yaml = yaml.safe_load(intent_match.group(1))
            if not isinstance(parsed_yaml, dict):
                problems.append("intent_yaml_not_mapping")
            else:
                try:
                    intent = IntentBlock.model_validate(parsed_yaml)
                except Exception as e:  # noqa: BLE001
                    problems.append(f"intent_schema_error: {e}")
        except yaml.YAMLError as e:
            problems.append(f"intent_yaml_unparseable: {e}")

    return ParsedCell(intent=intent, code=code, intent_first=intent_first,
                      parse_problems=problems)


# =============================================================================
# AST analysis — what does the code actually do?
# =============================================================================

# Network-call qualified names we recognize. Conservative — we'd rather miss
# an exotic library than false-positive on dict.get().
_NET_QUALNAMES = {
    "requests.get", "requests.post", "requests.put", "requests.delete",
    "requests.head", "requests.patch", "requests.request", "requests.Session",
    "urllib.request.urlopen", "urlopen",
    "httpx.get", "httpx.post", "httpx.put", "httpx.delete", "httpx.request",
    "httpx.Client", "httpx.AsyncClient", "httpx.stream",
    "urllib3.request",
    "socket.socket", "socket.create_connection",
    "aiohttp.ClientSession", "aiohttp.request",
}


@dataclass
class AstFindings:
    syntax_ok: bool
    syntax_error: Optional[str] = None
    write_calls: list[tuple[str, str]] = field(default_factory=list)
    net_calls: list[tuple[str, str]] = field(default_factory=list)
    bash_calls: list[str] = field(default_factory=list)
    dynamic_code: list[str] = field(default_factory=list)  # eval/exec/__import__/getattr-on-builtins
    imports: list[str] = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)
    used_skills: list[str] = field(default_factory=list)


def _qualname(node: ast.AST) -> str:
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _str_const(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # f-string
        parts: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("{...}")
        return "".join(parts)
    # Fold simple BinOp(Add, str, str) — `"rm" + " -rf " + "/"`.
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _str_const(node.left)
        right = _str_const(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def analyze(code: str) -> AstFindings:
    """Walk the AST and extract write targets, network calls, and red flags.

    Now does FIXED-POINT assignment resolution: `a = "x"; b = a; open(b, "w")`
    correctly resolves to "x", not the variable name "b".

    Conservative for unresolved Names: if `resolve()` can't find a string for
    a Name, we record the Name as the target. The downstream `_path_covers`
    used to silently allow these as "looks like a variable, trust it" — that
    was a real safety hole. We now flag them as undeclared unless the user
    explicitly listed the same Name in their intent's writes.
    """
    findings = AstFindings(syntax_ok=False)
    try:
        tree = ast.parse(code)
        findings.syntax_ok = True
    except SyntaxError as e:
        findings.syntax_error = f"line {e.lineno}: {e.msg}"
        return findings

    # Collect ALL string-typed bindings, then iterate to a fixed point so
    # `a = "x"; b = a; c = b` propagates "x" to all three.
    name_to_str: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value_str = _str_const(node.value)
            if value_str and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name_to_str.setdefault(node.targets[0].id, value_str)
            # Path("./out/foo") and similar single-string-arg calls
            if (
                isinstance(node.value, ast.Call)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.value.args
            ):
                first = _str_const(node.value.args[0])
                if first:
                    name_to_str.setdefault(node.targets[0].id, first)

    # Fixed-point: resolve `b = a` → resolve a → "x", set b → "x"
    for _ in range(5):  # bounded; usually converges in 1-2 passes
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            target_name = node.targets[0].id
            if target_name in name_to_str:
                continue
            # Variable-aliasing case: x = y where y is a known string
            if isinstance(node.value, ast.Name) and node.value.id in name_to_str:
                name_to_str[target_name] = name_to_str[node.value.id]
                changed = True
        if not changed:
            break

    def resolve(node: ast.AST) -> tuple[str, bool]:
        """Resolve a node to a string + whether resolution was confident.

        Returns (value, resolved). `resolved` is True for string literals,
        f-strings, BinOp+ folds, and traced Names. False when we fell back
        to a bare Name or qualname.
        """
        s = _str_const(node)
        if s is not None:
            return s, True
        if isinstance(node, ast.Name):
            if node.id in name_to_str:
                return name_to_str[node.id], True
            return node.id, False
        if isinstance(node, ast.Attribute):
            return _qualname(node), False
        if isinstance(node, ast.Call):
            # Path("x") — first arg is often the actual path
            if node.args:
                inner = _str_const(node.args[0])
                if inner is not None:
                    return inner, True
                # Or chain through: foo.bar() with foo being a known path
                if isinstance(node.args[0], ast.Name) and node.args[0].id in name_to_str:
                    return name_to_str[node.args[0].id], True
            # Method call on something we can resolve
            if isinstance(node.func, ast.Attribute):
                return resolve(node.func.value)
        return "", False

    for node in ast.walk(tree):
        # Imports — both for diagnostics and to detect skill activation
        if isinstance(node, ast.Import):
            for a in node.names:
                findings.imports.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("skills."):
                findings.used_skills.append(mod[len("skills."):])
            for a in node.names:
                findings.imports.append(f"{mod}.{a.name}" if mod else a.name)

        if not isinstance(node, ast.Call):
            continue

        qn = _qualname(node.func)
        base = qn.split(".")[-1]

        # Dangerous primitives
        if base == "eval" or qn == "eval":
            findings.dynamic_code.append("eval")
        if base == "exec" or qn == "exec":
            findings.dynamic_code.append("exec")
        if qn == "__import__":
            findings.dynamic_code.append("__import__")
        if base == "getattr" and node.args:
            # getattr on a builtins-ish name with a dynamic attr is suspicious
            if isinstance(node.args[0], ast.Name) and node.args[0].id in {"__builtins__", "builtins"}:
                findings.dynamic_code.append("getattr_builtins")

        # Track tool calls so callers can see what the cell is doing
        if base in {"Read", "Write", "Edit", "Bash", "search", "see",
                    "find_skill", "run_skill", "call_mcp", "register_skill"}:
            findings.used_tools.append(base)

        # ---- Writes -----------------------------------------------------
        if qn == "open" or base == "open":
            mode = "r"
            if len(node.args) >= 2:
                mode = _str_const(node.args[1]) or "r"
            for kw in node.keywords:
                if kw.arg == "mode":
                    mode = _str_const(kw.value) or mode
            if any(c in mode for c in "wax"):
                target = resolve(node.args[0])[0] if node.args else ""
                findings.write_calls.append(("open", target))
        elif base == "Write":
            target = resolve(node.args[0])[0] if node.args else ""
            findings.write_calls.append(("Write", target))
        elif base == "Edit":
            target = resolve(node.args[0])[0] if node.args else ""
            findings.write_calls.append(("Edit", target))
        elif base in {"write_text", "write_bytes"} and isinstance(node.func, ast.Attribute):
            recv = node.func.value
            target = ""
            if isinstance(recv, ast.Call) and recv.args:
                target = _str_const(recv.args[0]) or ""
            elif isinstance(recv, ast.Name):
                target = name_to_str.get(recv.id, recv.id)
            findings.write_calls.append((qn, target))
        elif qn in {"shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.move"}:
            target = resolve(node.args[1])[0] if len(node.args) >= 2 else ""
            findings.write_calls.append((qn, target))
        elif base in {"rename", "renames"}:
            target = ""
            if base == "rename" and isinstance(node.func, ast.Attribute):
                if node.args:
                    target = resolve(node.args[0])[0]
                if not target:
                    target = resolve(node.func.value)[0]
            elif len(node.args) >= 2:
                target = resolve(node.args[1])[0]
            elif node.args:
                target = resolve(node.args[0])[0]
            findings.write_calls.append((qn, target))
        elif base == "touch" and isinstance(node.func, ast.Attribute):
            findings.write_calls.append((qn, resolve(node.func.value)[0]))
        # Destructive filesystem ops: unlink/rmdir/rmtree — these REMOVE files,
        # which counts as a "write" for declaration purposes (the file changes).
        # NOTE: check os.* qualnames BEFORE the bare-base branch, so
        # os.unlink("/tmp/x") gets the path arg, not the receiver.
        elif qn in {"os.remove", "os.unlink", "os.rmdir", "shutil.rmtree"}:
            target = resolve(node.args[0])[0] if node.args else ""
            findings.write_calls.append((qn, target))
        elif base in {"unlink", "rmdir"} and isinstance(node.func, ast.Attribute):
            # Path("x").unlink() — receiver IS the target.
            target = resolve(node.func.value)[0]
            findings.write_calls.append((qn, target))

        # ---- Network ----------------------------------------------------
        if qn in _NET_QUALNAMES:
            url = ""
            if node.args:
                url = resolve(node.args[0])[0]
            findings.net_calls.append((qn, url))

        # ---- Bash / subprocess captures: imply both writes and network -----
        # Bash() is our own tool — first arg is the command string.
        if base == "Bash":
            cmd = _str_const(node.args[0]) if node.args else ""
            if cmd:
                findings.bash_calls.append(cmd)

        # subprocess.{run,Popen,call,check_call,check_output} — treat like Bash.
        # The first arg can be a string (shell-ish) or a list of args (argv form).
        if qn in {"subprocess.run", "subprocess.Popen", "subprocess.call",
                  "subprocess.check_call", "subprocess.check_output"}:
            if node.args:
                first = node.args[0]
                if isinstance(first, ast.List):
                    parts: list[str] = []
                    all_resolved = True
                    for elt in first.elts:
                        s = _str_const(elt)
                        if s is None:
                            r, ok = resolve(elt)
                            if not ok:
                                all_resolved = False
                            parts.append(r)
                        else:
                            parts.append(s)
                    if all_resolved or parts:
                        findings.bash_calls.append(" ".join(parts))
                else:
                    cmd_str = _str_const(first)
                    if cmd_str is None:
                        r, _ = resolve(first)
                        cmd_str = r
                    if cmd_str:
                        findings.bash_calls.append(cmd_str)

        # os.system / os.popen — old-school shell invocations.
        if qn in {"os.system", "os.popen"}:
            cmd_str = _str_const(node.args[0]) if node.args else None
            if cmd_str is None and node.args:
                r, _ = resolve(node.args[0])
                cmd_str = r
            if cmd_str:
                findings.bash_calls.append(cmd_str)

    # Bash heuristics — curl/wget are network; redirects are writes.
    for cmd in findings.bash_calls:
        if re.search(r"\b(curl|wget)\b", cmd):
            m = re.search(r"https?://([^/\s'\"]+)", cmd)
            findings.net_calls.append(("Bash:curl", m.group(1) if m else ""))
        m = re.search(r">>?\s*([^\s|;&]+)", cmd)
        if m:
            findings.write_calls.append(("Bash:redirect", m.group(1)))
        if re.search(r"\b(mv|cp|sed -i|tee)\b", cmd):
            tokens = cmd.split()
            target = tokens[-1] if tokens else ""
            findings.write_calls.append(("Bash:fs", target))

    return findings


# =============================================================================
# Path matching — declared vs actual.
# =============================================================================


def _norm_path(p: str) -> str:
    p = p.strip().lstrip("./").rstrip("/")
    return p.lower()


def _norm_host(h: str) -> str:
    h = h.strip().lower()
    m = re.match(r"https?://([^/]+)", h)
    return m.group(1) if m else h


def _path_covers(declared: set[str], actual: str) -> bool:
    """Is `actual` covered by any declaration in `declared`?

    Lenient on dir/file containment and glob patterns. We DO NOT trust bare
    variable names anymore (the Day-0 lenience was a safety hole).
    """
    if not actual:
        return True
    if actual in declared:
        return True
    for d in declared:
        if not d:
            continue
        if actual.startswith(d + "/") or d.startswith(actual + "/"):
            return True
        if actual.endswith(d) or d.endswith(actual):
            return True
        if any(c in d for c in "*?["):
            for variant in (actual, actual.lstrip("./")):
                if fnmatch.fnmatch(variant, d) or fnmatch.fnmatch(variant, d.lstrip("./")):
                    return True
            d_dir = d.rsplit("/", 1)[0] if "/" in d else ""
            if d_dir and (actual.startswith(d_dir + "/") or actual == d_dir):
                return True
    return False


# =============================================================================
# The gate decision
# =============================================================================


class GateAction(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass
class GateDecision:
    action: GateAction
    reasons: list[str] = field(default_factory=list)
    intent: Optional[IntentBlock] = None
    findings: Optional[AstFindings] = None
    parse_problems: list[str] = field(default_factory=list)


def check(text: str) -> GateDecision:
    """Run the gate on a raw model response.

    The hard rule: if the parser couldn't extract a clean cell with intent
    block + python block, deny. The agent has to retry. If the cell parses
    but the AST sees writes/network the intent didn't declare, escalate to
    confirm (interactive) or log + deny (auto). Dynamic-code primitives
    (eval/exec/getattr_builtins) are also confirm-required.
    """
    parsed = parse_cell(text)

    # Prose-only response (turn end) — not a gate concern.
    if parsed.code is None and not parsed.parse_problems:
        return GateDecision(action=GateAction.ALLOW, reasons=["prose_only"])

    if parsed.parse_problems:
        return GateDecision(
            action=GateAction.DENY,
            reasons=parsed.parse_problems,
            parse_problems=parsed.parse_problems,
        )

    assert parsed.code is not None
    assert parsed.intent is not None  # parse_problems would have caught it

    findings = analyze(parsed.code)

    if not findings.syntax_ok:
        return GateDecision(
            action=GateAction.DENY,
            reasons=[f"syntax_error: {findings.syntax_error}"],
            intent=parsed.intent,
            findings=findings,
        )

    declared_writes = {_norm_path(w) for w in parsed.intent.writes}
    declared_net = {_norm_host(n) for n in parsed.intent.network}
    actual_writes = {_norm_path(t) for (_, t) in findings.write_calls if t}
    actual_net = {_norm_host(h) for (_, h) in findings.net_calls if h}

    undeclared_writes = sorted(
        [a for a in actual_writes if not _path_covers(declared_writes, a)]
    )
    undeclared_net = sorted(
        [a for a in actual_net if not _path_covers(declared_net, a)]
    )

    reasons: list[str] = []
    if undeclared_writes:
        reasons.append(f"undeclared_writes={undeclared_writes}")
    if undeclared_net:
        reasons.append(f"undeclared_network={undeclared_net}")
    if findings.dynamic_code:
        reasons.append(f"dynamic_code={findings.dynamic_code}")

    if reasons:
        return GateDecision(
            action=GateAction.CONFIRM,
            reasons=reasons,
            intent=parsed.intent,
            findings=findings,
        )

    return GateDecision(
        action=GateAction.ALLOW,
        reasons=[],
        intent=parsed.intent,
        findings=findings,
    )

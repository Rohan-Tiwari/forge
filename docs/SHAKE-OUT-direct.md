# Forge v0.1 — direct probe findings (claude in-session)

These are findings I uncovered by direct probing of the live forge package
**before** the workflow's automated review came back. I'll merge with the
workflow's findings into a unified report once it lands.

## CRITICAL / HIGH

### H1 · Case-insensitive path bypass on macOS APFS (security)

**Where:** `forge.tools.is_protected_path` (`src/forge/tools.py`).
**Reproducer:**
```python
from forge.tools import is_protected_path
is_protected_path("~/.ssh/id_rsa")  # True
is_protected_path("~/.SSH/id_rsa")  # False — bypass!
```
But on the user's APFS volume (case-INSENSITIVE — verified with `mkdir /tmp/CaseSensTest && ls /tmp/casesenstest` returning the same dir), both paths point to the same on-disk directory. The protection is bypassed by uppercasing.

**Why this happens.** `_matches_pattern` calls `Path(pat).resolve()` and compares to `absolute = Path(input).resolve()`. On case-insensitive APFS, `resolve()` preserves the *input* casing, not the on-disk casing. So `~/.SSH` resolves to `/Users/x/.SSH` (which doesn't exist as a literal directory entry, but the kernel still routes I/O to `~/.ssh`).

**Fix.** Detect case-insensitivity at module load (`os.path.normcase` is the platform-aware helper) and lowercase both sides for comparison. Or: use `os.path.realpath` (which on macOS canonicalizes via vfs lookups) instead of `pathlib.resolve`.

**Severity:** High. A determined attacker (or model) could write to `~/.SSH/authorized_keys` or `~/.AWS/credentials` and bypass our protection.

---

### H2 · `_run_skill` returns a string repr instead of the actual object

**Where:** `forge.session.Session._run_skill` (`src/forge/session.py:114`).

The method synthesizes a Python script that calls the skill's `main(**kwargs)`, ends with the bare expression `_result`, and runs it via `kernel.execute`. The kernel returns the **repr of the last expression** as `obs.result`, which is a *string*. So `run_skill('project-stats')` returns the string `"{'path': '/foo', ...}"` — not the actual dict. The agent has to parse the repr back, which is unreliable.

**Reproducer (already seen in the dogfood log):**
```
cell.exec  intent=Run project-stats skill … ok=False
```
The cell failed because the model expected to receive a dict and instead got a string repr that didn't behave the way it expected.

**Other bugs in this same function:**
1. Path interpolation `'{skill.helpers_path}'` will break if path contains a single quote. Not common but possible.
2. `f"_result = _main(**{kwargs!r})"` interpolates `{repr(dict)}` directly into source — for any value whose repr isn't valid Python (Path objects, custom classes), this is a syntax error.
3. The bootstrap re-imports the helpers module on every call. No caching.

**Fix.** Stop synthesizing exec strings entirely. Pre-import the skill's helpers as part of skill activation, then expose `main` as a callable to the kernel directly via the kernel-globals injection path. The kernel already has `set_skill_runtime` for exactly this — extend it to also publish skill-main callables.

**Severity:** High. This is the single biggest functional bug — `run_skill` doesn't reliably work, which gates the whole "skill" value prop.

---

## MEDIUM

### M1 · Gate AST lint blind to string concatenation in Bash arg

**Where:** `forge.gate.analyze` (`src/forge/gate.py`).
**Reproducer:**
```python
from forge.gate import check
text = '''```intent
intent: "harmless"
writes: []
network: []
```
```py
Bash("rm" + " -rf " + "/")
```'''
print(check(text).action)  # GateAction.ALLOW — should be CONFIRM
```
The AST analyzer's `_str_const` handles single-string-constants and f-strings but not `BinOp(+, Const, Const)`. So concatenated strings produce no `bash_calls` finding, and the protected-action check at the *gate* level passes.

**Mitigated by.** The `Bash` tool wrapper still catches it at runtime (the resolved string contains `rm -rf /`), so the actual action would be blocked. But the gate's *intent verification* layer reports a false ALLOW, meaning interactive mode wouldn't prompt the user to confirm. That's a UX hole.

**Fix.** Extend `_str_const` to fold `BinOp(Add, str, str)` into a single string when both operands are constants. ~20 LOC.

**Severity:** Medium. Defense-in-depth gap, not a true bypass.

---

### M2 · `_str_const` doesn't handle simple variable resolution chains

**Where:** `forge.gate.analyze`.
**Reproducer:**
```python
from forge.gate import analyze
f = analyze('a = "x.txt"\nb = a\nopen(b, "w")')
print(f.write_calls)  # [('open', 'b')] — should be [('open', 'x.txt')]
```
The variable resolver tracks `a = "x.txt"` but not the indirection through `b = a`.

**Fix.** Resolve transitively until we hit a literal or a non-Name. Cap recursion at 5 to prevent loops.

**Severity:** Medium. Low practical impact; the AST is a heuristic, not authority.

---

### M3 · The `audit.session_log` context manager monkey-patches `audit.write`

**Where:** `forge.audit.session_log` (`src/forge/audit.py:79`).

`session_log` does `audit.write = with_session` then restores in `finally`. If two sessions ever ran concurrently against the same `AuditLog` instance, the second's enter would clobber the first's `write`. The Session is single-threaded today so this isn't broken, but it's a foot-gun for v0.2 (background tasks, multi-session).

**Fix.** Make `AuditLog.write` accept an optional `session=` kwarg, and remove the patching trick. Cleaner & threadsafe.

**Severity:** Medium (latent — will bite when concurrency arrives).

---

## LOW / NIT

### L1 · `is_protected_path` returns False on `OSError` from `_expand`

**Where:** `forge.tools._expand` / `is_protected_path`.

`_expand` catches `(ValueError, OSError)` and the caller returns `False`. So a path that errors during expansion is treated as **not protected**, which is the wrong default (fail-open). Should fail-closed: any path we can't reason about is rejected.

**Fix.** Treat expansion errors as "protected" (refuse the operation).

**Severity:** Low. Hard to weaponize, but the fail-open default is a smell.

---

### L2 · CLI `forge --version` only works without subcommand

**Where:** `forge.cli._root`.

Fixed earlier today; verified working.

---

### L3 · `forge log` table truncates intent text without showing the full content

**Where:** `forge.cli.log_cmd`.

The table caps detail strings at 80 chars. If an audit entry's intent is long, you can't read it. There's no `forge log --full` or `forge show <session>` to drill in.

**Fix.** Add a `--full` flag, or render a Rich panel per entry when there are <5 results.

**Severity:** Low (UX).

---

### L4 · `chat` REPL has no multi-line input

**Where:** `forge.cli.chat`.

Pasting a multi-line task into the REPL only sends the first line. No way to enter a paragraph.

**Fix.** Use `prompt_toolkit` for multi-line editing, or accept `<<<EOF` style heredocs.

**Severity:** Low (UX).

---

## What worked unexpectedly well

- **Marker-collision defense.** I tried to inject a fake `\x1eFORGE_RESULT\x1e{"ok": false}` line via user `print()` and it failed to spoof the kernel. Reason: the worker uses `contextlib.redirect_stdout(out_buf)` so user-code prints never reach the actual subprocess stdout. Real result line is the only one written by the worker. **This is a clean defense by construction, even if I didn't plan it explicitly.**
- **Path traversal.** `is_protected_path("./safe/../../../.ssh/leaked")` correctly returns True because we `resolve()` first.
- **Two intent fences.** The model emits a second sneakier intent block but the gate uses the first one and AST lint catches the discrepancy.
- **`getattr_builtins` heuristic.** `getattr(__builtins__, "ev"+"al")("1+1")` correctly flags `dynamic_code`.
- **Kernel timeout.** Infinite loop killed at exactly 2.0s.
- **Kernel survives `sys.exit`.** Cell fails, next cell runs cleanly.
- **`forge undo` end-to-end.** Edited a file, ran `forge undo`, file restored. Works.
- **Cost ceiling enforcement.** Local-only model means $0.00 cost, but the path through `_price` and `cost_ceiling_usd` is wired and would fire on first frontier call.

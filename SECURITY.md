# Security policy

## Threat model

Forge runs LLM-emitted Python on the user's machine. The honest threat
model is documented in [docs/SAFETY.md](docs/SAFETY.md) — please read it
before reporting an issue, since some categories of "bypass" are
intentional v0.x scope decisions, not bugs.

## What counts as a security issue

Real vulnerabilities (we want to hear about these):

- Bypass of the protected-paths denylist via any tool the agent can reach
- Bypass of the macOS `sandbox-exec` boundary (file write or network)
- Exfiltration of provider API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY,
  GITHUB_TOKEN, etc.) via any code path Forge spawns
- Code execution from a clone/fetch of a skill repo BEFORE the trust
  prompt
- Argument injection in any subprocess invocation (git, MCP servers, etc.)
- Crashes induced by a hostile/malformed MCP server
- Any way for a downloaded skill's `references/` or `assets/` content to
  cause arbitrary code execution

NOT vulnerabilities (out of scope for the current security model):

- A trusted skill with `eval`/`exec`/`subprocess` doing something the
  user authorized
- The agent running `rm` on the workspace after the user approved it in
  the preview prompt
- Side-channel timing or memory-disclosure attacks
- The CLI's output being parseable by another program (we don't promise
  a stable machine-readable format)

## Reporting

For real vulnerabilities, please **don't open a public issue**. Instead:

1. Open a private security advisory via GitHub's interface at
   https://github.com/Rohan-Tiwari/forge/security/advisories/new
2. Include a minimal reproducer if possible
3. Include the Forge version (`forge --version`) and OS

We'll respond within 7 days with either a fix or an explanation of why
it's not in scope.

## Disclosure

Once a fix lands in a release, we'll:
- Credit the reporter (unless they prefer anonymity)
- Document the issue in `CHANGELOG.md` under the relevant version
- Open the security advisory for public viewing

## Past advisories

None yet. v0.2.1 closed 5 critical findings from an internal audit but
none of those were reported externally — see `docs/V021-AUDIT.json` for
the audit report and `git log --grep "fix(v0.2.1-sec)"` for the fixes.

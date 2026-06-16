---
name: changelog-from-git
description: Generate a markdown changelog from git history between two refs. Use when the user asks for release notes, "what changed since v1.2", "summarize commits this week", or "draft a changelog". Returns a grouped markdown changelog.
when_to_use: |
  - User asks for a changelog or release notes
  - User wants a summary of git activity in a date range or between tags
  - Before tagging a new release
allowed_tools:
  - Bash(git:*)
  - Write(./CHANGELOG*.md)
license: Apache-2.0
metadata:
  version: 0.1.0
  author: forge-builtin
---

# changelog-from-git — release-note generator from git history

Procedural knowledge for turning git activity into a digestible changelog.

## Decision tree

1. Default range: from the last tag to HEAD. Override with `since` / `until` args.
2. Group commits by conventional-commits prefix (`feat:`, `fix:`, `docs:`, etc.).
   Commits without a prefix go to "Other".
3. By default, render to stdout. Pass `out_path` to write a file.

## Args

- `since` — git ref or date string. Default: the last tag (or `HEAD~30` if no tags).
- `until` — git ref. Default: `HEAD`.
- `out_path` — file to write. Default: None (returns the markdown).
- `repo` — path to the repo. Default: cwd.

---
name: project-stats
description: Compute structural statistics for a code project — file count by language, total LOC, largest files, oldest/newest files. Use when the user asks "how big is this project", "what's in this codebase", "give me stats", or wants a quick orientation in an unfamiliar repo.
when_to_use: |
  - User asks for project size, LOC, file counts
  - User is unfamiliar with a codebase and wants orientation
  - User wants to compare project size to another
allowed_tools:
  - Read
  - Bash(find:*)
  - Bash(wc:*)
  - Bash(rg:*)
license: Apache-2.0
metadata:
  version: 0.1.0
  author: forge-builtin
---

# project-stats — quick code-project structural stats

Procedural knowledge for orienting in a code project.

## Decision tree

1. Call `main(path)` for a one-shot summary, or compose the helpers yourself
   if the user wants something custom.
2. Default scope is the current directory tree. Pass `path=` to scope elsewhere.
3. Helpers respect `.gitignore` if `git` is available; otherwise they walk
   everything except `.git/`, `node_modules/`, `__pycache__/`.

## Output

A markdown summary with:
- Total file count + LOC
- Top 5 languages by LOC
- 5 largest files (by line count)
- Newest and oldest commits if a git repo

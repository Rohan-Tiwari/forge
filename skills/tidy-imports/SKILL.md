---
name: tidy-imports
description: Sort and deduplicate Python imports across a project, isort-style. Groups standard library, third-party, and local imports. Use when the user asks to "tidy imports", "sort imports", "clean up imports", or "run isort".
when_to_use: |
  - User mentions tidy/sort/clean/dedupe imports
  - Before opening a PR or after a big refactor
  - When `isort` isn't installed and the user wants quick cleanup
allowed_tools:
  - Read
  - Edit
  - Write
  - Bash(rg:*)
  - Bash(find:*)
license: Apache-2.0
metadata:
  version: 0.1.0
  author: forge-builtin
---

# tidy-imports — sort & dedupe Python imports

Procedural knowledge for cleaning import blocks. Pure-Python; doesn't require
isort or any other dependency.

## Decision tree

1. Default scope: every `*.py` file under `path` (defaults to cwd), respecting
   git ignore.
2. For each file, find the contiguous import block at the top (after the
   docstring + module-level comments + `from __future__` imports).
3. Sort and dedupe; preserve relative ordering within each group:
   1. `from __future__ import ...`
   2. Standard library imports
   3. Third-party imports
   4. Local (`.` / `..` / known project) imports
4. If `dry_run=True`, return the diff without writing.

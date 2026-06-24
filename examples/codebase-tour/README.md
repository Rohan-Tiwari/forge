# codebase-tour

An interactive `forge chat` session for getting oriented in an unfamiliar
codebase. Demonstrates `forge chat` + the `project-stats` skill + vision
(`see()` for architecture diagrams) + multi-cell composition.

## Setup

Nothing to install — Forge ships with the `project-stats` skill out of the box.

## Usage

```bash
cd path/to/unfamiliar/repo
forge chat
```

Try these prompts in sequence:

### 1. Orient

```
Use project-stats to summarize this repo: total files, LOC by language, top 5 largest files.
```

### 2. Find the entry points

```
Where does this codebase start? Look for main(), cli entrypoints, or __main__ blocks.
Group findings by file path.
```

### 3. Map data flow

```
Find every place we read configuration. List the file path, the var name, and a 1-line summary
of what it controls.
```

### 4. Visualize the architecture (if there are diagrams)

```
Look for any .png or .svg files in docs/ or .github/. If you find architecture
diagrams, use see() on each and summarize what they show.
```

### 5. Run-time questions

```
If I wanted to add a new "telemetry" feature, where would the changes go?
Walk me through the modules you'd touch and the order.
```

## Why this is useful

These prompts work well together because the kernel persists across cells —
the agent can save findings from prompt 1 into a Python variable, then refer
to it in prompt 5 without re-scanning the repo. The dry-run preview is
disabled for these read-only prompts because there are no writes; everything
just runs.

If you want the agent to also WRITE its findings to a file at the end:

```
Summarize everything you've learned so far into ARCHITECTURE.md in the current
directory. Use a table-of-contents at the top and link to specific files.
```

The dry-run preview will fire for that one (it's a write), so you'll see the
file diff before approving.

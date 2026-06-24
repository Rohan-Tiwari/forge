# daily-standup

A 9am-weekdays cron job that pulls your recent git activity and open PRs into
a single `standup.md` you can paste into Slack. Demonstrates `forge daemon` +
cron schedules + the `changelog-from-git` first-party skill.

## Setup

```bash
# 1. Configure your work repo path
# Edit daemon.toml below: change workspace = "~/work" to your actual project dir

# 2. (Optional) Set up the gh MCP server so the agent can read PRs
# Add to ~/.forge/mcp.toml:
#   [servers.gh]
#   cmd = "npx"
#   args = ["-y", "@modelcontextprotocol/server-github"]
#   [servers.gh.env]
#   GITHUB_PERSONAL_ACCESS_TOKEN = "github_pat_..."

# 3. Copy this folder's daemon.toml in
cat daemon.toml >> ~/.forge/daemon.toml

# 4. Start the daemon
forge daemon --background

# 5. Verify the schedule is registered
tail ~/.forge/daemon.log
```

## How it works

```toml
[schedules.daily-standup]
cron = "0 9 * * 1-5"     # 9:00am Monday–Friday
task = """
Generate today's standup. In this order:
1. Summarize git commits I authored in the last 24h (group by repo)
2. List open PRs assigned to or authored by me (use call_mcp("gh", ...))
3. Identify anything blocking — PR conversations awaiting my response
Write the result to ./standup-{date}.md
"""
workspace = "~/work"
```

The cron format is standard 5-field: `minute hour day-of-month month day-of-week`.

Substitutions:
- `{date}` → `2026-06-24`
- `{time}` → `09:00:00`

## Testing without waiting until 9am

```bash
# Run the task manually right now (same workspace + task)
forge run --auto \
  --cwd ~/work \
  "Generate today's standup. Summarize git commits I authored in the last 24h…"
```

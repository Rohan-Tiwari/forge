# triage-inbox

A file watcher that summarizes any PDF dropped into `~/Downloads` into a
markdown file next to it. Demonstrates `forge daemon` + watchers.

## Setup

```bash
# 1. Make sure you have a vision model for PDFs-as-images, or pdftotext for text PDFs
brew install poppler          # gives you pdftotext

# 2. Copy this folder's daemon.toml to your forge config
cp daemon.toml ~/.forge/daemon.toml

# 3. Start the daemon in the background
forge daemon --background

# 4. Test it
cp some-report.pdf ~/Downloads/
# … wait a few seconds …
cat ~/Downloads/some-report.summary.md
```

## How it works

The `daemon.toml` says:

```toml
[watchers.pdf-triage]
path = "~/Downloads"
pattern = "*.pdf"
event = "created"
task = """
Read the PDF at {path}. Extract the title, 3 key points, and any action items.
Write the result to {path}.summary.md (replace the .pdf extension with .summary.md).
Use pdftotext via Bash if it's text-based, or see() if image-based.
"""
cooldown_s = 10
```

The `{path}` token gets substituted with the actual file path. `cooldown_s: 10`
means if an editor saves the file 4× rapidly, only the first event fires —
saving real money / time.

## Stop the daemon

```bash
forge daemon --stop
```

## Logs

```bash
tail -f ~/.forge/daemon.log
```

"""Enable `python -m forge.cli` to run the Typer app (parity with the
`forge` console script, and with the pre-split flat module)."""
from __future__ import annotations

from forge.cli import app

if __name__ == "__main__":
    app()

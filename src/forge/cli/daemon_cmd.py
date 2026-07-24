"""forge.cli.daemon_cmd — the long-running daemon command.

Commands register onto the shared Typer app/skill_app from forge.cli._shared.
"""
from __future__ import annotations

from forge.cli._shared import (
    Path,
    app,
    console,
    err_console,
    signal,
    sys,
    typer,
)


@app.command()
def daemon(
    background: bool = typer.Option(
        False, "--background", "-b",
        help="Fork to background (logs to ~/.forge/daemon.log).",
    ),
    stop: bool = typer.Option(False, "--stop", help="Stop a backgrounded daemon."),
    status: bool = typer.Option(False, "--status", help="Show daemon status."),
    config: Path = typer.Option(
        None, "--config", "-c",
        help="Path to daemon.toml (default ~/.forge/daemon.toml).",
    ),
) -> None:
    """Long-lived process that triggers agent runs on filesystem or schedule events.

    Configure in ~/.forge/daemon.toml:

        [watchers.downloads-triage]
        path = "~/Downloads"
        pattern = "*.pdf"
        task = "Triage the PDF at {path} and write a summary."

        [schedules.daily-standup]
        cron = "0 9 * * 1-5"
        task = "Write today's standup notes."
    """
    import os as _os

    from forge.daemon import (
        _DAEMON_LOG_PATH,
        Daemon,
        clear_pid,
        is_running,
        load_config,
        read_pid,
        write_pid,
    )

    if status:
        if is_running():
            pid = read_pid()
            console.print(f"[green]daemon running[/] (pid {pid})")
            console.print(f"[dim]logs: {_DAEMON_LOG_PATH}[/]")
        else:
            console.print("[dim]daemon not running[/]")
        return

    if stop:
        pid = read_pid()
        if pid is None:
            err_console.print("[yellow]no daemon to stop[/]")
            raise typer.Exit(1)
        try:
            _os.kill(pid, signal.SIGTERM)
            console.print(f"[green]sent SIGTERM to pid {pid}[/]")
        except ProcessLookupError:
            console.print(f"[yellow]pid {pid} not found — clearing stale pid file[/]")
            clear_pid()
        return

    if is_running():
        err_console.print(
            f"[red]daemon already running (pid {read_pid()})[/]\n"
            "[dim]use --stop to kill, or --status to inspect[/]"
        )
        raise typer.Exit(1)

    cfg = load_config(config)
    if not cfg.watchers and not cfg.schedules:
        err_console.print(
            f"[yellow]no triggers configured.[/]\n"
            f"[dim]add watchers/schedules to "
            f"{config or '~/.forge/daemon.toml'}[/]"
        )
        raise typer.Exit(1)

    if background:
        # Double-fork to fully detach from terminal.
        pid = _os.fork()
        if pid > 0:
            console.print(f"[green]daemon backgrounded (pid {pid})[/]")
            console.print(f"[dim]logs: {_DAEMON_LOG_PATH}[/]")
            return
        _os.setsid()
        pid2 = _os.fork()
        if pid2 > 0:
            _os._exit(0)
        # Now in the daemonized grandchild.
        _DAEMON_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_fd = _os.open(
            _DAEMON_LOG_PATH,
            _os.O_WRONLY | _os.O_CREAT | _os.O_APPEND,
            0o644,
        )
        _os.dup2(log_fd, 1)
        _os.dup2(log_fd, 2)
        sys.stdin.close()
        write_pid(_os.getpid())

        def log_fn(msg: str) -> None:
            print(msg, flush=True)

        try:
            Daemon(cfg, log_fn=log_fn).run_forever()
        finally:
            clear_pid()
        return

    # Foreground.
    write_pid(_os.getpid())

    def log_fn(msg: str) -> None:
        console.print(msg)

    try:
        Daemon(cfg, log_fn=log_fn).run_forever()
    finally:
        clear_pid()


if __name__ == "__main__":
    app()

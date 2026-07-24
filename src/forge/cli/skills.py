"""forge.cli.skills — skill list / show / permit / install / diff / search / update.

Commands register onto the shared Typer app/skill_app from forge.cli._shared.
"""
from __future__ import annotations

from forge.cli._shared import (
    Panel,
    Path,
    PermissionStore,
    SkillRegistry,
    Syntax,
    Table,
    console,
    err_console,
    shutil,
    skill_app,
    typer,
)


@skill_app.command("list")
def skill_list() -> None:
    """List installed skills."""
    reg = SkillRegistry.scan()
    if not reg.skills:
        console.print("[dim]no skills installed[/]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("description")
    table.add_column("path", style="dim")
    for s in reg.skills:
        desc = s.description
        if len(desc) > 80:
            desc = desc[:77] + "..."
        table.add_row(s.name, desc, str(s.path))
    console.print(table)


@skill_app.command("show")
def skill_show(name: str) -> None:
    """Render a skill's SKILL.md + helpers + AST scan."""
    reg = SkillRegistry.scan()
    s = reg.get(name)
    if s is None:
        err_console.print(f"[red]no skill named {name!r}[/]")
        raise typer.Exit(1)
    console.print(Panel.fit(
        f"[bold]{s.name}[/]  ·  [dim]{s.path}[/]\n\n"
        f"[bold]description:[/] {s.description}\n"
        f"[bold]when_to_use:[/] {s.frontmatter.when_to_use or '(none)'}\n"
        f"[bold]model:[/] {s.frontmatter.model}  "
        f"[bold]effort:[/] {s.frontmatter.effort}\n"
        f"[bold]allowed_tools:[/] {s.frontmatter.allowed_tools}\n"
        f"[bold]license:[/] {s.frontmatter.license}",
        title="frontmatter",
    ))
    if s.body:
        console.print(Panel(
            Syntax(s.body, "markdown", theme="monokai"),
            title="body",
        ))
    if s.helpers_path:
        console.print(Panel(
            Syntax(s.helpers_path.read_text(), "python", theme="monokai", line_numbers=True),
            title=f"helpers.py · {s.helpers_path}",
        ))


@skill_app.command("permit")
def skill_permit(
    pattern: str = typer.Argument(..., help='Permission pattern. Examples: "Bash(git:*)", "Write(./out/**)", "Network(api.github.com)".'),
    persistent: bool = typer.Option(False, "--persistent", help="Save to ~/.forge/permissions.toml"),
) -> None:
    """Grant an "always allow" permission rule.

    By default, the rule is ephemeral (--persistent saves it). Patterns:
      Bash(<cmd>:*)         — any Bash starting with <cmd> (e.g. `git`, `rg`)
      Write(<glob>)         — any Write to a path matching the glob
      Network(<host>)       — exact hostname
      Skill(<name>)         — auto-allow that skill's cells
      *                     — blanket allow (use with care)
    """
    store = PermissionStore.load()
    if persistent:
        store.grant_persistent(pattern)
        console.print(f"[green]saved[/] persistent grant: {pattern}")
        console.print(f"[dim]→ {store.PERMISSIONS_PATH if hasattr(store, 'PERMISSIONS_PATH') else '~/.forge/permissions.toml'}[/]")
    else:
        console.print(
            "[yellow]session grants are added by typing 'a' at the preview prompt.[/]\n"
            "[dim]for a persistent grant, use --persistent.[/]"
        )


@skill_app.command("install")
def skill_install(
    spec: str = typer.Argument(
        ...,
        help='Install spec: "<github-shorthand>@<sha>" or "<git-url>@<ref>[:<subdir>]". '
             'Example: alice/forge-skills@a3f9c2c'
    ),
    pin: bool = typer.Option(
        False, "--pin",
        help="Allow installing a floating ref (main, master, HEAD). Without this, only shas/tags accepted.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the trust confirmation prompt. Dangerous — only use for known-good repos.",
    ),
) -> None:
    """Install a skill from a git repo, pinned to a specific sha."""
    from forge.installer import (
        FloatingRefError,
        InstallError,
        execute_install,
        parse_spec,
        prepare_install,
    )

    try:
        parsed = parse_spec(spec)
    except InstallError as e:
        err_console.print(f"[red]{e}[/]")
        raise typer.Exit(2)

    console.print(f"[dim]source:[/] {parsed.source}")
    console.print(f"[dim]ref:[/]    {parsed.ref}")
    if parsed.subdir:
        console.print(f"[dim]subdir:[/] {parsed.subdir}")

    try:
        plan = prepare_install(parsed, allow_floating=pin)
    except FloatingRefError as e:
        err_console.print(f"[red]{e}[/]")
        err_console.print("[yellow]Pass --pin to override (and accept the risk).[/]")
        raise typer.Exit(2)
    except InstallError as e:
        err_console.print(f"[red]{e}[/]")
        raise typer.Exit(1)

    # Show plan + findings
    console.print()
    console.print(Panel.fit(
        f"resolved sha: [bold]{plan.resolved_sha[:12]}[/]\n"
        f"skills found: {len(plan.skills_found)} "
        f"({', '.join(plan.skills_found) or '(none — repo is empty?)'})\n"
        f"install path: {plan.install_path}\n"
        f"findings:     {len(plan.findings)} total, "
        f"{len(plan.critical_findings)} critical",
        title="install plan",
    ))

    if plan.findings:
        console.print()
        console.print("[bold]AST scan findings[/]:")
        table = Table(show_header=True, header_style="bold")
        table.add_column("severity")
        table.add_column("file")
        table.add_column("line", justify="right")
        table.add_column("code")
        table.add_column("detail")
        for f in plan.findings[:30]:
            sev_color = {"critical": "red", "warn": "yellow"}.get(f.severity, "dim")
            table.add_row(
                f"[{sev_color}]{f.severity}[/]",
                Path(f.file).name,
                str(f.line),
                f.code,
                f.detail[:80],
            )
        if len(plan.findings) > 30:
            table.add_row("...", "...", "...", "...",
                          f"+ {len(plan.findings) - 30} more")
        console.print(table)

    if plan.critical_findings and not yes:
        console.print()
        console.print(
            "[red]This skill contains code that would be flagged at runtime "
            "(eval / subprocess / dynamic-attribute access). Review carefully "
            "before installing.[/]"
        )

    if not yes:
        # 5-second cooldown + confirm prompt
        console.print()
        console.print("[dim](cooldown — pausing 5s to give you time to read)[/]")
        import time
        time.sleep(5)
        if not typer.confirm("Trust this install?", default=False):
            console.print("[yellow]aborted[/]")
            # Cleanup the tmp clone
            shutil.rmtree(plan.workdir, ignore_errors=True)
            raise typer.Exit(1)

    entry = execute_install(plan)
    console.print()
    console.print(
        f"[green]installed[/] {entry.name}@{entry.sha[:12]} "
        f"({entry.skill_count} skill folders) → {entry.install_path}"
    )


@skill_app.command("diff")
def skill_diff(name: str = typer.Argument(..., help="Skill name (from `forge skill list`).")) -> None:
    """Show what would change if you reinstalled this skill at upstream HEAD."""
    from forge.installer import diff_installed
    console.print(diff_installed(name))


@skill_app.command("search")
def skill_search(
    query: str = typer.Argument(
        "", help="Search terms. Empty = list all skills tagged forge-skill."
    ),
    limit: int = typer.Option(10, "-n", "--limit"),
) -> None:
    """Search GitHub for repos tagged with the `forge-skill` topic.

    No auth needed for unauthenticated 60 req/hr. Set GITHUB_TOKEN for higher
    rate limits. Sorted by stars.
    """
    from forge.installer import search_skills

    console.print(f"[dim]searching github for skills{f' matching {query!r}' if query else ''}…[/]")
    response = search_skills(query, limit=limit)
    results = response["results"]
    if not results:
        if response["rate_limited"]:
            console.print(
                "[yellow]GitHub rate-limited the search request.[/]\n"
                "[dim]Set GITHUB_TOKEN (https://github.com/settings/tokens) "
                "and retry — that raises the limit from 60/hr to 5000/hr.[/]"
            )
        else:
            remaining = response["rate_limit_remaining"]
            remaining_hint = (
                f" ({remaining} req remaining in this hour)"
                if remaining is not None else ""
            )
            console.print(
                f"[yellow]no skills match.[/]{remaining_hint}\n"
                "[dim]Forge skills are GitHub repos tagged with the "
                "`forge-skill` topic. Try `forge skill search` (no query) "
                "to list all of them.[/]"
            )
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("repo")
    table.add_column("★", justify="right")
    table.add_column("updated", style="dim")
    table.add_column("description")
    for r in results:
        desc = r["description"] or "[dim](no description)[/]"
        if len(desc) > 70:
            desc = desc[:67] + "..."
        table.add_row(r["full_name"], r["stars"], r["updated"], desc)
    console.print(table)
    console.print(
        "\n[dim]install one with:[/]\n"
        "  forge skill install <repo>@<sha>"
    )


@skill_app.command("update")
def skill_update(
    name: str = typer.Argument(..., help="Skill name (from `forge skill list`)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Check for upstream changes and re-install at the latest sha.

    Looks up the default branch's HEAD on GitHub, compares to the
    installed sha, and re-invokes `skill install` if they differ.
    """
    from forge.installer import latest_sha, load_manifest

    entries = [e for e in load_manifest() if e.name == name]
    if not entries:
        err_console.print(f"[red]skill {name!r} not installed[/]")
        raise typer.Exit(1)
    latest = entries[-1]

    # Parse source like 'github.com/alice/forge-skills'
    src = latest.source
    if not src.startswith("github.com/"):
        err_console.print(
            f"[yellow]skill update only supports GitHub-sourced skills.[/]\n"
            f"[dim]installed source: {src}[/]"
        )
        raise typer.Exit(1)
    owner_repo = src[len("github.com/"):]
    try:
        owner, repo = owner_repo.split("/", 1)
    except ValueError:
        err_console.print(f"[red]can't parse owner/repo from {src!r}[/]")
        raise typer.Exit(1)

    console.print(f"[dim]checking {owner}/{repo} for updates…[/]")
    upstream = latest_sha(owner, repo)
    if upstream is None:
        err_console.print(
            "[yellow]could not reach GitHub. Network error or rate-limited.[/]"
        )
        raise typer.Exit(1)

    if upstream == latest.sha:
        console.print(f"[green]up to date[/] · {latest.sha[:12]}")
        return

    console.print(
        f"[yellow]update available:[/] {latest.sha[:12]} → {upstream[:12]}"
    )
    if not yes:
        if not typer.confirm("install the new version?", default=True):
            console.print("[dim]aborted[/]")
            return

    # Recurse into `skill install` for the actual work — same flow.
    console.print()
    from forge.installer import (
        SkillSpec,
        execute_install,
        prepare_install,
    )
    spec = SkillSpec(
        url=f"https://github.com/{owner}/{repo}.git",
        ref=upstream,
        source=src,
        name=name,
    )
    plan = prepare_install(spec)
    entry = execute_install(plan)
    console.print(
        f"[green]updated[/] {entry.name}@{entry.sha[:12]} → {entry.install_path}"
    )

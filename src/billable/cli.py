"""Command-line entry point.

Surface (committed in README.md "Run"):

    billable run --date today
    billable run --date 2026-05-07
    billable run --range 2026-05-01..2026-05-07
    billable run --no-llm
    billable run --reuse-cache
    billable run --stage narrate
    billable run --source cursor --source gdocs

The CLI is deliberately thin: it parses args, builds a `PipelineConfig`, and
calls `Pipeline.run()`. All real work lives in `core/`, `adapters/`, `llm/`,
`rollup/`, `renderers/`.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler

from billable import __version__
from billable.adapters.activitywatch import ActivityWatchAdapter
from billable.adapters.base import CaptureAdapter
from billable.adapters.cursor import CursorAdapter
from billable.adapters.google_docs import GoogleDocsAdapter
from billable.adapters.notes import NotesAdapter
from billable.core.mapper import ProjectMapper
from billable.core.pipeline import Pipeline, PipelineConfig
from billable.discovery import propose_projects_yaml, scan_machine
from billable.llm.client import OpenAIClient
from billable.notes.store import DEFAULT_NOTES_PATH, Note, append_note
from billable.renderers.markdown import MarkdownRenderer
from billable.rollup.daily import BillingPolicy

app = typer.Typer(
    name="billable",
    help="Capture what you did, render a billable timesheet.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


# --- Option parsing helpers -------------------------------------------------


def _parse_date(value: str) -> date:
    """Accepts 'today', 'yesterday', or an ISO date 'YYYY-MM-DD'."""
    value = value.strip().lower()
    if value == "today":
        return date.today()
    if value == "yesterday":
        return date.today() - timedelta(days=1)
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as e:
        raise typer.BadParameter(
            f"Could not parse date {value!r}. Use 'today', 'yesterday', or 'YYYY-MM-DD'."
        ) from e


def _parse_range(value: str) -> tuple[date, date]:
    """Accepts 'YYYY-MM-DD..YYYY-MM-DD' (inclusive on both ends)."""
    if ".." not in value:
        raise typer.BadParameter("Range must be 'YYYY-MM-DD..YYYY-MM-DD'.")
    start_str, end_str = value.split("..", 1)
    start = _parse_date(start_str)
    end = _parse_date(end_str)
    if end < start:
        raise typer.BadParameter("Range end is before range start.")
    return start, end


def _build_adapters(requested: list[str], cache_dir: Path) -> list[CaptureAdapter]:
    """Build adapter instances based on --source flags."""
    known = ["cursor", "notes", "activitywatch", "gdocs"]
    adapters: list[CaptureAdapter] = []
    for name in requested:
        if name == "cursor":
            adapters.append(CursorAdapter())
        elif name == "notes":
            adapters.append(NotesAdapter())
        elif name == "activitywatch":
            min_focus = float(os.environ.get("BILLABLE_AW_MIN_FOCUS_MIN", "2"))
            base_url = os.environ.get("BILLABLE_AW_URL", "http://localhost:5600")
            adapters.append(
                ActivityWatchAdapter(base_url=base_url, min_focus_minutes=min_focus)
            )
        elif name == "gdocs":
            client_secrets = Path(
                os.environ.get(
                    "GOOGLE_OAUTH_CLIENT_SECRETS",
                    "./config/google_oauth_client.json",
                )
            )
            if not client_secrets.exists():
                console.print(
                    f"[yellow]Skipping gdocs: client secrets not found at "
                    f"{client_secrets}. See README \"Google OAuth (one-time)\".[/yellow]"
                )
                continue
            adapters.append(
                GoogleDocsAdapter(
                    client_secrets_path=client_secrets,
                    token_cache_path=cache_dir / "google_token.json",
                )
            )
        else:
            raise typer.BadParameter(f"Unknown source {name!r}. Known: {known}")
    if not adapters:
        raise typer.BadParameter("No usable capture adapters were selected.")
    return adapters


# --- Commands ---------------------------------------------------------------


@app.command()
def note(
    text: Annotated[str, typer.Argument(help="The note text. Quote it if it has spaces.")],
    minutes: Annotated[
        int | None,
        typer.Option(
            "--minutes",
            "-m",
            help="Optional duration in minutes. Counts toward billable time.",
        ),
    ] = None,
    matter: Annotated[
        str | None,
        typer.Option(
            "--matter",
            help="Pin this note to a matter (matter_id from projects.yaml). "
            "Bypasses keyword/folder rules.",
        ),
    ] = None,
) -> None:
    """Append a timestamped note to the daily activity log.

    Bind a global Windows hotkey to this command for friction-free capture.
    See README "Hotkey notes" for the AutoHotkey/PowerShell snippet.

    Examples:
        billable note "Reviewed Smith brief, returned with comments."
        billable note "Call with Acme product team" -m 45 --matter acme-website
    """
    load_dotenv()
    from datetime import datetime as _dt

    if not text.strip():
        raise typer.BadParameter("Note text cannot be empty.")
    if minutes is not None and minutes <= 0:
        raise typer.BadParameter("--minutes must be positive.")

    n = Note(
        timestamp=_dt.now().astimezone(),
        text=text.strip(),
        minutes=minutes,
        matter_id=matter,
    )
    append_note(n)
    pieces = [f"[green]+ note[/green] @ {n.timestamp:%H:%M:%S}"]
    if minutes is not None:
        pieces.append(f"({minutes}m)")
    if matter:
        pieces.append(f"-> [cyan]{matter}[/cyan]")
    pieces.append(f": {text.strip()}")
    console.print(" ".join(pieces))
    console.print(f"  [dim]stored in {DEFAULT_NOTES_PATH}[/dim]")


@app.command()
def version() -> None:
    """Print the installed billable version."""
    console.print(f"billable {__version__}")


@app.command()
def discover(
    days: Annotated[
        int,
        typer.Option(
            "--days", help="How many days of history to scan."
        ),
    ] = 7,
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            help="Where to write the proposed YAML. Defaults to "
            "config/projects.proposed.yaml so it never clobbers your existing config.",
        ),
    ] = Path("./config/projects.proposed.yaml"),
    model: Annotated[
        str,
        typer.Option(
            "--model",
            help="LLM model to use for the proposal. Cheap models work fine here.",
        ),
    ] = "gpt-4o-mini",
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Overwrite config/projects.yaml with the proposal "
            "(creates a .bak first). Off by default \u2014 review the proposed "
            "file first.",
        ),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Verbose logging.")
    ] = False,
) -> None:
    """Auto-propose a `projects.yaml` from what's on this machine.

    Scans your Cursor workspaces and the last N days of ActivityWatch
    window patterns, sends the summary to the LLM, and writes a draft
    `projects.proposed.yaml`. You review it, then either copy it over
    `projects.yaml` yourself or rerun with `--apply`.

    First-time setup:
        billable discover                 # scan + draft, ~$0.01 of LLM
        # ...review config/projects.proposed.yaml...
        billable discover --apply          # overwrite, with .bak backup
    """
    load_dotenv()
    _setup_logging(verbose)

    console.print(f"[bold]billable discover[/bold] (last {days} days)")
    with console.status("[cyan]Scanning Cursor workspaces and ActivityWatch..."):
        scan = scan_machine(days=days)

    console.print(
        f"  cursor workspaces : [bold]{len(scan.cursor_workspaces)}[/bold]"
    )
    console.print(
        f"  browser patterns  : [bold]{len(scan.browser_patterns)}[/bold]"
        f" (AW {'reachable' if scan.activitywatch_available else 'NOT reachable'})"
    )
    console.print(f"  recent notes      : [bold]{scan.note_count}[/bold]")

    if not scan.cursor_workspaces and not scan.browser_patterns:
        console.print(
            "[yellow]Nothing to propose \u2014 no Cursor workspaces and no AW patterns "
            "found in the last {} days.[/yellow]".format(days)
        )
        raise typer.Exit(code=1)

    for ws in scan.cursor_workspaces[:10]:
        console.print(
            f"    \u2022 cursor: [cyan]{ws.folder_name}[/cyan] "
            f"({ws.prompt_count} prompts, last {ws.last_seen.strftime('%Y-%m-%d')})"
        )
    for bp in scan.browser_patterns[:10]:
        console.print(
            f"    \u2022 {bp.app}: [magenta]{bp.title_prefix}[/magenta] "
            f"({bp.occurrence_count}x, {bp.total_minutes}m)"
        )

    console.print(f"\n[cyan]Asking {model} to propose project mapping...[/cyan]")
    llm = OpenAIClient()
    result = propose_projects_yaml(scan, llm=llm, model=model)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.yaml_text, encoding="utf-8")
    console.print(
        f"\n[green]Proposed {result.project_count} projects[/green] -> [bold]{output}[/bold]"
    )
    if result.skipped_patterns:
        console.print(
            f"  [dim]({len(result.skipped_patterns)} browser patterns skipped as personal/generic)[/dim]"
        )

    target = output.parent / "projects.yaml"
    if apply:
        if target.exists():
            backup = target.with_suffix(".yaml.bak")
            target.rename(backup)
            console.print(f"  backed up existing -> [dim]{backup}[/dim]")
        output.rename(target)
        console.print(f"[green]Applied[/green] -> [bold]{target}[/bold]")
        console.print(
            "  Re-run [cyan]billable run --date today --reuse-cache --stage rollup[/cyan] "
            "to regenerate today's report against the new mapping."
        )
    else:
        console.print(
            f"\nReview [bold]{output}[/bold], then either:\n"
            f"  - copy it over [cyan]{target}[/cyan] yourself, or\n"
            f"  - rerun with [cyan]--apply[/cyan] (creates a .bak of your current yaml)."
        )


@app.command()
def run(
    date_arg: Annotated[
        str | None,
        typer.Option("--date", help="Day to bill: 'today', 'yesterday', or 'YYYY-MM-DD'."),
    ] = None,
    range_arg: Annotated[
        str | None,
        typer.Option("--range", help="Inclusive date range: 'YYYY-MM-DD..YYYY-MM-DD'."),
    ] = None,
    sources: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            help="Limit to one or more sources (e.g. cursor, gdocs). Repeatable.",
        ),
    ] = None,
    no_llm: Annotated[
        bool,
        typer.Option("--no-llm", help="Skip LLM stages; dump raw events as a debug file."),
    ] = False,
    reuse_cache: Annotated[
        bool,
        typer.Option(
            "--reuse-cache",
            help="Use cached events instead of re-pulling from sources.",
        ),
    ] = False,
    stage: Annotated[
        str | None,
        typer.Option(
            "--stage",
            help="Run only one stage: 'cluster' or 'narrate'. Implies --reuse-cache.",
        ),
    ] = None,
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directory containing projects.yaml + billing.yaml."),
    ] = Path("./config"),
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Verbose logging.")
    ] = False,
) -> None:
    """Capture activity, summarize, and render a timesheet."""
    load_dotenv()
    _setup_logging(verbose)

    if date_arg and range_arg:
        raise typer.BadParameter("Pass --date OR --range, not both.")

    target_dates: list[date]
    if range_arg:
        start, end = _parse_range(range_arg)
        target_dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    else:
        target_dates = [_parse_date(date_arg or "today")]

    if stage and stage not in {"cluster", "narrate"}:
        raise typer.BadParameter("--stage must be 'cluster' or 'narrate'.")

    cache_dir = Path(os.environ.get("BILLABLE_CACHE_DIR", "./cache"))
    out_dir = Path(os.environ.get("BILLABLE_OUT_DIR", "./out"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    default_sources = [
        s.strip()
        for s in os.environ.get(
            "BILLABLE_SOURCES", "cursor,notes,activitywatch,gdocs"
        ).split(",")
        if s.strip()
    ]
    requested_sources = sources or default_sources
    adapters = _build_adapters(requested_sources, cache_dir)

    # Config files.
    projects_path = config_dir / "projects.yaml"
    billing_path = config_dir / "billing.yaml"
    if not projects_path.exists():
        raise typer.BadParameter(
            f"Project config not found at {projects_path}. "
            f"Copy {config_dir}/projects.example.yaml to {projects_path} and edit it."
        )
    mapper = ProjectMapper.from_yaml(projects_path)
    policy = BillingPolicy.from_yaml(billing_path)

    # LLM client (only constructed if we need it — keeps --no-llm useful without an API key).
    llm: OpenAIClient | None = None
    cluster_model = os.environ.get("BILLABLE_MODEL_CLUSTER", "gpt-4o-mini")
    narrate_model = os.environ.get("BILLABLE_MODEL_NARRATE", "gpt-4o")
    if not no_llm:
        llm = OpenAIClient()

    renderer = MarkdownRenderer(mapper=mapper)

    console.print(f"[bold]billable {__version__}[/bold]")
    console.print(f"  dates           : {', '.join(d.isoformat() for d in target_dates)}")
    console.print(f"  sources         : {', '.join(a.name for a in adapters)}")
    console.print(f"  cluster_model   : {cluster_model}")
    console.print(f"  narrate_model   : {narrate_model}")
    console.print(f"  cache_dir       : {cache_dir}")
    console.print(f"  out_dir         : {out_dir}")
    console.print(f"  no_llm          : {no_llm}")
    console.print(f"  reuse_cache     : {reuse_cache or stage is not None}")
    console.print(f"  stage           : {stage or 'all'}")

    for d in target_dates:
        console.print(f"\n[bold cyan]== {d.isoformat()} ==[/bold cyan]")
        cfg = PipelineConfig(
            target_date=d,
            adapters=adapters,
            mapper=mapper,
            llm=llm,  # type: ignore[arg-type]  # may be None when skip_llm=True
            cluster_model=cluster_model,
            narrate_model=narrate_model,
            renderer=renderer,
            policy=policy,
            cache_dir=cache_dir,
            out_dir=out_dir,
            reuse_cache=reuse_cache or stage is not None,
            skip_llm=no_llm,
            only_stage=stage,  # type: ignore[arg-type]
        )
        entries = Pipeline(cfg).run()
        if no_llm:
            console.print(f"  -> debug dump in {out_dir / f'{d.isoformat()}.debug.md'}")
        else:
            console.print(
                f"  -> {len(entries)} entries written to {out_dir / f'{d.isoformat()}.md'}"
            )


def _setup_logging(verbose: bool) -> None:
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_time=False, show_path=False)],
    )


if __name__ == "__main__":
    app()

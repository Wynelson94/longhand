"""Demo runner: build a sandboxed Longhand store from the sample corpus,
then run a guided walkthrough of recall / project-status.

The demo never touches the user's real ~/.longhand or ~/.claude.
Everything happens under /tmp/longhand-demo-<timestamp>/.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule

from longhand.demo.corpus import generate_corpus
from longhand.parser import JSONLParser
from longhand.recall import recall as recall_pipeline
from longhand.recall.recall_pipeline import recall_project_status
from longhand.storage.store import LonghandStore

console = Console()


def _seed_corpus(store: LonghandStore, jsonl_dir: Path, project_dir: Path) -> int:
    """Generate the corpus, write JSONL files, ingest each. Returns event count."""
    sessions = generate_corpus(project_dir)
    total_events = 0
    for filename, events in sessions:
        path = jsonl_dir / filename
        with path.open("w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")
        parser = JSONLParser(path)
        parsed = list(parser.parse_events())
        session = parser.build_session(parsed)
        store.ingest_session(session, parsed)
        total_events += len(parsed)
    return total_events


def _run_recall(store: LonghandStore, query: str, *, now: datetime) -> None:
    """Run recall and pretty-print the narrative."""
    result = recall_pipeline(store, query, now=now)
    if result.narrative:
        console.print(
            Panel(Markdown(result.narrative), title=f"recall: {query!r}", border_style="cyan")
        )
    else:
        console.print(f"[yellow]No narrative produced for {query!r}[/yellow]")


def _run_project_status(store: LonghandStore, project: str) -> None:
    """Run recall_project_status and pretty-print."""
    result = recall_project_status(store, project)
    if result is not None and result.narrative:
        console.print(
            Panel(
                Markdown(result.narrative),
                title=f"recall_project_status({project!r})",
                border_style="magenta",
            )
        )
    else:
        console.print(
            f"[yellow]No project status produced for {project!r} (project may not be known to the store).[/yellow]"
        )


def run_demo(*, keep: bool = False) -> Path | None:
    """Run the full demo walkthrough.

    Args:
        keep: if True, leave the temp dir in place (printed to stdout) so the
            user can explore further with `LONGHAND_DATA_DIR=<path> longhand ...`.
            If False (default), clean up at the end.

    Returns:
        The path to the demo dir if `keep=True`, otherwise None.
    """
    # Demo anchor date — events in the corpus are dated 2026-05-15..17, so
    # recall's time parsing sees them as "today / yesterday / 2 days ago"
    # relative to this date instead of the actual now (which could be months
    # in the future from the corpus).
    demo_now = datetime(2026, 5, 17, 14, 0, 0, tzinfo=timezone.utc)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    demo_root = Path(tempfile.gettempdir()) / f"longhand-demo-{timestamp}"
    store_dir = demo_root / "store"
    jsonl_dir = demo_root / "jsonl"
    project_dir = demo_root / "demo-shop"

    store_dir.mkdir(parents=True, exist_ok=True)
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    # Project marker so cwd inference attributes events correctly
    (project_dir / ".git").mkdir(exist_ok=True)

    console.print(Rule("[bold cyan]Longhand demo[/bold cyan]"))
    console.print(
        "[dim]Sandbox at[/dim]",
        f"[dim]{demo_root}[/dim]",
        "[dim]— your real ~/.longhand and ~/.claude are NOT touched.[/dim]",
    )
    console.print()

    # 1. Seed
    store = LonghandStore(data_dir=store_dir)
    console.print("[bold]Seeding 3 fake Claude Code sessions into a temp store...[/bold]")
    n_events = _seed_corpus(store, jsonl_dir, project_dir)
    console.print(
        f"  → ingested 3 sessions, [cyan]{n_events}[/cyan] events on project [magenta]demo-shop[/magenta]"
    )
    console.print()
    console.print("[dim]Workflow in the corpus:[/dim]")
    console.print(
        "  [dim]• 2 days ago:[/dim] Stripe webhook handler + signature-verification bug fix"
    )
    console.print(
        "  [dim]• 1 day ago:[/dim] Supabase auth migration from createClient → SSR createServerClient"
    )
    console.print(
        "  [dim]• today:[/dim]    Quick 401 fix in /api/checkout caused by yesterday's auth migration"
    )
    console.print()

    # 2. Recall — cross-session bug retrieval
    console.print(Rule("[cyan]Try 1: cross-session bug retrieval[/cyan]"))
    console.print('[dim]Question: "the stripe signature bug on demo-shop"[/dim]')
    console.print(
        "[dim]Longhand finds the bug fix from 2 days ago — even though the session is closed.[/dim]"
    )
    console.print()
    _run_recall(store, "the stripe signature bug on demo-shop", now=demo_now)
    console.print()

    # 3. Recall — finding the auth migration pattern
    console.print(Rule('[cyan]Try 2: "where did I switch to SSR auth?"[/cyan]'))
    console.print('[dim]Question: "supabase ssr auth migration on demo-shop"[/dim]')
    console.print()
    _run_recall(store, "supabase ssr auth migration on demo-shop", now=demo_now)
    console.print()

    # 4. Project status — pick up where we left off
    console.print(Rule('[magenta]Try 3: "pick up where I left off on demo-shop"[/magenta]'))
    console.print(
        '[dim]recall_project_status("demo-shop") returns recent activity + last session outcome.[/dim]'
    )
    console.print()
    _run_project_status(store, "demo-shop")
    console.print()

    # 5. Wrap up
    console.print(Rule("[bold green]Done[/bold green]"))
    console.print("Like what you saw? Point Longhand at your real Claude Code history:")
    console.print()
    console.print("  [bold]pip install longhand[/bold]")
    console.print("  [bold]longhand setup[/bold]")
    console.print()
    console.print(
        "Then any future Claude Code session feeds the index automatically (SessionEnd + Stop hooks)."
    )
    console.print("Once you've worked a bit, try:")
    console.print('  [bold]longhand recall "that bug from last week"[/bold]')
    console.print()

    # 6. Cleanup
    if keep:
        console.print(f"[yellow]--keep[/yellow]: corpus preserved at [cyan]{demo_root}[/cyan]")
        console.print(
            f"  Explore with: [bold]LONGHAND_DATA_DIR={store_dir} longhand sessions[/bold]"
        )
        return demo_root
    else:
        shutil.rmtree(demo_root, ignore_errors=True)
        console.print("[dim]Cleaned up demo sandbox.[/dim]")
        return None

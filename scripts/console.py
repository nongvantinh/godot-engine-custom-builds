"""Shared Rich console + logging setup for build-godot.py and its sub-commands.

Every module that wants prettier terminal output goes through this module
instead of constructing its own ``Console``/``Progress``/``Table`` so the whole
CLI stays visually consistent.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from contextlib import contextmanager

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table

# ``soft_wrap=True`` disables Rich's automatic line-wrapping for plain
# console.print()/log calls. Log messages regularly contain long, single-line
# SCons/docker/gh command strings; wrapping them would break mid-token and is
# never wanted for a build log that also gets `tee`'d to a file.
console = Console(soft_wrap=True)

# Status -> style used consistently by every summary table in this project.
_STATUS_STYLES: dict[str, str] = {
    "ok": "bold green",
    "success": "bold green",
    "built": "bold green",
    "pushed": "bold green",
    "published": "bold green",
    "done": "bold green",
    "skipped": "yellow",
    "dry-run": "cyan",
    "failed": "bold red",
    "error": "bold red",
}


def configure_logging(verbose: bool) -> None:
    """Route stdlib ``logging`` through Rich for the whole process.

    Log lines get wrapped to the real terminal width when attached to one
    (nicer to read interactively). When output is piped or captured — CI,
    ``subprocess.run(capture_output=True)``, the per-platform ``tee`` log
    files — Rich's own table-based line layout would otherwise still wrap
    long SCons/docker/gh command lines at whatever width it falls back to,
    breaking `grep`/substring matching on those logs. A generous fixed width
    keeps piped output effectively unwrapped, matching plain `logging`.
    """
    level = logging.DEBUG if verbose else logging.INFO
    log_console = console if console.is_terminal else Console(
        soft_wrap=True, width=4096
    )
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=log_console,
                show_path=False,
                markup=False,
                rich_tracebacks=True,
            )
        ],
        force=True,
    )


def section(title: str, subtitle: str = "", *, style: str = "cyan") -> None:
    """Print a panel marking the start of a build/package/publish stage."""
    body = f"[bold]{title}[/bold]"
    if subtitle:
        body += f"\n[dim]{subtitle}[/dim]"
    console.print(Panel(body, border_style=style, expand=False))


def status_text(status: str) -> str:
    """Wrap *status* in the style used across summary tables, for reuse
    outside of :func:`summary_table` (e.g. inline log messages)."""
    style = _STATUS_STYLES.get(status.strip().lower())
    return f"[{style}]{status}[/{style}]" if style else status


def summary_table(
    title: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    status_column: int | Iterable[int] | None = None,
) -> None:
    """Print a summary table, e.g. per-platform/per-image build results.

    *status_column* is the 0-based index (or indices) of column(s) — commonly
    the last one(s) — whose values should be colorized via
    :data:`_STATUS_STYLES`.
    """
    status_columns: set[int] = set()
    if isinstance(status_column, int):
        status_columns = {status_column}
    elif status_column is not None:
        status_columns = set(status_column)

    table = Table(title=title, show_header=True, header_style="bold magenta")
    for col in columns:
        table.add_column(col)
    for row in rows:
        cells = [str(c) for c in row]
        for idx in status_columns:
            if idx < len(cells):
                cells[idx] = status_text(cells[idx])
        table.add_row(*cells)
    console.print(table)


class ResultTable:
    """Accumulates result rows and prints them as a summary table on demand.

    A multi-stage command (build a platform matrix, build a container
    matrix, run a release) wants the same summary table printed on every
    early return (one per failure branch) as well as on success. Wrapping
    the "append a row, then print" pair in one object means every call site
    reuses the same accumulator instead of re-declaring an identical
    print-the-table closure.
    """

    def __init__(
        self,
        title: str,
        columns: Sequence[str],
        *,
        status_column: int | Iterable[int] | None = None,
    ) -> None:
        self.title = title
        self.columns = columns
        self.status_column = status_column
        self.rows: list[Sequence[object]] = []

    def add(self, *cells: object) -> None:
        self.rows.append(cells)

    def print(self) -> None:
        summary_table(
            self.title, self.columns, self.rows, status_column=self.status_column
        )


@contextmanager
def spinner(description: str):
    """Spinner for steps with no incremental feedback of their own (e.g. a
    silent recursive chown) — NOT for steps that shell out to a subprocess
    that streams its own output (docker build/pull/run), where a spinner
    would fight the child process for the terminal.
    """
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(description, total=None)
        yield progress
        progress.update(task, completed=True)


def progress_bar() -> Progress:
    """A determinate progress bar (spinner + bar + percentage) for loops with
    a known item count, e.g. iterating a build matrix."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    )

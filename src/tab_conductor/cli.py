"""Command-line interface for tab-conductor.

Provides the ``tab-conductor`` command with subcommands for running plans,
inspecting run state, watching live progress, killing runs, generating bug
reports, and validating plan files.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import click

from tab_conductor import __version__
from tab_conductor.exceptions import PlanParseError
from tab_conductor.logging_config import get_logger
from tab_conductor.plan_parser import parse_plan, redact_text

_logger: logging.Logger = get_logger("tab_conductor.cli")

# Text extensions eligible for secret redaction in bugreport
_TEXT_EXTS = frozenset({".json", ".jsonl", ".yaml", ".yml", ".txt", ".md", ".log"})


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group()
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable DEBUG-level logging.",
)
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """tab-conductor: multi-tab orchestrator skill for Claude Code."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    if verbose:
        logging.getLogger("tab_conductor").setLevel(logging.DEBUG)
    else:
        logging.getLogger("tab_conductor").setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


@main.command()
def version() -> None:
    """Print the current version and exit."""
    click.echo(f"tab-conductor {__version__}")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@main.command()
@click.argument("plan_path", metavar="<plan.yaml>", type=click.Path(exists=True))
@click.pass_context
def validate(ctx: click.Context, plan_path: str) -> None:
    """Validate a YAML plan file without running it."""
    path = Path(plan_path)
    try:
        parsed = parse_plan(path)
    except PlanParseError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        ctx.exit(1)
        return
    click.echo(f"OK: {len(parsed.tasks)} task(s) — name={parsed.metadata.get('name', '')!r}")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@main.command()
@click.argument("plan_path", metavar="<plan.yaml>", type=click.Path(exists=True))
@click.option("--mock", is_flag=True, default=False, help="Use mock worker subprocess.")
@click.option("--max-parallel", default=None, type=int, help="Override max_parallel from plan.")
@click.option(
    "--cap-usd-per-worker",
    default=1.0,
    show_default=True,
    help="Per-worker USD spending cap.",
)
@click.option(
    "--cap-usd-global",
    default=5.0,
    show_default=True,
    help="Global USD spending cap.",
)
@click.option(
    "--require-hmac",
    is_flag=True,
    default=False,
    help="Pass HMAC key to workers.",
)
@click.option(
    "--state-dir",
    default=None,
    type=click.Path(),
    help="Root directory for state files (default: <cwd>/.orchestrator).",
)
@click.pass_context
def run(
    ctx: click.Context,
    plan_path: str,
    mock: bool,
    max_parallel: int | None,
    cap_usd_per_worker: float,
    cap_usd_global: float,
    require_hmac: bool,
    state_dir: str | None,
) -> None:
    """Run a plan YAML file."""
    from tab_conductor.orchestrator import Orchestrator, OrchestratorConfig

    path = Path(plan_path)
    try:
        parsed = parse_plan(path)
    except PlanParseError as exc:
        click.echo(f"Plan parse error: {exc}", err=True)
        ctx.exit(1)
        return

    resolved_state_dir = Path(state_dir) if state_dir else Path.cwd() / ".orchestrator"
    parallel = max_parallel if max_parallel is not None else parsed.metadata.get("max_parallel", 4)

    # Locate mock worker script
    mock_worker_path: Path | None = None
    if mock:
        candidate = (
            Path(__file__).parent.parent.parent.parent / "tests" / "fixtures" / "mock_worker.sh"
        )
        if not candidate.exists():
            # Try relative to cwd
            candidate2 = Path.cwd() / "tests" / "fixtures" / "mock_worker.sh"
            if candidate2.exists():
                candidate = candidate2
        mock_worker_path = candidate

    config = OrchestratorConfig(
        state_dir=resolved_state_dir,
        max_parallel=parallel,
        cap_usd_per_worker=cap_usd_per_worker,
        cap_usd_global=cap_usd_global,
        require_hmac=require_hmac,
        mock_mode=mock,
        mock_worker_path=mock_worker_path,
    )

    orch = Orchestrator(config=config, tasks=parsed.tasks)
    exit_code = orch.run()
    ctx.exit(exit_code)


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


@main.command(name="ls")
@click.option(
    "--state-root",
    default=None,
    type=click.Path(),
    help="Root directory containing run state dirs (default: <cwd>/.orchestrator).",
)
@click.pass_context
def ls_cmd(ctx: click.Context, state_root: str | None) -> None:
    """List all runs under the state root directory."""
    root = Path(state_root) if state_root else Path.cwd() / ".orchestrator"
    if not root.exists():
        click.echo("no runs found")
        return

    runs = sorted([d for d in root.iterdir() if d.is_dir() and (d / "state.json").exists()])
    if not runs:
        click.echo("no runs found")
        return

    click.echo(f"{'RUN_ID':<30} {'STATUS':<15} {'STARTED_AT'}")
    for run_dir in runs:
        state_path = run_dir / "state.json"
        try:
            with state_path.open("r", encoding="utf-8") as fh:
                st: dict[str, Any] = json.load(fh)
            click.echo(
                f"{st.get('run_id', run_dir.name):<30} "
                f"{st.get('status', '?'):<15} "
                f"{st.get('started_at', '?')}"
            )
        except (OSError, json.JSONDecodeError):
            click.echo(f"{run_dir.name:<30} {'ERROR':<15} ?")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@main.command()
@click.argument("run_id")
@click.option(
    "--state-root",
    default=None,
    type=click.Path(),
    help="Root directory containing run state dirs.",
)
@click.pass_context
def show(ctx: click.Context, run_id: str, state_root: str | None) -> None:
    """Show the current state of a run."""
    root = Path(state_root) if state_root else Path.cwd() / ".orchestrator"
    state_path = root / run_id / "state.json"
    if not state_path.exists():
        click.echo(f"run '{run_id}' not found", err=True)
        ctx.exit(1)
        return

    try:
        with state_path.open("r", encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"Failed to read state: {exc}", err=True)
        ctx.exit(1)
        return

    click.echo(json.dumps(st, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


@main.command()
@click.argument("run_id")
@click.option(
    "--state-root",
    default=None,
    type=click.Path(),
    help="Root directory containing run state dirs.",
)
@click.option(
    "--exit-after",
    default=None,
    type=int,
    help="Exit after N refresh ticks (useful for testing).",
)
@click.pass_context
def watch(ctx: click.Context, run_id: str, state_root: str | None, exit_after: int | None) -> None:
    """Watch live progress of a run (refreshes every 1 second)."""
    root = Path(state_root) if state_root else Path.cwd() / ".orchestrator"
    state_path = root / run_id / "state.json"

    if not state_path.exists():
        click.echo(f"run '{run_id}' not found", err=True)
        ctx.exit(1)
        return

    try:
        from rich.live import Live
        from rich.table import Table

        def _build_table(st: dict[str, Any]) -> Table:
            tbl = Table(title=f"Run {run_id}")
            tbl.add_column("Field")
            tbl.add_column("Value")
            tbl.add_row("status", st.get("status", "?"))
            tbl.add_row("version", str(st.get("version", 0)))
            tbl.add_row("cost_usd_total", f"{st.get('cost_usd_total', 0.0):.4f}")
            tbl.add_row("workers", str(len(st.get("workers", []))))
            tbl.add_row("tasks", str(len(st.get("tasks", []))))
            return tbl

        tick = 0
        with Live(refresh_per_second=2) as live:
            while True:
                try:
                    if state_path.exists():
                        with state_path.open("r", encoding="utf-8") as fh:
                            st = json.load(fh)
                        live.update(_build_table(st))
                except (OSError, json.JSONDecodeError):
                    pass

                tick += 1
                if exit_after is not None and tick >= exit_after:
                    break

                time.sleep(1)

    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# kill
# ---------------------------------------------------------------------------


@main.command()
@click.argument("run_id")
@click.option(
    "--state-root",
    default=None,
    type=click.Path(),
    help="Root directory containing run state dirs.",
)
@click.pass_context
def kill(ctx: click.Context, run_id: str, state_root: str | None) -> None:
    """Kill (SIGTERM) all workers of a run."""
    root = Path(state_root) if state_root else Path.cwd() / ".orchestrator"
    state_path = root / run_id / "state.json"

    if not state_path.exists():
        click.echo(f"run '{run_id}' not found", err=True)
        ctx.exit(1)
        return

    try:
        with state_path.open("r", encoding="utf-8") as fh:
            st: dict[str, Any] = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"Failed to read state: {exc}", err=True)
        ctx.exit(1)
        return

    workers: list[dict[str, Any]] = st.get("workers", [])
    killed = 0
    for w in workers:
        pid = w.get("pid")
        if pid is None:
            continue
        try:
            pgid = os.getpgid(int(pid))
            os.killpg(pgid, signal.SIGTERM)
            killed += 1
        except (ProcessLookupError, OSError):
            pass

    if killed == 0:
        click.echo(f"No live workers found; marking run '{run_id}' as halting.")
        # Best-effort state mark
        try:
            st["status"] = "halting"
            tmp_fd, tmp_path_str = tempfile.mkstemp(dir=state_path.parent, suffix=".json")
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(st, fh)
            os.replace(tmp_path_str, str(state_path))
        except OSError:
            pass
    else:
        click.echo(f"Sent SIGTERM to {killed} worker(s) of run '{run_id}'.")


# ---------------------------------------------------------------------------
# bugreport
# ---------------------------------------------------------------------------


@main.command()
@click.argument("run_id")
@click.option(
    "-o",
    "--output",
    default=None,
    type=click.Path(),
    help="Output path for the .tar.gz archive (default: <run_id>.tar.gz in cwd).",
)
@click.option(
    "--state-root",
    default=None,
    type=click.Path(),
    help="Root directory containing run state dirs.",
)
@click.pass_context
def bugreport(ctx: click.Context, run_id: str, output: str | None, state_root: str | None) -> None:
    """Generate a tar.gz bug report for a run, with secrets redacted."""
    root = Path(state_root) if state_root else Path.cwd() / ".orchestrator"
    run_dir = root / run_id

    if not run_dir.exists():
        click.echo(f"run '{run_id}' not found", err=True)
        ctx.exit(1)
        return

    out_path = Path(output) if output else Path.cwd() / f"{run_id}.tar.gz"

    with tarfile.open(str(out_path), "w:gz") as tar:
        for file_path in sorted(run_dir.rglob("*")):
            if not file_path.is_file():
                continue
            arcname = str(file_path.relative_to(root))
            suffix = file_path.suffix.lower()
            if suffix in _TEXT_EXTS:
                try:
                    text = file_path.read_text(encoding="utf-8", errors="replace")
                    redacted = redact_text(text)
                    encoded = redacted.encode("utf-8")
                    import io

                    info = tarfile.TarInfo(name=arcname)
                    info.size = len(encoded)
                    tar.addfile(info, io.BytesIO(encoded))
                except OSError:
                    tar.add(str(file_path), arcname=arcname)
            else:
                tar.add(str(file_path), arcname=arcname)

    click.echo(f"Bug report written to {out_path}")

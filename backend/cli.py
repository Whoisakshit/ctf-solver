"""Click CLI entry point."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console

from backend.config import Settings
from backend.models import default_models

console = Console()


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiodocker").setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", datefmt="%X"))
    logging.basicConfig(level=level, handlers=[handler], force=True)


@click.command()
@click.option("--ctfd-url", default=None, help="CTFd URL (overrides .env)")
@click.option("--ctfd-token", default=None, help="CTFd API token (overrides .env)")
@click.option("--image", default="ctf-sandbox", help="Docker sandbox image name")
@click.option("--models", multiple=True,
              help="Model specs (default: free-tier Gemini models — see backend/models.py for paid alternatives)")
@click.option("--gemini-tier", default=None, type=click.Choice(["free", "pro"]),
              help="Gemini model lineup — 'free' (default) or 'pro' (needs billing on your Google AI Studio project). Overrides GEMINI_TIER in .env.")
@click.option("--challenge", default=None, help="Solve a single challenge directory")
@click.option("--screenshot", "screenshot", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Solve a challenge from just a screenshot — no CTFd needed")
@click.option("--url", "target_url", default=None,
              help="Solve a challenge from just a URL — no CTFd needed (combine with --screenshot if you have both)")
@click.option("--file", "extra_files", multiple=True, type=click.Path(exists=True, dir_okay=False),
              help="Attach a downloaded challenge file (binary, pcap, zip, etc.) — repeatable, used with --screenshot/--url")
@click.option("--challenges-dir", default="challenges", help="Directory for challenge files")
@click.option("--no-submit", is_flag=True, help="Dry run — don't submit flags")
@click.option("--coordinator-model", default=None, help="Model for coordinator (default depends on --coordinator)")
@click.option("--coordinator", default="gemini", type=click.Choice(["gemini", "claude", "codex"]),
              help="Coordinator backend — 'gemini' needs only GEMINI_API_KEY, 'claude'/'codex' need their CLIs")
@click.option("--max-challenges", default=3, type=int,
              help="Max challenges solved concurrently (kept low by default — free-tier Gemini has tight RPM/RPD quotas)")
@click.option("--msg-port", default=0, type=int, help="Operator message port (0 = auto)")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging")
def main(
    ctfd_url: str | None,
    ctfd_token: str | None,
    image: str,
    models: tuple[str, ...],
    gemini_tier: str | None,
    challenge: str | None,
    screenshot: str | None,
    target_url: str | None,
    extra_files: tuple[str, ...],
    challenges_dir: str,
    no_submit: bool,
    coordinator_model: str | None,
    coordinator: str,
    max_challenges: int,
    msg_port: int,
    verbose: bool,
) -> None:
    """CTF Agent — multi-model solver swarm.

    Run without --challenge to start the full coordinator (Ctrl+C to stop).
    """
    _setup_logging(verbose)

    settings = Settings(sandbox_image=image)
    if ctfd_url:
        settings.ctfd_url = ctfd_url
    if ctfd_token:
        settings.ctfd_token = ctfd_token
    if gemini_tier:
        settings.gemini_tier = gemini_tier
    settings.max_concurrent_challenges = max_challenges

    model_specs = list(models) if models else default_models(settings)

    console.print("[bold]CTF Agent v2[/bold]")
    console.print(f"  CTFd: {settings.ctfd_url}")
    console.print(f"  Models: {', '.join(model_specs)}")
    console.print(f"  Image: {settings.sandbox_image}")
    console.print(f"  Max challenges: {max_challenges}")
    console.print()

    if screenshot or target_url or extra_files:
        asyncio.run(
            _run_quickstart(
                settings, screenshot, target_url, list(extra_files), model_specs, max_challenges, challenges_dir
            )
        )
    elif challenge:
        asyncio.run(_run_single(settings, challenge, model_specs, no_submit, max_challenges))
    else:
        asyncio.run(_run_coordinator(settings, model_specs, challenges_dir, no_submit, coordinator_model, coordinator, max_challenges, msg_port))


async def _run_single(
    settings: Settings,
    challenge_dir: str,
    model_specs: list[str],
    no_submit: bool,
    max_challenges: int,
) -> None:
    """Run a single challenge with a swarm."""
    from backend.agents.swarm import ChallengeSwarm
    from backend.cost_tracker import CostTracker
    from backend.ctfd import CTFdClient
    from backend.prompts import ChallengeMeta
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    max_containers = max_challenges * len(model_specs)
    configure_semaphore(max_containers)
    await cleanup_orphan_containers()

    challenge_path = Path(challenge_dir)
    meta_path = challenge_path / "metadata.yml"
    if not meta_path.exists():
        console.print(f"[red]No metadata.yml found in {challenge_dir}[/red]")
        sys.exit(1)

    meta = ChallengeMeta.from_yaml(meta_path)
    console.print(f"[bold]Challenge:[/bold] {meta.name} ({meta.category}, {meta.value} pts)")

    ctfd = CTFdClient(
        base_url=settings.ctfd_url,
        token=settings.ctfd_token,
        username=settings.ctfd_user,
        password=settings.ctfd_pass,
    )
    cost_tracker = CostTracker()

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_path),
        meta=meta,
        ctfd=ctfd,
        cost_tracker=cost_tracker,
        settings=settings,
        model_specs=model_specs,
        no_submit=no_submit,
    )

    try:
        result = await swarm.run()
        from backend.solver_base import FLAG_FOUND
        if result and result.status == FLAG_FOUND:
            console.print(f"\n[bold green]FLAG FOUND:[/bold green] {result.flag}")
        else:
            console.print("\n[bold red]No flag found.[/bold red]")

        console.print("\n[bold]Cost Summary:[/bold]")
        for agent_name in cost_tracker.by_agent:
            console.print(f"  {agent_name}: {cost_tracker.format_usage(agent_name)}")
        console.print(f"  [bold]Total: ${cost_tracker.total_cost_usd:.2f}[/bold]")
    finally:
        await ctfd.close()


async def _run_quickstart(
    settings: Settings,
    screenshot: str | None,
    target_url: str | None,
    extra_files: list[str],
    model_specs: list[str],
    max_challenges: int,
    challenges_dir: str,
) -> None:
    """Solve a single challenge from any combination of screenshot / file(s) / URL — no CTFd needed."""
    import shutil

    from backend.agents.swarm import ChallengeSwarm
    from backend.cost_tracker import CostTracker
    from backend.ctfd import CTFdClient
    from backend.prompts import ChallengeMeta
    from backend.quickstart import challenge_from_files, challenge_from_screenshot, challenge_from_url
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    if not settings.gemini_api_key:
        console.print(
            "[red]GEMINI_API_KEY is required for --screenshot/--url/--file extraction.[/red]\n"
            "Get a free key at https://aistudio.google.com/apikey and put it in .env."
        )
        sys.exit(1)

    max_containers = max_challenges * len(model_specs)
    configure_semaphore(max_containers)
    await cleanup_orphan_containers()

    console.print("[bold]Quickstart:[/bold] extracting challenge metadata with Gemini...")
    try:
        # Screenshot is the richest source of truth (has description text), so prefer it
        # when present; URL is next-best; files-only is the fallback with no description.
        if screenshot:
            challenge_path = await challenge_from_screenshot(
                screenshot, settings, challenges_dir, extra_url=target_url
            )
        elif target_url:
            challenge_path = await challenge_from_url(target_url, settings, challenges_dir)
        else:
            challenge_path = await challenge_from_files(
                extra_files, settings, challenges_dir, extra_url=target_url
            )
    except Exception as e:
        console.print(f"[red]Extraction failed:[/red] {e}")
        sys.exit(1)

    # Files get attached regardless of which branch built the challenge dir above
    # (challenge_from_files already attaches them itself, so skip double-copying).
    if extra_files and not (screenshot is None and target_url is None):
        dist_dir = Path(challenge_path) / "distfiles"
        dist_dir.mkdir(exist_ok=True)
        for f in extra_files:
            dest = dist_dir / Path(f).name
            shutil.copy2(f, dest)
            console.print(f"[dim]Attached {f} -> {dest}[/dim]")

    meta = ChallengeMeta.from_yaml(Path(challenge_path) / "metadata.yml")
    console.print(f"[bold]Challenge:[/bold] {meta.name} ({meta.category or 'unknown'})")
    console.print(f"[dim]Saved to {challenge_path}/metadata.yml[/dim]\n")

    # No CTFd instance backs a quickstart challenge — the solver reports the flag
    # directly instead of submitting it for scoring.
    ctfd = CTFdClient(
        base_url=settings.ctfd_url,
        token=settings.ctfd_token,
        username=settings.ctfd_user,
        password=settings.ctfd_pass,
    )
    cost_tracker = CostTracker()

    swarm = ChallengeSwarm(
        challenge_dir=challenge_path,
        meta=meta,
        ctfd=ctfd,
        cost_tracker=cost_tracker,
        settings=settings,
        model_specs=model_specs,
        no_submit=True,
    )

    try:
        result = await swarm.run()
        from backend.solver_base import FLAG_FOUND

        if result and result.status == FLAG_FOUND and result.flag:
            flag_file = Path(challenge_path) / "flag.txt"
            flag_file.write_text(result.flag + "\n", encoding="utf-8")
            console.print()
            console.print("=" * 60, style="bold green")
            console.print(f"  FLAG: {result.flag}", style="bold green")
            console.print("=" * 60, style="bold green")
            console.print(f"[dim]Also saved to {flag_file}[/dim]")
        else:
            console.print()
            console.print("=" * 60, style="bold red")
            console.print("  NO FLAG FOUND", style="bold red")
            console.print("=" * 60, style="bold red")
            console.print(
                f"[dim]Check {challenge_path}/ for solver traces, or try again — "
                "free-tier rate limits can cause a solver to bail early.[/dim]"
            )

        console.print("\n[bold]Cost Summary:[/bold]")
        for agent_name in cost_tracker.by_agent:
            console.print(f"  {agent_name}: {cost_tracker.format_usage(agent_name)}")
        console.print(f"  [bold]Total: ${cost_tracker.total_cost_usd:.2f}[/bold]")
    finally:
        await ctfd.close()


async def _run_coordinator(
    settings: Settings,
    model_specs: list[str],
    challenges_dir: str,
    no_submit: bool,
    coordinator_model: str | None,
    coordinator_backend: str,
    max_challenges: int,
    msg_port: int = 0,
) -> None:
    """Run the full coordinator (continuous until Ctrl+C)."""
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore

    max_containers = max_challenges * len(model_specs)
    configure_semaphore(max_containers)
    await cleanup_orphan_containers()
    console.print(f"[bold]Starting coordinator ({coordinator_backend}, Ctrl+C to stop)...[/bold]\n")

    if coordinator_backend == "codex":
        from backend.agents.codex_coordinator import run_codex_coordinator
        results = await run_codex_coordinator(
            settings=settings,
            model_specs=model_specs,
            challenges_root=challenges_dir,
            no_submit=no_submit,
            coordinator_model=coordinator_model,
            msg_port=msg_port,
        )
    elif coordinator_backend == "claude":
        from backend.agents.claude_coordinator import run_claude_coordinator
        results = await run_claude_coordinator(
            settings=settings,
            model_specs=model_specs,
            challenges_root=challenges_dir,
            no_submit=no_submit,
            coordinator_model=coordinator_model,
            msg_port=msg_port,
        )
    else:
        from backend.agents.gemini_coordinator import run_gemini_coordinator
        results = await run_gemini_coordinator(
            settings=settings,
            model_specs=model_specs,
            challenges_root=challenges_dir,
            no_submit=no_submit,
            coordinator_model=coordinator_model,
            msg_port=msg_port,
        )

    console.print("\n[bold]Final Results:[/bold]")
    for challenge, data in results.get("results", {}).items():
        console.print(f"  {challenge}: {data.get('flag', 'no flag')}")
    console.print(f"\n[bold]Total cost: ${results.get('total_cost_usd', 0):.2f}[/bold]")


@click.command()
@click.argument("message")
@click.option("--port", default=9400, type=int, help="Coordinator message port")
@click.option("--host", default="127.0.0.1", help="Coordinator host")
def msg(message: str, port: int, host: str) -> None:
    """Send a message to the running coordinator."""
    import json
    import urllib.request

    body = json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/msg",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            console.print(f"[green]Sent:[/green] {data.get('queued', message[:200])}")
    except Exception as e:
        console.print(f"[red]Failed:[/red] {e}")
        console.print("Is the coordinator running?")
        sys.exit(1)


if __name__ == "__main__":
    main()

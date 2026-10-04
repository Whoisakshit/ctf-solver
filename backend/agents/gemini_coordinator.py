"""Gemini (Pydantic AI) coordinator — free-tier friendly, no external CLI required.

Unlike the Claude SDK / Codex coordinators (which shell out to the `claude`/`codex`
CLIs and need their own subscriptions), this coordinator is a plain Pydantic AI
`Agent` running on `GoogleModel`, so it only needs `GEMINI_API_KEY`. It shares the
same tool logic (`coordinator_core`) and event loop (`coordinator_loop`) as the other
two backends — only the "how do I get an LLM to make a tool call" part differs.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage

from backend.agents.coordinator_core import (
    do_broadcast,
    do_bump_agent,
    do_check_swarm_status,
    do_fetch_challenges,
    do_get_solve_status,
    do_kill_swarm,
    do_read_solver_trace,
    do_spawn_swarm,
    do_submit_flag,
)
from backend.agents.coordinator_loop import build_deps, run_event_loop
from backend.config import Settings
from backend.deps import CoordinatorDeps
from backend.models import FREE_TIER_MODELS, model_id_from_spec, resolve_model, resolve_model_settings

logger = logging.getLogger(__name__)

COORDINATOR_PROMPT = """\
You are a CTF competition coordinator running for the ENTIRE duration of a live competition.
Your job is to maximize the number of challenges solved.

Strategy:
- Spawn swarms for unsolved challenges, prioritizing by solve count (easy first)
- Use read_solver_trace to monitor what each solver is doing and where it's stuck
- When agents are stuck, read their traces, then craft targeted bumps with specific technical guidance
- Use broadcast to share cross-solver insights (e.g. flag format discovery, shared vulnerabilities)

CRITICAL RULES:
- NEVER kill a swarm yourself unless told to. Solvers will keep trying indefinitely with
  different approaches. Even when stuck, they often unstick themselves after several bumps.
  Your job is to HELP them, not give up on them. The only time a swarm should die is when
  the flag is confirmed correct (this happens automatically).
- When a solver seems stuck, bump it with very specific technical guidance based on
  its trace. Tell it exactly what to try next — specific tools, techniques, approaches.
- You are running on a free-tier API key with tight rate limits. Be economical: only call
  a tool when it changes what you'd do next. Don't re-fetch challenges or statuses back to
  back with no new information.

You will receive event messages. Respond with tool calls to manage the competition, then a
short plain-text summary of what you did.
"""

# Cap on retained message history to stay within free-tier token/rate limits.
_MAX_HISTORY_MESSAGES = 60


def _build_coordinator_agent(model: Any, model_settings: Any) -> Agent[CoordinatorDeps, str]:
    """Build the coordinator agent — tools are thin wrappers around coordinator_core."""
    agent: Agent[CoordinatorDeps, str] = Agent(
        model,
        deps_type=CoordinatorDeps,
        system_prompt=COORDINATOR_PROMPT,
        model_settings=model_settings,
    )

    @agent.tool
    async def fetch_challenges(ctx: RunContext[CoordinatorDeps]) -> str:
        """List all challenges with category, points, solve count, and status."""
        return await do_fetch_challenges(ctx.deps)

    @agent.tool
    async def get_solve_status(ctx: RunContext[CoordinatorDeps]) -> str:
        """Check which challenges are solved and which swarms are running."""
        return await do_get_solve_status(ctx.deps)

    @agent.tool
    async def spawn_swarm(ctx: RunContext[CoordinatorDeps], challenge_name: str) -> str:
        """Launch all solver models on a challenge."""
        return await do_spawn_swarm(ctx.deps, challenge_name)

    @agent.tool
    async def check_swarm_status(ctx: RunContext[CoordinatorDeps], challenge_name: str) -> str:
        """Get per-agent progress for a swarm."""
        return await do_check_swarm_status(ctx.deps, challenge_name)

    @agent.tool
    async def submit_flag(ctx: RunContext[CoordinatorDeps], challenge_name: str, flag: str) -> str:
        """Submit a flag to CTFd."""
        return await do_submit_flag(ctx.deps, challenge_name, flag)

    @agent.tool
    async def kill_swarm(ctx: RunContext[CoordinatorDeps], challenge_name: str) -> str:
        """Cancel all agents for a challenge."""
        return await do_kill_swarm(ctx.deps, challenge_name)

    @agent.tool
    async def bump_agent(
        ctx: RunContext[CoordinatorDeps], challenge_name: str, model_spec: str, insights: str
    ) -> str:
        """Send targeted insights to a stuck agent."""
        return await do_bump_agent(ctx.deps, challenge_name, model_spec, insights)

    @agent.tool
    async def broadcast(ctx: RunContext[CoordinatorDeps], challenge_name: str, message: str) -> str:
        """Broadcast a strategic hint to ALL solvers on a challenge."""
        return await do_broadcast(ctx.deps, challenge_name, message)

    @agent.tool
    async def read_solver_trace(
        ctx: RunContext[CoordinatorDeps], challenge_name: str, model_spec: str, last_n: int = 20
    ) -> str:
        """Read recent trace events from a specific solver — what it tried and where it's stuck."""
        return await do_read_solver_trace(ctx.deps, challenge_name, model_spec, last_n)

    return agent


async def run_gemini_coordinator(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    no_submit: bool = False,
    coordinator_model: str | None = None,
    msg_port: int = 0,
) -> dict[str, Any]:
    """Run the Pydantic AI / Gemini coordinator with the shared event loop."""
    ctfd, cost_tracker, deps = build_deps(settings, model_specs, challenges_root, no_submit)
    deps.msg_port = msg_port

    spec = coordinator_model or FREE_TIER_MODELS[0]
    if "/" not in spec:
        spec = f"google/{spec}"

    model = resolve_model(spec, settings)
    model_settings = resolve_model_settings(spec)
    agent = _build_coordinator_agent(model, model_settings)

    history: list[ModelMessage] = []

    async def turn_fn(msg: str) -> None:
        nonlocal history
        from backend.ratelimit import with_rate_limit_retry

        logger.debug(f"Coordinator query: {msg[:200]}")

        async def _do_run():
            return await agent.run(msg, deps=deps, message_history=history or None)

        result = await with_rate_limit_retry(_do_run, model_id=model_id_from_spec(spec))
        history = result.all_messages()
        if len(history) > _MAX_HISTORY_MESSAGES:
            history = history[-_MAX_HISTORY_MESSAGES:]
        logger.info(f"Gemini coordinator turn done: {str(result.output)[:200]}")

    return await run_event_loop(deps, ctfd, cost_tracker, turn_fn)

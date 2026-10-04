"""Rate limiting for Gemini API calls.

Two layers, since free-tier quotas are tight enough that reacting to 429s alone wastes
real time:

1. **Proactive pacing** (`RateLimiter`) — a real sliding-window RPM/TPM tracker (see
   `backend.models.FREE_TIER_RPM`/`FREE_TIER_TPM`), so calls space themselves out and
   rarely hit a 429 in the first place. Unknown models get a conservative default.

2. **Reactive backoff** (`with_rate_limit_retry`) — a safety net for when a 429 happens
   anyway (shared quota across multiple agent instances, a burst, etc). Parses Google's
   own suggested `retryDelay` out of the error body when present, falls back to
   `Retry-After` header, then to exponential backoff.

Both are applied per real outbound API request via `PacedModel`, NOT once per solver
"turn" — a single `agent.run()` call can make many real model requests internally (one
per tool-calling round trip, since `backend/agents/solver.py` runs with
`UsageLimits(request_limit=None)` so the model can use tools freely within a turn).
Pacing only the outer call leaves every request after the first in a turn unpaced,
which is how production traffic still hit 19 RPM against a 15 RPM cap even with the
outer-only pacing in place. Wrapping the `Model` itself is the fix: it intercepts every
individual request, wherever it originates in the call graph.

The TPM side of that pacer also has to track *real* token counts, not a fixed guess: a
CTF solve's conversation history only grows, so a call late in a long solve carries far
more tokens than a call early on. A pacer that assumes every call is the same size
either paces too slowly early on (wasting quota) or — the failure actually seen in
production — too loosely late in a run, since a single fixed estimate can't track a
number that keeps climbing. `RateLimiter` below tracks a real 60s sliding window of
(timestamp, token_count) pairs per model and corrects each entry with the model's own
reported usage as soon as a response comes back (`record_usage`), so its estimate for
the *next* call is always anchored to the *last real* call, not a guess made once at
import time.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from typing import Awaitable, Callable, TypeVar

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Conservative fallback for any model not in backend.models.FREE_TIER_RPM (e.g. paid
# models, or a new free model Google ships before this table is updated).
_DEFAULT_RPM = 10
# Used only for a model_id's very first call, before any real usage has been observed.
_DEFAULT_EST_TOKENS_PER_CALL = 20_000
# Effective caps are held to this fraction of the published RPM/TPM limit. Slack for:
# other processes sharing the same API key (a second `quickstart`/`cli` invocation has
# its own in-memory window and doesn't know about this one's usage), Google counting
# the window slightly differently than a client-side clock can, and the fact that the
# token estimate for a call not yet made is still an estimate even when anchored to
# the last real one.
_SAFETY_MARGIN = 0.8


class RateLimiter:
    """Per-model-id real sliding-window pacing so concurrent solvers don't blow a
    shared RPM/TPM quota.

    Tracks actual (timestamp, token_count) pairs for calls in the last 60 seconds, per
    model_id, shared across every caller in the process (all solvers on all challenges
    draw on the same underlying Gemini quota). A call is allowed once both the request
    count and the token sum in that live window — after adding this call's estimated
    size — fit under `_SAFETY_MARGIN` of the published caps; otherwise it waits until
    the oldest entry ages out and re-checks.

    The token estimate for a call not yet made is anchored to the *last real* usage
    seen for that model_id (via `record_usage`), not a fixed guess — a solver's
    conversation only grows over a run, so "about like last time, plus some growth
    margin" tracks reality far better than a constant chosen once.
    """

    def __init__(
        self,
        rpm_by_model: dict[str, int] | None = None,
        tpm_by_model: dict[str, int] | None = None,
        default_est_tokens: int = _DEFAULT_EST_TOKENS_PER_CALL,
        safety_margin: float = _SAFETY_MARGIN,
    ) -> None:
        self._rpm = dict(rpm_by_model or {})
        self._tpm = dict(tpm_by_model or {})
        self._default_est = default_est_tokens
        self._margin = safety_margin
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Sliding window of (monotonic timestamp, token_count) for calls in the last
        # 60s, per model_id. A slot is appended (with an estimate) at wait_turn() time
        # and corrected in place by record_usage() once the real count is known.
        self._window: dict[str, deque[list]] = defaultdict(deque)
        # Last real (corrected) token count seen per model_id — the anchor for the
        # next call's estimate.
        self._last_tokens: dict[str, int] = {}

    def _prune(self, model_id: str, now: float) -> None:
        window = self._window[model_id]
        while window and now - window[0][0] > 60.0:
            window.popleft()

    def _estimated_next_tokens(self, model_id: str) -> int:
        last = self._last_tokens.get(model_id)
        if last is None:
            return self._default_est
        # Grow past the last real call — conversation history only gets longer
        # between consecutive calls to the same solver, never shorter.
        return int(last * 1.25)

    async def wait_turn(self, model_id: str) -> None:
        """Block until it's this model_id's turn to make a call, then reserve a slot.

        Returns only once a slot has been reserved in the sliding window — callers
        should follow up with `record_usage()` once the real response comes back so
        the reservation gets corrected to the real token count.
        """
        rpm_cap = max(int(self._rpm.get(model_id, _DEFAULT_RPM) * self._margin), 1)
        tpm_cap_raw = self._tpm.get(model_id)
        tpm_cap = int(tpm_cap_raw * self._margin) if tpm_cap_raw else None

        async with self._locks[model_id]:
            while True:
                now = time.monotonic()
                self._prune(model_id, now)
                window = self._window[model_id]
                next_tokens = self._estimated_next_tokens(model_id)
                used_tokens = sum(t for _, t in window)

                rpm_ok = len(window) < rpm_cap
                tpm_ok = tpm_cap is None or (used_tokens + next_tokens) <= tpm_cap
                if rpm_ok and tpm_ok:
                    window.append([now, next_tokens])
                    return

                # Wait until the oldest entry in the window ages past 60s, then retry.
                wait = 60.0 - (now - window[0][0]) if window else 1.0
                wait = max(wait, 0.25)
                logger.debug(
                    f"Rate limiter: pacing {model_id} (rpm_ok={rpm_ok}, tpm_ok={tpm_ok}, "
                    f"window={len(window)}/{rpm_cap} reqs, {used_tokens}+{next_tokens}/{tpm_cap} "
                    f"tokens), waiting {wait:.1f}s"
                )
                await asyncio.sleep(wait)

    def record_usage(self, model_id: str, input_tokens: int) -> None:
        """Correct the most recent reservation for model_id with its real token count.

        Called once a response comes back, so the window's running token sum — and the
        estimate used for this model_id's *next* call — reflect reality instead of the
        estimate made before the call went out.
        """
        if input_tokens <= 0:
            return
        self._last_tokens[model_id] = input_tokens
        window = self._window[model_id]
        if window:
            window[-1][1] = input_tokens


# One shared limiter for the whole process — every solver/coordinator/quickstart call
# against the same Gemini project draws from the same real quota, so they need to
# share the same pacing clock, not one each.
_shared_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _shared_limiter
    if _shared_limiter is None:
        from backend.models import FREE_TIER_RPM, FREE_TIER_TPM

        _shared_limiter = RateLimiter(FREE_TIER_RPM, FREE_TIER_TPM)
    return _shared_limiter


def _parse_retry_delay_from_body(body: object) -> float | None:
    """Extract Google's suggested retryDelay (e.g. '56s') from a 429 error body.

    The real shape from google-genai's APIError.details is a flat dict —
    {'code', 'message', 'status', 'details': [...]} — NOT nested under an 'error'
    key. Some proxies/providers do nest it, so both shapes are handled.
    """
    if not isinstance(body, dict):
        return None
    for candidate in (body, body.get("error") if isinstance(body.get("error"), dict) else {}):
        details = candidate.get("details", [])
        if not isinstance(details, list):
            continue
        for d in details:
            if not isinstance(d, dict):
                continue
            if str(d.get("@type", "")).endswith("RetryInfo") and "retryDelay" in d:
                m = re.match(r"([\d.]+)s?", str(d["retryDelay"]))
                if m:
                    return float(m.group(1))
    return None


async def with_rate_limit_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    model_id: str = "",
    max_retries: int = 8,
    base_delay: float = 2.0,
    max_delay: float = 150.0,
) -> T:
    """Call `fn()`, retrying on 429/503 using the server's suggested delay when given.

    `fn` should be a zero-arg callable (e.g. a lambda wrapping `agent.run(...)`) so it
    can be re-invoked on retry. Only retries transient/rate-limit errors — a 400/404
    (bad request, unknown model) is raised immediately since retrying won't help.

    Each retry re-reserves a fresh sliding-window slot via `wait_turn` — a retried call
    is still a real outbound request and needs to be paced/counted like any other, not
    just the first attempt.
    """
    from pydantic_ai.exceptions import ModelHTTPError

    for attempt in range(max_retries + 1):
        if model_id:
            await get_rate_limiter().wait_turn(model_id)
        try:
            return await fn()
        except ModelHTTPError as e:
            if e.status_code not in (429, 503) or attempt == max_retries:
                raise

            delay = e.retry_after
            if delay is None:
                delay = _parse_retry_delay_from_body(e.body)
            if delay is None:
                delay = min(base_delay * (2**attempt), max_delay)
            else:
                # Server said an exact number — trust it, plus a small buffer.
                delay = min(delay + 1.0, max_delay * 2)

            logger.warning(
                f"Rate limited (status {e.status_code}) on {model_id or 'model'}, "
                f"retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})"
            )
            await asyncio.sleep(delay)

    raise RuntimeError("unreachable")  # pragma: no cover


class PacedModel(WrapperModel):
    """Wraps a `Model` so EVERY real request to it goes through pacing + 429 retry.

    `WrapperModel` forwards everything (profile, settings, system, streaming, etc.) to
    the wrapped model unchanged — the only method overridden here is `request()`, the
    one place an actual HTTP call to the provider is made. That's what makes this the
    right place to enforce the quota: no matter how many times the agent graph calls
    `model.request()` inside a single `agent.run()` (one per tool round trip, since
    solvers run with `UsageLimits(request_limit=None)`), each call is individually
    paced and retried against the real per-model RPM/TPM quota — instead of only the
    first call of each solver "turn", which is what let bursts through before.

    Also feeds each response's real `input_tokens` back into the shared `RateLimiter`
    (`record_usage`) so the *next* call's pacing is based on this conversation's actual
    token growth instead of a fixed guess.
    """

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        response = await with_rate_limit_retry(
            lambda: self.wrapped.request(messages, model_settings, model_request_parameters),
            model_id=self.model_name,
        )
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None) if usage else None
        if input_tokens:
            get_rate_limiter().record_usage(self.model_name, input_tokens)
        return response


def paced(model: Model) -> Model:
    """Wrap `model` so every real request it makes is paced/retried against its quota.

    Use this on whatever `resolve_model()` returns before handing it to an `Agent` —
    wrapping happens once per solver, and every request that solver's agent makes
    (including internal tool-calling round trips within one `agent.run()`) is covered.
    """
    return PacedModel(model)

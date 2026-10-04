"""Model resolution — Bedrock, Azure OpenAI, Zen, Google AI Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from pydantic_ai.models import Model
from pydantic_ai.models.bedrock import BedrockConverseModel, BedrockModelSettings
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings

try:
    # pydantic-ai >= ~1.10 renamed these; keep both names working so this doesn't
    # break again on the next `uv sync` pulling a newer/older release.
    from pydantic_ai.models.openai import OpenAIChatModel as OpenAIModel
    from pydantic_ai.models.openai import OpenAIChatModelSettings as OpenAIModelSettings
except ImportError:
    from pydantic_ai.models.openai import OpenAIModel, OpenAIModelSettings

from pydantic_ai.providers.bedrock import BedrockProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

if TYPE_CHECKING:
    from backend.config import Settings

# Free-tier Gemini models (Google AI Studio) — the only paid-key-free option, so this
# is the default lineup. "google/..." specs route through Pydantic AI's generic
# GoogleModel and only need GEMINI_API_KEY.
#
# Real observed free-tier quotas (Google AI Studio usage dashboard, Aug 2026):
#   gemini-3-flash-preview:  5 RPM  /  20 RPD   <- unusable for a multi-turn agent,
#                                                   burns its whole daily quota in a
#                                                   couple of turns on ONE challenge
#   gemini-3.1-flash-lite:  15 RPM  / 500 RPD   <- the actually-usable free model
#
# So the default is a single model, not a race — racing gemini-3-flash-preview
# alongside it just wastes its 20/day quota and then 429s for the rest of the run.
FREE_TIER_MODELS: list[str] = [
    "google/gemini-3.1-flash-lite",
]

# Real per-model free-tier rate limits (requests per minute / requests per day),
# from the Google AI Studio usage dashboard. Used by backend.ratelimit to pace
# requests proactively instead of just reacting to 429s after the fact. Unknown
# models fall back to a conservative default (see backend/ratelimit.py).
#
# NOTE (Aug 2026 dashboard check): the RPM ceiling alone isn't the binding
# constraint for gemini-3.1-flash-lite — RPM sat right at 15/15 while TPM (input
# tokens/min) was already 299.5K against a 250K cap, which is what was actually
# triggering the 429s. RPM is kept at the real published value; FREE_TIER_TPM
# below (used together with FREE_TIER_RPM in backend.ratelimit) is what actually
# slows the pacer enough to stay under the token cap.
FREE_TIER_RPM: dict[str, int] = {
    "gemini-3-flash-preview": 5,
    "gemini-3.1-flash-lite": 15,
}
FREE_TIER_RPD: dict[str, int] = {
    "gemini-3-flash-preview": 20,
    "gemini-3.1-flash-lite": 500,
}
# Input tokens per minute cap, from the same dashboard. A long-running CTF solve
# accumulates conversation history, so later turns can be tens of thousands of
# tokens each — enough to blow TPM well before RPM is hit.
FREE_TIER_TPM: dict[str, int] = {
    "gemini-3-flash-preview": 250_000,
    "gemini-3.1-flash-lite": 250_000,
}

# Paid Gemini tier (requires billing on the Google AI Studio project) — much higher
# rate limits and access to the Pro reasoning model. gemini-3.1-pro-preview is the
# current flagship reasoning model; gemini-3.7-flash is a fast/cheap agentic racer
# that runs alongside it. Confirmed real, current model IDs as of Aug 2026 — check
# https://ai.google.dev/gemini-api/docs/models if either ever 404s.
PAID_GEMINI_MODELS: list[str] = [
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.7-flash",
]

# Paid/subscription lineup kept for reference — pass --models explicitly to use these,
# or set them as DEFAULT_MODELS yourself if you have the relevant API keys/CLIs.
PAID_MODELS: list[str] = [
    "claude-sdk/claude-opus-4-6/medium",
    "claude-sdk/claude-opus-4-6/max",
    "codex/gpt-5.4",
    "codex/gpt-5.4-mini",
    "codex/gpt-5.3-codex",
]

# Default model specs — free-tier Gemini by default; use default_models(settings) to
# respect GEMINI_TIER / --gemini-tier at runtime instead of this static fallback.
DEFAULT_MODELS: list[str] = list(FREE_TIER_MODELS)


def default_models(settings: Settings | None = None) -> list[str]:
    """Free vs paid Gemini model lineup, based on settings.gemini_tier ("free"/"pro")."""
    tier = getattr(settings, "gemini_tier", "free") if settings is not None else "free"
    if tier.strip().lower() in ("pro", "paid"):
        return list(PAID_GEMINI_MODELS)
    return list(FREE_TIER_MODELS)


# Context window sizes (tokens)
CONTEXT_WINDOWS: dict[str, int] = {
    "us.anthropic.claude-opus-4-6-v1": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "gpt-5.4": 1_000_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.3-codex": 1_000_000,
    "gpt-5.3-codex-spark": 128_000,
    "gemini-3-flash-preview": 1_000_000,
    "gemini-3.1-flash-lite": 1_000_000,
    "gemini-3.1-pro-preview": 1_000_000,
    "gemini-3.7-flash": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.5-flash-lite": 1_000_000,
    "gemini-2.0-flash-lite": 1_000_000,
}

# Models that support vision
VISION_MODELS: set[str] = {
    "us.anthropic.claude-opus-4-6-v1",
    "claude-opus-4-6",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3.7-flash",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
}


def resolve_model(spec: str, settings: Settings) -> Model:
    """Resolve a 'provider/model_id' spec to a Pydantic AI Model."""
    provider = provider_from_spec(spec)
    model_id = model_id_from_spec(spec)
    match provider:
        case "bedrock":
            if settings.aws_bearer_token:
                return BedrockConverseModel(
                    model_id,
                    provider=BedrockProvider(
                        api_key=settings.aws_bearer_token,
                        region_name=settings.aws_region,
                    ),
                )
            else:
                session = boto3.Session()
                client = session.client("bedrock-runtime", region_name=settings.aws_region)
                return BedrockConverseModel(
                    model_id,
                    provider=BedrockProvider(bedrock_client=client),
                )
        case "azure":
            return OpenAIModel(
                model_id,
                provider=OpenAIProvider(
                    base_url=settings.azure_openai_endpoint,
                    api_key=settings.azure_openai_api_key,
                ),
            )
        case "zen":
            return OpenAIModel(
                model_id,
                provider=OpenAIProvider(
                    base_url="https://opencode.ai/zen/v1",
                    api_key=settings.opencode_zen_api_key,
                ),
            )
        case "google":
            model = GoogleModel(
                model_id,
                provider=GoogleProvider(api_key=settings.gemini_api_key),
            )
            # Every real request this model makes goes through pacing + 429 retry —
            # not just the first request of each solver turn. See backend/ratelimit.py
            # (PacedModel) for why that distinction matters: a solver's agent.run()
            # can make several real Gemini requests internally (one per tool round
            # trip), and only the free/quota-constrained Gemini path needs this.
            from backend.ratelimit import paced

            return paced(model)
        case "claude-sdk" | "codex":
            raise ValueError(
                f"Provider '{provider}' uses its own solver backend, not Pydantic AI. "
                f"resolve_model() should not be called for {spec}."
            )
        case _:
            raise ValueError(f"Unknown provider: {provider}")


def resolve_model_settings(spec: str) -> ModelSettings:
    """Get provider-specific model settings with caching enabled."""
    provider = spec.split("/", 1)[0]
    match provider:
        case "bedrock":
            return BedrockModelSettings(
                max_tokens=128_000,
                bedrock_cache_instructions=True,
                bedrock_cache_tool_definitions=True,
                bedrock_cache_messages=True,
            )
        case "azure" | "zen":
            # Azure/Zen use OpenAI chat completions — server-side prompt caching
            # is automatic, no explicit config needed. Set max_tokens to avoid
            # reserving the full context window.
            return OpenAIModelSettings(
                max_tokens=128_000,
            )
        case "google":
            model_id = model_id_from_spec(spec)
            # Flash-Lite is the free-tier workhorse and is TPM-constrained (see
            # FREE_TIER_TPM) — a 64K max_tokens ceiling lets a single verbose turn eat
            # a big chunk of the whole per-minute token budget. Cap it lower; full
            # Flash/Pro models aren't the TPM bottleneck so keep their headroom.
            settings_kwargs: dict = {"max_tokens": 16_000 if "lite" in model_id else 64_000}
            # Flash-Lite has limited/no extended-thinking support — only request it
            # on full Flash models.
            if "lite" not in model_id:
                settings_kwargs["google_thinking_config"] = {
                    "thinking_level": "high",
                    "include_thoughts": True,
                }
            return GoogleModelSettings(**settings_kwargs)
        case _:
            return ModelSettings(max_tokens=128_000)


def model_id_from_spec(spec: str) -> str:
    """Extract just the model ID from a spec (strips effort suffix)."""
    parts = spec.split("/")
    return parts[1] if len(parts) >= 2 else spec


def provider_from_spec(spec: str) -> str:
    """Extract the provider from a spec."""
    return spec.split("/", 1)[0]


def effort_from_spec(spec: str) -> str | None:
    """Extract effort level from a spec like 'claude-sdk/claude-opus-4-6/max'."""
    parts = spec.split("/")
    if len(parts) >= 3 and parts[2] in ("low", "medium", "high", "max"):
        return parts[2]
    return None


def supports_vision(spec: str) -> bool:
    """Check if a model spec supports vision."""
    return model_id_from_spec(spec) in VISION_MODELS


def context_window(spec: str) -> int:
    """Get context window size for a model spec."""
    return CONTEXT_WINDOWS.get(model_id_from_spec(spec), 200_000)

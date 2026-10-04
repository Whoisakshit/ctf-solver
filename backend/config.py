"""Pydantic Settings — credentials from .env file + environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # CTFd
    ctfd_url: str = "http://localhost:8000"
    ctfd_user: str = "admin"
    ctfd_pass: str = "admin"
    ctfd_token: str = ""

    # API Keys
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # "free" (default) uses only free-tier Gemini models (backend.models.FREE_TIER_MODELS).
    # "pro" / "paid" switches the default lineup to backend.models.PAID_GEMINI_MODELS
    # (gemini-3.1-pro-preview + gemini-3.7-flash) — set GEMINI_TIER=pro in .env once
    # you've attached billing to your Google AI Studio project.
    gemini_tier: str = "free"

    # Provider-specific (optional, for Bedrock/Azure/Zen fallback)
    aws_region: str = "us-east-1"
    aws_bearer_token: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    opencode_zen_api_key: str = ""

    # Infra
    sandbox_image: str = "ctf-sandbox"
    # Kept modest by default since the free-tier Gemini API has low RPM/RPD/TPM
    # quotas — each concurrent challenge runs len(model_specs) solvers in parallel,
    # and each solver makes one Gemini call per turn. The shared RateLimiter paces
    # every call against the same quota regardless of how many challenges run at
    # once, but running challenges one-at-a-time keeps behavior predictable and
    # avoids several long-context solvers queuing up bursts back-to-back right when
    # the pacer opens a slot. Raise this once you've attached billing (GEMINI_TIER=pro).
    max_concurrent_challenges: int = 1
    max_attempts_per_challenge: int = 3
    container_memory_limit: str = "16g"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

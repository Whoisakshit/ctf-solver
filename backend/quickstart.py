"""Quickstart ingestion — turn a screenshot and/or a URL into a solvable challenge dir.

This bypasses CTFd entirely: point it at a screenshot of a challenge page (or of the
challenge artifact itself) and/or a bare URL, and it uses Gemini's vision + structured
output to build the same `metadata.yml` + `distfiles/` layout the rest of the agent
already expects, so it flows straight into `ChallengeSwarm`.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import yaml
from pydantic import BaseModel, Field
from pydantic_ai import Agent, BinaryContent

from backend.models import FREE_TIER_MODELS, model_id_from_spec, resolve_model, resolve_model_settings

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_EXTRACT_MODEL = FREE_TIER_MODELS[0]

_EXTRACT_PROMPT = """\
You are helping set up a CTF (Capture The Flag) challenge for an autonomous solver agent.
Read the material you're given and extract structured challenge metadata.

- If it's a screenshot of a CTF platform's challenge page: transcribe the title, category,
  point value, the FULL description text verbatim (including any flag-format hint like
  "flag format: CTF{...}"), and any connection command (e.g. "nc host 1337") or URL exactly
  as shown.
- If the image itself looks like the challenge artifact rather than a description
  (e.g. it might hide data via steganography, or is a forensics/misc puzzle image), say so
  plainly in the description, set is_image_artifact=true, and set category to something like
  forensics/stego/misc.
- If you're only given a URL and can't tell what kind of challenge it is, describe it as a
  live web/network target to investigate and leave category as your best guess.
- Never invent details that aren't actually present — leave fields blank/default instead of
  guessing point values, tags, or connection info you don't see.
"""


class ExtractedChallenge(BaseModel):
    name: str = Field(description="Challenge title, or a short descriptive slug if none is visible")
    category: str = Field(default="misc", description="pwn/rev/crypto/forensics/web/misc — best guess")
    value: int = Field(default=0, description="Point value if visible, else 0")
    description: str = Field(default="", description="Full challenge description/prompt, verbatim where possible")
    connection_info: str = Field(default="", description="nc command or target URL to connect to, if any")
    tags: list[str] = Field(default_factory=list)
    is_image_artifact: bool = Field(
        default=False,
        description="True if the image itself is the challenge artifact (stego/forensics), not just a description screenshot",
    )


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "quickstart-challenge"


def _unique_dir(challenges_dir: str, slug: str) -> Path:
    base = Path(challenges_dir)
    base.mkdir(parents=True, exist_ok=True)
    candidate = base / slug
    n = 2
    while candidate.exists():
        candidate = base / f"{slug}-{n}"
        n += 1
    return candidate


def _extraction_agent(settings: Settings, model_spec: str) -> Agent[None, ExtractedChallenge]:
    if not getattr(settings, "gemini_api_key", ""):
        raise RuntimeError(
            "GEMINI_API_KEY is required for --screenshot/--url quickstart extraction. "
            "Get a free key at https://aistudio.google.com/apikey and add it to .env."
        )
    model = resolve_model(model_spec, settings)
    model_settings = resolve_model_settings(model_spec)
    return Agent(
        model,
        model_settings=model_settings,
        output_type=ExtractedChallenge,
        system_prompt=_EXTRACT_PROMPT,
    )


async def _extract(agent: Agent[None, ExtractedChallenge], prompt, model_spec: str):
    """Run the extraction agent through the shared rate limiter/retry logic."""
    from backend.ratelimit import with_rate_limit_retry

    async def _do_run():
        return await agent.run(prompt)

    return await with_rate_limit_retry(_do_run, model_id=model_id_from_spec(model_spec))


def _write_challenge_dir(
    extracted: ExtractedChallenge,
    challenges_dir: str,
    image_bytes: bytes | None = None,
    image_name: str | None = None,
    raw_html: str = "",
) -> str:
    ch_dir = _unique_dir(challenges_dir, _slugify(extracted.name))
    ch_dir.mkdir(parents=True, exist_ok=True)

    if image_bytes and image_name:
        dist_dir = ch_dir / "distfiles"
        dist_dir.mkdir(exist_ok=True)
        (dist_dir / image_name).write_bytes(image_bytes)
    if raw_html:
        dist_dir = ch_dir / "distfiles"
        dist_dir.mkdir(exist_ok=True)
        (dist_dir / "page.html").write_text(raw_html, errors="replace", encoding="utf-8")

    description = extracted.description or "_No description could be extracted — investigate the target/attached files directly._"
    if extracted.is_image_artifact:
        description += (
            "\n\n_Note: the attached image itself may be the challenge artifact "
            "(check for steganography, hidden metadata, embedded files, etc.)._"
        )

    meta = {
        "name": extracted.name or "Quickstart Challenge",
        "category": extracted.category or "misc",
        "description": description,
        "value": extracted.value,
        "connection_info": extracted.connection_info,
        "tags": extracted.tags,
        "solves": 0,
    }
    (ch_dir / "metadata.yml").write_text(
        yaml.dump(meta, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    logger.info(f"Quickstart challenge built at {ch_dir}")
    return str(ch_dir)


async def challenge_from_screenshot(
    image_path: str,
    settings: Settings,
    challenges_dir: str = "challenges",
    model_spec: str = DEFAULT_EXTRACT_MODEL,
    extra_url: str | None = None,
) -> str:
    """Build a challenge directory from a screenshot of a CTF challenge (or the artifact itself).

    The raw image is always also saved into `distfiles/`, since it may itself be the
    puzzle (forensics/stego) rather than just a description screenshot.
    """
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Screenshot not found: {image_path}")

    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    data = path.read_bytes()

    prompt_parts: list = ["Extract this CTF challenge's metadata from the attached image."]
    if extra_url:
        prompt_parts.append(
            f"The user also gave this URL — it's likely the connection target: {extra_url}"
        )
    prompt_parts.append(BinaryContent(data=data, media_type=media_type))

    agent = _extraction_agent(settings, model_spec)
    result = await _extract(agent, prompt_parts, model_spec)
    extracted = result.output
    if extra_url and not extracted.connection_info:
        extracted.connection_info = extra_url

    return _write_challenge_dir(extracted, challenges_dir, image_bytes=data, image_name=path.name)


async def challenge_from_files(
    file_paths: list[str],
    settings: Settings,
    challenges_dir: str = "challenges",
    model_spec: str = DEFAULT_EXTRACT_MODEL,
    extra_url: str | None = None,
) -> str:
    """Build a challenge directory from just the downloaded file(s) — no screenshot needed.

    Uses a lightweight text-only pass (filenames + a small content sniff) to guess a
    name/category and write a "figure it out from the attached files" description, since
    there's no challenge-page text to transcribe.
    """
    if not file_paths:
        raise ValueError("challenge_from_files needs at least one file path")

    paths = [Path(p) for p in file_paths]
    for p in paths:
        if not p.exists():
            raise FileNotFoundError(f"Challenge file not found: {p}")

    sniffs = []
    for p in paths:
        head = p.read_bytes()[:200]
        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
        sniffs.append(f"- {p.name} ({p.stat().st_size} bytes), first bytes: {printable!r}")

    prompt = (
        "A CTF challenge gave the player only these downloaded file(s), no description "
        "text. Guess a short challenge name and the most likely category (pwn/rev/crypto/"
        "forensics/misc/web) from the filenames and byte sniff below. Leave description "
        "empty — it'll be auto-filled.\n\n" + "\n".join(sniffs)
    )
    if extra_url:
        prompt += f"\n\nA connection target was also given: {extra_url}"

    agent = _extraction_agent(settings, model_spec)
    result = await _extract(agent, prompt, model_spec)
    extracted = result.output
    extracted.description = (
        "No challenge description was given — only the attached file(s): "
        + ", ".join(p.name for p in paths)
        + ". Inspect them directly (file type, strings, headers, etc.) to figure out "
        "what's needed and find the flag."
    )
    if extra_url and not extracted.connection_info:
        extracted.connection_info = extra_url

    ch_dir = _write_challenge_dir(extracted, challenges_dir)
    dist_dir = Path(ch_dir) / "distfiles"
    dist_dir.mkdir(exist_ok=True)
    import shutil

    for p in paths:
        shutil.copy2(p, dist_dir / p.name)

    return ch_dir


async def challenge_from_url(
    url: str,
    settings: Settings,
    challenges_dir: str = "challenges",
    model_spec: str = DEFAULT_EXTRACT_MODEL,
) -> str:
    """Build a challenge directory from a URL — either a CTF platform page or the live target itself."""
    raw_html = ""
    fetch_ok = True
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0, verify=False) as client:
            resp = await client.get(url)
            raw_html = resp.text[:40_000]
    except Exception as e:
        logger.warning(f"Could not fetch {url} directly, treating it as an opaque live target: {e}")
        fetch_ok = False

    prompt = (
        f"The challenge URL is: {url}\n\n"
        + (
            f"Fetched page content (this may be the live challenge target itself, not a "
            f"description page — judge from context):\n\n{raw_html}"
            if fetch_ok and raw_html
            else (
                "The URL could not be fetched directly (may require auth, or block simple "
                "GET requests). Treat the URL itself as the challenge's connection_info/target "
                "for a live web challenge that the solver should investigate directly."
            )
        )
    )

    agent = _extraction_agent(settings, model_spec)
    result = await _extract(agent, prompt, model_spec)
    extracted = result.output
    if not extracted.connection_info:
        extracted.connection_info = url

    return _write_challenge_dir(extracted, challenges_dir, raw_html=raw_html if fetch_ok else "")

# CTF Agent

Autonomous CTF (Capture The Flag) solver that races multiple AI models against challenges in parallel. Built in a weekend, we used it to solve all 52/52 challenges and win **1st place at BSidesSF 2026 CTF**.

> **This fork runs entirely on the free-tier Gemini API** (Google AI Studio, no credit card
> needed) by default, and adds a **quickstart mode**: point it at a screenshot and/or a URL
> for a single challenge — no CTFd instance required — and it sets up and solves the
> challenge directly. See [Quickstart: solve from a screenshot or URL](#quickstart-solve-from-a-screenshot-or-url)
> and [Free-Tier Gemini Setup](#free-tier-gemini-setup) below.

Built by [Veria Labs](https://verialabs.com), founded by members of [.;,;.](https://ctftime.org/team/222911) (smiley), the [#1 US CTF team on CTFTime in 2024 and 2025](https://ctftime.org/stats/2024/US). We build AI agents that find and exploit real security vulnerabilities for large enterprises.

## Results

| Competition | Challenges Solved | Result |
|-------------|:-:|--------|
| **BSidesSF 2026** | 52/52 (100%) | **1st place ($1,500)** |

The agent solves challenges across all categories — pwn, rev, crypto, forensics, web, and misc.

## How It Works

A **coordinator** LLM manages the competition while **solver swarms** attack individual challenges. Each swarm runs multiple models simultaneously — the first to find the flag wins.

```
                        +-----------------+
                        |  CTFd Platform  |
                        +--------+--------+
                                 |
                        +--------v--------+
                        |  Poller (5s)    |
                        +--------+--------+
                                 |
                        +--------v--------+
                        | Coordinator LLM |
                        | (Claude/Codex)  |
                        +--------+--------+
                                 |
              +------------------+------------------+
              |                  |                  |
     +--------v--------+ +------v---------+ +------v---------+
     | Swarm:          | | Swarm:         | | Swarm:         |
     | challenge-1     | | challenge-2    | | challenge-N    |
     |                 | |                | |                |
     |  Opus (med)     | |  Opus (med)    | |                |
     |  Opus (max)     | |  Opus (max)    | |     ...        |
     |  GPT-5.4        | |  GPT-5.4       | |                |
     |  GPT-5.4-mini   | |  GPT-5.4-mini  | |                |
     |  GPT-5.3-codex  | |  GPT-5.3-codex | |                |
     +--------+--------+ +--------+-------+ +----------------+
              |                    |
     +--------v--------+  +-------v--------+
     | Docker Sandbox  |  | Docker Sandbox |
     | (isolated)      |  | (isolated)     |
     |                 |  |                |
     | pwntools, r2,   |  | pwntools, r2,  |
     | gdb, python...  |  | gdb, python... |
     +-----------------+  +----------------+
```

Each solver runs in an isolated Docker container with CTF tools pre-installed. Solvers never give up — they keep trying different approaches until the flag is found.

## Free-Tier Gemini Setup

```bash
# Install
uv sync

# Build sandbox image
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .

# Configure credentials — only GEMINI_API_KEY is required
cp .env.example .env
# Get a free key (no credit card) at https://aistudio.google.com/apikey
# and set GEMINI_API_KEY=... in .env

# Option A: Web GUI (opens in your browser at http://127.0.0.1:8420)
uv run ctf-web

# Option B: CLI, against a CTFd instance
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --challenges-dir challenges \
  -v
```

By default the solver and coordinator both run on **`gemini-3.1-flash-lite`** alone —
the only free-tier Gemini model with a workable daily quota for a multi-turn agent (see
"Free tier vs Pro tier" below for the numbers behind that choice). A built-in rate
limiter (`backend/ratelimit.py`) paces requests to that model's real RPM automatically,
and retries with Google's own suggested delay if a 429 slips through anyway — so you
shouldn't need to babysit it, but `-v` logs will show `Rate limited... retrying in Ns`
if it happens.

If you *do* have Anthropic/OpenAI keys or the `claude`/`codex` CLIs set up, the original
paid lineup is still there — see [Solver Models](#solver-models) and
[Coordinator Backends](#coordinator-backends) below.

## Quickstart: solve from a screenshot, file, or URL

The **web GUI** (`uv run ctf-web`) is the easiest way to do this — upload a screenshot
and/or a file and/or paste a URL, hit Solve, and watch it work with a live log and a
flag banner at the end.

For the CLI equivalent, for a one-off challenge you don't want to wire up a whole CTFd
instance for:

```bash
# From a screenshot of the challenge page (or of the challenge artifact itself,
# e.g. a stego image)
uv run ctf-solve --screenshot ./challenge.png -v

# From a downloaded file alone (binary, zip, pcap, ...) — no screenshot needed
uv run ctf-solve --file ./chall_binary -v

# From just a URL (works for web challenges where the URL *is* the target)
uv run ctf-solve --url https://chal.example.com/ -v

# Any combination — screenshot/file/url can all be given together
uv run ctf-solve --screenshot ./challenge.png --file ./handout.zip --url https://chal.example.com/ -v
```

This uses Gemini's vision + structured output to read the screenshot (transcribing the
title, description, points, flag-format hints, and any `nc`/URL connection info) and/or
fetch the URL, writes a normal `challenges/<slug>/metadata.yml` + `distfiles/` (any
screenshot/files you give it are always kept in `distfiles/` too, in case they're
themselves the puzzle), and then runs the same solver swarm used for CTFd challenges. No
CTFd instance is contacted — since there's nothing to verify a flag against, the solvers
report the flag directly (and it's written to `<challenge_dir>/flag.txt`) instead of
submitting it for scoring.

## Web GUI

```bash
uv run ctf-web
```

Starts a local FastAPI server at `http://127.0.0.1:8420` and opens it in your browser.
It's a thin front end over the exact same quickstart pipeline described above — upload a
screenshot and/or file(s), paste a URL, or any mix, and hit **Solve challenge**. Progress
streams into the log panel live (Server-Sent Events), and the flag — or an error — shows
up in a banner at the end, with a Copy button.

It only runs **one challenge at a time** by design (each solve spins up real Docker
containers and burns free-tier Gemini quota, so a second concurrent job would just
compete with the first for both); a second submission while one is running gets a
"already solving something" message instead of queuing silently.

Nothing here is exposed outside your machine — it binds to `127.0.0.1` only.

## Coordinator Backends

```bash
# Pydantic AI / Gemini coordinator (default) — only needs GEMINI_API_KEY, no CLI install
uv run ctf-solve --coordinator gemini ...

# Claude SDK coordinator — needs the `claude` CLI + an Anthropic-backed subscription/key
uv run ctf-solve --coordinator claude ...

# Codex coordinator (GPT-5.4 via JSON-RPC) — needs the `codex` CLI
uv run ctf-solve --coordinator codex ...
```

## Free tier vs Pro tier

Google AI Studio's free tier has very different quotas per model — tight enough that
picking the wrong one silently breaks a multi-turn agent partway through a challenge.
Real numbers, from the Google AI Studio usage dashboard (`aistudio.google.com` → API
Keys → your key → Usage), Aug 2026:

| Model | RPM | RPD (requests/day) | Usable for solving? |
|---|---|---|---|
| `gemini-3-flash-preview` | 5 | **20** | No — a real solve run burns through 20 requests in a couple of turns on *one* challenge, then 429s for the rest of the day |
| `gemini-3.1-flash-lite` | 15 | 500 | Yes — this is the default |

That's why the default lineup is `gemini-3.1-flash-lite` alone, not a race between the
two — racing `gemini-3-flash-preview` alongside it just burns its 20/day quota for no
benefit, since it stops contributing after a few turns anyway. Check your own dashboard
occasionally, since Google does change these numbers.

**Once you have billing attached** to your Google AI Studio project (Settings →
Billing), switch to the paid lineup:

```bash
# .env
GEMINI_TIER=pro
```
or per-run:
```bash
uv run ctf-solve --gemini-tier pro --screenshot ./chal.png -v
```

This switches to `gemini-3.1-pro-preview` (the flagship reasoning model) racing
alongside `gemini-3.7-flash` (fast/cheap agentic model) — both confirmed real, current
model IDs as of Aug 2026. Paid tier has vastly higher rate limits, but isn't literally
unlimited, so the same rate limiter/retry logic still applies — it'll just almost never
trigger.

## Solver Models

Default model lineup (free-tier Gemini, configurable in `backend/models.py`):

| Model | Provider | Notes |
|-------|----------|-------|
| Gemini 3.1 Flash-Lite | Google AI Studio | Free tier, 15 RPM / 500 RPD — the only free model with a workable daily quota |

Paid lineup (`GEMINI_TIER=pro`, see [Free tier vs Pro tier](#free-tier-vs-pro-tier) above):

| Model | Provider | Notes |
|-------|----------|-------|
| Gemini 3.1 Pro Preview | Google AI Studio | Paid only, flagship reasoning model |
| Gemini 3.7 Flash | Google AI Studio | Paid, fast/cheap agentic racer alongside Pro |

`backend/models.py` also keeps the original Claude/Codex lineup around as `PAID_MODELS`
(distinct from the Gemini paid tier above) — pass `--models` explicitly to use it if you
have the relevant keys/CLIs:

| Model | Provider | Notes |
|-------|----------|-------|
| Claude Opus 4.6 (medium) | Claude SDK | Balanced speed/quality |
| Claude Opus 4.6 (max) | Claude SDK | Deep reasoning |
| GPT-5.4 | Codex | Best overall solver |
| GPT-5.4-mini | Codex | Fast, good for easy challenges |
| GPT-5.3-codex | Codex | Reasoning model (xhigh effort) |

## Sandbox Tooling

Each solver gets an isolated Docker container pre-loaded with CTF tools:

| Category | Tools |
|----------|-------|
| **Binary** | radare2, GDB, objdump, binwalk, strings, readelf |
| **Pwn** | pwntools, ROPgadget, angr, unicorn, capstone |
| **Crypto** | SageMath, RsaCtfTool, z3, gmpy2, pycryptodome, cado-nfs |
| **Forensics** | volatility3, Sleuthkit (mmls/fls/icat), foremost, exiftool |
| **Stego** | steghide, stegseek, zsteg, ImageMagick, tesseract OCR |
| **Web** | curl, nmap, Python requests, flask |
| **Misc** | ffmpeg, sox, Pillow, numpy, scipy, PyTorch, podman |

## Features

- **Multi-model racing** — multiple AI models attack each challenge simultaneously
- **Auto-spawn** — new challenges detected and attacked automatically
- **Coordinator LLM** — reads solver traces, crafts targeted technical guidance
- **Cross-solver insights** — findings shared between models via message bus
- **Docker sandboxes** — isolated containers with full CTF tooling
- **Operator messaging** — send hints to running solvers mid-competition

## Configuration

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

```env
GEMINI_API_KEY=...

# Only needed for a live CTFd competition (skip for --screenshot/--url quickstart)
CTFD_URL=https://ctf.example.com
CTFD_TOKEN=ctfd_your_token

# Only needed if you opt into the paid claude-sdk/codex solver or coordinator backends
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

All settings can also be passed as environment variables or CLI flags.

## Requirements

- Python 3.14+ (3.10+ should also work if you relax `requires-python` in `pyproject.toml`)
- Docker
- A free Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) (default setup)
- `codex` CLI (only if using `--coordinator codex` / `codex/*` solver models)
- `claude` CLI, bundled with claude-agent-sdk (only if using `--coordinator claude` / `claude-sdk/*` solver models)

## Acknowledgements

- [es3n1n/Eruditus](https://github.com/es3n1n/Eruditus) — CTFd interaction and HTML helpers in `pull_challenges.py`

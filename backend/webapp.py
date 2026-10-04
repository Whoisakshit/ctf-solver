"""Local web GUI for the CTF agent — `uv run ctf-web` and open http://127.0.0.1:8420.

Reuses the exact same quickstart (screenshot/file/URL) + solver-swarm pipeline as the
CLI's `--screenshot`/`--url`/`--file` flags — this is just a browser front end over it,
with live log streaming via Server-Sent Events so you can watch it work.

Single-job-at-a-time by design: each solve spins up real Docker containers and burns
free-tier Gemini quota, so queuing a second job on top would just fight the first one
for both.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse

from backend.config import Settings
from backend.models import default_models

logger = logging.getLogger(__name__)

app = FastAPI(title="CTF Agent")

UPLOAD_ROOT = Path(tempfile.gettempdir()) / "ctf-agent-web-uploads"

_JOBS: dict[str, "Job"] = {}
_lock = asyncio.Lock()
_current_job_id: str | None = None

_DONE_SENTINEL = "__CTF_AGENT_JOB_DONE__"


@dataclass
class Job:
    id: str
    status: str = "running"  # running | done | error
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    log_history: list[str] = field(default_factory=list)
    flag: str | None = None
    challenge_name: str | None = None
    error: str | None = None
    cost_usd: float = 0.0

    def log(self, line: str) -> None:
        self.log_history.append(line)
        self.queue.put_nowait(line)


class _QueueLogHandler(logging.Handler):
    """Routes backend.* logger output into a job's live log queue."""

    def __init__(self, job: Job) -> None:
        super().__init__()
        self.job = job
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.job.log(self.format(record))
        except Exception:
            pass


async def _run_job(
    job: Job,
    settings: Settings,
    screenshot_path: str | None,
    file_paths: list[str],
    url: str | None,
    model_specs: list[str],
    max_challenges: int,
    challenges_dir: str,
) -> None:
    from backend.agents.swarm import ChallengeSwarm
    from backend.cost_tracker import CostTracker
    from backend.ctfd import CTFdClient
    from backend.prompts import ChallengeMeta
    from backend.quickstart import challenge_from_files, challenge_from_screenshot, challenge_from_url
    from backend.sandbox import cleanup_orphan_containers, configure_semaphore
    from backend.solver_base import FLAG_FOUND

    backend_logger = logging.getLogger("backend")
    handler = _QueueLogHandler(job)
    backend_logger.addHandler(handler)
    backend_logger.setLevel(logging.INFO)

    ctfd = None
    try:
        job.log("Setting up sandbox...")
        max_containers = max_challenges * len(model_specs)
        configure_semaphore(max_containers)
        await cleanup_orphan_containers()

        job.log("Extracting challenge metadata with Gemini...")
        if screenshot_path:
            challenge_path = await challenge_from_screenshot(
                screenshot_path, settings, challenges_dir, extra_url=url
            )
        elif url:
            challenge_path = await challenge_from_url(url, settings, challenges_dir)
        else:
            challenge_path = await challenge_from_files(
                file_paths, settings, challenges_dir, extra_url=url
            )

        # challenge_from_files already attaches files itself; only copy extras here
        # when the screenshot/URL branch built the dir instead.
        if file_paths and (screenshot_path or url):
            dist_dir = Path(challenge_path) / "distfiles"
            dist_dir.mkdir(exist_ok=True)
            for f in file_paths:
                dest = dist_dir / Path(f).name
                shutil.copy2(f, dest)
                job.log(f"Attached {Path(f).name}")

        meta = ChallengeMeta.from_yaml(Path(challenge_path) / "metadata.yml")
        job.challenge_name = meta.name
        job.log(f"Challenge: {meta.name} ({meta.category or 'unknown'})")

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

        result = await swarm.run()
        if result and result.status == FLAG_FOUND and result.flag:
            job.flag = result.flag
            (Path(challenge_path) / "flag.txt").write_text(result.flag + "\n", encoding="utf-8")
            job.log(f"FLAG FOUND: {result.flag}")
        else:
            job.log("No flag found.")
        job.cost_usd = cost_tracker.total_cost_usd
        job.log(f"Total cost: ${cost_tracker.total_cost_usd:.4f}")
        job.status = "done"
    except Exception as e:
        logger.exception("Job %s failed", job.id)
        job.error = str(e)
        job.status = "error"
        job.log(f"ERROR: {e}")
    finally:
        if ctfd is not None:
            await ctfd.close()
        backend_logger.removeHandler(handler)
        job.queue.put_nowait(_DONE_SENTINEL)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _HTML_PAGE


@app.get("/api/status")
async def api_status() -> dict:
    return {"busy": _current_job_id is not None, "job_id": _current_job_id}


@app.post("/api/solve")
async def api_solve(
    url: str = Form(default=""),
    screenshot: UploadFile | None = File(default=None),
    files: list[UploadFile] = File(default=[]),
) -> dict:
    global _current_job_id

    settings = Settings()
    if not settings.gemini_api_key:
        raise HTTPException(
            400,
            "GEMINI_API_KEY is not set. Add a free key from "
            "https://aistudio.google.com/apikey to your .env and restart the server.",
        )

    async with _lock:
        if _current_job_id is not None:
            raise HTTPException(409, "Another challenge is already being solved — wait for it to finish.")
        job_id = uuid.uuid4().hex[:12]
        _current_job_id = job_id

    job = Job(job_id)
    _JOBS[job_id] = job

    job_dir = UPLOAD_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    screenshot_path: str | None = None
    if screenshot is not None and screenshot.filename:
        screenshot_path = str(job_dir / screenshot.filename)
        with Path(screenshot_path).open("wb") as f:
            f.write(await screenshot.read())

    file_paths: list[str] = []
    for uf in files or []:
        if uf.filename:
            p = job_dir / uf.filename
            with p.open("wb") as f:
                f.write(await uf.read())
            file_paths.append(str(p))

    clean_url = url.strip() or None

    if not screenshot_path and not clean_url and not file_paths:
        async with _lock:
            _current_job_id = None
        raise HTTPException(400, "Give at least a screenshot, a file, or a URL.")

    model_specs = list(default_models(settings))

    async def runner() -> None:
        global _current_job_id
        try:
            await _run_job(
                job,
                settings,
                screenshot_path,
                file_paths,
                clean_url,
                model_specs,
                max_challenges=1,
                challenges_dir="challenges",
            )
        finally:
            async with _lock:
                _current_job_id = None

    asyncio.create_task(runner())
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job")

    async def event_gen():
        for line in list(job.log_history):
            yield f"data: {json.dumps(line)}\n\n"
        if job.status != "running":
            yield _result_event(job)
            return
        while True:
            line = await job.queue.get()
            if line == _DONE_SENTINEL:
                yield _result_event(job)
                break
            yield f"data: {json.dumps(line)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _result_event(job: Job) -> str:
    payload = {
        "status": job.status,
        "flag": job.flag,
        "challenge_name": job.challenge_name,
        "error": job.error,
        "cost_usd": job.cost_usd,
    }
    return f"event: result\ndata: {json.dumps(payload)}\n\n"


_HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CTF Agent</title>
<style>
  :root {
    --bg: #0b0d12;
    --panel: #131722;
    --border: #232838;
    --text: #e6e9f0;
    --muted: #8b93a7;
    --accent: #4ade80;
    --accent-dim: #1f6b43;
    --danger: #f87171;
    --mono: "SF Mono", "JetBrains Mono", "Fira Code", ui-monospace, Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
    min-height: 100vh;
  }
  header {
    padding: 28px 32px 16px;
    border-bottom: 1px solid var(--border);
  }
  header h1 {
    margin: 0;
    font-size: 22px;
    font-weight: 650;
    letter-spacing: -0.01em;
  }
  header h1 .dot { color: var(--accent); }
  header p { margin: 6px 0 0; color: var(--muted); font-size: 13.5px; }
  main {
    max-width: 880px;
    margin: 0 auto;
    padding: 28px 32px 60px;
    display: grid;
    gap: 22px;
  }
  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px 22px;
  }
  .panel h2 {
    margin: 0 0 14px;
    font-size: 13px;
    font-weight: 650;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
  }
  label {
    display: block;
    font-size: 13px;
    color: var(--muted);
    margin-bottom: 6px;
    margin-top: 16px;
  }
  label:first-of-type { margin-top: 0; }
  input[type=text], input[type=url] {
    width: 100%;
    background: #0e1118;
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 14px;
    font-family: var(--mono);
  }
  input[type=text]:focus, input[type=url]:focus { outline: 1px solid var(--accent); border-color: var(--accent); }
  .file-row {
    display: flex;
    align-items: center;
    gap: 10px;
    background: #0e1118;
    border: 1px dashed var(--border);
    border-radius: 8px;
    padding: 10px 12px;
  }
  .file-row input[type=file] { color: var(--muted); font-size: 13px; flex: 1; }
  .hint { font-size: 12px; color: var(--muted); margin-top: 6px; }
  button#solve-btn {
    margin-top: 20px;
    width: 100%;
    background: var(--accent);
    color: #06210f;
    border: none;
    border-radius: 8px;
    padding: 13px;
    font-size: 14.5px;
    font-weight: 650;
    cursor: pointer;
  }
  button#solve-btn:disabled { background: var(--accent-dim); color: #7fa38c; cursor: not-allowed; }
  #error-box {
    display: none;
    margin-top: 14px;
    background: #2a1216;
    border: 1px solid #5a2530;
    color: var(--danger);
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 13px;
  }
  #log-panel { display: none; }
  #log {
    background: #0a0c11;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px;
    height: 320px;
    overflow-y: auto;
    font-family: var(--mono);
    font-size: 12.5px;
    line-height: 1.6;
    color: #b9c0d4;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .status-line { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; font-size: 13px; color: var(--muted); }
  .spinner {
    width: 13px; height: 13px; border-radius: 50%;
    border: 2px solid var(--border); border-top-color: var(--accent);
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  #result-panel { display: none; }
  .flag-banner {
    background: linear-gradient(135deg, #0f2e1c, #0a1f14);
    border: 1px solid var(--accent-dim);
    border-radius: 10px;
    padding: 18px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }
  .flag-banner.none { background: #241417; border-color: #5a2530; }
  .flag-banner code {
    font-family: var(--mono);
    font-size: 15px;
    color: var(--accent);
    word-break: break-all;
  }
  .flag-banner.none code { color: var(--danger); }
  .flag-banner button {
    background: transparent;
    border: 1px solid var(--accent-dim);
    color: var(--accent);
    border-radius: 6px;
    padding: 7px 12px;
    font-size: 12.5px;
    cursor: pointer;
    white-space: nowrap;
  }
  .meta-row { margin-top: 10px; font-size: 12.5px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>CTF Agent<span class="dot">.</span></h1>
  <p>Give it a screenshot, a downloaded file, or a URL — it sets up the challenge and solves it with free-tier Gemini.</p>
</header>
<main>
  <div class="panel">
    <h2>New challenge</h2>
    <label for="screenshot-input">Screenshot (challenge page or the artifact itself)</label>
    <div class="file-row"><input type="file" id="screenshot-input" accept="image/*"></div>

    <label for="files-input">Downloaded file(s) — binary, zip, pcap, etc.</label>
    <div class="file-row"><input type="file" id="files-input" multiple></div>

    <label for="url-input">URL — the challenge's target or connection info</label>
    <input type="text" id="url-input" placeholder="https://chal.example.com or nc host port">
    <div class="hint">Give at least one of the three. All three can be combined.</div>

    <button id="solve-btn" onclick="solve()">Solve challenge</button>
    <div id="error-box"></div>
  </div>

  <div class="panel" id="log-panel">
    <h2>Progress</h2>
    <div class="status-line"><span class="spinner" id="spinner"></span><span id="status-text">Working...</span></div>
    <div id="log"></div>
  </div>

  <div class="panel" id="result-panel">
    <h2>Result</h2>
    <div id="flag-banner" class="flag-banner">
      <code id="flag-text"></code>
      <button onclick="copyFlag()">Copy</button>
    </div>
    <div class="meta-row" id="meta-text"></div>
  </div>
</main>
<script>
let currentFlag = "";

function esc(s) { return s; }

async function solve() {
  const btn = document.getElementById("solve-btn");
  const errBox = document.getElementById("error-box");
  errBox.style.display = "none";

  const screenshot = document.getElementById("screenshot-input").files[0];
  const files = document.getElementById("files-input").files;
  const url = document.getElementById("url-input").value.trim();

  if (!screenshot && files.length === 0 && !url) {
    errBox.textContent = "Give at least a screenshot, a file, or a URL.";
    errBox.style.display = "block";
    return;
  }

  const fd = new FormData();
  if (screenshot) fd.append("screenshot", screenshot);
  for (const f of files) fd.append("files", f);
  if (url) fd.append("url", url);

  btn.disabled = true;
  btn.textContent = "Solving...";
  document.getElementById("log-panel").style.display = "block";
  document.getElementById("result-panel").style.display = "none";
  document.getElementById("log").textContent = "";
  document.getElementById("status-text").textContent = "Working...";
  document.getElementById("spinner").style.display = "inline-block";

  let resp;
  try {
    resp = await fetch("/api/solve", { method: "POST", body: fd });
  } catch (e) {
    showError("Could not reach the server: " + e);
    resetButton();
    return;
  }

  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    showError(body.detail || ("Request failed: " + resp.status));
    resetButton();
    return;
  }

  const { job_id } = await resp.json();
  streamJob(job_id);
}

function streamJob(jobId) {
  const logEl = document.getElementById("log");
  const es = new EventSource(`/api/jobs/${jobId}/stream`);

  es.onmessage = (evt) => {
    const line = JSON.parse(evt.data);
    logEl.textContent += line + "\\n";
    logEl.scrollTop = logEl.scrollHeight;
  };

  es.addEventListener("result", (evt) => {
    const data = JSON.parse(evt.data);
    es.close();
    resetButton();
    document.getElementById("spinner").style.display = "none";
    document.getElementById("status-text").textContent =
      data.status === "error" ? "Failed" : "Done";
    showResult(data);
  });

  es.onerror = () => {
    document.getElementById("status-text").textContent = "Connection lost — job may still be running server-side.";
  };
}

function showResult(data) {
  const panel = document.getElementById("result-panel");
  const banner = document.getElementById("flag-banner");
  const flagText = document.getElementById("flag-text");
  const metaText = document.getElementById("meta-text");
  panel.style.display = "block";

  if (data.flag) {
    currentFlag = data.flag;
    banner.classList.remove("none");
    flagText.textContent = data.flag;
  } else {
    currentFlag = "";
    banner.classList.add("none");
    flagText.textContent = data.error ? ("Error: " + data.error) : "No flag found";
  }

  const bits = [];
  if (data.challenge_name) bits.push("Challenge: " + data.challenge_name);
  if (typeof data.cost_usd === "number") bits.push("Cost: $" + data.cost_usd.toFixed(4));
  metaText.textContent = bits.join("  ·  ");
}

function showError(msg) {
  const errBox = document.getElementById("error-box");
  errBox.textContent = msg;
  errBox.style.display = "block";
}

function resetButton() {
  const btn = document.getElementById("solve-btn");
  btn.disabled = false;
  btn.textContent = "Solve challenge";
}

function copyFlag() {
  if (!currentFlag) return;
  navigator.clipboard.writeText(currentFlag);
}
</script>
</body>
</html>
"""


def run() -> None:
    """Entry point for `uv run ctf-web` — starts the server and opens the browser."""
    import webbrowser

    import uvicorn

    port = 8420
    local_url = f"http://127.0.0.1:{port}"
    print(f"CTF Agent web GUI: {local_url}")
    try:
        webbrowser.open(local_url)
    except Exception:
        pass
    uvicorn.run("backend.webapp:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    run()

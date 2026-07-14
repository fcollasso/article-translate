#!/usr/bin/env python3
"""
server.py — Web frontend for traduzir.py with live LM Studio / GPU metrics.

Run:
  .venv/bin/python server.py          # http://localhost:8010

How metrics work:
  Translation jobs run traduzir.py with --base-url pointed at this server's
  /llmproxy/v1, which forwards every request to the real LM Studio server
  (LOCAL_BASE_URL from .env) while recording token usage and timing.
  GPU stats come from nvidia-smi; model info from LM Studio's native
  /api/v0/models endpoint.
"""

import os
import pty
import re
import select
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from traduzir import load_env

SCRIPT_DIR = Path(__file__).resolve().parent
ENV = load_env(SCRIPT_DIR / ".env")

LOCAL_BASE_URL = ENV.get("LOCAL_BASE_URL", "http://localhost:1234/v1").rstrip("/")
LMSTUDIO_ROOT = LOCAL_BASE_URL.removesuffix("/v1")
PORT = int(ENV.get("FRONTEND_PORT", "8010"))

UPLOADS_DIR = SCRIPT_DIR / "uploads"
OUTPUT_DIR = SCRIPT_DIR / "output" / "web"
FRONTEND_FILE = SCRIPT_DIR / "frontend" / "index.html"

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
HISTORY_MAX = 150
SAMPLE_INTERVAL = 2.0  # seconds between metric samples

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\].*?(?:\x07|\x1b\\)")

FILE_LABELS = [
    (".mono.pdf", "PDF traduzido (mono)"),
    (".dual.pdf", "PDF bilíngue (dual)"),
    (".glossary.csv", "Glossário (CSV)"),
]

# babeldoc pipeline stages mapped to overall-progress ranges. Weights are
# empirical: translation dominates wall-clock time by far.
STAGE_BOUNDS = {
    "DetectScannedFile": (0, 2),
    "Parse PDF and Create Intermediate Representation": (2, 4),
    "Parse Page Layout": (4, 6),
    "Parse Paragraphs": (6, 7),
    "Parse Formulas and Styles": (7, 8),
    "Automatic Term Extraction": (8, 15),
    "Translate Paragraphs": (15, 88),
    "Typesetting": (88, 93),
    "Add Fonts": (93, 96),
    "Generate drawing instructions": (96, 97),
    "Subset font": (97, 98),
    "Save PDF": (98, 100),
}
PROGRESS_RE = re.compile(
    r"(?P<stage>[A-Za-z][\w /,()-]*?) \(\d+/\d+\)\s+\S*\s*(?P<x>\d+)/(?P<y>\d+)"
)
# babeldoc's overall bar ("translate ━━━ 42/100") — authoritative when present
OVERALL_RE = re.compile(r"^translate\s+\S*\s*(?P<x>\d+)/100\b")


# ---------------------------------------------------------------- LLM stats

class LlmStats:
    """Aggregates usage recorded by the /llmproxy passthrough."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests_total = 0
        self.requests_active = 0
        self.prompt_tokens_total = 0
        self.completion_tokens_total = 0
        self.tok_s_last: float | None = None
        self._tok_s_samples: list[float] = []
        # (timestamp, completion_tokens) of recently finished requests,
        # used to compute instantaneous throughput for the history chart
        self._recent: deque[tuple[float, int]] = deque(maxlen=512)

    def start_request(self) -> None:
        with self.lock:
            self.requests_active += 1

    def end_request(self, usage: dict | None, duration: float) -> None:
        with self.lock:
            self.requests_active -= 1
            self.requests_total += 1
            if not usage:
                return
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            self.prompt_tokens_total += prompt
            self.completion_tokens_total += completion
            self._recent.append((time.time(), completion))
            if completion and duration > 0:
                self.tok_s_last = completion / duration
                self._tok_s_samples.append(self.tok_s_last)

    def throughput(self, window: float = 6.0) -> float | None:
        """Completion tokens/s over the last `window` seconds (all requests combined)."""
        now = time.time()
        with self.lock:
            tokens = sum(n for t, n in self._recent if now - t <= window)
            active = self.requests_active
        if tokens == 0 and active == 0:
            return None
        return round(tokens / window, 1)

    def snapshot(self) -> dict:
        with self.lock:
            avg = (sum(self._tok_s_samples) / len(self._tok_s_samples)) if self._tok_s_samples else None
            return {
                "requests_total": self.requests_total,
                "requests_active": self.requests_active,
                "prompt_tokens_total": self.prompt_tokens_total,
                "completion_tokens_total": self.completion_tokens_total,
                "tok_s_last": round(self.tok_s_last, 1) if self.tok_s_last else None,
                "tok_s_avg": round(avg, 1) if avg else None,
            }


STATS = LlmStats()
HISTORY: deque[dict] = deque(maxlen=HISTORY_MAX)


# ---------------------------------------------------------------- GPU / model info

NVIDIA_SMI = shutil.which("nvidia-smi") or "/usr/lib/wsl/lib/nvidia-smi"


def read_gpu() -> dict | None:
    try:
        out = subprocess.run(
            [NVIDIA_SMI, "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        util, used, total, temp = [v.strip() for v in out.stdout.strip().splitlines()[0].split(",")]
        return {"util_pct": int(util), "vram_used_mib": int(used),
                "vram_total_mib": int(total), "temp_c": int(temp)}
    except Exception:
        return None


def read_model() -> dict | None:
    try:
        r = httpx.get(f"{LMSTUDIO_ROOT}/api/v0/models", timeout=3)
        for m in r.json().get("data", []):
            if m.get("type") == "llm" and m.get("state") == "loaded":
                return {k: m.get(k) for k in
                        ("id", "state", "quantization", "loaded_context_length", "max_context_length")}
        return None
    except Exception:
        return None


def sampler_loop() -> None:
    while True:
        gpu = read_gpu()
        sample = {
            "t": time.time(),
            "tok_s": STATS.throughput(),
            "gpu_util": gpu["util_pct"] if gpu else None,
            "vram_used_mib": gpu["vram_used_mib"] if gpu else None,
            "temp_c": gpu["temp_c"] if gpu else None,
        }
        HISTORY.append(sample)
        time.sleep(SAMPLE_INTERVAL)


# ---------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    filename: str
    pdf_path: Path
    out_dir: Path
    status: str = "queued"  # queued | running | done | error
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    progress: float | None = None  # 0-100 while running
    _tail: deque = field(default_factory=lambda: deque(maxlen=60), repr=False)

    @property
    def log_path(self) -> Path:
        return self.out_dir / "run.log"

    def log_tail(self, max_chars: int = 4000) -> str:
        return "\n".join(self._tail)[-max_chars:]

    def files(self) -> list[dict]:
        found = []
        if not self.out_dir.exists():
            return found
        for f in sorted(self.out_dir.iterdir()):
            for suffix, label in FILE_LABELS:
                if f.name.endswith(suffix):
                    found.append({"label": label, "name": f.name,
                                  "url": f"/api/jobs/{self.id}/files/{f.name}",
                                  "size": f.stat().st_size})
        return found

    def to_dict(self) -> dict:
        return {
            "id": self.id, "filename": self.filename, "status": self.status,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "error": self.error,
            "progress": round(self.progress, 1) if self.progress is not None else None,
            "log_tail": self.log_tail() if self.status in ("running", "error") else "",
            "files": self.files() if self.status == "done" else [],
        }


JOBS: dict[str, Job] = {}
JOB_ORDER: list[str] = []  # most recent first
JOB_QUEUE: deque[str] = deque()
JOBS_LOCK = threading.Lock()


def _stage_progress(line: str) -> float | None:
    overall = OVERALL_RE.match(line)
    if overall:
        return float(overall.group("x"))
    m = PROGRESS_RE.search(line)
    if not m:
        return None
    lo_hi = STAGE_BOUNDS.get(m.group("stage").strip())
    x, y = int(m.group("x")), int(m.group("y"))
    if not lo_hi or y == 0:
        return None
    lo, hi = lo_hi
    return lo + (hi - lo) * min(x / y, 1.0)


def run_job(job: Job, proxy_url: str) -> int:
    """Run traduzir.py under a pty so babeldoc's rich progress bars render
    (they carry per-stage counters we parse into job.progress). The log file
    gets progress frames throttled to ~1/s; other lines are kept verbatim."""
    cmd = [sys.executable, str(SCRIPT_DIR / "traduzir.py"), str(job.pdf_path),
           "--backend", "local", "--base-url", proxy_url, "--out", str(job.out_dir)]
    env = {**os.environ, "COLUMNS": "200", "LINES": "50", "TERM": "xterm-256color"}
    master, slave = pty.openpty()
    proc = subprocess.Popen(cmd, stdout=slave, stderr=slave, stdin=subprocess.DEVNULL,
                            cwd=SCRIPT_DIR, env=env, close_fds=True)
    os.close(slave)
    buffer = ""
    last_frame_write = 0.0
    with open(job.log_path, "w", encoding="utf-8") as log:
        while True:
            ready, _, _ = select.select([master], [], [], 1.0)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:  # pty closed on child exit
                    break
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                *lines, buffer = re.split(r"[\r\n]+", buffer)
                for raw in lines:
                    line = ANSI_RE.sub("", raw).strip()
                    if not line:
                        continue
                    pct = _stage_progress(line)
                    if pct is not None:
                        job.progress = max(job.progress or 0.0, pct)
                        if time.time() - last_frame_write < 1.0:
                            continue  # throttle: rich redraws many times/s
                        last_frame_write = time.time()
                    job._tail.append(line)
                    log.write(line + "\n")
            elif proc.poll() is not None:
                break
        log.flush()
    os.close(master)
    return proc.wait()


def worker_loop() -> None:
    """Runs one translation at a time — the GPU can't take two papers at once."""
    proxy_url = f"http://127.0.0.1:{PORT}/llmproxy/v1"
    while True:
        with JOBS_LOCK:
            job_id = JOB_QUEUE.popleft() if JOB_QUEUE else None
        if job_id is None:
            time.sleep(0.5)
            continue
        job = JOBS[job_id]
        job.status, job.started_at = "running", time.time()
        try:
            returncode = run_job(job, proxy_url)
            if returncode == 0 and job.files():
                job.status, job.progress = "done", 100.0
            else:
                job.status = "error"
                job.error = f"traduzir.py saiu com código {returncode} (ver log)"
        except Exception as exc:
            job.status, job.error = "error", str(exc)
        job.finished_at = time.time()


# ---------------------------------------------------------------- app

app = FastAPI(title="tradutor-artigos")

app.mount("/favicon", StaticFiles(directory=SCRIPT_DIR / "favicon"), name="favicon")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_FILE, media_type="text/html")


@app.post("/api/jobs", status_code=201)
async def create_job(file: UploadFile) -> dict:
    name = Path(file.filename or "").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, detail="Envie um arquivo .pdf")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, detail="Arquivo maior que 200 MB")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, detail="O arquivo não parece ser um PDF válido")

    job_id = uuid.uuid4().hex[:12]
    job_dir = UPLOADS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = job_dir / name
    pdf_path.write_bytes(data)

    job = Job(id=job_id, filename=name, pdf_path=pdf_path, out_dir=OUTPUT_DIR / job_id)
    job.out_dir.mkdir(parents=True, exist_ok=True)
    with JOBS_LOCK:
        JOBS[job_id] = job
        JOB_ORDER.insert(0, job_id)
        JOB_QUEUE.append(job_id)
    return {"id": job_id, "filename": name}


@app.get("/api/jobs")
def list_jobs() -> dict:
    with JOBS_LOCK:
        return {"jobs": [JOBS[jid].to_dict() for jid in JOB_ORDER]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, detail="Job não encontrado")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/files/{name}")
def download(job_id: str, name: str) -> FileResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, detail="Job não encontrado")
    path = (job.out_dir / Path(name).name).resolve()
    if not path.is_file() or job.out_dir.resolve() not in path.parents:
        raise HTTPException(404, detail="Arquivo não encontrado")
    return FileResponse(path, filename=path.name)


@app.get("/api/metrics")
def metrics() -> dict:
    return {
        "model": read_model(),
        "llm": STATS.snapshot(),
        "gpu": read_gpu(),
        "history": list(HISTORY),
    }


# ---------------------------------------------------------------- LLM proxy

PROXY_CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
HOP_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding"}


@app.api_route("/llmproxy/v1/{path:path}", methods=["GET", "POST"])
async def llm_proxy(path: str, request: Request) -> Response:
    url = f"{LOCAL_BASE_URL}/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    body = await request.body()
    is_completion = request.method == "POST" and path.rstrip("/").endswith("completions")

    if is_completion:
        STATS.start_request()
    start = time.monotonic()
    usage = None
    try:
        upstream = await PROXY_CLIENT.request(request.method, url, headers=headers, content=body)
        if is_completion and upstream.headers.get("content-type", "").startswith("application/json"):
            try:
                usage = upstream.json().get("usage")
            except ValueError:
                pass
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS},
        )
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"proxy: {exc}"}, status_code=502)
    finally:
        if is_completion:
            STATS.end_request(usage, time.monotonic() - start)


def main() -> None:
    if not FRONTEND_FILE.exists():
        sys.exit(f"[erro] Frontend não encontrado: {FRONTEND_FILE}")
    UPLOADS_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=sampler_loop, daemon=True).start()
    threading.Thread(target=worker_loop, daemon=True).start()
    print(f"Tradutor de Artigos: http://localhost:{PORT}  (LM Studio: {LOCAL_BASE_URL})")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()

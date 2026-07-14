#!/usr/bin/env python3
"""
server.py — Web frontend do traduzir.py com autenticação por token,
persistência em SQLite, arquivos no Cloudflare R2 e métricas LM Studio/GPU.

Rodar:
  .venv/bin/python server.py             # http://127.0.0.1:8010 (dev)
  docker compose up -d --build           # produção no desktop (0.0.0.0:8010)

Tokens de acesso (a UI pede um token na entrada):
  python server.py token create <nome>   # imprime o token UMA vez
  python server.py token list
  python server.py token revoke <nome>

Como funciona:
  - Jobs e tokens ficam em SQLite (data/traduzai.db) — sobrevivem a restart.
  - Com R2_* configurado no .env, as saídas sobem para o R2 e os downloads
    viram URLs pré-assinadas servidas direto pela Cloudflare. Sem R2, os
    arquivos são servidos localmente com URL assinada (HMAC, 1h de validade)
    — assinada porque links <a> não carregam o header Authorization.
  - Jobs rodam traduzir.py com --base-url apontado para /llmproxy/v1 deste
    servidor, que repassa ao LM Studio (LOCAL_BASE_URL) registrando tokens
    e duração de cada requisição. /llmproxy só aceita conexões de localhost.
  - GPU via nvidia-smi; info do modelo via API nativa /api/v0/models.
"""

import hashlib
import hmac
import json
import os
import pty
import re
import secrets
import select
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from traduzir import load_env

SCRIPT_DIR = Path(__file__).resolve().parent
ENV = load_env(SCRIPT_DIR / ".env")


def cfg(key: str, default: str = "") -> str:
    """Variável de ambiente > .env > default (o docker-compose usa env vars)."""
    return os.environ.get(key) or ENV.get(key) or default


LOCAL_BASE_URL = cfg("LOCAL_BASE_URL", "http://localhost:1234/v1").rstrip("/")
LMSTUDIO_ROOT = LOCAL_BASE_URL.removesuffix("/v1")
HOST = cfg("FRONTEND_HOST", "127.0.0.1")
PORT = int(cfg("FRONTEND_PORT", "8010"))

DATA_DIR = SCRIPT_DIR / "data"
DB_PATH = Path(cfg("DB_PATH", str(DATA_DIR / "traduzai.db")))
UPLOADS_DIR = SCRIPT_DIR / "uploads"
OUTPUT_DIR = SCRIPT_DIR / "output" / "web"
FRONTEND_FILE = SCRIPT_DIR / "frontend" / "index.html"

R2_ACCOUNT_ID = cfg("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = cfg("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = cfg("R2_SECRET_ACCESS_KEY")
R2_BUCKET = cfg("R2_BUCKET")
R2_ENABLED = all((R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET))

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
HISTORY_MAX = 150
SAMPLE_INTERVAL = 2.0  # seconds between metric samples
DOWNLOAD_URL_TTL = 3600  # validade (s) das URLs de download

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

DOWNLOAD_SECRET = b""  # definido por init_db()


# ---------------------------------------------------------------- SQLite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  token_hash  TEXT NOT NULL,
  created_at  REAL NOT NULL,
  revoked_at  REAL
);
CREATE TABLE IF NOT EXISTS jobs (
  id          TEXT PRIMARY KEY,
  filename    TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'queued',
  error       TEXT,
  files       TEXT NOT NULL DEFAULT '[]',
  created_at  REAL NOT NULL,
  started_at  REAL,
  finished_at REAL
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> bytes:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(db()) as conn, conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key='download_secret'").fetchone()
        if row is None:
            secret = secrets.token_hex(32)
            conn.execute("INSERT INTO meta VALUES ('download_secret', ?)", (secret,))
        else:
            secret = row["value"]
    return bytes.fromhex(secret)


# ---------------------------------------------------------------- tokens de acesso

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_token(token: str | None) -> tuple[str | None, bool]:
    """(nome do token se válido, existe algum token cadastrado)."""
    with closing(db()) as conn:
        rows = conn.execute("SELECT name, token_hash FROM tokens WHERE revoked_at IS NULL").fetchall()
    name = None
    if token:
        h = hash_token(token)
        for row in rows:  # percorre todos — comparação em tempo constante
            if hmac.compare_digest(row["token_hash"], h):
                name = row["name"]
    return name, bool(rows)


def bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


# ---------------------------------------------------------------- Cloudflare R2

_r2_client = None
_r2_lock = threading.Lock()


def r2():
    global _r2_client
    with _r2_lock:
        if _r2_client is None:
            import boto3  # import tardio: só exigido com R2 configurado

            _r2_client = boto3.client(
                "s3",
                endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
                aws_access_key_id=R2_ACCESS_KEY_ID,
                aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                region_name="auto",
            )
    return _r2_client


def r2_presign(key: str, filename: str) -> str:
    return r2().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": R2_BUCKET,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=DOWNLOAD_URL_TTL,
    )


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

class RunState:
    """Estado volátil do job em execução: progresso 0-100 e tail do log.
    O restante do estado vive no SQLite e sobrevive a restarts."""

    def __init__(self) -> None:
        self.progress: float | None = None
        self.tail: deque[str] = deque(maxlen=60)


RUNNING: dict[str, RunState] = {}
RUNNING_LOCK = threading.Lock()


def job_out_dir(job_id: str) -> Path:
    return OUTPUT_DIR / job_id


def log_tail(job_id: str, max_chars: int = 4000) -> str:
    with RUNNING_LOCK:
        state = RUNNING.get(job_id)
        if state and state.tail:
            return "\n".join(state.tail)[-max_chars:]
    # após restart o tail em memória se perde; o run.log (já sem ANSI) cobre
    try:
        return (job_out_dir(job_id) / "run.log").read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""


def sign_download(job_id: str, name: str, exp: int) -> str:
    msg = f"{job_id}\n{name}\n{exp}".encode()
    return hmac.new(DOWNLOAD_SECRET, msg, hashlib.sha256).hexdigest()


def file_url(job_id: str, f: dict) -> str:
    if R2_ENABLED and f.get("key"):
        return r2_presign(f["key"], f["name"])
    exp = int(time.time()) + DOWNLOAD_URL_TTL
    return f"/api/jobs/{job_id}/files/{quote(f['name'])}?exp={exp}&sig={sign_download(job_id, f['name'], exp)}"


def job_to_dict(row: sqlite3.Row) -> dict:
    status = row["status"]
    with RUNNING_LOCK:
        state = RUNNING.get(row["id"])
        progress = state.progress if state else None
    if status == "done":
        progress = 100.0
    files = json.loads(row["files"]) if status == "done" else []
    return {
        "id": row["id"],
        "filename": row["filename"],
        "status": status,
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        "progress": round(progress, 1) if progress is not None else None,
        "log_tail": log_tail(row["id"]) if status in ("running", "error") else "",
        "files": [
            {"label": f["label"], "name": f["name"], "size": f["size"], "url": file_url(row["id"], f)}
            for f in files
        ],
    }


def collect_outputs(job_id: str) -> list[dict]:
    found = []
    out_dir = job_out_dir(job_id)
    if not out_dir.exists():
        return found
    for f in sorted(out_dir.iterdir()):
        for suffix, label in FILE_LABELS:
            if f.name.endswith(suffix):
                found.append({"label": label, "name": f.name, "size": f.stat().st_size, "path": f})
    return found


def upload_outputs(job_id: str, files: list[dict], log) -> list[dict]:
    """Sobe as saídas para o R2; em falha (ou sem R2) o arquivo fica servido localmente."""
    result = []
    for f in files:
        path = f.pop("path")
        if R2_ENABLED:
            key = f"jobs/{job_id}/{f['name']}"
            content_type = "application/pdf" if f["name"].endswith(".pdf") else "text/csv"
            try:
                r2().upload_file(str(path), R2_BUCKET, key, ExtraArgs={"ContentType": content_type})
                f["key"] = key
            except Exception as exc:
                log.write(f"\n[aviso] upload para o R2 falhou ({f['name']}): {exc}\n")
        result.append(f)
    return result


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


def run_job(job_id: str, pdf_path: Path, out_dir: Path, state: RunState, proxy_url: str) -> int:
    """Run traduzir.py under a pty so babeldoc's rich progress bars render
    (they carry per-stage counters we parse into state.progress). The log file
    gets progress frames throttled to ~1/s; other lines are kept verbatim."""
    cmd = [sys.executable, str(SCRIPT_DIR / "traduzir.py"), str(pdf_path),
           "--backend", "local", "--base-url", proxy_url, "--out", str(out_dir)]
    env = {**os.environ, "COLUMNS": "200", "LINES": "50", "TERM": "xterm-256color"}
    master, slave = pty.openpty()
    proc = subprocess.Popen(cmd, stdout=slave, stderr=slave, stdin=subprocess.DEVNULL,
                            cwd=SCRIPT_DIR, env=env, close_fds=True)
    os.close(slave)
    buffer = ""
    last_frame_write = 0.0
    with open(out_dir / "run.log", "w", encoding="utf-8") as log:
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
                        state.progress = max(state.progress or 0.0, pct)
                        if time.time() - last_frame_write < 1.0:
                            continue  # throttle: rich redraws many times/s
                        last_frame_write = time.time()
                    state.tail.append(line)
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
        with closing(db()) as conn:
            row = conn.execute(
                "SELECT id, filename FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
        if row is None:
            time.sleep(0.5)
            continue
        job_id, filename = row["id"], row["filename"]
        out_dir = job_out_dir(job_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        state = RunState()
        with RUNNING_LOCK:
            RUNNING[job_id] = state
        with closing(db()) as conn, conn:
            conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?",
                         (time.time(), job_id))

        status, error, files_json = "error", None, "[]"
        try:
            returncode = run_job(job_id, UPLOADS_DIR / job_id / filename, out_dir, state, proxy_url)
            files = collect_outputs(job_id)
            if returncode == 0 and files:
                with open(out_dir / "run.log", "a", encoding="utf-8") as log:
                    files_json = json.dumps(upload_outputs(job_id, files, log))
                status = "done"
            else:
                error = f"traduzir.py saiu com código {returncode} (ver log)"
        except Exception as exc:
            error = str(exc)
        with closing(db()) as conn, conn:
            conn.execute("UPDATE jobs SET status=?, error=?, files=?, finished_at=? WHERE id=?",
                         (status, error, files_json, time.time(), job_id))
        if status == "done":  # tail de erro fica em memória p/ o "ver log" da UI
            with RUNNING_LOCK:
                RUNNING.pop(job_id, None)


# ---------------------------------------------------------------- app

app = FastAPI(title="tradutor-artigos")

app.mount("/favicon", StaticFiles(directory=SCRIPT_DIR / "favicon"), name="favicon")

DOWNLOAD_PATH_RE = re.compile(r"^/api/jobs/[^/]+/files/[^/]+$")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # /llmproxy é uso interno do traduzir.py — nunca aceito de fora da máquina
    if path.startswith("/llmproxy/"):
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            return JSONResponse({"detail": "llmproxy é interno"}, status_code=403)
        return await call_next(request)
    if path.startswith("/api/"):
        # downloads usam URL assinada (o <a> do navegador não manda header)
        if request.method == "GET" and DOWNLOAD_PATH_RE.match(path):
            return await call_next(request)
        name, any_tokens = await run_in_threadpool(check_token, bearer_token(request))
        if name is None:
            detail = ("token inválido ou ausente" if any_tokens
                      else "nenhum token cadastrado — crie um com: python server.py token create <nome>")
            return JSONResponse({"detail": detail}, status_code=401)
        request.state.token_name = name
    return await call_next(request)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_FILE, media_type="text/html")


@app.get("/api/auth/check")
def auth_check(request: Request) -> dict:
    return {"ok": True, "name": request.state.token_name}


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
    (job_dir / name).write_bytes(data)

    with closing(db()) as conn, conn:
        conn.execute("INSERT INTO jobs (id, filename, created_at) VALUES (?, ?, ?)",
                     (job_id, name, time.time()))
    return {"id": job_id, "filename": name}


@app.get("/api/jobs")
def list_jobs() -> dict:
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100").fetchall()
    return {"jobs": [job_to_dict(r) for r in rows]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, detail="Job não encontrado")
    return job_to_dict(row)


@app.get("/api/jobs/{job_id}/files/{name}")
def download(job_id: str, name: str, exp: int = 0, sig: str = ""):
    if exp < time.time() or not hmac.compare_digest(sign_download(job_id, name, exp), sig):
        raise HTTPException(403, detail="link expirado ou inválido — recarregue a página")
    path = (job_out_dir(job_id) / Path(name).name).resolve()
    if not path.is_file() or job_out_dir(job_id).resolve() not in path.parents:
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


# ---------------------------------------------------------------- CLI de tokens

def token_cli(args: list[str]) -> None:
    global DOWNLOAD_SECRET
    DOWNLOAD_SECRET = init_db()
    usage = "uso: python server.py token {create <nome> | list | revoke <nome>}"
    action = args[0] if args else ""
    if action == "create" and len(args) == 2:
        token = secrets.token_urlsafe(32)
        try:
            with closing(db()) as conn, conn:
                conn.execute("INSERT INTO tokens (name, token_hash, created_at) VALUES (?, ?, ?)",
                             (args[1], hash_token(token), time.time()))
        except sqlite3.IntegrityError:
            sys.exit(f"[erro] já existe um token chamado {args[1]!r}")
        print(f"Token criado ({args[1]}) — guarde agora, ele não será mostrado de novo:\n\n  {token}\n")
    elif action == "list":
        with closing(db()) as conn:
            rows = conn.execute("SELECT name, created_at, revoked_at FROM tokens ORDER BY created_at").fetchall()
        if not rows:
            print("Nenhum token cadastrado.")
        for row in rows:
            state = "revogado" if row["revoked_at"] else "ativo"
            created = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["created_at"]))
            print(f"  {row['name']:<20} {state:<9} criado em {created}")
    elif action == "revoke" and len(args) == 2:
        with closing(db()) as conn, conn:
            n = conn.execute("UPDATE tokens SET revoked_at=? WHERE name=? AND revoked_at IS NULL",
                             (time.time(), args[1])).rowcount
        print(f"Token {args[1]!r} revogado." if n else f"[erro] token ativo {args[1]!r} não encontrado.")
    else:
        sys.exit(usage)


# ---------------------------------------------------------------- main

def main() -> None:
    global DOWNLOAD_SECRET
    if not FRONTEND_FILE.exists():
        sys.exit(f"[erro] Frontend não encontrado: {FRONTEND_FILE}")
    UPLOADS_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_SECRET = init_db()

    if R2_ENABLED:
        try:
            r2()
        except ImportError:
            sys.exit("[erro] R2 configurado no .env mas boto3 não está instalado (pip install boto3)")

    # jobs que estavam rodando quando o processo morreu não voltam sozinhos;
    # os 'queued' continuam na fila (o PDF de entrada está em uploads/)
    with closing(db()) as conn, conn:
        conn.execute(
            "UPDATE jobs SET status='error', error='interrompido por reinício do servidor', finished_at=? "
            "WHERE status='running'",
            (time.time(),),
        )

    _, any_tokens = check_token(None)
    if not any_tokens:
        print("[aviso] nenhum token de acesso cadastrado — crie um com: python server.py token create <nome>")

    threading.Thread(target=sampler_loop, daemon=True).start()
    threading.Thread(target=worker_loop, daemon=True).start()
    r2_note = "R2 ativo" if R2_ENABLED else "R2 desativado (arquivos servidos localmente)"
    print(f"Tradutor de Artigos: http://{HOST}:{PORT}  (LM Studio: {LOCAL_BASE_URL} | {r2_note})")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "token":
        token_cli(sys.argv[2:])
    else:
        main()

#!/usr/bin/env python3
"""
server.py — Web frontend do traduzir.py com autenticação por token,
persistência em SQLite, arquivos no Cloudflare R2 e métricas LM Studio/GPU.

Rodar:
  .venv/bin/python server.py             # http://127.0.0.1:8010 (dev)
  docker compose up -d --build           # produção no desktop (0.0.0.0:8010)

Tokens de acesso (a UI pede um token na entrada):
  python server.py token create <nome>                   # imprime o token UMA vez
  python server.py token create <nome> --token <valor>   # registra o token da outra máquina
  python server.py token list
  python server.py token revoke <nome>

Manutenção:
  python server.py r2 push               # sobe para o R2 as saídas que só existem em disco
  python server.py purge                 # zera histórico e arquivos locais (recusa o que
                                         # não está no R2; --force ignora)

Como funciona:
  - Cada máquina (desktop, mac) roda uma instância completa e independente deste
    servidor — fila, banco e LM Studio próprios. Quem junta as duas é o nginx da
    VPS, que roteia /n/<NODE_ID>/ para cada uma removendo o prefixo; o frontend
    descobre quem está no ar pelo GET /node e deixa escolher onde rodar o job.
  - Jobs e tokens ficam em SQLite (data/traduzai.db) — sobrevivem a restart.
  - Terminar uma tradução deixa os arquivos NA MÁQUINA; nada vai para o acervo
    sozinho. Mandar para o R2 é decisão explícita, pelo 'enviar para o acervo'
    (POST /api/jobs/<id>/archive) ou pelo 'server.py r2 push' em lote.
  - Download de arquivo que está na máquina sai dela com URL assinada (HMAC, 1h)
    — assinada porque links <a> não carregam o header Authorization. Já o que
    está no acervo baixa direto da Cloudflare, por URL pré-assinada do R2.
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
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
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

# Identidade deste nó. Cada máquina (desktop, mac) roda uma instância completa e
# independente; quem junta as duas é o nginx da VPS, que roteia /n/<id>/ para cada
# uma removendo o prefixo — por isso nada aqui precisa saber em que caminho é servido.
NODE_ID = cfg("NODE_ID", "local")
NODE_LABEL = cfg("NODE_LABEL") or NODE_ID

# Modelos ofertados no seletor do site (JSON no .env; ver .env.example). Cada
# entrada: {"id": <model key no LM Studio>, "label": ..., "context": tokens,
# "gpus": [[índices]]} — índices na ordem do nvidia-smi/LM Studio, e cada lista
# interna é uma combinação permitida (a primeira é o default). Sem MODEL_OPTIONS
# não há seletor e tudo se comporta como antes (LOCAL_MODEL direto, nenhum
# load/unload de modelo — caso do nó mac).
try:
    MODEL_OPTIONS: list[dict] = json.loads(cfg("MODEL_OPTIONS", "[]"))
except json.JSONDecodeError:
    print("MODEL_OPTIONS não é JSON válido — seletor de modelo desativado", file=sys.stderr)
    MODEL_OPTIONS = []

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
  finished_at REAL,
  model       TEXT,
  gpus        TEXT
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
        for col in ("model", "gpus"):  # bancos criados antes do seletor de modelo
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
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
    """Agrega todas as GPUs do nó: VRAM somada (pool total disponível para o
    LM Studio), utilização e temperatura pela pior placa (é ela o gargalo
    quando o modelo está dividido entre as duas)."""
    try:
        out = subprocess.run(
            [NVIDIA_SMI, "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        gpus = [[int(v.strip()) for v in line.split(",")]
                for line in out.stdout.strip().splitlines()]
        if not gpus:
            return None
        return {"util_pct": max(g[0] for g in gpus),
                "vram_used_mib": sum(g[1] for g in gpus),
                "vram_total_mib": sum(g[2] for g in gpus),
                "temp_c": max(g[3] for g in gpus)}
    except Exception:
        return None


_gpu_names: list[str] | None = None


def gpu_names() -> list[str]:
    """Nomes das GPUs na ordem dos índices (cacheado — não muda com o servidor
    de pé). Vira rótulo dos botões de placa no site."""
    global _gpu_names
    if _gpu_names is None:
        try:
            out = subprocess.run([NVIDIA_SMI, "--query-gpu=name", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=5)
            _gpu_names = [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]
        except Exception:
            _gpu_names = []
    return _gpu_names


_has_gpu: bool | None = None


def has_gpu() -> bool:
    """Cacheado: o /node é chamado como probe a cada poucos segundos por aba
    aberta e não pode disparar um nvidia-smi de cada vez."""
    global _has_gpu
    if _has_gpu is None:
        _has_gpu = read_gpu() is not None
    return _has_gpu


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
        "model": row["model"],
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


def strip_paths(files: list[dict]) -> list[dict]:
    """Tira o Path (não serializa em JSON) antes de guardar no banco.

    Terminar uma tradução NÃO manda nada para o acervo: quem decide é o Felipe,
    pelo botão 'enviar para o acervo'. Enquanto isso o arquivo fica na máquina e
    o download sai dela, por URL assinada."""
    for f in files:
        f.pop("path", None)
    return files


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


_LOADED_COMBO: tuple[str, tuple[int, ...]] | None = None


def ensure_model(model_id: str, gpu_idx: list[int], context: int | None, state: RunState) -> None:
    """Deixa o LM Studio com exatamente este modelo carregado nestas GPUs.

    Usa o SDK oficial (websocket, mesma porta do servidor) porque a API REST
    não expõe seleção de GPU. Descarrega o que estiver na memória antes — a
    fila é serial e a VRAM não comporta dois modelos. Com mais de uma placa o
    split é por prioridade (favorMainGpu na primeira da combinação), não
    'evenly': as placas são assimétricas e a sobra é que vai para a menor."""
    global _LOADED_COMBO
    combo = (model_id, tuple(gpu_idx))
    if _LOADED_COMBO == combo:
        return
    import lmstudio as lms  # import tardio: dependência só exercitada com MODEL_OPTIONS

    names = ", ".join(gpu_names()[i] if i < len(gpu_names()) else f"GPU {i}" for i in gpu_idx)
    state.tail.append(f"Carregando {model_id} ({names}) no LM Studio…")
    client = lms.Client(LMSTUDIO_ROOT.split("://", 1)[-1])
    try:
        for handle in client.llm.list_loaded():
            handle.unload()
        n_gpus = max(len(gpu_names()), max(gpu_idx) + 1)
        gpu: dict = {"ratio": 1.0,
                     "disabled_gpus": [i for i in range(n_gpus) if i not in gpu_idx]}
        if len(gpu_idx) > 1:
            gpu["split_strategy"] = "favorMainGpu"
            gpu["main_gpu"] = gpu_idx[0]
        config: dict = {"gpu": gpu}
        if context:
            config["context_length"] = context
        client.llm.load_new_instance(model_id, config=config)
        _LOADED_COMBO = combo
        state.tail.append("Modelo carregado.")
    finally:
        client.close()


def run_job(job_id: str, pdf_path: Path, out_dir: Path, state: RunState, proxy_url: str,
            model: str | None = None) -> int:
    """Run traduzir.py under a pty so babeldoc's rich progress bars render
    (they carry per-stage counters we parse into state.progress). The log file
    gets progress frames throttled to ~1/s; other lines are kept verbatim."""
    cmd = [sys.executable, str(SCRIPT_DIR / "traduzir.py"), str(pdf_path),
           "--backend", "local", "--base-url", proxy_url, "--out", str(out_dir)]
    if model:
        cmd += ["--model", model]
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
                "SELECT id, filename, model, gpus FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
        if row is None:
            time.sleep(0.5)
            continue
        job_id, filename, model = row["id"], row["filename"], row["model"]
        gpu_idx = json.loads(row["gpus"]) if row["gpus"] else []
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
            if model and gpu_idx:
                opt = next((o for o in MODEL_OPTIONS if o.get("id") == model), None)
                ensure_model(model, gpu_idx, (opt or {}).get("context"), state)
            returncode = run_job(job_id, UPLOADS_DIR / job_id / filename, out_dir, state, proxy_url,
                                 model=model)
            files = collect_outputs(job_id)
            if returncode == 0 and files:
                files_json = json.dumps(strip_paths(files))
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


@app.get("/node")
def node() -> dict:
    """Identidade do nó + probe de saúde. Sem auth de propósito: o frontend
    precisa saber quais máquinas estão no ar antes de haver um token na mão
    (e para mostrar o nó como offline em vez de fingir que ele não existe)."""
    info = {"id": NODE_ID, "label": NODE_LABEL, "gpu": has_gpu()}
    if MODEL_OPTIONS:
        info["models"] = [{"id": o.get("id"), "label": o.get("label") or o.get("id"),
                           "gpus": o.get("gpus", [])} for o in MODEL_OPTIONS]
        info["gpu_names"] = gpu_names()
    return info


@app.get("/api/auth/check")
def auth_check(request: Request) -> dict:
    return {"ok": True, "name": request.state.token_name}


def validate_model_choice(model: str | None, gpus: str | None) -> tuple[str | None, str | None]:
    """(model, gpus em JSON) validados contra MODEL_OPTIONS — ou (None, None)
    quando o nó não tem seletor / o cliente não mandou nada."""
    if not MODEL_OPTIONS or not model:
        return None, None
    opt = next((o for o in MODEL_OPTIONS if o.get("id") == model), None)
    if opt is None:
        raise HTTPException(400, detail="Modelo desconhecido — recarregue a página")
    combos = [sorted(c) for c in opt.get("gpus", [])]
    if gpus:
        try:
            req = sorted(int(x) for x in gpus.split(","))
        except ValueError:
            raise HTTPException(400, detail="Parâmetro de GPUs inválido")
        if combos and req not in combos:
            raise HTTPException(400, detail="Combinação de placas não permitida para este modelo")
    else:
        req = combos[0] if combos else []
    return model, json.dumps(req)


@app.post("/api/jobs", status_code=201)
async def create_job(file: UploadFile, model: str | None = Form(default=None),
                     gpus: str | None = Form(default=None)) -> dict:
    name = Path(file.filename or "").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, detail="Envie um arquivo .pdf")
    model, gpus_json = validate_model_choice(model, gpus)
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
        conn.execute("INSERT INTO jobs (id, filename, created_at, model, gpus) VALUES (?, ?, ?, ?, ?)",
                     (job_id, name, time.time(), model, gpus_json))
    return {"id": job_id, "filename": name, "model": model}


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


JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# nome de arquivo é digitado à mão: fora barras, controles e afins, deixa passar
# acento e espaço — quem vai ler é o Felipe, não o sistema de arquivos
UNSAFE_NAME_RE = re.compile(r"[^\w \-.()\[\]À-ÿ]", re.UNICODE)


def clean_base_name(raw: str) -> str:
    name = UNSAFE_NAME_RE.sub("", (raw or "").strip())
    return re.sub(r"\s+", " ", name).strip(" .")[:120]


def split_suffix(name: str) -> str:
    """O sufixo é o que identifica o arquivo no site ('PDF traduzido', 'bilíngue',
    'Glossário'), então renomear preserva ele e mexe só no nome-base."""
    for suffix, _ in FILE_LABELS:
        if name.endswith(suffix):
            return suffix
    return Path(name).suffix


def r2_job_keys(job_id: str) -> list[dict]:
    keys = []
    for page in r2().get_paginator("list_objects_v2").paginate(
            Bucket=R2_BUCKET, Prefix=f"jobs/{job_id}/"):
        keys += page.get("Contents", [])
    return keys


@app.post("/api/jobs/{job_id}/archive")
def archive_job(job_id: str) -> dict:
    """Sobe as saídas do job para o R2 e some com ele desta máquina.

    A ordem importa: só apaga depois que TODOS os arquivos subiram. Falha no meio
    deixa tudo como estava, para arquivar nunca virar perda de tradução."""
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(400, detail="identificador de trabalho inválido")
    if not R2_ENABLED:
        raise HTTPException(503, detail="R2 não configurado nesta máquina")
    with closing(db()) as conn:
        row = conn.execute("SELECT status, files FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, detail="Trabalho não encontrado")
    if row["status"] != "done":
        raise HTTPException(400, detail="só dá para arquivar trabalho concluído")

    files = json.loads(row["files"])
    try:
        for f in files:
            if f.get("key"):
                continue
            path = job_out_dir(job_id) / f["name"]
            if not path.is_file():
                raise HTTPException(409, detail=f"arquivo sumiu do disco: {f['name']}")
            key = f"jobs/{job_id}/{f['name']}"
            content_type = "application/pdf" if f["name"].endswith(".pdf") else "text/csv"
            r2().upload_file(str(path), R2_BUCKET, key, ExtraArgs={"ContentType": content_type})
            f["key"] = key
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, detail=f"falha ao subir para o R2: {exc}")

    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    for path in (job_out_dir(job_id), UPLOADS_DIR / job_id):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    return {"archived": len(files)}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    """Tira o trabalho desta máquina: histórico e arquivos em disco.

    Não encosta no R2 de propósito — trabalho e acervo são coisas separadas. O
    que já foi para o acervo vive lá e só sai por lá (DELETE /api/files/{id}).
    Aqui é descarte do que está na máquina: 'enviar para o acervo' guarda antes
    de liberar, 'excluir' joga fora."""
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(400, detail="identificador de trabalho inválido")
    with closing(db()) as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, detail="Trabalho não encontrado")
    if row["status"] == "running":
        raise HTTPException(409, detail="trabalho em andamento — espere terminar")

    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    for path in (job_out_dir(job_id), UPLOADS_DIR / job_id):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    return {"deleted": job_id}


@app.post("/api/files/{job_id}/name")
async def rename_files(job_id: str, request: Request) -> dict:
    """Troca o nome-base dos arquivos de um job no R2. Renomear em S3 é cópia +
    remoção, mas a cópia é do lado do servidor: o conteúdo não trafega."""
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(400, detail="identificador de trabalho inválido")
    if not R2_ENABLED:
        raise HTTPException(503, detail="R2 não configurado nesta máquina")
    body = await request.json()
    base = clean_base_name(body.get("name", "") if isinstance(body, dict) else "")
    if not base:
        raise HTTPException(400, detail="nome vazio ou só com caracteres inválidos")

    try:
        objects = r2_job_keys(job_id)
        if not objects:
            raise HTTPException(404, detail="nada no acervo para este trabalho")
        renamed = []
        for obj in objects:
            old = obj["Key"]
            new = f"jobs/{job_id}/{base}{split_suffix(old.rsplit('/', 1)[-1])}"
            if new == old:
                renamed.append(old)
                continue
            r2().copy_object(Bucket=R2_BUCKET, CopySource={"Bucket": R2_BUCKET, "Key": old}, Key=new)
            r2().delete_object(Bucket=R2_BUCKET, Key=old)
            renamed.append(new)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, detail=f"falha ao renomear no R2: {exc}")
    return {"name": base, "files": [k.rsplit("/", 1)[-1] for k in renamed]}


@app.delete("/api/files/{job_id}")
def delete_files(job_id: str) -> dict:
    """Apaga do R2 tudo de um job — é o que sustenta o acervo do site.

    Vive nos nós, e não no hub, de propósito: só as máquinas têm credencial de
    escrita no bucket. O hub, que é a peça exposta na internet, segue apenas com
    leitura e não consegue destruir nada. O custo é precisar de uma máquina
    ligada para apagar, o que é aceitável para uma operação não urgente."""
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(400, detail="identificador de trabalho inválido")
    if not R2_ENABLED:
        raise HTTPException(503, detail="R2 não configurado nesta máquina")

    prefix = f"jobs/{job_id}/"
    keys = []
    try:
        for page in r2().get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix=prefix):
            keys += [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            r2().delete_objects(Bucket=R2_BUCKET, Delete={"Objects": keys})
    except Exception as exc:
        raise HTTPException(502, detail=f"falha ao apagar no R2: {exc}")

    # se este nó também tiver o job no histórico/disco, some com ele junto —
    # senão sobraria um item na lista apontando para arquivo que não existe mais
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    for path in (job_out_dir(job_id), UPLOADS_DIR / job_id):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    return {"deleted": len(keys)}


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

def r2_push_cli() -> None:
    """Sobe para o R2 as saídas de jobs concluídos que ainda só existem em disco.
    Serve para migrar o que foi traduzido antes de o R2 existir, e é idempotente:
    arquivo que já tem chave é pulado."""
    if not R2_ENABLED:
        sys.exit("[erro] R2 não configurado no .env (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, ...)")
    with closing(db()) as conn:
        rows = conn.execute("SELECT id, filename, files FROM jobs WHERE status='done'").fetchall()

    sent = skipped = missing = 0
    for row in rows:
        files = json.loads(row["files"])
        changed = False
        for f in files:
            if f.get("key"):
                skipped += 1
                continue
            path = job_out_dir(row["id"]) / f["name"]
            if not path.is_file():
                print(f"  [aviso] sumiu do disco: {row['id']}/{f['name']}")
                missing += 1
                continue
            key = f"jobs/{row['id']}/{f['name']}"
            content_type = "application/pdf" if f["name"].endswith(".pdf") else "text/csv"
            r2().upload_file(str(path), R2_BUCKET, key, ExtraArgs={"ContentType": content_type})
            f["key"] = key
            changed = True
            sent += 1
            print(f"  ↑ {key}")
        if changed:
            with closing(db()) as conn, conn:
                conn.execute("UPDATE jobs SET files=? WHERE id=?", (json.dumps(files), row["id"]))
    print(f"\n{sent} enviado(s), {skipped} já estavam no R2"
          + (f", {missing} sem arquivo em disco" if missing else ""))


def purge_cli(force: bool) -> None:
    """Zera o histórico de jobs e os arquivos locais desta máquina. Recusa apagar
    o que ainda não está no R2 — senão a limpeza vira perda de tradução."""
    with closing(db()) as conn:
        rows = conn.execute("SELECT id, status, files FROM jobs").fetchall()
    at_risk = [r["id"] for r in rows
               if r["status"] == "done" and any(not f.get("key") for f in json.loads(r["files"]))]
    if at_risk and not force:
        sys.exit(f"[erro] {len(at_risk)} job(s) concluído(s) têm arquivo fora do R2 "
                 f"({', '.join(at_risk[:3])}{'…' if len(at_risk) > 3 else ''}).\n"
                 f"       Rode 'python server.py r2 push' antes, ou use --force para apagar assim mesmo.")

    with closing(db()) as conn, conn:
        removed = conn.execute("DELETE FROM jobs").rowcount
    freed = 0
    for base in (OUTPUT_DIR, UPLOADS_DIR):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            freed += sum(f.stat().st_size for f in child.rglob("*") if f.is_file()) if child.is_dir() else child.stat().st_size
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    print(f"{removed} job(s) removido(s) do histórico; {freed / 1024 / 1024:.1f} MB liberados em disco.")


def token_cli(args: list[str]) -> None:
    global DOWNLOAD_SECRET
    DOWNLOAD_SECRET = init_db()
    usage = ("uso: python server.py token {create <nome> [--token <valor>] | list | revoke <nome>}\n"
             "  --token: registra um token existente em vez de sortear um novo — é assim que\n"
             "           o mesmo token passa a valer nos dois nós (desktop e mac).")
    action = args[0] if args else ""
    if action == "create" and (len(args) == 2 or (len(args) == 4 and args[2] == "--token")):
        given = args[3] if len(args) == 4 else ""
        token = given or secrets.token_urlsafe(32)
        try:
            with closing(db()) as conn, conn:
                conn.execute("INSERT INTO tokens (name, token_hash, created_at) VALUES (?, ?, ?)",
                             (args[1], hash_token(token), time.time()))
        except sqlite3.IntegrityError:
            # o nome só fica reservado enquanto o token está ativo: revogado pode
            # ser reaproveitado, senão revogar queimaria o nome para sempre
            with closing(db()) as conn, conn:
                freed = conn.execute("DELETE FROM tokens WHERE name=? AND revoked_at IS NOT NULL",
                                     (args[1],)).rowcount
                if not freed:
                    sys.exit(f"[erro] já existe um token ATIVO chamado {args[1]!r} — revogue antes")
                conn.execute("INSERT INTO tokens (name, token_hash, created_at) VALUES (?, ?, ?)",
                             (args[1], hash_token(token), time.time()))
        if given:
            print(f"Token {args[1]!r} registrado neste nó ({NODE_ID}).")
        else:
            print(f"Token criado ({args[1]}) — guarde agora, ele não será mostrado de novo:\n\n  {token}\n"
                  f"\nPara valer também na outra máquina, rode lá:\n"
                  f"  python server.py token create {args[1]} --token {token}\n")
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
    print(f"Tradutor de Artigos [nó: {NODE_ID}]: http://{HOST}:{PORT}  "
          f"(LM Studio: {LOCAL_BASE_URL} | {r2_note})")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "token":
        token_cli(sys.argv[2:])
    elif cmd == "r2" and sys.argv[2:3] == ["push"]:
        DOWNLOAD_SECRET = init_db()
        r2_push_cli()
    elif cmd == "purge":
        DOWNLOAD_SECRET = init_db()
        purge_cli("--force" in sys.argv)
    elif cmd in ("r2", "purge"):
        sys.exit("uso: python server.py {r2 push | purge [--force]}")
    else:
        main()

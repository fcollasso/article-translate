#!/usr/bin/env python3
"""
hub.py — a peça sempre ligada do traduzia. Roda na VPS, não traduz nada.

Faz duas coisas:
  1. serve o frontend (um lugar só: some a divergência entre as máquinas e o
     site nunca depende de um PC estar ligado para abrir);
  2. lista e assina os arquivos direto do Cloudflare R2, para os downloads
     funcionarem com desktop e Mac desligados.

Enviar um artigo novo continua exigindo um nó no ar — a tradução (fila, pdf2zh,
LM Studio) vive nas máquinas, em /n/<id>/, e o nginx roteia para lá.

Rodar:
  docker compose up -d --build          # na VPS
  python hub.py hash <token>            # gera o hash para HUB_TOKEN_HASHES

Configuração (env):
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
  HUB_TOKEN_HASHES  — sha256 dos tokens aceitos, separados por vírgula. Guardamos
                      só o hash: o token em si continua existindo apenas nos nós
                      e no navegador do Felipe.
"""

import hashlib
import hmac
import os
import re
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

APP_DIR = Path(__file__).resolve().parent
FRONTEND_FILE = APP_DIR / "frontend" / "index.html"
FAVICON_DIR = APP_DIR / "favicon"

HOST = os.environ.get("HUB_HOST", "0.0.0.0")
PORT = int(os.environ.get("HUB_PORT", "8020"))
NODE_LABEL = os.environ.get("NODE_LABEL", "Servidor")

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
# Normalmente derivado do account id; existe como override para apontar o hub a
# outro storage S3 (foi assim que a listagem/assinatura foi testada contra um
# MinIO local, sem credenciais reais da Cloudflare).
R2_ENDPOINT = os.environ.get("R2_ENDPOINT") or f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
R2_ENABLED = all((R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET))

TOKEN_HASHES = [h.strip().lower() for h in os.environ.get("HUB_TOKEN_HASHES", "").split(",") if h.strip()]

DOWNLOAD_URL_TTL = 3600
LIST_CACHE_TTL = 20.0  # s — o R2 cobra por listagem; o frontend faz poll
MAX_JOBS = 200

# mesmos rótulos do server.py dos nós, para a lista ficar igual nos dois lugares
FILE_LABELS = [
    (".mono.pdf", "PDF traduzido (mono)"),
    (".dual.pdf", "PDF bilíngue (dual)"),
    (".glossary.csv", "Glossário (CSV)"),
]
KEY_RE = re.compile(r"^jobs/(?P<job>[^/]+)/(?P<name>.+)$")


def label_for(name: str) -> str:
    for suffix, label in FILE_LABELS:
        if name.endswith(suffix):
            return label
    return name


# ---------------------------------------------------------------- auth

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_token(token: str | None) -> bool:
    if not token or not TOKEN_HASHES:
        return False
    h = hash_token(token)
    ok = False
    for known in TOKEN_HASHES:  # percorre todos — comparação em tempo constante
        if hmac.compare_digest(known, h):
            ok = True
    return ok


def bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


# ---------------------------------------------------------------- R2

_r2_client = None
_r2_lock = threading.Lock()


def r2():
    global _r2_client
    with _r2_lock:
        if _r2_client is None:
            import boto3

            _r2_client = boto3.client(
                "s3",
                endpoint_url=R2_ENDPOINT,
                aws_access_key_id=R2_ACCESS_KEY_ID,
                aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                region_name="auto",
            )
    return _r2_client


_cache: dict = {"at": 0.0, "jobs": []}
_cache_lock = threading.Lock()


def list_jobs() -> list[dict]:
    """Agrupa as chaves jobs/<id>/<arquivo> do bucket. Cacheado por alguns
    segundos porque o frontend faz poll e listagem no R2 é cobrada."""
    with _cache_lock:
        if time.time() - _cache["at"] < LIST_CACHE_TTL:
            return _cache["jobs"]

    jobs: dict[str, dict] = {}
    paginator = r2().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix="jobs/"):
        for obj in page.get("Contents", []):
            m = KEY_RE.match(obj["Key"])
            if not m:
                continue
            job = jobs.setdefault(m.group("job"), {"id": m.group("job"), "modified": 0.0, "files": []})
            modified = obj["LastModified"].timestamp()
            job["modified"] = max(job["modified"], modified)
            job["files"].append({
                "key": obj["Key"],
                "name": m.group("name"),
                "label": label_for(m.group("name")),
                "size": obj["Size"],
            })

    result = sorted(jobs.values(), key=lambda j: j["modified"], reverse=True)[:MAX_JOBS]
    for job in result:
        job["files"].sort(key=lambda f: f["name"])
    with _cache_lock:
        _cache["at"] = time.time()
        _cache["jobs"] = result
    return result


def presign(key: str, filename: str) -> str:
    return r2().generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET, "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"'},
        ExpiresIn=DOWNLOAD_URL_TTL,
    )


# ---------------------------------------------------------------- app

app = FastAPI(title="traduzia-hub")

if FAVICON_DIR.is_dir():
    app.mount("/favicon", StaticFiles(directory=FAVICON_DIR), name="favicon")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # /api/hub/node é o probe de saúde: precisa responder antes de haver token
    if path.startswith("/api/hub/") and path != "/api/hub/node":
        if not await run_in_threadpool(check_token, bearer_token(request)):
            detail = ("token inválido ou ausente" if TOKEN_HASHES
                      else "nenhum token configurado no hub (HUB_TOKEN_HASHES)")
            return JSONResponse({"detail": detail}, status_code=401)
    return await call_next(request)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_FILE, media_type="text/html")


@app.get("/api/hub/node")
def node() -> dict:
    """Identidade do hub. O frontend usa isto para saber que o servidor está no
    ar mesmo quando nenhuma máquina de tradução está."""
    return {"id": "hub", "label": NODE_LABEL, "r2": R2_ENABLED}


@app.get("/api/hub/auth/check")
def auth_check() -> dict:
    return {"ok": True}


@app.get("/api/hub/files")
def files() -> dict:
    """Tudo que já foi traduzido, direto do R2 — independe dos PCs."""
    if not R2_ENABLED:
        raise HTTPException(503, detail="R2 não configurado no hub")
    try:
        jobs = list_jobs()
    except Exception as exc:
        raise HTTPException(502, detail=f"falha ao listar o R2: {exc}")
    return {"jobs": [
        {
            "id": job["id"],
            "modified": job["modified"],
            "files": [
                {"name": f["name"], "label": f["label"], "size": f["size"],
                 "url": presign(f["key"], f["name"])}
                for f in job["files"]
            ],
        }
        for job in jobs
    ]}


def main() -> None:
    if not FRONTEND_FILE.exists():
        sys.exit(f"[erro] frontend não encontrado: {FRONTEND_FILE}")
    if not TOKEN_HASHES:
        print("[aviso] HUB_TOKEN_HASHES vazio — ninguém consegue entrar. "
              "Gere com: python hub.py hash <token>")
    if not R2_ENABLED:
        print("[aviso] R2 não configurado — a aba de arquivos fica indisponível")
    print(f"traduzia hub: http://{HOST}:{PORT}  (R2: {'ok' if R2_ENABLED else 'off'})")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "hash":
        print(hash_token(sys.argv[2]))
    else:
        main()

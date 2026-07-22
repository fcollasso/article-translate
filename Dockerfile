# Backend do traduzia.com.br — roda no desktop (Docker Desktop / WSL2).
# A inferência fica FORA do container, no LM Studio do Windows
# (host.docker.internal); aqui só roda o pdf2zh + servidor web.
FROM python:3.12-slim

# onnxruntime (DocLayout-YOLO do babeldoc) precisa de libgomp
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# fastapi/uvicorn/httpx já vêm como dependências do pdf2zh-next; boto3 é do R2
RUN pip install --no-cache-dir pdf2zh-next==2.9.0 boto3 python-multipart

# pré-baixa os assets do babeldoc (DocLayout-YOLO, fontes, CMaps) na imagem.
# o --warmup da 2.9.0 termina com um AssertionError cosmético — por isso o || true
RUN pdf2zh_next --warmup || true

WORKDIR /app
COPY traduzir.py server.py ./
COPY frontend ./frontend
COPY favicon ./favicon
COPY patches ./patches

ENV FRONTEND_HOST=0.0.0.0
EXPOSE 8010
CMD ["python", "server.py"]

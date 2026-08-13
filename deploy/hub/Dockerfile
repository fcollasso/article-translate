# Hub do traduzia — roda na VPS (187.77.195.108), atrás do quark-nginx.
# Imagem enxuta de propósito: aqui não entra pdf2zh nem nada de tradução, só o
# frontend e o acesso ao R2. Build a partir da RAIZ do repo:
#   docker compose -f deploy/hub/docker-compose.yml up -d --build
FROM python:3.12-slim

RUN pip install --no-cache-dir fastapi uvicorn boto3

WORKDIR /app
COPY deploy/hub/hub.py ./
COPY frontend ./frontend
COPY favicon ./favicon

EXPOSE 8020
CMD ["python", "hub.py"]

# ESTADO.md — tradutor-artigos

## Sessão atual: 2026-07-14 (setup da máquina via Claude Code, MacBook)

### Feito
- `uv` 0.11.28 instalado via brew; `pdf2zh-next` 2.9.0 instalado via `uv tool install --python 3.12`
- Todos os 14 flags usados no `traduzir.py` conferidos contra `pdf2zh_next -h` da 2.9.0 — todos existem
- `.env` criado a partir do `.env.example` (backend local, qwen3-14b; `GEMINI_API_KEY` ainda vazio)
- Smoke test `--dry-run` nos dois backends: ok (gemini falha corretamente sem key)
- Fix no `traduzir.py`: `OUTPUT_DIR` relativo vindo do `.env` agora é ancorado na pasta do projeto (antes resolvia contra o CWD de quem chamava)
- Repo git já existia com commit inicial (veio do bootstrap); `.env` confirmado como ignorado
- Paper de teste baixado: `artigos/2511.15247.pdf` (6 págs, PINNs) — pasta `artigos/` adicionada ao `.gitignore`
- `pdf2zh_next --warmup` rodado para pré-baixar o modelo de layout (DocLayout-YOLO) — cache ok em `~/.cache/babeldoc` (337 MB). Obs: `--warmup` termina com AssertionError inofensivo ("At least one input file is required") depois de baixar tudo — bug cosmético da CLI 2.9.0
- LM Studio instalado via `brew install --cask lm-studio`; CLI `lms` em `~/.lmstudio/bin/lms`
- Qwen3 14B MLX baixado (8,3 GB): id exato `qwen/qwen3-14b` — `.env` atualizado com esse id
- Sanity test da API: tradução correta, mas thinking do Qwen3 gastou 322 tokens de raciocínio vs 36 de resposta → adicionado no traduzir.py: `/no_think` via `--custom-system-prompt` quando o modelo local é Qwen (replica o role block padrão do babeldoc, ver `_build_role_block` em il_translator.py)
- Teste real do paper foi iniciado mas **interrompido por reboot do Mac** — `output/` está vazia, precisa rodar de novo

### Como retomar o teste interrompido
```bash
~/.lmstudio/bin/lms server start
~/.lmstudio/bin/lms load qwen/qwen3-14b -c 8192 --parallel 2 -y
python3 traduzir.py artigos/2511.15247.pdf
```

## Sessão anterior: 2026-07-14 (bootstrap via Claude.ai)
- Pesquisa: decidido usar pdf2zh-next (PDFMathTranslate 2.0) como motor, em vez de implementar extração/reconstrução de PDF do zero
- Criados: traduzir.py (wrapper batch, 2 backends, dry-run), .env.example, README.md, CLAUDE.md

### Próximos passos
1. Carregar Qwen3 14B (build MLX) no LM Studio e iniciar o servidor (Developer → Start Server, porta 1234); conferir o model id exato e ajustar `LOCAL_MODEL` no `.env` se diferir de `qwen3-14b`
2. Teste real: `python3 traduzir.py artigos/2511.15247.pdf` e comparar com a versão do Linnk que já temos
3. Colar `GEMINI_API_KEY` no `.env` (https://aistudio.google.com/apikey) e repetir o teste com `--backend gemini` (QPS=1)

### Decisões em aberto
- Qual modelo local vence no benchmark (Qwen3 14B vs Gemma 3 12B no MacBook)
- Se o desktop RTX 3050 terá LM Studio próprio (8B) ou só consome o servidor do Mac via Tailscale

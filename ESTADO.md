# ESTADO.md — tradutor-artigos

## Sessão atual: 2026-07-14 (tarde — projeto migra para o desktop)

### Decisão
Rodar inferência local no MacBook (24 GB) se mostrou inviável: o Qwen3 14B consome
a máquina inteira e chegou a coincidir com um reboot no meio do teste. **O projeto
passa a rodar no desktop (i9-10900F + RTX 3050 8GB + 64GB RAM).** CLAUDE.md atualizado.

### Feito (limpeza do MacBook)
- LM Studio desinstalado (`brew uninstall --cask --zap`), `~/.lmstudio` removido (9 GB, incluía o Qwen3 14B), linha de PATH do lms removida do `~/.zshrc`
- `pdf2zh-next` desinstalado (`uv tool uninstall`), caches removidos: `~/.cache/babeldoc` (337 MB), `~/.config/pdf2zh`
- `uv` desinstalado (brew) + `~/.local/share/uv` e `~/.cache/uv` removidos (~1,9 GB)
- Mantidos no Mac: o repositório em si e `artigos/2511.15247.pdf` (gitignored); o `.env` local ficou (inofensivo, sem key)

### Aprendizados da sessão da manhã (valem para o desktop)
- Flags do `traduzir.py` conferidos ok contra pdf2zh-next 2.9.0
- `pdf2zh_next --warmup` pré-baixa os assets de layout; termina com AssertionError inofensivo ("At least one input file is required") — bug cosmético da 2.9.0
- `LOCAL_MODEL` precisa ser o id exato que o LM Studio expõe (ex.: `qwen/qwen3-14b`, com namespace) — conferir em `curl localhost:1234/v1/models`
- Thinking do Qwen3 gasta ~10x mais tokens que a tradução; o `traduzir.py` já injeta `/no_think` automaticamente quando o modelo é Qwen

### Feito (setup no desktop, via Claude Code/WSL2)
- Repo já estava em `/mnt/g/article-translate` (drive G: montado no WSL)
- `.venv` criado no projeto com o Python 3.12.3 do sistema; `pdf2zh-next 2.9.0` instalado via pip (sem uv); `.venv/` no `.gitignore`. Obs: instalar em DrvFs (G:) é lento (~10 min) mas funciona
- LM Studio roda no **Windows** (CLI: `/mnt/c/Users/fcoll/.lmstudio/bin/lms.exe`). O servidor estava preso em `127.0.0.1` e o WSL não alcançava — reiniciado com `--bind 0.0.0.0`. **Não confirmado se o bind persiste entre restarts do LM Studio; se der connection timeout, rodar `lms.exe server start --bind 0.0.0.0` de novo**
- Rotas WSL→Windows testadas ok: LAN `http://192.168.1.2:1234/v1` (usada no `.env`) e Tailscale `http://100.98.187.95:1234/v1` (alternativa estável). O IP do gateway vEthernet (192.168.48.x) muda entre reboots — não usar
- **Qwen3 8B Q4_K_M GGUF** baixado (5.03 GB, id exato `qwen/qwen3-8b`) e carregado (4.68 GiB, cabe na VRAM). A máquina já tinha `openai/gpt-oss-20b` (12 GB) — candidato a comparação futura
- Sanity test da API a partir do WSL: tradução EN→PT-BR correta, `/no_think` ok (0 reasoning tokens)
- `.env` criado: backend local, `qwen/qwen3-8b`, `LOCAL_QPS=2`/`LOCAL_WORKERS=2` (conservador p/ a 3050; subir se aguentar)
- Paper de teste re-baixado: `artigos/2511.15247.pdf`
- `--dry-run` ok nos dois backends (gemini falha corretamente sem key)
- `pdf2zh_next --warmup` rodado (DocLayout-YOLO em `~/.cache/babeldoc` do WSL)

### Como rodar
```bash
# Se o LM Studio reiniciou (bind + modelo):
/mnt/c/Users/fcoll/.lmstudio/bin/lms.exe server start --bind 0.0.0.0
/mnt/c/Users/fcoll/.lmstudio/bin/lms.exe load qwen/qwen3-8b -c 8192 --parallel 2 -y

# Traduzir:
.venv/bin/python traduzir.py artigos/2511.15247.pdf
```

- Fix no `traduzir.py`: o binário `pdf2zh_next` agora é resolvido ao lado do Python em execução (venv) antes de cair no PATH — no Mac ele estava no PATH via uv tool, aqui não
- **Teste real concluído**: `.venv/bin/python traduzir.py artigos/2511.15247.pdf` → `output/2511.15247.pt-BR.mono.pdf` (16 págs) + `.dual.pdf` + `.glossary.csv` em ~46 min (Qwen3 8B, QPS=2/workers=2). Amostra do texto ok em pt-BR. O pdf2zh gera um glossário CSV automaticamente — útil p/ o item de glossário do backlog

### Frontend web (mesma sessão, tarde)
- Qualidade do Qwen3 8B aprovada pelo Felipe → **Gemini adiado** (fica no backlog)
- `LOCAL_QPS`/`LOCAL_WORKERS` subidos para 4. Decisão: **não** aumentar `--parallel` do LM Studio — VRAM já fica ~91% com o modelo carregado; slots extras arriscam estourar p/ RAM compartilhada. Com QPS 4 a GPU trabalha a ~93% durante o job (antes ficava ociosa entre lotes)
- Criado `server.py` (FastAPI/uvicorn — já vinham no venv de carona com o pdf2zh): serve o frontend, fila de jobs (1 por vez), upload/download, e um **proxy LLM interno** (`/llmproxy/v1`) — o traduzir.py é chamado com `--base-url` apontando pro proxy, que repassa ao LM Studio registrando tokens e duração de cada requisição. GPU via `nvidia-smi` (funciona no WSL), info do modelo via API nativa `/api/v0/models` do LM Studio
- Criado `frontend/index.html` (single-file, sem dependências, feito por agente de design seguindo a skill dataviz): drag-and-drop de PDF, lista de jobs com log ao vivo, downloads, dashboard com contadores LLM + sparklines de tokens/s, GPU, VRAM e temperatura
- Rodar: `.venv/bin/python server.py` → **http://localhost:8010** (o navegador do Windows alcança o localhost do WSL). Porta configurável via `FRONTEND_PORT` no `.env`
- Teste e2e: upload via API ok, rejeição de não-PDF ok, job real disparado com métricas ao vivo (4 req ativas, GPU 93%, ~28 tok/s por requisição)

### Próximos passos
1. Comparar `output/2511.15247.pt-BR.mono.pdf` com a versão do Linnk
2. Usar o frontend no dia a dia; ajustar o que incomodar
3. (futuro) `GEMINI_API_KEY` no `.env` e testar `--backend gemini` (QPS=1)

### Decisões em aberto
- Qual modelo local vence no benchmark no desktop (Qwen3 8B vs Gemma 3 4B; 12B+ não cabe na VRAM)
- Se vale usar o Gemini para artigos urgentes/qualidade máxima (free tier dá conta de poucos artigos/dia)

## Sessão 2026-07-14 (manhã — setup no MacBook, depois desfeito)
- uv + pdf2zh-next 2.9.0 instalados e validados; LM Studio + Qwen3 14B MLX (id `qwen/qwen3-14b`)
- Fix: `OUTPUT_DIR` relativo do `.env` ancorado na pasta do projeto (commit `e904235`)
- Fix: `/no_think` automático para modelos Qwen no backend local (commit `88c9643`)
- Teste real interrompido por reboot do Mac; ambiente inteiro desinstalado em seguida (ver sessão atual)

## Sessão 2026-07-14 (bootstrap via Claude.ai)
- Pesquisa: decidido usar pdf2zh-next (PDFMathTranslate 2.0) como motor, em vez de implementar extração/reconstrução de PDF do zero
- Criados: traduzir.py (wrapper batch, 2 backends, dry-run), .env.example, README.md, CLAUDE.md

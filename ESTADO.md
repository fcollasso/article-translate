# ESTADO.md — tradutor-artigos

## Sessão atual: 2026-07-14 (noite — publicação em traduzia.com.br)

### Decisão
Felipe comprou **traduzia.com.br** (Hostinger) e o app vai para a web: o nginx da VPS
de projetos (187.77.195.108, o mesmo do quark/luppai) faz TLS e proxy via Tailscale até
o backend no desktop. Autenticação = token de acesso (estilo API key) com tela de login;
storage dos PDFs no Cloudflare R2 (free tier); **SQLite** (não Postgres) para tokens + jobs.

### Feito (código — no Mac, testado com venv 3.12 descartável)
- `server.py`: auth por Bearer token (SHA-256 em SQLite, comparação constant-time, CLI
  `python server.py token create|list|revoke`), jobs persistidos em SQLite (`data/traduzai.db`,
  WAL; no restart, 'running' vira erro "interrompido" e 'queued' retoma sozinho), saídas sobem
  pro R2 (boto3, import tardio) com download por URL pré-assinada direto da Cloudflare, fallback
  local com URL assinada HMAC de 1h (links `<a>` não mandam header Authorization), `/llmproxy`
  restrito a localhost, bind configurável via `FRONTEND_HOST`. O progresso via pty do desktop
  foi preservado intacto (agora em `RunState`, memória)
- `frontend/index.html`: tela de login (token em localStorage `traduzai.token`), Authorization
  em todo fetch, 401 → volta pro login, botão "sair", downloads re-apontam o href a cada poll
  (URLs assinadas expiram). Design system intocado
- `Dockerfile` + `docker-compose.yml` (desktop): python:3.12-slim + pdf2zh-next 2.9.0 + boto3,
  warmup do babeldoc na build da imagem, `gpus: all` só p/ métricas (inferência continua no LM
  Studio do Windows via `host.docker.internal`), DB em volume nomeado (WAL não é confiável em
  bind mount DrvFs), uploads/output em bind mount normal
- `deploy/nginx/traduzia.conf` (vhost definitivo) + `traduzia.http-only.conf` (bootstrap ACME)
- Testes que passaram: 401 sem token / token errado, login/check, upload+fila+worker (erro
  esperado sem pdf2zh no Mac, com log), download assinado sem auth + tamper/expiração → 403,
  revoke → 401 imediato, recovery pós-restart, sintaxe JS via node --check

### Feito (VPS + DNS — https://traduzia.com.br já responde)
- DNS na Hostinger apontado p/ `187.77.195.108` (feito pelo Felipe durante a sessão)
- Certificado Let's Encrypt emitido (traduzia.com.br + www, expira 2026-10-12; renovação =
  mesmo esquema dos outros domínios da VPS)
- Vhost definitivo ativo em `/root/quark/nginx/conf.d/traduzia.conf` (cópia versionada em
  `deploy/nginx/traduzia.conf`): TLS ok, HTTP→HTTPS 301 ok, `/llmproxy` → 403 ok.
  **A raiz dá 504 até o desktop conectar** (esperado: falta Tailscale + backend)
- Tailscale 1.98.8 instalado — **pendente autorização do Felipe** (link nas pendências)
- Regra MASQUERADE docker→tailnet + unit systemd `tailscale-docker-masq` (persistente)

### Pendências (nesta ordem)
1. **Commit/push deste repo** (as mudanças estão no Mac, não comitadas)
2. **Tailscale**: autorizar a VPS no tailnet: https://login.tailscale.com/a/1b00baa401f9a2
   (depois, no admin do Tailscale, desativar key expiry da VPS)
3. **Desktop**: `git pull`; instalar Docker Desktop (WSL2) se não tiver; preencher R2_* no `.env`
   (bucket `traduzia` criado no painel Cloudflare > R2 — opcional, funciona sem); 
   `docker compose up -d --build`;
   `docker compose exec traduzia python server.py token create felipe`; se der timeout de fora,
   liberar a porta 8010 no firewall do Windows
4. Teste e2e: https://traduzia.com.br → login → traduzir um paper → conferir download saindo do R2

### Comandos de referência
```bash
# validar a rota VPS→desktop (depois do Tailscale autorizado e do compose up no desktop):
curl -s http://100.98.187.95:8010/ | head -c 100                              # do host da VPS
docker exec quark-nginx wget -qO- http://100.98.187.95:8010/ | head -c 100    # do container
```

### Notas
- A marca na UI segue "**traduzai.**" (nome do design system); o domínio é tradu**zia**.com.br —
  decidir se rebatiza a UI ou se fica assim
- Sem R2 configurado tudo continua funcionando: os downloads saem do próprio desktop
- Renovação do certificado: mesmo esquema dos outros domínios da VPS

## Sessão 2026-07-14 (tarde — projeto migra para o desktop)

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
- Teste e2e concluído: upload via API ok, rejeição de não-PDF ok, downloads ok. **O mesmo paper caiu de ~46 min para 14,8 min com QPS 4** (64 requisições, 62k tokens de prompt, 31k de resposta; GPU ~93%, 60°C, PC utilizável). O tok/s por requisição cai (~12) porque 4 dividem a GPU, mas o throughput agregado triplica
- Decisão: manter o `.dual.pdf` — custa só montagem de PDF (zero tokens/GPU), e serve de auditoria da tradução contra o original

### Design system "traduzai" implementado (mesma sessão)
- Felipe gerou um design system hifi via Claude Design; handoff descompactado em `design-system/design_handoff_traduzai/` (README com tokens oklch + protótipo `traduzai.dc.html`)
- `frontend/index.html` reescrito pixel-perfect pelo agente de design: marca "traduzai.", coluna única 860px, temas dark/light (toggle persistido, default dark), Instrument Sans + JetBrains Mono via Google Fonts (com fallbacks offline), dropzone, barra de progresso listrada com etapas, métricas colapsáveis (3 stats + 2×2 sparklines), footer Feynman. Validado com Chrome headless contra o protótipo nos 2 temas + contra o backend real
- Backend ganhou **progresso real do job** (`Job.progress` 0–100): o traduzir.py roda sob pseudo-TTY para o rich renderizar as barras do babeldoc, e o server parseia a barra geral `translate x/100` (fonte primária) + contadores por etapa (sinal precoce), com guard monotônico contra os resets visuais do rich. Frames de progresso no log limitados a ~1/s; log_tail agora vem de memória
- Estágios do babeldoc mapeados em `STAGE_BOUNDS` no server.py (pesos empíricos; tradução = 15–88%)

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

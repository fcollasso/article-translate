# ESTADO.md — tradutor-artigos

## Sessão atual: 2026-08-13 (dois nós + hub na VPS)

### Decisão 2 (mesma sessão, depois): o frontend sai dos PCs e vai para a VPS
Proposta do Felipe, e melhor que o desenho anterior. Antes, a página era servida por
um dos PCs (upstream com failover): o site morria com as duas máquinas desligadas e o
frontend precisava ser rebuildado nas duas — defeito que apareceu na prática, quando o
seletor não aparecia porque a imagem do Mac tinha o `server.py` novo e o `index.html`
velho.

Agora são **três peças**, e a VPS é a única que precisa estar ligada:
```
/  e /api/hub/  → hub (container na VPS): serve a página e o acervo lido do R2
/n/desktop/     → 100.98.187.95:8010   ┐ só a tradução (fila, pdf2zh, LM Studio)
/n/mac/         → 100.67.176.89:8010   ┘
```
Com os dois PCs desligados: o site abre, o login passa (o hub confere o token) e dá
para baixar tudo que já foi traduzido. Só não dá para **enviar** artigo novo.

Limite conhecido e aceito: a *lista de jobs* (com progresso, erro e log) continua vindo
dos nós — com tudo desligado você vê o acervo, não o histórico de execução. Espelhar
isso na VPS foi considerado e descartado por complexidade.

- `deploy/hub/` (novo): `hub.py` (FastAPI ~200 linhas) + Dockerfile + compose. Guarda só
  o **hash** do token (`HUB_TOKEN_HASHES`); credenciais R2 de leitura bastam ali.
- `frontend/index.html`: login passa pelo hub, seção "Acervo" (R2), dropzone avisa quando
  não há máquina ligada.
- `deploy/nginx/traduzia.conf`: reescrito para as três peças; o upstream com failover saiu.
  O hub é alcançado pelo **nome do container** — exige o hub na mesma rede Docker do
  quark-nginx (`QUARK_NETWORK`, default `quark_default` — **confirmar na VPS**).

### Decisão
O Mac volta a rodar inferência e vira o **segundo nó** do traduzia. Cada máquina roda o
backend inteiro (fila, banco e LM Studio próprios) e o site escolhe onde traduzir — com
uma desligada, a outra atende sozinha. Alternativa descartada: o Mac servir só de GPU
remota para o desktop, que continuaria orquestrando (mais simples, mas o site inteiro
continuaria caindo com o desktop desligado). Instalação via Docker, para paridade de
versões com o desktop.

Isso reverte parcialmente a decisão de 2026-07-14 ("não usar o Mac para inferência"):
aquilo era sobre o **Qwen3 14B**, que consumia a máquina inteira; o Qwen3 8B Q4 são ~5 GB
e cabem folgados nos 24 GB. Se atrapalhar, é só não escolher o Mac no seletor.

### Arquitetura
O nginx da VPS vira o roteador — é o único componente sempre ligado:
```
/                → upstream com failover (desktop; mac como backup)
/n/desktop/…     → 100.98.187.95:8010   via Tailscale
/n/mac/…         → <IP_DO_MAC>:8010     via Tailscale
/llmproxy/ e /n/<id>/llmproxy/ → 403
```
O `proxy_pass` termina em `/`, então o prefixo é removido antes do repasse: o backend não
sabe em que caminho é servido e o código é **idêntico** nos dois nós. `traduzir.py` não
mudou nada.

### Feito (código, testado no Mac)
- `server.py`: `GET /node` sem auth (`{id, label, gpu}`) como identidade + probe de saúde;
  `has_gpu()` cacheado (o probe roda a cada poucos segundos, não pode chamar `nvidia-smi`
  toda vez); `NODE_ID`/`NODE_LABEL` via `cfg()`; `token create <nome> --token <valor>` para
  registrar o **mesmo** token nas duas máquinas (continua só como hash).
- `frontend/index.html`: roster de nós + probe a cada 15s, seletor "rodar em" no topo do
  upload, base path por nó em todo fetch, **lista de jobs unificada** (as duas máquinas,
  com etiqueta), métricas do nó selecionado, URLs de download prefixadas quando relativas
  (a do R2 é absoluta e passa direto), cards de GPU escondidos em nó sem NVIDIA, e
  **auth por nó**: 401 de uma máquina marca só ela como "sem acesso" — só volta ao login
  quando nenhuma máquina no ar aceita o token. Sem o nginx na frente (localhost:8010) cai
  em modo de máquina única automaticamente.
- `docker-compose.mac.yml` (sem `gpus: all`, `NODE_ID=mac`); `NODE_ID=desktop` no compose
  do desktop; `deploy/nginx/traduzia.conf` reescrito; `.env.example`, README e CLAUDE.md.

### Setup do Mac (feito nesta sessão)
- Tailscale autorizado: **100.67.176.89** (o do desktop segue 100.98.187.95).
- LM Studio instalado, CLI bootstrapado, `qwen/qwen3-8b@q4_k_m` **GGUF** baixado (5.03 GB).
  GGUF e não MLX de propósito: o id exposto fica `qwen/qwen3-8b`, **igual ao do desktop**,
  então o mesmo `LOCAL_MODEL` serve nas duas. Servidor com `--bind 0.0.0.0` e
  `-c 8192 --parallel 2`. MLX fica como ideia futura se o tempo incomodar.
- Imagem arm64 (2.45 GB) monta sem compilar nada: todas as deps com C-extension
  (`onnxruntime`, `onnx`, `opencv-headless`, `pymupdf`, `rtree`, `scipy`) têm wheel
  `manylinux aarch64`. Era o principal risco da decisão pelo Docker.

### Tradução real no Mac: funciona, mas é lenta
`2511.15247.pdf` (16 págs) traduzido ponta a ponta pelo nó Mac: mono 4,8 MB + dual
8,7 MB + glossário. Valida pdf2zh, DocLayout-YOLO, o patch de justificação e o LM
Studio local em arm64.

**78,4 min**, contra ~14,8 min do desktop no mesmo artigo. Não é o modelo: o Mac faz
~40 tok/s por requisição contra ~12 do desktop. O peso está na **extração automática
de termos**, que sozinha passou de 20 min (629/821 em 21:53 numa amostra). A
investigar antes de adotar o Mac como principal — suspeitas: cache frio (o desktop já
tinha rodado esse artigo) e/ou `--parallel 2` estreito demais para os 24 GB unificados.

### Outros testes que passaram
- `/node` → `{"id":"mac","label":"MacBook M5 Pro","gpu":false}` (detectou a ausência de
  GPU sozinho); `/api/*` 401 sem token; token replicado com `--token` aceito.
- **Topologia completa local** (nginx com o vhost real + hub + MinIO + o nó Mac), com o
  desktop simulado desligado: `/` servido pelo hub, `/api/hub/files` listando o bucket,
  `/n/mac/…` ok, `/n/desktop/…` → 502, `/n/mac/llmproxy/…` → 403 (a regex tinha que vir
  antes, senão o caminho novo furava o bloqueio).
- **R2 validado sem credenciais reais**, contra um MinIO local (daí o override
  `R2_ENDPOINT` no hub): listagem agrupada por job, URL assinada baixando de verdade com
  `Content-Disposition` correto, URL adulterada → 403.
- **JS do frontend rodado de verdade** em Node com DOM mínimo contra essa topologia, em
  4 modos: site (17 asserções), **offline** (as duas máquinas caídas: não desloga e o
  acervo funciona), token válido só numa máquina, e modo direto. Todos ok. (Harness ficou
  no scratchpad da sessão, não foi versionado — vale portar para o repo algum dia.)

### Pendências (nesta ordem)
1. **R2**: Felipe criou um bucket novo; falta preencher `R2_*` no `.env` das duas máquinas
   (campos já preparados) e no ambiente do hub. Sugerido: token R/W para os PCs, token só
   de leitura para o hub (é a peça exposta na internet).
2. **Rebuild do nó Mac** — a imagem que está rodando tem o `index.html` antigo (o build
   correu antes das mudanças do frontend). Adiado a pedido do Felipe.
3. **Desktop**: `git pull` + `docker compose up -d --build`. Enquanto não fizer, ele
   responde 404 em `/node` e aparece como offline no seletor (confirmado por curl).
4. **VPS**: subir o hub (`deploy/hub/`), **confirmar o nome da rede Docker do quark**
   (`QUARK_NETWORK`, default `quark_default`) e aplicar o vhost novo.
5. Token: replicar o mesmo valor nas duas máquinas (`--token`) e o hash no hub.
6. Conferência visual da UI no navegador — feita só por código até agora; o Felipe vai
   testar pelo site.
7. Investigar a lentidão da extração de termos no Mac.

---

## Sessão: 2026-07-22 (qualidade da saída — alinhamento e palavras coladas)

### Diagnóstico (artigo 2601.13956v1, traduzido no desktop com qwen3-8b)
- "Sem indentação" = **justificação perdida**: o typesetter do BabelDOC (0.6.2) re-diagrama
  parágrafos traduzidos sempre alinhados à esquerda a partir do canto sup. esquerdo da caixa
  original, e encolhe fonte/entrelinha quando o pt-BR (~25% mais longo) não cabe. Sem flag para
  justificar — limitação upstream, não é bug nosso. Prova: parágrafo que o modelo falhou ficou
  em inglês E perfeitamente justificado (original intocado).
- Outros achados no mesmo PDF: 1º parágrafo da introdução não traduzido (chunk falhou no 8B),
  palavras coladas ("AssistentesDistribuída") por placeholders de rich text, e linha de
  watermark do BabelDOC em chinês no topo.

### Feito
- `traduzir.py`: `--watermark-output-mode no_watermark` sempre (remove a linha em chinês) e
  novo flag `--no-rich-text` → `--disable-rich-text-translate` (evita palavras coladas com
  modelos pequenos, ao custo de negrito/itálico inline). Dry-run validado no Mac.
- **Justificação resolvida com patch próprio** (Felipe aprovou "apenas em nosso repo"):
  `patches/sitecustomize.py` embrulha `Typesetting._layout_typesetting_units` do BabelDOC e
  redistribui a sobra de cada linha entre os espaços (última linha do parágrafo fica à
  esquerda; teto de 2x a mediana do espaço evita esticar linha que quebrou cedo). Injetado
  pelo traduzir.py via PYTHONPATH + TRADUZIR_JUSTIFY=1; `--no-justify` desliga; qualquer
  exceção degrada para o comportamento original com log. Invariantes do BabelDOC v0.6.2
  verificados no fonte (relocate cria unidades novas; char/unicode ancoram no current_y
  exato; relocation_transform tem tx no índice 4). Dockerfile agora copia `patches/`.
- Validação no Mac: 7 testes de unidade da lógica (fakes duck-typed) + e2e real com
  pdf2zh-next 2.9.0 em venv descartável e um LLM falso (eco prefixado, sem inferência —
  regra do Mac respeitada): A/B das 2 primeiras páginas do 2601.13956v1 confirma corpo
  justificado nas duas margens vs. controle irregular. Flags novos conferidos no
  `pdf2zh_next -h` da 2.9.0.
- README (exemplos + Solução de problemas) e CLAUDE.md (exceção documentada à regra de
  não reimplementar) atualizados.
- `from __future__ import annotations` no traduzir.py (python3 default do Mac é 3.9).

### Recalibração pós-teste real no desktop (mesmo dia, mais tarde)
- Felipe traduziu o 2601.13956v1 no desktop com o patch: **67% das linhas de corpo saíram
  justificadas** (medido com pymupdf), mas o restante caiu no teto de estiramento — que foi
  calibrado no teste em inglês, onde as sobras de linha são pequenas. Em pt-BR (palavras
  longas, sem hifenização) as sobras chegam a p95=54pt com fonte 8pt.
- Novo teto: max(1 em da fonte mediana da linha, 2x a mediana dos espaços). Fonte lida de
  `char.pdf_style.font_size` / `unit.font_size` com fallback para a regra antiga. Teste de
  unidade novo (nº 8) + e2e refeito: parágrafos de corpo justificam quase por completo;
  linhas com palavras coladas (poucos espaços) continuam à esquerda por design.
- Partes não traduzidas no PDF do desktop (título, autores, abstract em inglês): **não é o
  patch** — são os blocos mais densos em placeholders de estilo (negrito, sobrescritos) e o
  qwen3-8b falha neles de forma não determinística; o BabelDOC mantém o original (que fica
  passthrough, perfeitamente justificado). Ontem os mesmos blocos saíram traduzidos porém
  com palavras coladas. Mitigações: rodar de novo (cache reaproveita os chunks bons),
  `--no-rich-text` (derruba os placeholders) ou `--backend gemini`.

### Pendências
- Desktop: `git pull` (+ rebuild do Docker se for usar o site) e retraduzir o 2601.13956v1,
  idealmente com `--no-rich-text`, para validar o novo teto + menos falhas de chunk.
- Benchmark local vs `--backend gemini` continua em aberto.

---

## Sessão: 2026-07-14 (noite — publicação em traduzia.com.br)

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

# CLAUDE.md — tradutor-artigos

## O que é este projeto

Wrapper CLI em Python sobre o `pdf2zh_next` (PDFMathTranslate 2.0 / BabelDOC) para traduzir artigos científicos EN→PT-BR preservando layout, fórmulas e figuras. Uso pessoal (mestrado do Felipe). Dois backends: LM Studio local (`--openaicompatible`) e Gemini API (`--gemini`).

## Arquitetura

- `traduzir.py` — CLI e núcleo. Sem dependências externas (só stdlib). Monta e executa comandos `pdf2zh_next`.
- `server.py` + `frontend/index.html` — frontend web (FastAPI): fila de jobs, progresso via pty, métricas, auth por token de acesso, jobs/tokens em SQLite (`data/`), saídas no Cloudflare R2 (opcional). Publicado em https://traduzia.com.br: nginx da VPS de projetos (187.77.195.108) → Tailscale → Docker.
- `deploy/hub/hub.py` — **o hub**: roda na VPS, é a única peça sempre ligada. Serve o frontend (um lugar só, sem divergência entre máquinas) e lista/assina o acervo direto do R2, para o site abrir e os downloads funcionarem com os PCs desligados. Não traduz nada. Guarda só o *hash* do token (`HUB_TOKEN_HASHES`), nunca o token.
- **Dois nós**: desktop e Mac rodam cada um o backend inteiro (fila, banco e LM Studio próprios), e o site escolhe onde traduzir. O nginx da VPS roteia `/n/<NODE_ID>/` para cada máquina **removendo o prefixo** — por isso o backend é agnóstico de caminho e o código é idêntico nos dois; o que muda é o `NODE_ID` do compose (`docker-compose.yml` = desktop, `docker-compose.mac.yml` = mac). O frontend descobre quem está no ar pelo `GET /node` (sem auth) e degrada por nó: máquina fora do ar fica desabilitada no seletor, e um 401 de uma delas não desloga do site (com três lugares guardando token, é normal valer só em alguns). Confs de deploy em `deploy/`.
- `.env` — configuração (backend padrão, modelos, keys, R2). Nunca comitar.
- `output/` — PDFs gerados (`.mono.pdf` traduzido, `.dual.pdf` bilíngue).
- O trabalho pesado (layout detection, chunking, reconstrução do PDF) é todo do pdf2zh_next/BabelDOC — **não reimplementar nada disso aqui**.
- `patches/sitecustomize.py` — exceção consciente à regra acima: patch de justificação de parágrafos (o BabelDOC só alinha à esquerda). Não reimplementa o typesetter; embrulha `_layout_typesetting_units` e redistribui a sobra de cada linha entre os espaços. Injetado pelo traduzir.py via `PYTHONPATH` + `TRADUZIR_JUSTIFY=1` no subprocesso do pdf2zh_next; desligável com `--no-justify`. Depende de internals do BabelDOC 0.6.x (invariantes no cabeçalho do arquivo) — **revalidar após qualquer update do pdf2zh-next**; se quebrar, degrada sozinho para alinhado à esquerda sem afetar a tradução.

## Convenções

- Código e identificadores em inglês; mensagens de CLI e docs em pt-BR.
- Flags do pdf2zh_next foram verificados contra `pdf2zh_next -h` em julho/2026. Se algo quebrar após update, rodar `-h` de novo antes de mexer no código.
- Idioma destino: `pt-BR` (código validado na doc Language-Codes do projeto).

## Ambiente do Felipe

- **Desktop i9-10900F + RTX 3050 8GB + 64GB RAM** (LM Studio + modelo até 8B Q4 na VRAM, ex.: Qwen3 8B GGUF).
- **MacBook M5 Pro (24 GB)**: segundo nó do site desde 2026-08-13. Reverte parcialmente a decisão de 2026-07-14 de não usar o Mac para inferência — aquilo era sobre o **Qwen3 14B**, que consumia a máquina inteira; o Qwen3 8B Q4 (~5 GB) cabe folgado nos 24 GB. Se voltar a atrapalhar o uso do Mac, é só não escolher essa máquina no site. Sem métricas de GPU aqui (não há `nvidia-smi`, e o container não enxerga a GPU da Apple): o painel esconde os cards de GPU/VRAM/temperatura nesse nó e mantém tok/s.
- Tailscale conecta as duas máquinas e a VPS — é por ele que o nginx alcança os dois nós.

## Backlog / ideias futuras

- [ ] Glossário fixo de termos (pdf2zh_next tem suporte a term extraction via `--term-*` — avaliar se vale usar Gemini para extração de termos + local para tradução)
- [ ] Integração com Zotero (existe plugin third-party: zotero-pdf2zh)
- [ ] Modo watch: monitorar pasta de downloads e traduzir automaticamente
- [ ] Comparar qualidade local vs Gemini num artigo de referência (o paper de PINNs/colapso gravitacional é bom benchmark: pesado em jargão de física + ML)

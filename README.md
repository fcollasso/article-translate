# tradutor-artigos

Tradução de artigos científicos (PDF → PDF) preservando layout, fórmulas, figuras e tabelas — substituto local/barato do Linnk.ai + DeepL.

Motor: [pdf2zh-next](https://github.com/PDFMathTranslate/PDFMathTranslate-next) (PDFMathTranslate 2.0, baseado em BabelDOC + DocLayout-YOLO).
Wrapper: `traduzir.py` com dois backends intercambiáveis:

| Backend | Quando usar | Custo |
|---|---|---|
| `local` (LM Studio) | Sem limite de uso, offline, privacidade | R$ 0 (tempo de GPU) |
| `gemini` | Qualidade máxima, artigo urgente | Free tier ou ~centavos/artigo |

## Instalação

Requer Python 3.11–3.12. O projeto recomenda `uv`:

```bash
# macOS (MacBook M5)
brew install uv
uv tool install --python 3.12 pdf2zh-next

# Windows/Linux (desktop RTX 3050)
pip install uv
uv tool install --python 3.12 pdf2zh-next
```

Verifique: `pdf2zh_next --version`. Na primeira execução ele baixa o modelo de layout (DocLayout-YOLO), então rode um teste com internet.

Depois:

```bash
cp .env.example .env
# edite o .env (modelo local e/ou GEMINI_API_KEY)
```

## Uso

```bash
# Um artigo, backend padrão do .env
python traduzir.py artigo.pdf

# Pasta inteira via Gemini
python traduzir.py ~/mestrado/artigos/ --backend gemini

# LM Studio rodando no MacBook, executando do desktop (Tailscale)
python traduzir.py artigo.pdf --backend local --base-url http://macbook-m5.tailnet.ts.net:1234/v1

# Só o PDF traduzido (sem o bilíngue lado a lado)
python traduzir.py artigo.pdf --mono-only

# Modelos pequenos colando palavras? Traduza sem estilos inline
python traduzir.py artigo.pdf --no-rich-text

# Sem a justificação de parágrafos (patch local; ver Solução de problemas)
python traduzir.py artigo.pdf --no-justify

# Conferir o comando gerado sem executar
python traduzir.py artigo.pdf --dry-run
```

Saída em `./output/`: `artigo.no_watermark.mono.pdf` (traduzido) e `.dual.pdf` (bilíngue).

## Setup do LM Studio

1. Baixe o modelo (sugestões abaixo), carregue e inicie o servidor: aba **Developer → Start Server** (porta 1234).
2. Copie o **model id** exato mostrado pelo servidor para `LOCAL_MODEL` no `.env`.
3. Para acesso remoto via Tailscale, habilite "Serve on Local Network" no LM Studio.

### Modelos recomendados por máquina

**Desktop RTX 3050 8 GB (máquina do projeto):**
- `Qwen3 8B` (GGUF, Q4_K_M) — cabe inteiro na VRAM
- `Gemma 3 4B` — se quiser velocidade máxima
- Evite 12B+: vai transbordar para a RAM (os 64 GB ajudam, mas a velocidade despenca)

**MacBook M5 Pro (24 GB — não usar para inferência):**
- Testado com `Qwen3 14B` MLX: a tradução funciona, mas consome a máquina inteira e a deixa inutilizável durante o processo
- Se um dia precisar, o Mac pode ser *cliente* do desktop via Tailscale (`--base-url`)

### Dica de qualidade

Em modelos Qwen3, desative o modo "thinking" para tradução (no LM Studio ou com `/no_think` no system prompt do servidor) — raciocínio não melhora tradução e triplica o tempo.

## Gemini (free tier)

1. Gere a key em https://aistudio.google.com/apikey
2. Cole em `GEMINI_API_KEY` no `.env`
3. Mantenha `GEMINI_QPS=1` no free tier (evita erro 429). Um artigo de ~16 páginas consome bastante requisições; o free tier diário dá conta de poucos artigos/dia — para volume, o tier pago custa centavos por artigo.

## Web — traduzia.com.br

Frontend web com login por token, fila de jobs, progresso e métricas ao vivo:

```
navegador → traduzia.com.br → nginx na VPS (TLS, bloqueia /llmproxy)
                                  ↓ Tailscale
                          desktop: Docker [server.py + SQLite]
                                  ↓ host.docker.internal
                          LM Studio (RTX 3050) — inferência

downloads: navegador → Cloudflare R2 (URL pré-assinada, sem passar pelo túnel)
```

No desktop:

```bash
docker compose up -d --build
docker compose exec traduzia python server.py token create felipe   # imprime o token 1x
```

- Tokens e jobs ficam em SQLite (volume `traduzia-data`); gerencie com `token list` / `token revoke <nome>`.
- R2 é opcional: preencha `R2_*` no `.env` (seção no `.env.example`); sem ele os arquivos são servidos do desktop com URL assinada.
- O vhost do nginx da VPS está versionado em `deploy/nginx/traduzia.conf`; o passo a passo do deploy está no `ESTADO.md`.
- Sem Docker (`.venv/bin/python server.py`) também funciona, mas exige `pip install boto3`, `FRONTEND_HOST=0.0.0.0` e portproxy do Windows→WSL — o Docker Desktop publica a porta no Windows sozinho.

## Solução de problemas

- **Erro 429 (Gemini):** reduza `GEMINI_QPS` ou aguarde a janela de rate limit.
- **Timeout no backend local:** modelo grande demais para a máquina; troque por um menor ou aumente `--openai-compatible-timeout` (edite `build_command` no traduzir.py).
- **Tradução truncada/estranha:** alguns modelos pequenos "conversam" em vez de só traduzir; suba para 8B+ ou use `--gemini`.
- **Palavras coladas/quebradas ("AssistentesDistribuída", "t entam"):** artefato dos placeholders de estilo inline com modelos pequenos; use `--no-rich-text` (perde negrito/itálico no corpo do texto).
- **Justificação de parágrafos:** o typesetter do BabelDOC só alinha à esquerda; este repo corrige com um patch próprio (`patches/sitecustomize.py`, injetado pelo `traduzir.py` via PYTHONPATH) que distribui a sobra de cada linha entre os espaços. Ligado por padrão; desligue com `--no-justify`. Linhas que quebraram cedo demais (ex.: fórmula larga) ficam à esquerda de propósito (teto de estiramento). Se um update do pdf2zh-next/babeldoc quebrar o patch, ele se desativa sozinho e loga o erro — a tradução nunca falha por causa dele.
- **Parágrafo ficou em inglês no PDF final:** o modelo falhou naquele chunk e o BabelDOC manteve o original; rode de novo (o cache pula o que já foi traduzido) ou use `--gemini`.
- **Flags mudaram após update do pdf2zh-next:** rode `pdf2zh_next -h` e ajuste `build_command()`.

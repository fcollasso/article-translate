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

Três peças. O **hub** roda na VPS e é a única que precisa estar sempre ligada: ele
serve a página e o acervo (lido direto do R2). As **máquinas** só traduzem, e você
escolhe no site em qual rodar:

```
navegador → traduzia.com.br → nginx na VPS (TLS, bloqueia /llmproxy)
                                  │
              ┌───────────────────┼───────────────────┐
        /  e  /api/hub/      /n/desktop/          /n/mac/
              │                   └──── Tailscale ────┘
    hub (container na VPS)      Docker [server.py + SQLite] × 2
      página + acervo do R2          ↓ host.docker.internal
              │                  LM Studio (RTX 3050 / M5 Pro)
              ↓
      Cloudflare R2 ←──── os nós sobem as saídas para cá

downloads: navegador → R2 (URL pré-assinada, sem passar pelo túnel)
```

Com desktop e Mac desligados o site **abre normalmente**, você entra e baixa o que
já foi traduzido — só não dá para enviar artigo novo, porque a tradução vive nas
máquinas.

O nginx remove o prefixo `/n/<id>/` antes de repassar, então o backend é o mesmo
código nas duas máquinas — o que muda é só o `NODE_ID` do compose.

No desktop:

```bash
docker compose up -d --build
docker compose exec traduzia python server.py token create felipe   # imprime o token 1x
```

No Mac (mesmo repo, compose próprio — sem `gpus: all`):

```bash
docker compose -f docker-compose.mac.yml up -d --build
# o MESMO token do desktop, para não precisar trocar de token ao trocar de máquina:
docker compose -f docker-compose.mac.yml exec traduzia \
  python server.py token create felipe --token <token do desktop>
```

Na VPS (o hub — página e acervo):

```bash
# o hub só aceita o hash do token, nunca o token em si
docker run --rm hub-hub:latest python hub.py hash <o mesmo token>

# preencha HUB_TOKEN_HASHES e as R2_* (leitura basta) num .env ao lado do compose
docker compose -f deploy/hub/docker-compose.yml up -d --build
```

O hub precisa estar na **mesma rede Docker do quark-nginx** para o vhost alcançá-lo
pelo nome do container; confira o nome da rede com
`docker inspect quark-nginx -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}'`
e ajuste `QUARK_NETWORK` se não for `quark_default`.

- Cada máquina tem **fila, banco e LM Studio próprios**: nada é compartilhado entre elas além do bucket R2. A lista de trabalhos no site junta as duas, com etiqueta de qual rodou o quê.
- Tokens e jobs ficam em SQLite (volume `traduzia-data`); gerencie com `token list` / `token revoke <nome>`. O token é **por máquina** — use `--token` para replicar o mesmo valor na outra, e o hash dele no hub. Revogar de verdade = revogar nos três lugares.
- R2 é o que sustenta o acervo: preencha `R2_*` no `.env` das duas máquinas (seção no `.env.example`). Sem ele tudo continua funcionando, mas os downloads passam pelo túnel e o acervo do site fica indisponível com os PCs desligados.
- O vhost do nginx da VPS está versionado em `deploy/nginx/traduzia.conf`; o passo a passo do deploy está no `ESTADO.md`.
- Sem Docker (`.venv/bin/python server.py`) também funciona, mas exige `pip install boto3`, `FRONTEND_HOST=0.0.0.0` e portproxy do Windows→WSL — o Docker Desktop publica a porta no Windows sozinho.
- Aberto direto na máquina (`http://localhost:8010`) o site funciona igual, só sem o seletor: sem o nginx na frente não existe prefixo `/n/`, e o frontend entra em modo de máquina única.

### Subir uma máquina nova (do zero)

Duas coisas **não vêm no clone** e precisam ser providenciadas: o `.env` (está no `.gitignore`) e o LM Studio com o modelo.

1. **Clonar o repo e copiar o `.env`** de outra máquina (backend padrão, modelo, `LOCAL_QPS`/`LOCAL_WORKERS`, credenciais do R2). Ajuste `LOCAL_MODEL` para o id exato que o LM Studio de lá expõe.
2. **LM Studio** (a inferência roda fora do container):
   - Baixar o modelo `qwen/qwen3-8b` (GGUF Q4_K_M, ~5 GB)
   - Subir o servidor aceitando conexões externas — senão o container não alcança:
     ```
     lms server start --bind 0.0.0.0        # no Mac: "Serve on Local Network" nas configurações
     ```
   - Carregar o modelo com `-c 8192 --parallel 2` (config validada para 8 GB de VRAM)
3. **Subir o container**: `docker compose up -d --build` no desktop, `docker compose -f docker-compose.mac.yml up -d --build` no Mac. O compose já aponta para o LM Studio via `host.docker.internal:1234` — não precisa mexer em IP.
4. **Criar o token de acesso** (o frontend exige) — veja acima.

Depois é só abrir `http://localhost:8010`.

Para a máquina entrar no traduzia.com.br: instalar o Tailscale nela, pegar o IP `100.x` e apontar o `location /n/<id>/` correspondente no vhost do nginx da VPS (`deploy/nginx/traduzia.conf`). Uma máquina nova (um terceiro nó) precisa também entrar na constante `NODES` do `frontend/index.html`.

## Solução de problemas

- **Erro 429 (Gemini):** reduza `GEMINI_QPS` ou aguarde a janela de rate limit.
- **Timeout no backend local:** modelo grande demais para a máquina; troque por um menor ou aumente `--openai-compatible-timeout` (edite `build_command` no traduzir.py).
- **Tradução truncada/estranha:** alguns modelos pequenos "conversam" em vez de só traduzir; suba para 8B+ ou use `--gemini`.
- **Palavras coladas/quebradas ("AssistentesDistribuída", "t entam"):** artefato dos placeholders de estilo inline com modelos pequenos; use `--no-rich-text` (perde negrito/itálico no corpo do texto).
- **Justificação de parágrafos:** o typesetter do BabelDOC só alinha à esquerda; este repo corrige com um patch próprio (`patches/sitecustomize.py`, injetado pelo `traduzir.py` via PYTHONPATH) que distribui a sobra de cada linha entre os espaços. Ligado por padrão; desligue com `--no-justify`. Linhas que quebraram cedo demais (ex.: fórmula larga) ficam à esquerda de propósito (teto de estiramento). Se um update do pdf2zh-next/babeldoc quebrar o patch, ele se desativa sozinho e loga o erro — a tradução nunca falha por causa dele.
- **Parágrafo ficou em inglês no PDF final:** o modelo falhou naquele chunk e o BabelDOC manteve o original; rode de novo (o cache pula o que já foi traduzido) ou use `--gemini`.
- **Flags mudaram após update do pdf2zh-next:** rode `pdf2zh_next -h` e ajuste `build_command()`.

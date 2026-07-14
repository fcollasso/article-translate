# ESTADO.md — tradutor-artigos

## Sessão atual: 2026-07-14 (bootstrap via Claude.ai)

### Feito
- Pesquisa: decidido usar pdf2zh-next (PDFMathTranslate 2.0) como motor, em vez de implementar extração/reconstrução de PDF do zero
- Flags da CLI verificados contra `pdf2zh_next -h` (versão instalada via pip em jul/2026): `--openaicompatible` + `--openai-compatible-{model,base-url,api-key}`, `--gemini` + `--gemini-{model,api-key}`, `--lang-out pt-BR`, `--qps`, `--no-dual/--no-mono`
- Criados: traduzir.py (wrapper batch, 2 backends, dry-run), .env.example, README.md, CLAUDE.md

### Próximos passos (primeira sessão no Claude Code)
1. `uv tool install --python 3.12 pdf2zh-next` no MacBook
2. `cp .env.example .env` e configurar
3. Teste real: traduzir o paper 2511.15247 (PINNs) com backend local e comparar com a versão do Linnk que já temos
4. Testar backend gemini com free tier (QPS=1)
5. `git init` + primeiro commit (garantir .env no .gitignore)

### Decisões em aberto
- Qual modelo local vence no benchmark (Qwen3 14B vs Gemma 3 12B no MacBook)
- Se o desktop RTX 3050 terá LM Studio próprio (8B) ou só consome o servidor do Mac via Tailscale

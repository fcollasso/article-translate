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

### Próximos passos (no desktop)
1. Clonar `git@github.com:fcollasso/article-translate.git`
2. Instalar: `pip install uv` → `uv tool install --python 3.12 pdf2zh-next` → `pdf2zh_next --warmup`
3. LM Studio + **Qwen3 8B (GGUF Q4_K_M)** — cabe nos 8 GB de VRAM da 3050; carregar e subir o servidor (porta 1234)
4. `cp .env.example .env`, ajustar `LOCAL_MODEL` com o id exato do servidor
5. Teste real: `python traduzir.py artigos/2511.15247.pdf` (baixar o paper de novo: https://arxiv.org/pdf/2511.15247) e comparar com a versão do Linnk
6. `GEMINI_API_KEY` no `.env` e testar `--backend gemini` (QPS=1)

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

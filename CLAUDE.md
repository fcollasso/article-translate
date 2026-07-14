# CLAUDE.md — tradutor-artigos

## O que é este projeto

Wrapper CLI em Python sobre o `pdf2zh_next` (PDFMathTranslate 2.0 / BabelDOC) para traduzir artigos científicos EN→PT-BR preservando layout, fórmulas e figuras. Uso pessoal (mestrado do Felipe). Dois backends: LM Studio local (`--openaicompatible`) e Gemini API (`--gemini`).

## Arquitetura

- `traduzir.py` — único ponto de entrada. Sem dependências externas (só stdlib). Monta e executa comandos `pdf2zh_next`.
- `.env` — configuração (backend padrão, modelos, keys). Nunca comitar.
- `output/` — PDFs gerados (`.mono.pdf` traduzido, `.dual.pdf` bilíngue).
- O trabalho pesado (layout detection, chunking, reconstrução do PDF) é todo do pdf2zh_next/BabelDOC — **não reimplementar nada disso aqui**.

## Convenções

- Código e identificadores em inglês; mensagens de CLI e docs em pt-BR.
- Flags do pdf2zh_next foram verificados contra `pdf2zh_next -h` em julho/2026. Se algo quebrar após update, rodar `-h` de novo antes de mexer no código.
- Idioma destino: `pt-BR` (código validado na doc Language-Codes do projeto).

## Ambiente do Felipe

- MacBook M5 Pro: máquina principal de inferência local (LM Studio + MLX). Modelos: Qwen3 14B / Gemma 3 12B.
- Desktop i9-10900F + RTX 3050 8GB + 64GB RAM: roda até 8B Q4 na VRAM; preferencialmente atua como cliente do MacBook via Tailscale.
- Tailscale conecta as duas máquinas (LOCAL_BASE_URL pode apontar para o hostname da tailnet).

## Backlog / ideias futuras

- [ ] Glossário fixo de termos (pdf2zh_next tem suporte a term extraction via `--term-*` — avaliar se vale usar Gemini para extração de termos + local para tradução)
- [ ] Integração com Zotero (existe plugin third-party: zotero-pdf2zh)
- [ ] Modo watch: monitorar pasta de downloads e traduzir automaticamente
- [ ] Comparar qualidade local vs Gemini num artigo de referência (o paper de PINNs/colapso gravitacional é bom benchmark: pesado em jargão de física + ML)

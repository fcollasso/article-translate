# Handoff: traduzai — plataforma de tradução de artigos

## Overview
**traduzai** é uma plataforma local de tradução de artigos científicos (PDF, EN → PT-BR) usando `pdf2zh_next` + um LLM rodando no LM Studio (ex.: `qwen/qwen3-8b`). A UI tem um fluxo principal (upload → progresso → resultado com downloads) e um painel colapsável de métricas ao vivo do modelo/GPU.

## About the Design Files
Os arquivos `.dc.html` neste pacote são **referências de design em HTML** — protótipos que mostram aparência e comportamento pretendidos, **não código de produção para copiar diretamente**. A tarefa é **recriar estes designs no ambiente do codebase alvo** (React, Vue, Svelte, etc.) usando os padrões e bibliotecas já estabelecidos — ou, se não houver frontend ainda, escolher o framework mais adequado e implementar lá. Abra os `.dc.html` no navegador para ver o design renderizado (o `support.js` na pasta é o runtime deles).

## Fidelity
**High-fidelity (hifi).** Cores, tipografia, espaçamentos, raios e estados são finais. Recriar pixel-perfect. Os **dados são simulados** no protótipo — conectar à API real do backend / LM Studio.

## Design Tokens
Todas as cores em `oklch`. Dois temas via atributo `data-theme` no `<body>` (default: dark).

### Tema escuro
| Token | Valor | Uso |
|---|---|---|
| `--bg` | `oklch(0.145 0.008 255)` | fundo da página |
| `--surface` | `oklch(0.185 0.009 255)` | cards |
| `--surface-2` | `oklch(0.225 0.01 255)` | trilhas de progresso, hovers, botões secundários |
| `--border` | `oklch(0.27 0.012 255)` | contornos padrão |
| `--border-strong` | `oklch(0.34 0.014 255)` | contornos em hover / dropzone |
| `--text` | `oklch(0.94 0.005 255)` | texto principal |
| `--text-2` | `oklch(0.7 0.012 255)` | texto secundário |
| `--text-3` | `oklch(0.52 0.014 255)` | texto terciário / rótulos |
| `--accent` | `oklch(0.72 0.15 250)` | azul — ação, progresso |
| `--accent-soft` | `oklch(0.72 0.15 250 / 0.14)` | fundo de badges/ícones azuis |
| `--green` | `oklch(0.72 0.15 160)` | sucesso, conexão |
| `--green-soft` | `oklch(0.72 0.15 160 / 0.14)` | fundo badge verde |
| `--amber` | `oklch(0.78 0.13 80)` | atenção, temperatura |
| `--amber-soft` | `oklch(0.78 0.13 80 / 0.14)` | fundo badge âmbar |
| `--red` | `oklch(0.66 0.17 25)` | erro, destrutivo |
| `--shadow` | `0 1px 2px oklch(0 0 0/.25), 0 8px 24px oklch(0 0 0/.25)` | sombra de cards principais |
| VRAM roxo (só gráfico) | `oklch(0.72 0.15 290)` | sparkline VRAM |

### Tema claro
| Token | Valor |
|---|---|
| `--bg` | `oklch(0.975 0.003 255)` |
| `--surface` | `oklch(1 0 0)` |
| `--surface-2` | `oklch(0.955 0.004 255)` |
| `--border` | `oklch(0.9 0.006 255)` |
| `--border-strong` | `oklch(0.82 0.01 255)` |
| `--text` | `oklch(0.22 0.015 255)` |
| `--text-2` | `oklch(0.45 0.015 255)` |
| `--text-3` | `oklch(0.6 0.012 255)` |
| `--accent` | `oklch(0.55 0.17 250)` (soft: `/ 0.1`) |
| `--green` | `oklch(0.56 0.14 160)` (soft: `/ 0.12`) |
| `--amber` | `oklch(0.66 0.13 80)` (soft: `/ 0.14`) |
| `--red` | `oklch(0.55 0.19 25)` |
| `--shadow` | `0 1px 2px oklch(0 0 0/.06), 0 8px 24px oklch(0 0 0/.06)` |

Regra: acentos compartilham lightness/chroma (0.72/0.15 no escuro), só o hue varia. Variantes `-soft` = mesma cor com alpha ~0.14, usadas como fundo de badges/ícones. Acento nunca é cor de texto longo.

### Tipografia
- **Instrument Sans** (Google Fonts, 400/500/600/700) — UI, títulos, corpo.
- **JetBrains Mono** (400/500/600/700) — SOMENTE dados técnicos: números, nomes de arquivo, nomes de modelo, tokens. Nunca em parágrafos.

Escala:
| Papel | Spec |
|---|---|
| display / wordmark | 26px · 700 · letter-spacing -0.03em |
| título | 16px · 600 |
| corpo | 13.5px · 400 |
| rótulo de seção | 11.5px · 600 · caps · letter-spacing 0.12em · `--text-3` |
| dado grande | 24–26px · 600 mono · letter-spacing -0.02em |
| dado pequeno / meta | 12–12.5px · mono ou sans · `--text-3` |

### Espaçamento e raios
- Escala base 4px: 4 / 8 / 12 / 16 / 24 / 32. Gap entre seções da página: 28px. Padding de cards: 16–20px.
- Raios: controles/botões **10px** · cards **14px** · dropzone **16px** · pills/badges **999px**. Não misturar raios no mesmo nível hierárquico.
- Container: max-width **860px**, centralizado, padding lateral 24px.

## Screens / Views

### Tela única: plataforma (traduzai.dc.html)
Coluna única centralizada (860px), seções com gap 28px, de cima para baixo:

**1. Header** — flex space-between.
- Esquerda: wordmark "traduz**ai**." (26px/700, "ai" em `--accent`, ponto final em `--green` maior 30px) + linha meta em mono 12px `--text-3`: `EN → PT-BR · pdf2zh_next · LM Studio local` ("EN → PT-BR" em `--text-2`).
- Direita: pill "backend conectado" (borda `--border`, fundo `--surface`, 12.5px/500 `--text-2`, ponto 7px `--green` com glow `box-shadow: 0 0 8px var(--green)` e animação pulse 2.4s) + botão toggle de tema (mesma pill, ícone círculo meio-preenchido 12px, label "claro"/"escuro").

**2. Dropzone de upload**
- Borda 1.5px dashed `--border-strong`, raio 16px, fundo `--surface`, padding 44px 24px, conteúdo centralizado.
- Ícone: quadrado 44px raio 12px fundo `--accent-soft` com seta "↑" mono em `--accent`.
- Título "Arraste um PDF aqui" (16px/600); subtítulo "ou clique para escolher — o artigo entra na fila de tradução" (13.5px `--text-3`).
- Estados: hover → borda `--border-strong`→ mais forte; **dragover** → borda `--accent`, fundo `--accent-soft`. Clique abre file picker (`accept=".pdf"`).

**3. Seção Trabalhos** — rótulo caps "TRABALHOS", lista de cards de job (mais recente no topo).
Card de job: borda `--border`, raio 14px, fundo `--surface`, sombra `--shadow`, padding 18px 20px, gap interno 14px.
- Linha 1: badge de status + nome do arquivo (mono 14px/600) + info de tempo à direita (12.5px `--text-3`).
- **Job ativo**: linha etapa ("extraindo texto do PDF" <20% · "traduzindo parágrafos" <90% · "gerando PDFs finais" ≥90%) + percentual mono à direita; barra 6px raio pill, trilha `--surface-2`, preenchimento azul com listra animada (repeating-linear-gradient 90°, blocos 16px alternando `--accent` e accent a 75%, `background-size 32px`, animação deslizando 32px/0.8s linear infinite), largura transition .5s.
- **Job concluído**: linha de botões de download (wrap, gap 8px): "PDF bilíngue (dual) 8,7 MB", "Glossário (CSV) 21,6 KB", "PDF traduzido (mono) 4,8 MB". Cada um: seta "↓" mono em `--accent` + label 13px/600 + tamanho 12px `--text-3`; borda `--border`, raio 10px, fundo `--surface-2`; hover → borda `--accent`.

**4. Seção Métricas do modelo (colapsável)** — header é um botão texto: chevron "▶" (rotaciona 90° aberto, transition .2s) + "MÉTRICAS DO MODELO" caps + quando **fechada** um resumo inline mono: `· 10,8 tok/s · GPU 19%`. Aberta por padrão; abre automaticamente ao iniciar um job.
Conteúdo (gap 12px):
- **Card do modelo**: nome `qwen/qwen3-8b` (mono 14.5px/600) + badge "carregado" (verde); linha com "quantização → Q4_K_M" e "contexto carregado / máximo → 8.192 / 32.768 tokens" (rótulo 12px `--text-3`, valor mono 14px/600); barra estática 5px, 25% preenchida em `--accent`.
- **Grid 3 colunas de stats**: Requisições (64, "0 ativas agora"), Tokens de prompt (62,1 mil, "acumulados na sessão"), Tokens de resposta (30,7 mil). Valor em mono 26px/600.
- **Grid 2×2 de gráficos**: Velocidade de geração (azul, tok/s, sub "média da sessão: 11,9 tok/s"), Utilização da GPU (verde, %, sub "RTX 3050"), VRAM (roxo `oklch(0.72 0.15 290)`, "7.500 MiB", sub "de 8.192 MiB · uso alto"), Temperatura (âmbar, °C, sub "limite: 83 °C"). Cada card: chip de cor 8px raio 3px + rótulo 12.5px `--text-2`; valor mono 24px + unidade 12px `--text-3`; sparkline SVG `viewBox 0 0 260 44`, altura 44px, polyline stroke 1.5, cor do gráfico, sem fill/eixos, ~60 pontos.

**5. Footer** — citação Feynman em itálico 12.5px `--text-3`: “O que eu não consigo criar, eu não entendo.” — Richard Feynman.

### Documentação (Design System.dc.html)
Página de referência dos tokens/componentes acima — usar como consulta visual; não precisa ser implementada no produto (opcional: rota interna `/design`).

## Interactions & Behavior
- **Upload**: clique na dropzone → file picker (.pdf); drag&drop também. Ao enviar, cria job no topo da lista com status ativo e abre a seção de métricas.
- **Progresso**: no protótipo é simulado (tick ~1.2s, +0–4.5%/tick); na implementação real, vem do backend (WebSocket/SSE/polling). Etapas mapeadas por faixa de % (ver acima).
- **Conclusão**: badge vira "concluído" (verde), barra some, aparecem os 3 botões de download.
- **Métricas ao vivo**: atualização ~1.2s; sparklines mantêm janela de 60 amostras. GPU/temperatura sobem quando há job ativo.
- **Toggle de tema**: troca `data-theme` no body; transições de background/cor .3s; persistir escolha (localStorage).
- **Hovers**: bordas escurecem/acendem (`--border-strong` ou `--accent`); botões primários `brightness(1.1)`; transições .15–.2s.
- Badge "backend conectado": animação pulse (opacity 1→.35→1, 2.4s ease-in-out infinite) reservada para estados vivos.

## State Management
- `theme: 'dark' | 'light'` (persistido)
- `backendConnected: boolean`
- `jobs: { id, name, status: 'queued'|'active'|'done'|'failed', pct, stage, submittedAt, finishedAt, files: {label, url, size}[] }[]`
- `metricsOpen: boolean`
- `model: { name, quant, ctxLoaded, ctxMax, loaded: boolean }`
- `session: { requests, activeRequests, promptTokens, responseTokens, tokPerSec, avgTokPerSec }`
- Séries temporais (60 amostras): `speed[]`, `gpu[]`, `vram[]`, `temp[]`
- Fontes de dados: API do backend de tradução (jobs) + API do LM Studio / telemetria de GPU (métricas).

## Badges de status (spec)
Pill 999px, 11.5px/600, ponto 6px em `currentColor`, fundo na variante `-soft` da mesma cor:
- na fila → `--surface-2` / `--text-2` · traduzindo → azul · concluído → verde · uso alto → âmbar · falhou → vermelho.

## Botões (spec)
Padding 9px 18px, raio 10px, 13.5px/600:
- **Primário** (máx. 1/tela): fundo `--accent`, texto escuro `oklch(0.13 0.02 255)`.
- **Secundário**: fundo `--surface-2`, borda `--border-strong`; hover borda `--accent`.
- **Ghost**: sem fundo, `--text-2`; hover fundo `--surface-2`.
- **Destrutivo**: fundo `--red` soft, borda e texto `--red`.
- **Desabilitado**: fundo `--surface-2`, texto `--text-3`, cursor not-allowed.

## Assets
Nenhuma imagem/ícone externo. Ícones são glifos tipográficos (↑ ↓ ▶) e formas CSS simples. Fontes via Google Fonts (Instrument Sans, JetBrains Mono).

## Files
- `traduzai.dc.html` — tela principal (referência primária)
- `Design System.dc.html` — documentação visual de tokens e componentes
- `support.js` — runtime dos protótipos; apenas para abrir os `.dc.html` no navegador, ignorar na implementação

#!/usr/bin/env python3
"""
traduzir.py — Batch scientific PDF translation wrapper around pdf2zh_next (PDFMathTranslate 2.0 / BabelDOC).

Backends:
  local   -> LM Studio (or any OpenAI-compatible server) via --openaicompatible
  gemini  -> Google Gemini API via --gemini

Usage:
  python traduzir.py paper.pdf
  python traduzir.py papers/ --backend gemini
  python traduzir.py paper.pdf --backend local --model qwen3-14b
  python traduzir.py papers/ --out traduzidos/ --mono-only

Configuration is read from a .env file in the same directory (see .env.example).
CLI flags override .env values.
"""

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def find_pdf2zh() -> str:
    """Prefer the pdf2zh_next installed alongside the running Python (venv), else rely on PATH."""
    candidate = Path(sys.executable).parent / "pdf2zh_next"
    return str(candidate) if candidate.exists() else "pdf2zh_next"


def load_env(path: Path) -> dict:
    """Minimal .env parser (no external dependencies)."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def collect_pdfs(inputs: list[str]) -> list[Path]:
    pdfs: list[Path] = []
    for item in inputs:
        p = Path(item).expanduser()
        if p.is_dir():
            pdfs.extend(sorted(p.glob("*.pdf")))
        elif p.suffix.lower() == ".pdf" and p.exists():
            pdfs.append(p)
        else:
            print(f"[aviso] Ignorando entrada inválida: {item}", file=sys.stderr)
    # Skip files that are already translation outputs
    return [p for p in pdfs if not any(tag in p.stem for tag in (".mono", ".dual", "_translated"))]


def build_command(pdf: Path, args: argparse.Namespace, env: dict) -> list[str]:
    cmd = [
        find_pdf2zh(),
        str(pdf),
        "--lang-in", args.lang_in,
        "--lang-out", args.lang_out,
        "--output", str(args.out),
    ]

    if args.mono_only:
        cmd.append("--no-dual")
    if args.dual_only:
        cmd.append("--no-mono")

    if args.backend == "local":
        model = args.model or env.get("LOCAL_MODEL", "qwen3-14b")
        base_url = args.base_url or env.get("LOCAL_BASE_URL", "http://localhost:1234/v1")
        api_key = env.get("LOCAL_API_KEY", "lm-studio")  # LM Studio ignores the key, but the field is required
        cmd += [
            "--openaicompatible",
            "--openai-compatible-model", model,
            "--openai-compatible-base-url", base_url,
            "--openai-compatible-api-key", api_key,
            "--qps", str(args.qps or env.get("LOCAL_QPS", "4")),
            "--pool-max-workers", str(env.get("LOCAL_WORKERS", "4")),
        ]
        if "qwen" in model.lower():
            # Qwen3 thinking mode wastes ~10x tokens on translation; /no_think disables it.
            # Mirrors babeldoc's default role block ("Follow all rules strictly." is re-appended by it).
            cmd += [
                "--custom-system-prompt",
                f"/no_think You are a professional {args.lang_out} native translator "
                f"who needs to fluently translate text into {args.lang_out}.",
            ]
    elif args.backend == "gemini":
        api_key = env.get("GEMINI_API_KEY", "")
        if not api_key:
            sys.exit("[erro] GEMINI_API_KEY não definido no .env")
        model = args.model or env.get("GEMINI_MODEL", "gemini-2.5-flash")
        cmd += [
            "--gemini",
            "--gemini-model", model,
            "--gemini-api-key", api_key,
            # Free tier is rate-limited: keep QPS low to avoid 429s
            "--qps", str(args.qps or env.get("GEMINI_QPS", "1")),
        ]
    return cmd


def main() -> None:
    parser = argparse.ArgumentParser(description="Tradução de artigos científicos (PDF) preservando layout.")
    parser.add_argument("inputs", nargs="+", help="Arquivo(s) PDF ou diretório(s) contendo PDFs")
    parser.add_argument("--backend", choices=["local", "gemini"], default=None,
                        help="Backend de tradução (padrão: BACKEND do .env, senão 'local')")
    parser.add_argument("--model", default=None, help="Override do modelo do backend")
    parser.add_argument("--base-url", default=None, help="Override da URL do servidor local (ex: máquina remota via Tailscale)")
    parser.add_argument("--lang-in", default="en", help="Idioma de origem (padrão: en)")
    parser.add_argument("--lang-out", default="pt-BR", help="Idioma de destino (padrão: pt-BR)")
    parser.add_argument("--out", default=None, help="Diretório de saída (padrão: ./output)")
    parser.add_argument("--qps", default=None, help="Limite de requisições por segundo")
    parser.add_argument("--mono-only", action="store_true", help="Gerar apenas o PDF traduzido (sem versão bilíngue)")
    parser.add_argument("--dual-only", action="store_true", help="Gerar apenas o PDF bilíngue lado a lado")
    parser.add_argument("--dry-run", action="store_true", help="Mostrar comandos sem executar")
    args = parser.parse_args()

    env = load_env(SCRIPT_DIR / ".env")
    args.backend = args.backend or env.get("BACKEND", "local")
    out = Path(args.out or env.get("OUTPUT_DIR") or (SCRIPT_DIR / "output")).expanduser()
    if args.out is None and not out.is_absolute():
        # A relative OUTPUT_DIR from .env is anchored to the project dir, not the CWD
        out = SCRIPT_DIR / out
    args.out = out
    args.out.mkdir(parents=True, exist_ok=True)

    pdfs = collect_pdfs(args.inputs)
    if not pdfs:
        sys.exit("[erro] Nenhum PDF encontrado nas entradas fornecidas.")

    print(f"Backend: {args.backend} | Destino: {args.lang_out} | Saída: {args.out}")
    print(f"{len(pdfs)} arquivo(s) na fila:\n" + "\n".join(f"  - {p.name}" for p in pdfs))

    failures = []
    for i, pdf in enumerate(pdfs, 1):
        cmd = build_command(pdf, args, env)
        print(f"\n[{i}/{len(pdfs)}] Traduzindo: {pdf.name}")
        if args.dry_run:
            # Mask the API key in dry-run output
            safe = [("***" if cmd[j - 1].endswith("api-key") else c) for j, c in enumerate(cmd)]
            print("  " + " ".join(shlex.quote(c) for c in safe))
            continue
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"[erro] Falha ao traduzir {pdf.name} (código {result.returncode})", file=sys.stderr)
            failures.append(pdf.name)

    print("\n" + "=" * 50)
    if failures:
        print(f"Concluído com {len(failures)} falha(s): {', '.join(failures)}")
        sys.exit(1)
    print(f"Concluído! Arquivos em: {args.out}")


if __name__ == "__main__":
    main()

"""
Patch de justificação do traduzia — vive só neste repo, sem fork do BabelDOC.

Como funciona: o traduzir.py injeta este diretório no PYTHONPATH do subprocesso
pdf2zh_next e liga TRADUZIR_JUSTIFY=1. O Python importa `sitecustomize`
automaticamente no boot do interpretador; aqui embrulhamos
Typesetting._layout_typesetting_units do BabelDOC para, depois do layout normal
(alinhado à esquerda), distribuir a sobra até a margem direita entre os espaços
de cada linha — justificação clássica, como no PDF original.

Invariantes do BabelDOC 0.6.x dos quais este patch depende (verificados na
v0.6.2, a pinada pelo pdf2zh-next 2.9.0):
  - unidades char/unicode são ancoradas exatamente no current_y da linha
    (permite reconstruir as linhas por igualdade de y);
  - relocate() cria unidades novas a cada layout (mutar o resultado é seguro);
  - render() lê as coordenadas dos payloads (char.box / formular.* / unit.x);
  - relocation_transform de curvas/forms é (a, b, c, d, e, f) com tx no índice 4.

Se o upstream mudar qualquer um deles, o wrap loga o erro e devolve o layout
original intacto — a tradução nunca quebra por causa do patch.
"""

import logging
import os

logger = logging.getLogger("traduzia.justify")

# Nunca esticar um espaço além de _MAX_STRETCH x a largura mediana dos espaços
# da linha: linha que quebrou cedo demais (ex.: fórmula larga) fica à esquerda.
_MAX_STRETCH = 2.0
_EPS = 0.05  # tolerância em pt para "linha já encosta na margem"


def _shift_box(box, dx):
    box.x += dx
    box.x2 += dx


def _shift_unit(unit, dx):
    """Translada horizontalmente uma unidade já posicionada pelo layout."""
    if unit.char is not None:
        _shift_box(unit.char.box, dx)
        vb = getattr(unit.char, "visual_bbox", None)
        if vb is not None and vb.box is not None:
            _shift_box(vb.box, dx)
    elif unit.formular is not None:
        f = unit.formular
        _shift_box(f.box, dx)
        for ch in f.pdf_character:
            _shift_box(ch.box, dx)
            vb = getattr(ch, "visual_bbox", None)
            if vb is not None and vb.box is not None:
                _shift_box(vb.box, dx)
        for item in list(f.pdf_curve) + list(f.pdf_form):
            if item.box is not None:
                _shift_box(item.box, dx)
            rt = list(item.relocation_transform)
            rt[4] += dx
            item.relocation_transform = rt
    else:  # unidade unicode: render() lê unit.x
        unit.x += dx
    # largura/altura não mudam com translação; só o box cacheado fica obsoleto
    unit.box_cache = None


def _line_anchor_y(unit):
    """y exato em que o layout ancorou a unidade (fórmulas não ancoram)."""
    if unit.char is not None:
        return unit.char.box.y
    if unit.unicode is not None:
        return unit.y
    return None


def _split_lines(units):
    """Reconstrói as linhas: todo char/unicode de uma linha compartilha o mesmo
    current_y (igualdade float exata no relocate); fórmulas seguem a linha
    corrente. Fórmula sozinha na linha acaba anexada à anterior — inofensivo,
    porque a linha resultante não justifica (sobra <= 0 ou sem espaços)."""
    lines = [[]]
    line_y = None
    for u in units:
        ay = _line_anchor_y(u)
        if ay is not None and line_y is not None and abs(ay - line_y) > 1e-6:
            lines.append([])
            line_y = ay
        elif ay is not None and line_y is None:
            line_y = ay
        lines[-1].append(u)
    return lines


def _justify_line(line, box):
    content = [u for u in line if not u.is_space]
    if len(content) < 2:
        return
    rightmost = max(u.box.x2 for u in content)
    leftover = box.x2 - rightmost
    if leftover <= _EPS:
        return
    # espaços elegíveis: os que ficam entre o primeiro e o último não-espaço
    gaps = [u for u in line if u.is_space and u.box.x < rightmost]
    if not gaps:
        return
    widths = sorted(u.box.x2 - u.box.x for u in gaps)
    median_gap = widths[len(widths) // 2]
    extra = leftover / len(gaps)
    if median_gap <= 0 or extra > _MAX_STRETCH * median_gap:
        return
    shift = 0.0
    gap_ids = {id(u) for u in gaps}
    for u in line:
        if shift:
            _shift_unit(u, shift)
        if id(u) in gap_ids:
            shift += extra


def _justify_units(units, box):
    lines = _split_lines(units)
    # a última linha de um parágrafo nunca é justificada (convenção tipográfica)
    for line in lines[:-1]:
        _justify_line(line, box)


def _install():
    from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting

    if getattr(Typesetting._layout_typesetting_units, "_traduzia_justify", False):
        return
    orig = Typesetting._layout_typesetting_units

    def wrapped(self, typesetting_units, box, scale, line_skip, paragraph,
                use_english_line_break=True):
        units, all_fit = orig(self, typesetting_units, box, scale, line_skip,
                              paragraph, use_english_line_break)
        try:
            _justify_units(units, box)
        except Exception:
            logger.exception("justificação falhou; parágrafo fica alinhado à esquerda")
        return units, all_fit

    wrapped._traduzia_justify = True
    wrapped.__wrapped__ = orig
    Typesetting._layout_typesetting_units = wrapped
    logger.info("patch de justificação do traduzia ativo")


if os.environ.get("TRADUZIR_JUSTIFY") == "1":
    try:
        _install()
    except Exception:  # babeldoc ausente ou API mudou: segue sem justificar
        logger.exception("patch de justificação não instalado")

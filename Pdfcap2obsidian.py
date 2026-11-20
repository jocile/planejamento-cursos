#!/usr/bin/env python3
"""Converte capítulos ou seções de um PDF em notas Markdown do Obsidian.

Diferente do ``pdf2obsidian.py`` (que converte o PDF inteiro), este script:

  * detecta o sumário do PDF (marcadores embutidos, sumário impresso ou títulos
    por tamanho de fonte) e permite escolher o que converter, de forma
    interativa ou por parâmetro;
  * converte só o trecho escolhido, cortando no ponto exato em que o título
    aparece na página (vários títulos podem dividir a mesma página);
  * preserva o texto palavra por palavra. Só muda a forma: parágrafos,
    títulos, negrito/itálico, callouts e frontmatter. Ao final, compara o
    resultado com o texto extraído do PDF e avisa se alguma palavra mudou.

Requisitos: Python 3.10+  ->  pip install pymupdf

Exemplos:
    python pdfcap2obsidian.py livro.pdf --list
    python pdfcap2obsidian.py livro.pdf                       # pergunta o que converter
    python pdfcap2obsidian.py livro.pdf -s "Marcos 1" -o vault/estudos
    python pdfcap2obsidian.py livro.pdf -s 14 16-18 --merge --title "Marcos 1 (trecho)"
    python pdfcap2obsidian.py livro.pdf --pages 14-49 --title "Trecho sem sumário"
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

import pymupdf

logger = logging.getLogger("pdfcap2obsidian")

NOTE_OPEN, NOTE_CLOSE = "\ue000", "\ue001"  # delimitam a marca de nota de rodapé
NOTE_MARK = re.compile(f"{NOTE_OPEN}(\\d+){NOTE_CLOSE}")
INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')
SENTENCE_END = tuple('.!?:;…)"”’»]')


class ConversionError(Exception):
    """Erro ao ler ou converter o PDF."""


# --------------------------------------------------------------------------- #
# Modelo de dados
# --------------------------------------------------------------------------- #


@dataclass
class Run:
    """Trecho de texto com um único estilo."""

    text: str
    bold: bool = False
    italic: bool = False


@dataclass
class Line:
    """Uma linha visual da página, já unida (spans na mesma linha de base)."""

    page: int  # base 0
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    runs: list[Run]
    idx: int = -1

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)


@dataclass
class TocEntry:
    """Item do sumário e, depois de localizado, a posição do seu título."""

    level: int
    title: str
    page: int  # base 1
    n: int = 0  # número exibido na listagem (base 1)
    start: int | None = None  # primeira linha do título
    hend: int | None = None  # linha seguinte ao título


@dataclass
class Layout:
    """Todo o texto do PDF em linhas, mais as estatísticas de layout."""

    lines: list[Line]
    page_first: dict[int, int]
    page_end: dict[int, int]  # exclusivo
    notes: list[str]
    margin: float
    right: float
    leading: float
    n_pages: int


# --------------------------------------------------------------------------- #
# Utilitários de texto
# --------------------------------------------------------------------------- #


def norm(text: str) -> str:
    """Forma de comparação: sem acento, sem caixa, só letras e números."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", plain.casefold())


def sanitize_name(name: str) -> str:
    """Remove caracteres que quebram nomes de arquivo ou links no Obsidian."""
    cleaned = INVALID_NAME_CHARS.sub(" ", name)
    return re.sub(r"\s+", " ", cleaned).strip(" .") or "sem-titulo"


def core_title(title: str) -> str:
    """Título sem a referência final (``Título - Mar. 1:1-4`` -> ``Título``)."""
    title = re.sub(r"\s+", " ", title).strip()
    for sep in (" - ", " – ", " — "):
        cut = title.rfind(sep)
        if cut > 0 and re.search(r"\d", title[cut + len(sep) :]):
            return title[:cut].strip()
    return title


def escape_md(text: str) -> str:
    """Escapa o que o Markdown/Obsidian interpretaria, sem mudar o texto."""
    text = text.replace("\\", "\\\\")
    for char in "*_`$":
        text = text.replace(char, "\\" + char)
    text = text.replace("<", "\\<")
    for token in ("[[", "]]", "~~", "=="):
        text = text.replace(token, "".join("\\" + c for c in token))
    return text


def escape_block_start(text: str) -> str:
    """Impede que o início do parágrafo vire título, citação ou lista."""
    if match := re.match(r"^(\d+)([.)])(\s)", text):
        return f"{match.group(1)}\\{match.group(2)}{text[match.end(1) + 1 :]}"
    if re.match(r"^(#{1,6}\s|[>+\-]\s?|={3,}|-{3,})", text):
        return "\\" + text
    return text


def yaml_quote(text: str) -> str:
    """Texto entre aspas duplas válido em YAML (JSON é um subconjunto)."""
    return json.dumps(text, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Extração do PDF
# --------------------------------------------------------------------------- #


def _style(span: dict) -> tuple[bool, bool]:
    font = span["font"].lower()
    bold = bool(span["flags"] & 16) or "bold" in font
    italic = bool(span["flags"] & 2) or "italic" in font or "oblique" in font
    return bold, italic


def page_lines(page: pymupdf.Page, page_no: int) -> list[Line]:
    """Agrupa os spans da página em linhas visuais, na ordem de leitura.

    O agrupamento é por sobreposição vertical, e não pelas "linhas" do PyMuPDF,
    porque alguns PDFs partem uma linha justificada em vários fragmentos.
    """
    spans = [
        span
        for block in page.get_text("dict")["blocks"]
        if block.get("type") == 0
        for raw in block["lines"]
        for span in raw["spans"]
        if span["text"]
    ]
    spans.sort(key=lambda s: (s["bbox"][1], s["bbox"][0]))
    groups: list[list[dict]] = []
    boxes: list[list[float]] = []  # y0, y1 de cada grupo
    for span in spans:
        top, bottom = span["bbox"][1], span["bbox"][3]
        for position in range(len(groups) - 1, max(len(groups) - 4, -1), -1):
            g_top, g_bottom = boxes[position]
            overlap = min(bottom, g_bottom) - max(top, g_top)
            if overlap >= 0.4 * min(bottom - top, g_bottom - g_top):
                groups[position].append(span)
                boxes[position] = [min(g_top, top), max(g_bottom, bottom)]
                break
        else:
            groups.append([span])
            boxes.append([top, bottom])

    lines: list[Line] = []
    for group in groups:
        group.sort(key=lambda s: s["bbox"][0])
        runs: list[Run] = []
        previous = None
        for span in group:
            text = span["text"]
            if previous is not None:
                gap = span["bbox"][0] - previous["bbox"][2]
                if not previous["text"][-1:].isspace() and not text[:1].isspace():
                    if gap > 20:
                        runs.append(Run("   "))  # lacuna larga: coluna/tabulação
                    elif gap > 1.5:
                        runs.append(Run(" "))
            bold, italic = _style(span)
            runs.append(Run(text, bold, italic))
            previous = span
        if not "".join(run.text for run in runs).strip():
            continue
        main = max(group, key=lambda s: len(s["text"].strip()))
        lines.append(
            Line(
                page=page_no,
                x0=min(s["bbox"][0] for s in group),
                y0=min(s["bbox"][1] for s in group),
                x1=max(s["bbox"][2] for s in group),
                y1=max(s["bbox"][3] for s in group),
                size=main["size"],
                runs=runs,
            )
        )
    lines.sort(key=lambda ln: (ln.y0, ln.x0))
    return lines


def load_layout(doc: pymupdf.Document) -> Layout:
    """Lê o PDF inteiro, remove cabeçalhos/rodapés e separa notas de rodapé."""
    pages = [(page.rect.height, page_lines(page, no)) for no, page in enumerate(doc)]

    # Cabeçalho/rodapé: texto que se repete (números de página viram "#").
    zone_counts: Counter[str] = Counter()
    for height, lines in pages:
        zone_counts.update(
            {
                re.sub(r"\d+", "#", norm(ln.text) or "")
                + "|"
                + re.sub(r"\d+", "#", ln.text.strip())
                for ln in lines
                if ln.y0 < 0.10 * height or ln.y1 > 0.93 * height
            }
        )
    threshold = max(3, int(0.2 * len(pages)))
    repeated = {key for key, count in zone_counts.items() if count >= threshold}

    def is_running(ln: Line, height: float) -> bool:
        if not (ln.y0 < 0.10 * height or ln.y1 > 0.93 * height):
            return False
        key = (
            re.sub(r"\d+", "#", norm(ln.text) or "")
            + "|"
            + re.sub(r"\d+", "#", ln.text.strip())
        )
        return key in repeated

    body_pages = [
        (height, [ln for ln in lines if not is_running(ln, height)])
        for height, lines in pages
    ]

    sizes: Counter[float] = Counter()
    for _, lines in body_pages:
        for ln in lines:
            sizes[round(ln.size * 2) / 2] += len(ln.text.strip())
    body_size = sizes.most_common(1)[0][0] if sizes else 12.0

    # Notas de rodapé: linha pequena iniciada por "*" e as que vêm abaixo dela.
    notes: list[str] = []
    flow: list[Line] = []
    page_first: dict[int, int] = {}
    page_end: dict[int, int] = {}
    for page_no, (_, lines) in enumerate(body_pages):
        foot_start = next(
            (
                k
                for k, ln in enumerate(lines)
                if ln.text.lstrip().startswith("*") and ln.size <= 0.85 * body_size
            ),
            None,
        )
        page_notes: list[str] = []
        if foot_start is not None:
            chunk: list[str] = []
            for ln in lines[foot_start:]:
                text = ln.text.strip()
                if text.startswith("*") and chunk:
                    page_notes.append(" ".join(chunk))
                    chunk = []
                chunk.append(text.lstrip("*").strip())
            page_notes.append(" ".join(chunk))
            lines = lines[:foot_start]
        # liga cada "*" do corpo a uma nota, na ordem em que aparecem
        pending = list(page_notes)
        for ln in lines:
            for run in ln.runs:
                while pending and "*" in run.text:
                    notes.append(pending.pop(0))
                    run.text = run.text.replace(
                        "*", f"{NOTE_OPEN}{len(notes) - 1}{NOTE_CLOSE}", 1
                    )
        page_first[page_no] = len(flow)
        for ln in lines:
            ln.idx = len(flow)
            flow.append(ln)
        page_end[page_no] = len(flow)

    solid = [ln for ln in flow if len(ln.text.strip()) >= 40]
    margin = Counter(round(ln.x0) for ln in solid).most_common(1)[0][0] if solid else 0
    xs = sorted(ln.x1 for ln in solid)
    right = xs[int(0.9 * (len(xs) - 1))] if xs else 0
    gaps = [
        b.y0 - a.y0
        for a, b in zip(flow, flow[1:])
        if a.page == b.page and 8 < b.y0 - a.y0 < 40
    ]
    leading = sorted(gaps)[len(gaps) // 2] if gaps else 14.0
    return Layout(flow, page_first, page_end, notes, margin, right, leading, len(pages))


# --------------------------------------------------------------------------- #
# Sumário
# --------------------------------------------------------------------------- #


def toc_from_outline(doc: pymupdf.Document) -> list[TocEntry]:
    """Marcadores (bookmarks) embutidos no PDF: a fonte mais confiável."""
    return [
        TocEntry(level, re.sub(r"\s+", " ", title).strip(), page)
        for level, title, page in doc.get_toc(simple=True)
        if page >= 1 and title.strip()
    ]


PRINTED_TOC = re.compile(
    r"^(?P<title>\S.*?\S)\s*(?:\.{2,}|·{2,}|…+|_{2,}|\s{3,})\s*(?P<page>\d{1,4})\s*$"
)


def toc_from_printed(
    layout: Layout, toc_pages: int, page_offset: int | None
) -> list[TocEntry]:
    """Lê um sumário impresso (``Título ..... 12``) nas primeiras páginas."""
    found: list[tuple[Line, str, int]] = []
    for page_no in range(min(toc_pages, layout.n_pages)):
        rows = []
        for ln in layout.lines[layout.page_first[page_no] : layout.page_end[page_no]]:
            if match := PRINTED_TOC.match(ln.text.strip()):
                rows.append(
                    (ln, match.group("title").strip(" ."), int(match.group("page")))
                )
        if len(rows) >= 4:
            found.extend(rows)
    if not found:
        return []

    def hits(offset: int) -> int:
        total = 0
        for _, title, printed in found:
            page_no = printed + offset - 1
            if 0 <= page_no < layout.n_pages:
                wanted = norm(core_title(title))
                span = layout.lines[
                    layout.page_first[page_no] : layout.page_end[page_no]
                ]
                total += any(norm(ln.text) == wanted for ln in span)
        return total

    if page_offset is None:
        page_offset = max(range(-40, 61), key=lambda o: (hits(o), -abs(o)))
        if hits(page_offset) < 2:
            page_offset = 0
    indents = sorted({round(ln.x0 / 6) for ln, _, _ in found})
    return [
        TocEntry(indents.index(round(ln.x0 / 6)) + 1, title, printed + page_offset)
        for ln, title, printed in found
        if 1 <= printed + page_offset <= layout.n_pages
    ]


def toc_from_fonts(layout: Layout) -> list[TocEntry]:
    """Último recurso: linhas curtas, maiores que o corpo ou em negrito."""
    sizes: Counter[float] = Counter()
    for ln in layout.lines:
        sizes[round(ln.size * 2) / 2] += len(ln.text.strip())
    body = sizes.most_common(1)[0][0]
    candidates = [
        ln
        for ln in layout.lines
        if 3 <= len(ln.text.strip()) <= 90
        and not ln.text.strip().endswith((",", ";"))
        and (
            ln.size >= 1.15 * body
            or (all(r.bold for r in ln.runs if r.text.strip()) and ln.size >= body)
        )
    ]
    ranking = sorted({round(ln.size * 2) / 2 for ln in candidates}, reverse=True)[:4]
    entries: list[TocEntry] = []
    for ln in candidates:
        size = round(ln.size * 2) / 2
        if size not in ranking:
            continue
        previous = entries[-1] if entries else None
        text = re.sub(r"\s+", " ", ln.text).strip()
        if (
            previous
            and previous.hend == ln.idx
            and layout.lines[previous.start].size == ln.size
        ):  # título quebrado em duas linhas
            previous.title += " " + text
            previous.hend = ln.idx + 1
            continue
        entry = TocEntry(ranking.index(size) + 1, text, ln.page + 1)
        entry.start, entry.hend = ln.idx, ln.idx + 1
        entries.append(entry)
    return entries


def locate_entries(layout: Layout, entries: list[TocEntry]) -> None:
    """Acha a linha exata de cada título (vários títulos podem dividir a página).

    A busca começa na página do marcador e segue em ordem, então o sumário
    impresso (outra página) e títulos repetidos não atrapalham. Se o título do
    marcador tiver um erro de digitação em relação ao texto, usa busca aproximada.
    """
    previous = -1
    lines = layout.lines
    for entry in entries:
        if entry.start is not None:  # já localizado (sumário por fontes)
            previous = entry.hend - 1
            continue
        wanted = {norm(core_title(entry.title)), norm(entry.title)} - {""}
        pages = [
            p
            for p in (entry.page - 1, entry.page, entry.page - 2)
            if 0 <= p < layout.n_pages
        ]

        def candidates(page_no: int):
            first = max(layout.page_first[page_no], previous + 1)
            for k in range(first, layout.page_end[page_no]):
                for span in (1, 2, 3):
                    chunk = lines[k : k + span]
                    if len(chunk) == span and all(c.page == page_no for c in chunk):
                        yield k, k + span, norm("".join(c.text for c in chunk))

        found = None
        for page_no in pages:
            found = next(
                ((k, e) for k, e, text in candidates(page_no) if text in wanted), None
            )
            if found:
                break
        if not found:
            target = norm(core_title(entry.title))
            for page_no in pages:
                best = max(
                    (
                        (SequenceMatcher(None, text, target).ratio(), k, e)
                        for k, e, text in candidates(page_no)
                        if len(text) >= 4
                    ),
                    default=None,
                )
                if best and best[0] >= 0.82:
                    found = (best[1], best[2])
                    logger.warning(
                        "Título aproximado: sumário %r x texto %r",
                        entry.title,
                        " ".join(c.text.strip() for c in lines[best[1] : best[2]]),
                    )
                    break
        if found:
            entry.start, entry.hend = found
            previous = entry.hend - 1
        else:
            logger.warning(
                "Título não localizado na página %d: %r", entry.page, entry.title
            )


def build_toc(
    doc: pymupdf.Document, layout: Layout, args: argparse.Namespace
) -> list[TocEntry]:
    """Escolhe a melhor fonte de sumário e localiza os títulos no texto."""
    order = {"auto": ("outline", "printed", "fonts")}.get(args.toc, (args.toc,))
    entries: list[TocEntry] = []
    for source in order:
        if source == "outline":
            entries = toc_from_outline(doc)
        elif source == "printed":
            entries = toc_from_printed(layout, args.toc_pages, args.page_offset)
        else:
            entries = toc_from_fonts(layout)
        if entries:
            logger.info("Sumário: %d itens (fonte: %s)", len(entries), source)
            break
    for number, entry in enumerate(entries, 1):
        entry.n = number
    locate_entries(layout, entries)
    return entries


# --------------------------------------------------------------------------- #
# Seleção
# --------------------------------------------------------------------------- #


def print_toc(entries: list[TocEntry], max_level: int | None = None) -> None:
    """Mostra o sumário numerado, com recuo por nível."""
    base = min((e.level for e in entries), default=1)
    for entry in entries:
        if max_level is None or entry.level <= max_level:
            indent = "  " * (entry.level - base)
            print(f"{entry.n:>4}  {indent}{entry.title}  (p. {entry.page})")


def resolve_selection(tokens: list[str], entries: list[TocEntry]) -> list[TocEntry]:
    """Interpreta números, intervalos, ``level:N``, ``re:regex`` e títulos."""
    chosen: dict[int, TocEntry] = {}
    for raw in tokens:
        for token in filter(None, (t.strip() for t in re.split(r"[;,]", raw))):
            matches: list[TocEntry]
            if token.lower() in {"all", "todos", "tudo"}:
                matches = list(entries)
            elif m := re.fullmatch(r"(\d+)\s*-\s*(\d+)", token):
                low, high = sorted((int(m.group(1)), int(m.group(2))))
                matches = [e for e in entries if low <= e.n <= high]
            elif token.isdigit():
                matches = [e for e in entries if e.n == int(token)]
            elif m := re.fullmatch(r"(?:level|nivel|nível):(\d+)", token, re.I):
                matches = [e for e in entries if e.level == int(m.group(1))]
            elif token.lower().startswith("re:"):
                pattern = re.compile(token[3:], re.I)
                matches = [e for e in entries if pattern.search(e.title)]
            else:
                wanted = norm(token)
                exact = [
                    e
                    for e in entries
                    if wanted in (norm(e.title), norm(core_title(e.title)))
                ]
                matches = exact or [e for e in entries if wanted in norm(e.title)]
            if not matches:
                raise ValueError(f"Nada no sumário corresponde a {token!r}.")
            chosen.update({e.n: e for e in matches})
    selected = sorted(chosen.values(), key=lambda e: e.n)
    # itens dentro de um capítulo já escolhido seriam duplicados
    pruned: list[TocEntry] = []
    for entry in selected:
        parent = next(
            (
                p
                for p in pruned
                if p.n < entry.n and entry.n < next_boundary(entries, p)
            ),
            None,
        )
        if parent:
            logger.info("%r já está dentro de %r; ignorado.", entry.title, parent.title)
        else:
            pruned.append(entry)
    return pruned


def next_boundary(entries: list[TocEntry], entry: TocEntry) -> int:
    """Número do próximo item de nível igual ou superior (fim do trecho)."""
    return next(
        (e.n for e in entries[entry.n :] if e.level <= entry.level), len(entries) + 1
    )


def ask_selection(entries: list[TocEntry]) -> list[str]:
    """Pergunta no terminal o que converter."""
    if not sys.stdin.isatty():
        raise ConversionError(
            "Sem terminal para perguntar. Use --sections (ou --pages)."
        )
    print_toc(entries)
    print(
        "\nO que converter? Números (14), intervalos (14-16), títulos (Marcos 1),\n"
        "level:3, re:regex ou 'todos'. Separe vários com vírgula."
    )
    return [input("Seleção: ").strip()]


# --------------------------------------------------------------------------- #
# Conversão para Markdown
# --------------------------------------------------------------------------- #


def normalize_runs(runs: list[Run]) -> list[Run]:
    """Une trechos vizinhos de mesmo estilo; espaços ficam fora de ``*...*``."""

    def merge(items: list[Run]) -> list[Run]:
        merged: list[Run] = []
        for run in items:
            if not run.text:
                continue
            if merged and (merged[-1].bold, merged[-1].italic) == (
                run.bold,
                run.italic,
            ):
                merged[-1].text += run.text
            else:
                merged.append(Run(run.text, run.bold, run.italic))
        return merged

    merged = merge(runs)
    styles = [(r.bold, r.italic) for r in merged]
    for k, run in enumerate(merged):
        if run.text.strip():
            continue
        before = styles[k - 1] if k > 0 else None
        after = styles[k + 1] if k + 1 < len(merged) else None
        run.bold, run.italic = before if before and before == after else (False, False)
    return merge(merged)


def runs_to_md(runs: list[Run]) -> str:
    """Converte trechos em Markdown, escapando o que for sintaxe."""
    parts: list[str] = []
    for run in normalize_runs(runs):
        core = run.text.strip()
        if not core:
            parts.append(" ")
            continue
        lead = run.text[: len(run.text) - len(run.text.lstrip())]
        trail = run.text[len(run.text.rstrip()) :]
        mark = (
            "***"
            if run.bold and run.italic
            else "**"
            if run.bold
            else "*"
            if run.italic
            else ""
        )
        parts.append(f"{lead}{mark}{escape_md(core)}{mark}{trail}")
    text = re.sub(r"[ \t\u00a0]+", " ", "".join(parts)).strip()
    return escape_block_start(text)


def lines_to_runs(lines: list[Line]) -> list[Run]:
    """Junta as linhas de um parágrafo. Hífen no fim da linha é mantido.

    Neste tipo de PDF o hífen de fim de linha é de palavra composta
    (``confessá-`` + ``lo`` = ``confessá-lo``), então a junção é sem espaço e o
    hífen continua lá. Isso difere do pdf2obsidian.py, que remove o hífen.
    """
    runs: list[Run] = []
    for k, line in enumerate(lines):
        current = [Run(r.text, r.bold, r.italic) for r in line.runs]
        if k:
            while runs and not runs[-1].text.strip():
                runs.pop()
            if runs:
                runs[-1].text = runs[-1].text.rstrip()
            tail = "".join(r.text for r in runs)
            head = line.text.lstrip()
            glue = (
                tail.endswith("-")
                and len(tail) > 1
                and tail[-2].isalpha()
                and head[:1].islower()
            )
            if current:
                current[0].text = current[0].text.lstrip()
            if not glue:
                runs.append(Run(" "))
        runs.extend(current)
    return runs


def split_callouts(runs: list[Run], enabled: bool) -> list[tuple[str, list[Run]]]:
    """Separa citações em itálico: parágrafo todo em itálico, ou frase inicial longa."""
    visible = [r for r in runs if r.text.strip()]
    if not enabled or not visible:
        return [("p", runs)]
    total = sum(len(r.text.strip()) for r in visible)
    if all(r.italic for r in visible) and total >= 20:
        return [("quote", runs)]
    lead: list[Run] = []
    for k, run in enumerate(runs):
        if run.italic or not run.text.strip():
            lead.append(run)
            continue
        rest = runs[k:]
        text = "".join(r.text for r in lead).strip()
        if (
            len(text) >= 50
            and text.endswith(SENTENCE_END)
            and any(r.italic for r in lead)
        ):
            return [("quote", lead), ("p", rest)]
        break
    return [("p", runs)]


@dataclass
class RenderContext:
    """Estado compartilhado ao renderizar um trecho."""

    layout: Layout
    link_refs: bool = False
    callouts: bool = True
    footnote_defs: list[str] = field(default_factory=list)

    def resolve_notes(self, text: str) -> str:
        """Troca as marcas de nota de rodapé por ``[^n]`` e registra a definição."""

        def swap(match: re.Match[str]) -> str:
            self.footnote_defs.append(self.layout.notes[int(match.group(1))])
            return f"[^{len(self.footnote_defs)}]"

        return NOTE_MARK.sub(swap, text)


def new_paragraph(prev: Line, line: Line, layout: Layout) -> bool:
    """Decide se ``line`` abre um parágrafo novo (recuo, espaço ou fim curto)."""
    if line.x0 > layout.margin + 10:
        return True
    prev_text = prev.text.rstrip()
    short = prev.x1 < layout.right - 60
    if prev.page == line.page and line.y0 - prev.y0 > 1.5 * layout.leading:
        return True
    if short and prev_text.endswith(SENTENCE_END):
        return True
    bold_only = all(r.bold for r in prev.runs if r.text.strip())
    return bool(
        bold_only and short and not all(r.bold for r in line.runs if r.text.strip())
    )


def heading_text(layout: Layout, entry: TocEntry) -> str:
    """Texto do título: palavras do PDF, com a caixa do sumário quando ele a dá."""
    if entry.start is None:
        return core_title(entry.title)
    pdf = re.sub(
        r"\s+", " ", " ".join(l.text for l in layout.lines[entry.start : entry.hend])
    ).strip()
    letters = [c for c in pdf if c.isalpha()]
    if not letters or not all(c.isupper() for c in letters):
        return pdf
    toc_words = {norm(w): w for w in core_title(entry.title).split()}
    words = []
    for word in pdf.split():
        candidate = toc_words.get(norm(word))
        # palavra do sumário com maiúscula no meio é erro de digitação: ignora
        words.append(
            candidate
            if candidate and not re.search(r"[a-zà-ÿ][A-ZÀ-Ý]", candidate)
            else word.lower()
        )
    words[0] = words[0][:1].upper() + words[0][1:]
    return " ".join(words)


def slice_bounds(
    layout: Layout, entries: list[TocEntry], entry: TocEntry
) -> tuple[int, int]:
    """Intervalo de linhas do trecho: do fim do título até o próximo de mesmo nível."""
    end_entry = next((e for e in entries[entry.n :] if e.level <= entry.level), None)
    if entry.hend is not None:
        start = entry.hend
    else:
        start = layout.page_first[min(entry.page - 1, layout.n_pages - 1)]
    if end_entry is None:
        end = len(layout.lines)
    elif end_entry.start is not None:
        end = end_entry.start
    else:
        end = layout.page_first[min(end_entry.page - 1, layout.n_pages - 1)]
    return start, max(start, end)


def drop_chapter_index(layout: Layout, children: list[TocEntry], start: int) -> int:
    """Pula o índice do capítulo (lista dos títulos das seções antes da primeira)."""
    first = next(
        (c for c in children if c.start is not None and c.start >= start), None
    )
    if first is None or first.start == start:
        return start
    titles = {norm(core_title(c.title)) for c in children}
    before = layout.lines[start : first.start]
    listed = sum(any(norm(ln.text).startswith(t) for t in titles if t) for ln in before)
    return first.start if listed >= 0.6 * len(before) else start


def render_slice(
    ctx: RenderContext,
    entries: list[TocEntry],
    entry: TocEntry | None,
    start: int,
    end: int,
    children: list[TocEntry],
) -> tuple[str, list[Line]]:
    """Renderiza as linhas [start, end). Retorna o Markdown e as linhas usadas."""
    layout = ctx.layout
    lines = layout.lines
    in_slice = [c for c in children if c.start is not None and start <= c.start < end]
    headings = {c.start: c for c in in_slice}
    levels = sorted({c.level for c in in_slice})
    base_level = 3 if entry is not None else 2
    level_of = {lv: min(base_level + rank, 6) for rank, lv in enumerate(levels)}
    top_level = levels[0] if levels else None

    out: list[str] = []
    used: list[Line] = []
    para: list[Line] = []
    after_heading = False

    def emit_paragraph() -> None:
        nonlocal after_heading
        if not para:
            return
        used.extend(para)
        runs = lines_to_runs(para)
        for kind, part in split_callouts(runs, ctx.callouts):
            if kind == "quote":
                plain = [Run(r.text, r.bold, False) for r in part]
                body = ctx.resolve_notes(runs_to_md(plain))
                out.append("> [!quote]\n> " + body)
                continue
            md = ctx.resolve_notes(runs_to_md(part))
            if after_heading and ctx.link_refs:
                md = link_reference(md)
            out.append(md)
            after_heading = False
        para.clear()

    i = start
    previous: Line | None = None
    while i < end:
        if i in headings:
            emit_paragraph()
            child = headings[i]
            if child.level == top_level and out:
                out.append("---")
            used.extend(lines[child.start : child.hend])
            out.append(f"{'#' * level_of[child.level]} {heading_text(layout, child)}")
            i = child.hend
            previous = None
            after_heading = True
            continue
        line = lines[i]
        if previous is not None and new_paragraph(previous, line, layout):
            emit_paragraph()
        para.append(line)
        previous = line
        i += 1
    emit_paragraph()
    if ctx.footnote_defs:
        out.append(
            "\n".join(
                f"[^{n}]: {escape_md(t)}" for n, t in enumerate(ctx.footnote_defs, 1)
            )
        )
    return "\n\n".join(out), used


def link_reference(md: str) -> str:
    """``**Marcos 1:1-4**`` -> ``**[[Marcos 1#1|Marcos 1:1-4]]**`` (texto idêntico)."""
    match = re.fullmatch(
        r"\*\*(?P<ref>(?P<book>.+?)\s+(?P<ch>\d+):(?P<v>\d+)[^*]*)\*\*", md
    )
    if not match:
        return md
    return f"**[[{match['book']} {match['ch']}#{match['v']}|{match['ref']}]]**"


# --------------------------------------------------------------------------- #
# Verificação de fidelidade
# --------------------------------------------------------------------------- #


def squash(text: str) -> str:
    """Forma comparável: sem espaços, sem ``*``, sem caixa."""
    return re.sub(r"\s+", "", text.replace("*", "")).casefold()


def reference_from_pdf(used: list[Line], notes: list[str]) -> str:
    """Texto do PDF para as linhas usadas (mais as notas de rodapé ligadas)."""
    raw = "".join(NOTE_MARK.sub("", ln.text) for ln in used)
    return squash(raw + "".join(notes))


def comparable_from_md(markdown: str) -> str:
    """Remove só a sintaxe que o conversor adicionou e deixa as palavras."""
    kept = []
    for line in markdown.split("\n"):
        if line.strip() == "---":
            continue
        kept.append(re.sub(r"^\s*(?:#{1,6}\s+|>\s?)+", "", line))
    text = "\n".join(kept)
    text = re.sub(r"\[!\w+\]", "", text)
    text = re.sub(r"\[\^\d+\]:?", "", text)
    text = re.sub(r"\[\[[^\]|]*\|", "", text).replace("]]", "")
    text = re.sub(r"\\(.)", r"\1", text)
    return squash(text)


def verify(markdown: str, reference: str) -> str | None:
    """Retorna None se o texto é idêntico, ou uma descrição da primeira diferença."""
    produced = comparable_from_md(markdown)
    if produced == reference:
        return None
    at = next(
        (k for k, (a, b) in enumerate(zip(produced, reference)) if a != b),
        min(len(produced), len(reference)),
    )
    return (
        f"primeira diferença no caractere {at}: "
        f"saída …{produced[max(0, at - 30) : at + 30]}… x PDF …{reference[max(0, at - 30) : at + 30]}… "
        f"(saída {len(produced)} x PDF {len(reference)} caracteres)"
    )


# --------------------------------------------------------------------------- #
# Notas
# --------------------------------------------------------------------------- #


def build_frontmatter(
    title: str, style: str, tags: list[str], draft: bool, description: str
) -> str:
    """Frontmatter no estilo do Digital Garden, do Quartz ou nenhum."""
    if style == "none":
        return ""
    lines = ["---", f"title: {yaml_quote(title)}"]
    if style == "quartz":
        lines += [
            f"description: {yaml_quote(description)}",
            f"date: '{date.today().isoformat()}'",
            f"draft: {str(draft).lower()}",
        ]
    lines.append("tags:" if tags else "tags: []")
    lines += [f"  - {tag}" for tag in tags]
    if style == "dg":
        lines.append(f"dg-publish: {str(not draft).lower()}")
    lines += ["---", ""]
    return "\n".join(lines)


def convert_entry(
    layout: Layout, entries: list[TocEntry], entry: TocEntry, args: argparse.Namespace
) -> tuple[str, str | None, int]:
    """Converte um item do sumário. Retorna (corpo, aviso de fidelidade, nº de palavras)."""
    start, end = slice_bounds(layout, entries, entry)
    children = [e for e in entries[entry.n : next_boundary(entries, entry) - 1]]
    if children and not args.keep_index:
        start = drop_chapter_index(layout, children, start)
    ctx = RenderContext(layout, link_refs=args.link_refs, callouts=not args.no_callouts)
    body, used = render_slice(ctx, entries, entry, start, end, children)
    issue = None
    if not args.no_verify:
        issue = verify(body, reference_from_pdf(used, ctx.footnote_defs))
    heading = f"## {heading_text(layout, entry)}"
    return f"{heading}\n\n{body}".rstrip(), issue, len(body.split())


def title_for(entry: TocEntry, book: str, args: argparse.Namespace) -> str:
    """Título da nota a partir do modelo (``{book}``, ``{title}``, ``{n}``)."""
    number = re.search(r"(\d+)\s*$", core_title(entry.title))
    return args.title_template.format(
        book=book, title=core_title(entry.title), n=number.group(1) if number else ""
    ).strip(" -")


def write_note(
    path: Path, front: str, title: str, panel: str | None, body: str
) -> None:
    """Grava a nota: frontmatter, H1, link de volta opcional e o corpo."""
    head = [front.rstrip("\n"), "", f"# {title}", ""] if front else [f"# {title}", ""]
    if panel:
        head += [f"← [[{panel}|Voltar ao Painel Geral]]", "", "---", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(head).lstrip("\n") + "\n" + body.strip() + "\n")


def parse_pages(spec: str, total: int) -> tuple[int, int]:
    """``14-49`` -> (13, 48) em base 0; ``20-`` vai até o fim."""
    match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d*))?", spec.strip())
    if not match:
        raise ValueError(f"Intervalo de páginas inválido: {spec!r}")
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) else (total if "-" in spec else first)
    return first - 1, min(last, total) - 1


def run(args: argparse.Namespace) -> int:
    """Fluxo principal. Retorna 0 (ok), 1 (erro) ou 2 (texto diferente do PDF)."""
    if not args.pdf.is_file():
        logger.error("PDF não encontrado: %s", args.pdf)
        return 1
    try:
        doc = pymupdf.open(args.pdf)
    except Exception as exc:  # pymupdf lança vários tipos de erro
        logger.error("Não foi possível abrir %s: %s", args.pdf, exc)
        return 1
    with doc:
        layout = load_layout(doc)
        if not layout.lines:
            logger.error("Sem texto extraível (PDF escaneado? rode um OCR antes).")
            return 1
        meta_title = (doc.metadata or {}).get("title", "") or ""
        book = args.book or (
            meta_title.strip() if len(meta_title.strip()) > 3 else args.pdf.stem
        )
        entries = [] if args.pages else build_toc(doc, layout, args)

    if args.pages:
        first, last = parse_pages(args.pages, layout.n_pages)
        entry = TocEntry(
            level=1, title=args.title or f"Páginas {args.pages}", page=first + 1, n=1
        )
        entry.start = entry.hend = layout.page_first[first]
        selected = [entry]
        entries = [entry]
        slice_end = layout.page_end[last]
    else:
        if not entries:
            logger.error(
                "Nenhum sumário detectado. Use --pages para converter por páginas."
            )
            return 1
        if args.list:
            print_toc(entries, args.level)
            return 0
        try:
            tokens = args.sections or ask_selection(entries)
            selected = resolve_selection(tokens, entries)
        except (ValueError, ConversionError) as exc:
            logger.error("%s", exc)
            return 1
        slice_end = None
    if not selected:
        logger.error("Nada selecionado.")
        return 1
    if args.title and len(selected) > 1 and not args.merge:
        logger.warning("--title com várias seleções: use --merge ou --title-template.")

    bodies: list[tuple[TocEntry, str]] = []
    failures = 0
    for entry in selected:
        if slice_end is not None:
            ctx = RenderContext(
                layout, link_refs=args.link_refs, callouts=not args.no_callouts
            )
            body, used = render_slice(
                ctx,
                entries,
                None,
                entry.hend,
                slice_end + 1 if False else layout.page_end[last],
                [],
            )
            issue = (
                None
                if args.no_verify
                else verify(body, reference_from_pdf(used, ctx.footnote_defs))
            )
            words = len(body.split())
        else:
            body, issue, words = convert_entry(layout, entries, entry, args)
        if issue:
            failures += 1
            logger.error("TEXTO DIFERENTE DO PDF em %r: %s", entry.title, issue)
        else:
            logger.info("Fiel ao PDF: %r (%d palavras)", entry.title, words)
        bodies.append((entry, body))

    front_args = (args.fm, args.tags, args.draft)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.merge:
        title = args.title or " + ".join(core_title(e.title) for e, _ in bodies)
        note = args.output / f"{sanitize_name(title)}.md"
        write_note(
            note,
            build_frontmatter(title, *front_args, args.description or ""),
            title,
            args.panel,
            "\n\n---\n\n".join(b for _, b in bodies),
        )
        logger.info("OK -> %s", note)
    else:
        for entry, body in bodies:
            title = (
                args.title
                if (args.title and len(bodies) == 1)
                else title_for(entry, book, args)
            )
            note = args.output / f"{sanitize_name(title)}.md"
            write_note(
                note,
                build_frontmatter(title, *front_args, args.description or ""),
                title,
                args.panel,
                body,
            )
            logger.info("OK -> %s", note)
    return 2 if failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Lê os argumentos de linha de comando."""
    p = argparse.ArgumentParser(
        description="Converte capítulos/seções de um PDF em notas Markdown do Obsidian."
    )
    p.add_argument("pdf", type=Path, help="PDF de origem")
    p.add_argument(
        "-o", "--output", type=Path, default=Path("notas"), help="Pasta de saída"
    )
    sel = p.add_argument_group("seleção")
    sel.add_argument(
        "-s",
        "--sections",
        nargs="+",
        help="Itens do sumário: número, intervalo (14-16), título, level:N ou re:regex",
    )
    sel.add_argument("--list", action="store_true", help="Só mostra o sumário e sai")
    sel.add_argument("--level", type=int, help="Com --list: mostra até este nível")
    sel.add_argument(
        "--pages",
        help='Converte um intervalo de páginas (base 1), sem sumário. Ex.: "14-49"',
    )
    sel.add_argument(
        "--toc",
        choices=("auto", "outline", "printed", "fonts"),
        default="auto",
        help="Fonte do sumário (padrão: auto)",
    )
    sel.add_argument(
        "--toc-pages",
        type=int,
        default=20,
        help="Páginas iniciais a examinar para o sumário impresso",
    )
    sel.add_argument(
        "--page-offset",
        type=int,
        help="Diferença entre a página impressa e a do PDF (sumário impresso)",
    )
    out = p.add_argument_group("saída")
    out.add_argument("--book", help="Nome do livro (padrão: metadado do PDF)")
    out.add_argument("--title", help="Título fixo da nota")
    out.add_argument(
        "--title-template",
        default="{book} - {title}",
        help="Modelo do título: {book}, {title}, {n}. Ex.: '{book} - Capítulo {n}'",
    )
    out.add_argument("--description", help="Descrição (frontmatter quartz)")
    out.add_argument("-t", "--tags", nargs="*", default=[], help="Tags do frontmatter")
    out.add_argument(
        "--fm",
        choices=("dg", "quartz", "none"),
        default="dg",
        help="Estilo do frontmatter (padrão: dg = Digital Garden)",
    )
    out.add_argument(
        "--draft", action="store_true", help="draft: true (ou dg-publish: false)"
    )
    out.add_argument(
        "--panel", help="Nota a que o link 'Voltar ao Painel Geral' aponta"
    )
    out.add_argument(
        "--merge", action="store_true", help="Junta as seleções em uma só nota"
    )
    fmt = p.add_argument_group("formatação")
    fmt.add_argument(
        "--no-callouts",
        action="store_true",
        help="Não transforma citações em itálico em callouts",
    )
    fmt.add_argument(
        "--link-refs",
        action="store_true",
        help="Transforma a referência bíblica sob o título em wikilink com alias",
    )
    fmt.add_argument(
        "--keep-index",
        action="store_true",
        help="Mantém a lista de seções que abre cada capítulo",
    )
    fmt.add_argument(
        "--no-verify", action="store_true", help="Pula a verificação de fidelidade"
    )
    return p.parse_args(argv)


def main() -> int:
    """Ponto de entrada."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    return run(parse_args())


if __name__ == "__main__":
    sys.exit(main())

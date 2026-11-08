#!/usr/bin/env python3
"""Converte PDFs do Diário de Classe e do PTD (Plano de Trabalho do Docente) do
Senac em notas Markdown para o Obsidian/Quartz.

Diferente do pdf2obsidian.py (que converte qualquer PDF), este script entende a
estrutura dos relatórios exportados do sistema acadêmico: lê o PDF por posição
(o PDF é impresso "deitado", com página rotacionada), ignora cabeçalho e rodapé
repetidos e monta a nota no padrão do vault:

    PTD     -> # Plano de Trabalho do Docente - UCx
               ## Situações de Aprendizagem -> ### SAx -> #### Aula N
               ## Avaliações / ## Contribuição ... Projeto Integrador
    Diário  -> # Diário de Classe - UCx
               ## Avaliações / ## Conhecimentos, Habilidades e Atitudes da UC
               ## Aulas -> ### dd/mm/aaaa - tema

Dados de alunos (Feedbacks e Observações do Diário) NÃO são convertidos: a nota
costuma ir para um repositório/site público e esses dados são pessoais.

Requisitos: Python 3.10+  ->  pip install pymupdf

Exemplos:
    python senac2obsidian.py PTD_2026_12_331.pdf -o vault/docs
    python senac2obsidian.py pasta_pdfs/ -o vault/docs --draft
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import pymupdf

logger = logging.getLogger("senac2obsidian")

ROW_TOLERANCE = 3.0  # pontos: linhas com y parecido formam uma "linha de tabela"
HEADER_LIMIT = {"ptd": 150.0, "diario": 118.0}  # y abaixo do qual é cabeçalho
CHA_LABELS = ("Conhecimentos", "Habilidades", "Atitudes e Valores")
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
FOOTER_RE = re.compile(r"^(Impresso em|Pág\. \d+ de \d+)")
LABEL_START = re.compile(r"^(Objetivo|Conteúdo|Atividades?)\s*:")
STOPWORDS = {"de", "da", "do", "das", "dos", "e"}
LIST_ITEM_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s")


class ConversionError(Exception):
    """Erro ao converter um PDF específico."""


# --------------------------------------------------------------------------
# Extração de linhas por posição
# --------------------------------------------------------------------------


@dataclass
class Cell:
    """Trecho de texto de uma linha, com posição visual (página já rotacionada)."""

    x: float
    x1: float
    y: float
    text: str
    bold: bool
    size: float


@dataclass
class Row:
    """Células que estão na mesma altura da página."""

    page: int
    y: float
    cells: list[Cell]

    @property
    def text(self) -> str:
        return " ".join(cell.text for cell in self.cells)

    @property
    def first(self) -> Cell:
        return self.cells[0]


def extract_rows(doc: pymupdf.Document, max_pages: int | None = None) -> list[Row]:
    """Lê todas as páginas e devolve as linhas na ordem visual de leitura."""
    rows: list[Row] = []
    for page_number, page in enumerate(doc):
        if max_pages is not None and page_number >= max_pages:
            break
        matrix = page.rotation_matrix
        cells: list[Cell] = []
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                text = re.sub(r"\s+", " ", "".join(s["text"] for s in line["spans"]))
                text = text.strip()
                if not text:
                    continue
                rect = pymupdf.Rect(line["bbox"]) * matrix
                span = line["spans"][0]
                bold = bool(span["flags"] & 16) or "bold" in span["font"].lower()
                cells.append(Cell(rect.x0, rect.x1, rect.y0, text, bold, span["size"]))
        cells.sort(key=lambda cell: (cell.y, cell.x))
        current: Row | None = None
        for cell in cells:
            if current is None or cell.y - current.y > ROW_TOLERANCE:
                current = Row(page_number, cell.y, [])
                rows.append(current)
            current.cells.append(cell)
    for row in rows:
        row.cells.sort(key=lambda cell: cell.x)
    return rows


def detect_kind(rows: list[Row]) -> str | None:
    """Reconhece o tipo de relatório pelo título da primeira página."""
    for row in rows:
        if row.page > 0:
            break
        if "Plano de Trabalho do Docente" in row.text:
            return "ptd"
        if "Diário de Classe" in row.text:
            return "diario"
    return None


def split_rows(rows: list[Row], kind: str) -> tuple[list[Row], list[Row], str | None]:
    """Separa cabeçalho (1ª página), corpo e a data de impressão do rodapé."""
    limit = HEADER_LIMIT[kind]
    header = [r for r in rows if r.page == 0 and r.y < limit]
    body: list[Row] = []
    printed: str | None = None
    for row in rows:
        if row.y < limit:
            continue
        footer = [c for c in row.cells if FOOTER_RE.match(c.text)]
        if footer:
            match = re.search(r"(\d{2})/(\d{2})/(\d{4})", row.text)
            if match and not printed:
                printed = f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
            continue
        body.append(row)
    return header, body, printed


# --------------------------------------------------------------------------
# Utilitários de texto
# --------------------------------------------------------------------------


def wrap_join(items: list[tuple[str, float]], margin: float) -> list[str]:
    """Une linhas que o PDF quebrou na margem direita (texto corrido)."""
    result: list[str] = []
    previous_x1 = 0.0
    for text, x1 in items:
        if result and previous_x1 >= margin - 30 and not LABEL_START.match(text):
            result[-1] += " " + text
        else:
            result.append(text)
        previous_x1 = x1
    return result


def add_item(items: list[str], text: str) -> None:
    """Adiciona um item de lista; junta quebras de linha (item sem ponto final)."""
    if items and not items[-1].rstrip().endswith((".", "!", "?")):
        items[-1] += " " + text
    else:
        items.append(text)


def protect(text: str) -> str:
    """Evita que *args, **kwargs, __init__ etc. virem formatação Markdown."""
    text = re.sub(r"(?<!\w)__(\w+)__(?!\w)", r"`__\1__`", text)
    if not re.search(r"\*\*[^*\n]+\*\*", text):
        text = text.replace("*", r"\*")
    return text


def slug_words(text: str) -> str:
    """ "Programador de Sistemas" -> "programador-sistemas" (formato das tags)."""
    words = re.findall(r"[A-Za-zÀ-ÿ0-9]+", text.lower())
    plain = [w for w in words if w not in STOPWORDS]
    import unicodedata

    joined = "-".join(plain)
    return unicodedata.normalize("NFKD", joined).encode("ascii", "ignore").decode()


def file_words(text: str) -> str:
    """ "Programador de Sistemas" -> "Programador-de-Sistemas" (nome do arquivo)."""
    return "-".join(re.findall(r"[A-Za-zÀ-ÿ0-9]+", text))


def yaml_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def callout(kind: str, title: str, lines: list[str]) -> list[str]:
    """Monta um callout; linhas vazias viram ">" para o bloco não quebrar."""
    out = [f"> [!{kind}] {title}"]
    if lines and LIST_ITEM_RE.match(lines[0]):
        lines = [""] + lines  # lista logo após o título precisa de linha em branco
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else ""
        # Campos seguidos precisam de quebra forçada (dois espaços), senão o Quartz
        # junta tudo em um único parágrafo.
        if (
            line
            and following
            and not LIST_ITEM_RE.match(line)
            and not LIST_ITEM_RE.match(following)
            and not line.endswith("  ")
        ):
            line += "  "
        out.append(f"> {line}" if line else ">")
    return out


def field_line(label: str, value: str) -> str:
    return f"**{label}**: {protect(value)}"


def bullet_block(label: str, items: list[str]) -> list[str]:
    """Rótulo em negrito + lista (com linha em branco antes da lista: MD032)."""
    if not items:
        return []
    return ["", f"**{label}**", ""] + [f"- {protect(item)}" for item in items]


def render_lines(lines: list[str]) -> list[str]:
    """Transforma linhas de texto livre em parágrafos/listas Markdown.

    Linhas seguidas viram um parágrafo com quebras (dois espaços no fim); itens
    de lista viram lista; linhas curtas terminadas em ":" viram negrito.
    """
    groups: list[tuple[str, list[str]]] = []
    for raw in lines:
        text = re.sub(r"^>\s*", "", raw.strip())
        if not text:
            continue
        if re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)", text):
            kind, text = "list", re.sub(r"^[*+]\s+", "- ", text)
        elif text.endswith(":") and len(text) <= 25:
            kind, text = "title", f"**{text}**"
        else:
            kind = "text"
            label = re.match(r"^(Objetivo|Conteúdo|Atividades?)\s*:\s*(.*)$", text)
            text = (
                f"**{label.group(1)}**: {protect(label.group(2))}"
                if label
                else protect(text)
            )
        if kind == "list":
            text = protect(text) if not text.startswith(("- ", "1")) else text
        if groups and groups[-1][0] == kind and kind != "title":
            groups[-1][1].append(text)
        else:
            groups.append((kind, [text]))
    out: list[str] = []
    for kind, items in groups:
        if out:
            out.append("")
        if kind == "text":
            out += [item + "  " for item in items[:-1]] + [items[-1]]
        else:
            out += items
    return out


# --------------------------------------------------------------------------
# Identificação (cabeçalho da primeira página)
# --------------------------------------------------------------------------

LABEL_RE = re.compile(
    r"^(Turma|Estado|Situação|Carga Horária|Execução da Unidade Curricular|"
    r"Minuto de Hora Aula|Quantidade de Aulas|Docente\(s\)|"
    r"Característica da Unidade Curricular|Característica|Frequência|"
    r"Plano de Execução|CH Presencial|Forma de Execução)\s*:\s*(.*)$"
)
BARE_LABELS = {"Docente(s)", "CH"}
IDENT_ORDER = {
    "ptd": [
        "Turma",
        "UC",
        "Estado",
        "Execução da Unidade Curricular",
        "Carga Horária",
        "Minuto de Hora Aula",
        "Quantidade de Aulas",
        "Docente(s)",
        "Frequência",
        "Plano de Execução",
        "CH Presencial",
        "Característica",
    ],
    "diario": [
        "Turma",
        "UC",
        "Situação",
        "Carga Horária",
        "CH",
        "Execução da Unidade Curricular",
        "Docente(s)",
        "Forma de Execução",
        "Característica da Unidade Curricular",
    ],
}


def parse_identification(header: list[Row]) -> dict[str, str]:
    """Extrai os campos do cabeçalho (incluindo valores abaixo do rótulo)."""
    cells = [c for row in header for c in row.cells]
    ident: dict[str, str] = {}

    def is_label(text: str) -> bool:
        return bool(
            LABEL_RE.match(text) or text in BARE_LABELS or re.match(r"^UC \d+:", text)
        )

    def below(cell: Cell) -> str:
        near = [
            c
            for c in cells
            if abs(c.x - cell.x) < 12 and 3 < c.y - cell.y < 14 and not is_label(c.text)
        ]
        near.sort(key=lambda c: c.y)
        return near[0].text if near else ""

    for cell in cells:
        uc = re.match(r"^UC (\d+):\s*(.+)$", cell.text)
        if uc:
            ident["UC"] = f"UC {uc.group(1)}: {uc.group(2)}"
            continue
        if cell.text in BARE_LABELS:
            ident[cell.text] = below(cell)
            continue
        match = LABEL_RE.match(cell.text)
        if match:
            label, value = match.group(1), match.group(2).strip()
            extra = below(cell)
            ident[label] = " ".join(part for part in (value, extra) if part)
    return ident


def ident_info(ident: dict[str, str]) -> dict[str, str]:
    """Códigos usados em nome de arquivo, tags e título."""
    turma = ident.get("Turma", "")
    match = re.match(r"^([\d.]+)\s*-\s*(.+)$", turma)
    code, course = (match.group(1), match.group(2)) if match else (turma, "")
    uc = re.match(r"^UC (\d+):\s*(.+)$", ident.get("UC", ""))
    return {
        "turma": code,
        "curso": course,
        "uc_num": uc.group(1) if uc else "",
        "uc_nome": uc.group(2) if uc else "",
    }


# --------------------------------------------------------------------------
# Modelo de dados
# --------------------------------------------------------------------------


@dataclass
class CHA:
    """Conhecimentos, Habilidades e Atitudes e Valores."""

    data: dict[str, list[str]] = field(
        default_factory=lambda: {label: [] for label in CHA_LABELS}
    )

    def empty(self) -> bool:
        return not any(self.data.values())


@dataclass
class SA:
    ch: str
    name: str
    forma: str = ""
    planejamento: list[tuple[str, float]] = field(default_factory=list)
    descricao: list[tuple[str, float]] = field(default_factory=list)
    recursos: list[tuple[str, float]] = field(default_factory=list)
    indicadores: list[str] = field(default_factory=list)
    cha: CHA = field(default_factory=CHA)


@dataclass
class Aula:
    numero: int
    header: list[Cell] = field(default_factory=list)
    recursos: list[tuple[str, float]] = field(default_factory=list)
    plano: list[tuple[str, float]] = field(default_factory=list)
    cha: CHA = field(default_factory=CHA)

    def info(self) -> dict[str, str]:
        """Classifica as células da linha-título da aula pela posição."""
        fields = {"data": "", "horario": "", "ambiente": "", "docente": "", "forma": ""}
        sa_cells: list[Cell] = []
        for cell in sorted(
            self.header, key=lambda c: (c.x >= 565, c.y if c.x >= 565 else 0, c.x)
        ):
            if cell.x >= 565:
                sa_cells.append(cell)
            elif cell.x < 75:
                continue
            elif cell.x < 120:
                fields["data"] = cell.text
            elif cell.x < 190:
                fields["horario"] = cell.text
            elif cell.x < 280:
                fields["ambiente"] = cell.text
            elif cell.x < 460:
                fields["docente"] = cell.text
            else:
                fields["forma"] = cell.text
        fields["sa"] = " ".join(c.text for c in sorted(sa_cells, key=lambda c: c.y))
        fields["date"] = fields["data"]
        return fields


@dataclass
class Avaliacao:
    aula: int = 0
    forma: str = ""
    sa: str = ""
    procedimento: str = ""
    descricao: list[tuple[str, float]] = field(default_factory=list)
    indicadores: list[str] = field(default_factory=list)


@dataclass
class PTD:
    sas: list[SA] = field(default_factory=list)
    aulas: list[Aula] = field(default_factory=list)
    avaliacoes: list[Avaliacao] = field(default_factory=list)
    pi_titulo: str = ""
    pi_texto: list[tuple[str, float]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Parser do PTD
# --------------------------------------------------------------------------


def _cha_columns(row: Row) -> list[tuple[float, str]]:
    return [(c.x, c.text) for c in row.cells if c.text in CHA_LABELS]


def _assign_column(columns: list[tuple[float, str]], cell: Cell) -> str:
    chosen = columns[0][1]
    for x, label in columns:
        if x <= cell.x + 10:
            chosen = label
    return chosen


def parse_ptd(body: list[Row]) -> PTD:
    """Lê as linhas do corpo do PTD e monta SAs, aulas, avaliações e PI."""
    ptd = PTD()
    section = ""
    sub = ""
    sa: SA | None = None
    aula: Aula | None = None
    aval: Avaliacao | None = None
    columns: list[tuple[float, str]] = []

    for row in body:
        first, text = row.first, row.text
        # Títulos de seção (fonte grande)
        if first.bold and first.size >= 9:
            title = first.text
            section = {
                "Situações de Aprendizagem": "sa",
                "Planejamento de Aula": "aulas",
                "Avaliações": "avals",
                "Atividades": "atividades",
            }.get(
                title, "pi" if title.startswith("Contribuição da Unidade") else section
            )
            sub = ""
            continue
        if re.match(
            r"^\d+ (Situações de Aprendizagem|Planejamentos de Aula|Avaliações)", text
        ):
            continue

        if section == "sa":
            if first.bold:
                if first.text == "Carga Horária":
                    continue
                label = first.text
                if label in CHA_LABELS:
                    columns, sub = _cha_columns(row), "cha"
                elif label.startswith("Planejamento da Situação"):
                    sub = "planejamento"
                elif label.startswith("Descrição da Situação"):
                    sub = "descricao"
                elif label == "Recursos Didáticos":
                    sub = "recursos"
                elif label == "Indicadores":
                    sub = "indicadores"
                continue
            if re.match(r"^\d+h$", first.text) and len(row.cells) >= 2:
                sa = SA(
                    first.text,
                    row.cells[1].text,
                    row.cells[2].text if len(row.cells) > 2 else "",
                )
                ptd.sas.append(sa)
                sub = "name"
                continue
            if sa is None:
                continue
            if sub == "name":
                sa.name += " " + text
            elif sub in ("planejamento", "descricao", "recursos"):
                getattr(sa, sub).append((text, row.cells[-1].x1))
            elif sub == "indicadores":
                if first.text.startswith("Indicador "):
                    sa.indicadores.append(first.text)
                elif sa.indicadores:
                    sa.indicadores[-1] += " " + first.text
            elif sub == "cha" and columns:
                for cell in row.cells:
                    add_item(sa.cha.data[_assign_column(columns, cell)], cell.text)

        elif section == "aulas":
            header_match = re.match(r"^Aula:\s*(\d+)$", first.text)
            if header_match and not first.bold:
                aula = Aula(int(header_match.group(1)), list(row.cells))
                ptd.aulas.append(aula)
                sub = "header"
                continue
            if aula is None:
                continue
            if first.bold:
                label = first.text
                if label in CHA_LABELS:
                    columns, sub = _cha_columns(row), "cha"
                elif label == "Recursos Didáticos":
                    sub = "recursos"
                elif label == "Planejamento de Aula":
                    sub = "plano"
                continue
            if sub == "header":
                aula.header.extend(row.cells)
            elif sub in ("recursos", "plano"):
                getattr(aula, sub).append((text, row.cells[-1].x1))
            elif sub == "cha" and columns:
                for cell in row.cells:
                    add_item(aula.cha.data[_assign_column(columns, cell)], cell.text)

        elif section == "avals":
            header_match = re.match(r"^Aula:\s*(\d+)$", first.text)
            if first.bold:
                if first.text.startswith("Avaliação"):
                    sub = "body"
                continue
            if header_match:
                aval = Avaliacao(int(header_match.group(1)))
                aval.forma = row.cells[1].text if len(row.cells) > 1 else ""
                aval.sa = row.cells[2].text if len(row.cells) > 2 else ""
                ptd.avaliacoes.append(aval)
                sub = "sa"
                continue
            if aval is None:
                continue
            if sub == "sa":
                aval.sa += " " + text
            elif text.startswith("Procedimento de Avaliação:"):
                aval.procedimento = text.split(":", 1)[1].strip()
                sub = "procedimento"
            elif text.startswith("Descrição da Avaliação:"):
                aval.descricao = [(text.split(":", 1)[1].strip(), row.cells[-1].x1)]
                sub = "descricao"
            elif text.startswith("Indicador "):
                aval.indicadores.append(text)
                sub = "indicador"
            elif sub == "descricao":
                aval.descricao.append((text, row.cells[-1].x1))
            elif sub == "indicador" and aval.indicadores:
                aval.indicadores[-1] += " " + text

        elif section == "pi":
            if not ptd.pi_titulo and text.startswith("Projeto Integrador"):
                ptd.pi_titulo = text
            else:
                ptd.pi_texto.append((text, row.cells[-1].x1))

    return ptd


# --------------------------------------------------------------------------
# Parser do Diário de Classe
# --------------------------------------------------------------------------


@dataclass
class Dia:
    data: str
    horario: str = ""
    ambiente: str = ""
    docente: str = ""
    sa: str = ""
    recursos: list[tuple[str, float]] = field(default_factory=list)
    conteudo: list[tuple[str, float]] = field(default_factory=list)
    cha: CHA = field(default_factory=CHA)


@dataclass
class Diario:
    avaliacoes: list[Avaliacao] = field(default_factory=list)
    avaliacao_info: list[dict[str, str]] = field(default_factory=list)
    dias: list[Dia] = field(default_factory=list)
    ignorado: list[str] = field(default_factory=list)


def parse_diario(body: list[Row]) -> Diario:
    """Lê o corpo do Diário (avaliações e aulas); ignora dados de alunos."""
    diario = Diario()
    section = ""
    sub = ""
    aval: Avaliacao | None = None
    info: dict[str, str] = {}
    dia: Dia | None = None
    current_cha = ""

    for row in body:
        first, text = row.first, row.text
        if first.bold and len(row.cells) == 1:
            title = first.text
            if title == "Avaliação":
                section, sub = "aval", ""
                continue
            if title == "Conteúdo":
                section, sub = "conteudo", ""
                continue
            if title in ("Feedbacks", "Observações", "Histórico de Revisões"):
                section = "ignorar"
                if title not in diario.ignorado:
                    diario.ignorado.append(title)
                continue
        if section == "ignorar":
            continue

        if section == "aval":
            if first.bold:
                continue  # cabeçalho da tabela
            if re.match(r"^\d+$", first.text) and len(row.cells) >= 5 and first.x < 60:
                aval = Avaliacao()
                cells = row.cells
                info = {
                    "Dia Letivo": cells[1].text,
                    "Tipo de Avaliação": cells[2].text,
                    "Docente": cells[3].text,
                    "Procedimento de Avaliação": cells[4].text,
                }
                aval.descricao = (
                    [(cells[5].text, cells[5].x1)] if len(cells) > 5 else []
                )
                diario.avaliacoes.append(aval)
                diario.avaliacao_info.append(info)
                sub = "descricao"
                continue
            if aval is None:
                continue
            if text.startswith("Indicador "):
                aval.indicadores.append(text)
                sub = "indicador"
            elif sub == "indicador" and first.x < 30:
                aval.indicadores[-1] += " " + text
            elif first.x >= 440:
                aval.descricao.append((text, row.cells[-1].x1))

        elif section == "conteudo":
            if first.bold:
                if first.text in ("Dia Letivo",):
                    continue
                label = first.text
                if label == "Situação de Aprendizagem":
                    sub = "sa"
                elif label == "Recursos Didáticos":
                    sub = "recursos"
                elif label == "Conteúdo da Aula":
                    sub = "conteudo"
                elif label in CHA_LABELS:
                    sub, current_cha = "cha", label
                continue
            if DATE_RE.match(first.text) and len(row.cells) >= 2:
                cells = row.cells
                dia = Dia(
                    cells[0].text,
                    cells[1].text if len(cells) > 1 else "",
                    cells[2].text if len(cells) > 2 else "",
                    cells[3].text if len(cells) > 3 else "",
                )
                diario.dias.append(dia)
                sub = "dia"
                continue
            if dia is None:
                continue
            if sub == "dia" and len(row.cells) == 1 and not dia.docente:
                dia.docente = text
            elif sub == "sa":
                dia.sa = (dia.sa + " " + text).strip()
            elif sub == "recursos":
                dia.recursos.append((text, row.cells[-1].x1))
            elif sub == "conteudo":
                dia.conteudo.append((text, row.cells[-1].x1))
            elif sub == "cha":
                add_item(dia.cha.data[current_cha], text)
    return diario


# --------------------------------------------------------------------------
# Renderização
# --------------------------------------------------------------------------


def frontmatter(
    title: str, description: str, tags: list[str], note_date: str, draft: bool
) -> list[str]:
    return [
        "---",
        f"title: {yaml_quote(title)}",
        f"description: {yaml_quote(description)}",
        f"date: '{note_date}'",
        f"draft: {str(draft).lower()}",
        "tags:",
        *[f"  - {tag}" for tag in tags],
        "---",
        "",
    ]


def ident_callout(
    ident: dict[str, str], kind: str, extra: list[str] | None = None
) -> list[str]:
    lines = [
        field_line(label, ident[label])
        for label in IDENT_ORDER[kind]
        if ident.get(label)
    ]
    return callout("info", "Identificação", lines + (extra or []))


def clean_title(text: str, limit: int = 100) -> str:
    text = re.sub(r"^>\s*", "", text.strip()).strip(" .*_")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def day_title(first_line: str, limit: int = 70) -> tuple[str, bool]:
    """Título curto da aula a partir da primeira linha do conteúdo.

    Returns:
        O título e se ele já contém a linha inteira (então a linha sai do corpo).
    """
    line = first_line.strip()
    label = re.match(r"^(Conhecimentos|Objetivo|Conteúdo)\s*:\s*", line)
    text = re.sub(r"^(Conhecimentos|Objetivo|Conteúdo|Aula\s*\d+)\s*:\s*", "", line)
    text = clean_title(text, 400)
    if len(text) > limit:
        text = re.split(r"\s*[:.]\s", text, maxsplit=1)[0]
        return clean_title(text, limit), False
    return text, label is None


MOBILIZED = {
    "Conhecimentos": "mobilizados",
    "Habilidades": "mobilizadas",
    "Atitudes e Valores": "mobilizados",
}


def cha_blocks(
    cha: CHA, baseline: dict[str, list[str]] | None, mobilized: bool = False
) -> list[str]:
    """Listas de CHA; com baseline, só as que diferem da lista completa da UC."""
    lines: list[str] = []
    for label in CHA_LABELS:
        items = cha.data[label]
        if not items:
            continue
        if baseline is not None and set(items) == set(baseline.get(label, [])):
            continue
        lines += bullet_block(
            f"{label} {MOBILIZED[label]}" if mobilized else label, items
        )
    return lines


AULA_KEYS = [
    "Horário",
    "Ambiente Pedagógico",
    "Docente(s)",
    "Forma de Execução",
    "Recursos Didáticos",
]


def norm_name(name: str) -> str:
    return re.sub(r"\W+", "", name).lower()


def same_sa(first: str, second: str) -> bool:
    """Compara nomes de SA ignorando pontuação, caixa e quebras de linha."""
    a, b = norm_name(first), norm_name(second)
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def split_common(
    props: list[dict[str, str]],
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Separa o que se repete em todas as aulas (valor mais comum) do que difere.

    Returns:
        O valor comum de cada propriedade e, para cada aula, só as propriedades
        cujo valor é diferente do comum.
    """
    common: dict[str, str] = {}
    for key in AULA_KEYS:
        values = [p[key] for p in props if p.get(key)]
        if values:
            top = Counter(v.casefold() for v in values).most_common(1)[0][0]
            common[key] = next(v for v in values if v.casefold() == top)
    overrides = [
        {
            k: p[k]
            for k in AULA_KEYS
            if p.get(k) and k in common and p[k].casefold() != common[k].casefold()
        }
        for p in props
    ]
    return common, overrides


def common_block(common: dict[str, str], ident: dict[str, str]) -> list[str]:
    """Bloco "Padrão das aulas" com o que se repete (sem repetir a identificação)."""
    lines = []
    for key in AULA_KEYS:
        value = common.get(key, "")
        if not value:
            continue
        if (
            key == "Docente(s)"
            and value.casefold() == ident.get("Docente(s)", "").casefold()
        ):
            continue
        lines.append(field_line(key, value))
    return ["", "**Padrão das aulas**"] + lines if lines else []


@dataclass
class PtdRef:
    """O que o Diário aproveita do PTD da mesma turma/UC."""

    sa_names: list[str]
    aulas: dict[str, tuple[int, str]]  # data -> (número da aula, nome da SA)


def make_ref(ptd: PTD) -> PtdRef:
    aulas: dict[str, tuple[int, str]] = {}
    for aula in ptd.aulas:
        data = aula.info()
        sa_name = next(
            (sa.name for sa in ptd.sas if same_sa(sa.name, data["sa"])), data["sa"]
        )
        aulas[data["date"]] = (aula.numero, sa_name)
    return PtdRef([sa.name for sa in ptd.sas], aulas)


def render_ptd(
    ptd: PTD, ident: dict[str, str], margin: float, full_cha: bool
) -> list[str]:
    info = ident_info(ident)
    props = []
    for aula in ptd.aulas:
        data = aula.info()
        props.append(
            {
                "Horário": data["horario"],
                "Ambiente Pedagógico": data["ambiente"],
                "Docente(s)": data["docente"],
                "Forma de Execução": data["forma"],
                "Recursos Didáticos": " ".join(wrap_join(aula.recursos, margin)),
            }
        )
    common, overrides = split_common(props)
    override_of = {aula.numero: extra for aula, extra in zip(ptd.aulas, overrides)}

    out = [f"# Plano de Trabalho do Docente - UC{info['uc_num']}", ""]
    out += ident_callout(ident, "ptd", common_block(common, ident))
    out += ["", "---", "", "## Situações de Aprendizagem", ""]

    for number, sa in enumerate(ptd.sas, 1):
        out += [f"### SA{number} - {sa.name.rstrip('.')}", ""]
        lines = [
            field_line("Carga Horária", sa.ch),
            field_line("Situação de Aprendizagem", sa.name),
        ]
        if sa.forma:
            lines.append(field_line("Forma de Execução", sa.forma))
        for label, rows in (
            ("Planejamento da Situação de Aprendizagem", sa.planejamento),
            ("Descrição da Situação de Aprendizagem", sa.descricao),
            ("Recursos Didáticos", sa.recursos),
        ):
            if rows:
                lines += [
                    "",
                    f"**{label}**",
                    protect(" ".join(wrap_join(rows, margin))),
                ]
        lines += bullet_block("Indicadores", sa.indicadores)
        for label in CHA_LABELS:
            lines += bullet_block(label, sa.cha.data[label])
        out += callout("abstract", f"Situação de Aprendizagem {number}", lines) + [""]

        for aula in sorted(ptd.aulas, key=lambda a: a.numero):
            data = aula.info()
            if not same_sa(sa.name, data["sa"]):
                continue
            plan = wrap_join(aula.plano, margin)
            title = ""
            if plan and not LABEL_START.match(plan[0]):
                title = clean_title(re.sub(r"^Aula\s*\d+\s*:\s*", "", plan.pop(0)))
            out += [f"#### Aula {aula.numero}" + (f" - {title}" if title else ""), ""]
            lines = [field_line("Tema", title)] if title else []
            lines += [field_line(k, v) for k, v in override_of[aula.numero].items()]
            body = render_lines(plan)
            if body:
                lines += [""] + body if lines else body
            lines += cha_blocks(
                aula.cha, None if full_cha else sa.cha.data, mobilized=True
            )
            out += callout("tip", f"Aula {aula.numero} - {data['date']}", lines) + [""]

    out += ["---", "", "## Avaliações", ""]
    for number, aval in enumerate(ptd.avaliacoes, 1):
        lines = []
        if aval.sa:
            lines.append(field_line("Situação de Aprendizagem", aval.sa))
        if aval.forma:
            lines.append(field_line("Forma de Execução", aval.forma))
        if aval.procedimento:
            lines.append(field_line("Procedimento de Avaliação", aval.procedimento))
        if aval.descricao:
            lines.append(
                field_line(
                    "Descrição da Avaliação",
                    " ".join(wrap_join(aval.descricao, margin)),
                )
            )
        lines += bullet_block("Indicadores", aval.indicadores)
        out += callout("warning", f"Avaliação {number} (Aula {aval.aula})", lines) + [
            ""
        ]

    if ptd.pi_titulo or ptd.pi_texto:
        out += [
            "---",
            "",
            "## Contribuição da Unidade Curricular para o Projeto Integrador",
            "",
        ]
        lines = (
            [protect(" ".join(wrap_join(ptd.pi_texto, margin)))] if ptd.pi_texto else []
        )
        out += callout("info", ptd.pi_titulo or "Projeto Integrador", lines) + [""]
    return out


def render_diario(
    diario: Diario,
    ident: dict[str, str],
    margin: float,
    full_cha: bool,
    ref: PtdRef | None,
) -> list[str]:
    info = ident_info(ident)

    # Número da aula e SA: do PTD (pela data) ou, sem PTD, pela ordem dos dias.
    entries: list[tuple[int, str, Dia]] = []
    for position, dia in enumerate(diario.dias, 1):
        number, sa_name = position, dia.sa
        if ref and dia.data in ref.aulas:
            number, sa_name = ref.aulas[dia.data]
        entries.append((number, sa_name or dia.sa, dia))

    props = [
        {
            "Horário": dia.horario,
            "Ambiente Pedagógico": dia.ambiente,
            "Docente(s)": dia.docente,
            "Recursos Didáticos": " ".join(wrap_join(dia.recursos, margin)),
        }
        for _, _, dia in entries
    ]
    common, overrides = split_common(props)
    override_of = {id(dia): extra for (_, _, dia), extra in zip(entries, overrides)}

    out = [f"# Diário de Classe - UC{info['uc_num']}", ""]
    out += ident_callout(ident, "diario", common_block(common, ident)) + [""]

    # A lista completa de CHA da UC é a mais longa vista entre os dias.
    baseline: dict[str, list[str]] = {label: [] for label in CHA_LABELS}
    for dia in diario.dias:
        for label in CHA_LABELS:
            if len(dia.cha.data[label]) > len(baseline[label]):
                baseline[label] = dia.cha.data[label]

    out += ["---", "", "## Avaliações", ""]
    for number, (aval, extra) in enumerate(
        zip(diario.avaliacoes, diario.avaliacao_info), 1
    ):
        lines = [
            field_line(label, value)
            for label, value in extra.items()
            if value and label != "Dia Letivo"
        ]
        if aval.descricao:
            lines.append(
                field_line("Descrição", " ".join(wrap_join(aval.descricao, margin)))
            )
        lines += bullet_block("Indicadores", aval.indicadores)
        out += callout(
            "warning", f"Avaliação {number} ({extra.get('Dia Letivo', '')})", lines
        ) + [""]

    if any(baseline.values()):
        out += [
            "---",
            "",
            "## Conhecimentos, Habilidades e Atitudes e Valores da UC",
            "",
        ]
        lines = []
        for label in CHA_LABELS:
            lines += bullet_block(label, baseline[label])
        out += callout("abstract", "Elementos de competência da UC", lines[1:]) + [""]

    # Grupos por SA, na ordem e com a numeração do PTD quando ele existe.
    groups: list[tuple[str, str, list[tuple[int, str, Dia]]]] = []
    if ref and ref.sa_names:
        for number, name in enumerate(ref.sa_names, 1):
            items = [e for e in entries if same_sa(e[1], name)]
            if items:
                groups.append((f"SA{number} - ", name, items))
        used = {id(e[2]) for _, _, items in groups for e in items}
        rest = [e for e in entries if id(e[2]) not in used]
        if rest:
            groups.append(("", "Aulas sem SA correspondente no PTD", rest))
    else:
        for entry in entries:
            for _, name, items in groups:
                if same_sa(name, entry[1]):
                    items.append(entry)
                    break
            else:
                groups.append(("", entry[1], [entry]))

    out += ["---", "", "## Aulas", ""]
    for prefix, name, items in groups:
        out += [f"### {prefix}{name.rstrip('.')}", ""]
        for number, _, dia in sorted(items, key=lambda e: e[0]):
            content = wrap_join(dia.conteudo, margin)
            title, consumed = day_title(content[0]) if content else ("", False)
            if consumed:
                content = content[1:]
            out += [f"#### Aula {number}" + (f" - {title}" if title else ""), ""]
            lines = [field_line(k, v) for k, v in override_of[id(dia)].items()]
            body = render_lines(content)
            if body:
                lines += [""] + body if lines else body
            lines += cha_blocks(dia.cha, None if full_cha else baseline, mobilized=True)
            out += callout("tip", f"Aula {number} - {dia.data}", lines) + [""]
    return out


# --------------------------------------------------------------------------
# Conversão de um arquivo
# --------------------------------------------------------------------------


@dataclass
class Parsed:
    """PDF já lido: tipo, identificação e dados estruturados."""

    kind: str
    ident: dict[str, str]
    info: dict[str, str]
    margin: float
    printed: str | None
    data: PTD | Diario | None


def parse_pdf(pdf_path: Path, first_page_only: bool = False) -> Parsed:
    """Lê um PDF do Senac. Com first_page_only lê só o necessário para identificá-lo."""
    try:
        with pymupdf.open(pdf_path) as doc:
            rows = extract_rows(doc, 1 if first_page_only else None)
    except Exception as exc:
        raise ConversionError(f"{pdf_path.name}: {exc}") from exc
    kind = detect_kind(rows)
    if kind is None:
        raise ConversionError(
            f"{pdf_path.name}: não parece um Diário de Classe nem um PTD do Senac "
            "(use o pdf2obsidian.py para PDFs genéricos)."
        )
    header, body, printed = split_rows(rows, kind)
    ident = parse_identification(header)
    long_lines = [
        c.x1 for r in body for c in r.cells if c.size < 8 and len(c.text) > 120
    ]
    margin = max(long_lines) if long_lines else 800.0
    data = None
    if not first_page_only:
        data = parse_ptd(body) if kind == "ptd" else parse_diario(body)
    return Parsed(kind, ident, ident_info(ident), margin, printed, data)


def find_ptd(
    pdf_path: Path, info: dict[str, str], explicit: Path | None
) -> PtdRef | None:
    """Procura o PTD da mesma turma e UC (informado ou na mesma pasta do Diário)."""
    candidates = [explicit] if explicit else sorted(pdf_path.parent.glob("*.pdf"))
    for candidate in candidates:
        if candidate is None or candidate.resolve() == pdf_path.resolve():
            continue
        try:
            peek = parse_pdf(candidate, first_page_only=True)
        except ConversionError:
            if explicit:
                raise
            continue
        same = (peek.info["turma"], peek.info["uc_num"]) == (
            info["turma"],
            info["uc_num"],
        )
        if peek.kind == "ptd" and (same or explicit):
            full = parse_pdf(candidate)
            assert isinstance(full.data, PTD)
            return make_ref(full.data)
    return None


def convert_pdf(
    pdf_path: Path,
    output_dir: Path,
    extra_tags: list[str],
    draft: bool,
    full_cha: bool,
    ptd_pdf: Path | None = None,
) -> Path:
    """Converte um PDF do Diário ou do PTD e grava a nota .md."""
    parsed = parse_pdf(pdf_path)
    info, ident = parsed.info, parsed.ident
    if parsed.kind == "ptd":
        assert isinstance(parsed.data, PTD)
        content = render_ptd(parsed.data, ident, parsed.margin, full_cha)
        prefix, title_kind, label = "PTD", "PTD", "Plano de Trabalho do Docente"
    else:
        assert isinstance(parsed.data, Diario)
        ref = find_ptd(pdf_path, info, ptd_pdf)
        if ref is None:
            logger.warning(
                "%s: PTD da mesma turma/UC não encontrado; aulas numeradas pela ordem dos dias "
                "e SAs sem número (use --ptd para indicar o PDF do PTD).",
                pdf_path.name,
            )
        content = render_diario(parsed.data, ident, parsed.margin, full_cha, ref)
        prefix, title_kind, label = "Diario", "Diário de Classe", "Diário de Classe"
        if parsed.data.ignorado:
            logger.info(
                "%s: seções com dados de alunos ignoradas: %s",
                pdf_path.name,
                ", ".join(parsed.data.ignorado),
            )

    title = f"{title_kind} - {info['turma']} UC{info['uc_num']} - {info['curso']}"
    description = (
        f"{label} da UC {info['uc_num']} ({info['uc_nome']}), turma {info['turma']}, "
        f"{ident.get('Execução da Unidade Curricular', '')}"
    ).strip(" ,")
    tags = [
        prefix.lower(),
        "senac",
        f"uc{info['uc_num']}",
        slug_words(info["curso"]),
    ] + extra_tags
    tags = [tag for tag in dict.fromkeys(tags) if tag]
    lines = (
        frontmatter(
            title, description, tags, parsed.printed or date.today().isoformat(), draft
        )
        + content
    )
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines).rstrip() + "\n")

    output_dir.mkdir(parents=True, exist_ok=True)
    note_path = (
        output_dir
        / f"{prefix}-{info['turma']}-UC{info['uc_num']}-{file_words(info['curso'])}.md"
    )
    with note_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return note_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Converte Diário de Classe e PTD do Senac em notas Obsidian."
    )
    parser.add_argument("source", type=Path, help="PDF ou pasta com PDFs")
    parser.add_argument("-o", "--output", type=Path, default=Path("notas"))
    parser.add_argument("-t", "--tags", nargs="*", default=[], help="Tags extras")
    parser.add_argument("--draft", action="store_true", help="Marca draft: true")
    parser.add_argument(
        "--ptd",
        type=Path,
        default=None,
        help="PDF do PTD usado para numerar aulas e SAs do Diário (padrão: procura na pasta do Diário)",
    )
    parser.add_argument(
        "--full-cha",
        action="store_true",
        help="Repete Conhecimentos/Habilidades/Atitudes em todas as aulas (padrão: só quando diferem da UC)",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    if not args.source.exists():
        logger.error("Origem não encontrada: %s", args.source)
        return 1
    pdfs = (
        [args.source] if args.source.is_file() else sorted(args.source.rglob("*.pdf"))
    )
    if not pdfs:
        logger.error("Nenhum PDF encontrado em %s", args.source)
        return 1
    failures = 0
    for pdf in pdfs:
        try:
            note = convert_pdf(
                pdf, args.output, args.tags, args.draft, args.full_cha, args.ptd
            )
            logger.info("OK  %s -> %s", pdf.name, note)
        except ConversionError as exc:
            failures += 1
            logger.error("FALHA %s", exc)
    logger.info(
        "Concluído: %d convertido(s), %d falha(s).", len(pdfs) - failures, failures
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

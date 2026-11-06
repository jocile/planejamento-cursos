#!/usr/bin/env python3
"""Converte arquivos PDF em notas Markdown formatadas para o Obsidian/Quartz.

Cada PDF vira uma nota .md com:
  * frontmatter no padrão do vault (title, description, date, draft, tags);
  * corpo em Markdown (títulos, listas e tabelas detectados pelo pymupdf4llm);
  * imagens extraídas para uma pasta de anexos e referenciadas como ![[img.png]].

Requisitos: Python 3.10+  ->  uv add pymupdf4llm   (ou: pip install pymupdf4llm)

Exemplos:
    python pdf2obsidian.py manual.pdf -o vault/docs
    python pdf2obsidian.py pasta_pdfs/ -o vault/docs --tags didatica senac
    python pdf2obsidian.py pasta_pdfs/ -o vault/docs --no-images --draft
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys

# No Windows, subprocessos internos do pymupdf podem emitir cp1252 e quebrar a
# leitura em UTF-8. Forçar UTF-8 aqui, antes dos imports pesados, evita isso.
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
from datetime import date
from pathlib import Path

import pymupdf
import pymupdf4llm

logger = logging.getLogger("pdf2obsidian")

# Caracteres que quebram nomes de arquivo ou links no Obsidian.
INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')
IMAGE_MD = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
EXTRA_BLANKS = re.compile(r"\n{3,}")


class ConversionError(Exception):
    """Erro ao converter um PDF específico."""


def parse_page_spec(spec: str, total: int) -> set[int]:
    """Converte "1-4,10,60-" em um conjunto de índices de página (base 0).

    Args:
        spec: Páginas em base 1, separadas por vírgula. "5-" significa até o fim.
        total: Número de páginas do PDF.

    Returns:
        Índices (base 0) das páginas citadas, limitados ao tamanho do PDF.

    Raises:
        ValueError: Se o texto não seguir o formato esperado.
    """
    pages: set[int] = set()
    for part in filter(None, (chunk.strip() for chunk in spec.split(","))):
        match = re.fullmatch(r"(\d+)(?:(-)(\d*))?", part)
        if not match:
            raise ValueError(f"Intervalo de páginas inválido: {part!r}")
        start = int(match.group(1))
        if match.group(2):
            end = int(match.group(3)) if match.group(3) else total
        else:
            end = start
        pages.update(range(start - 1, min(end, total)))
    return {page for page in pages if page >= 0}


def sanitize_name(name: str) -> str:
    """Remove caracteres inválidos de um nome de arquivo.

    Args:
        name: Nome original (sem extensão).

    Returns:
        Nome seguro para uso no Obsidian, com espaços normalizados.
    """
    cleaned = INVALID_NAME_CHARS.sub(" ", name)
    return re.sub(r"\s+", " ", cleaned).strip() or "sem-titulo"


def yaml_quote(text: str) -> str:
    """Escapa um texto para uso seguro em um valor YAML entre aspas simples."""
    return "'" + text.replace("'", "''") + "'"


def extract_title(markdown: str, metadata: dict[str, str], fallback: str) -> str:
    """Define o título da nota.

    Ordem de preferência: metadado do PDF, primeiro título H1/H2 do texto,
    nome do arquivo.
    """
    meta_title = (metadata.get("title") or "").strip()
    is_junk = re.search(r"\.(indd|docx?|pdf|pptx?|qxd|tmp)$", meta_title, re.IGNORECASE)
    if len(meta_title) > 3 and not is_junk:
        return meta_title
    heading = re.search(r"^#{1,2}\s+(.+)$", markdown, flags=re.MULTILINE)
    if heading:
        return heading.group(1).strip("*_ ").strip()
    return fallback


def extract_description(markdown: str, max_chars: int = 200) -> str:
    """Usa o primeiro parágrafo "de verdade" como descrição (sem títulos/listas)."""
    for block in markdown.split("\n\n"):
        text = " ".join(block.split())
        if len(text) > 120 and not text.startswith(
            ("#", "|", "-", "*", "![", ">", "<", "!")
        ):
            text = text.replace("**", "").replace("_", " ")
            return text[:max_chars].rstrip() + ("…" if len(text) > max_chars else "")
    return ""


SUPERSCRIPTS = {"a": "ª", "o": "º", **dict(zip("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹"))}
SUP_TAG = re.compile(r"<sup>(.*?)</sup>", re.IGNORECASE)
PICTURE_BLOCK = re.compile(
    r"<!-- Start of picture text -->(.*?)<!-- End of picture text -->", re.DOTALL
)
BR_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)
PAGE_NUMBER_LINE = re.compile(r"^\*\*\d{1,3}\*\*\s*$", re.MULTILINE)
DROP_CAP = re.compile(
    r"^(?:\*\*)?([A-ZÀ-Ý])(?:\*\*)?[ \t]*\n\n([a-zà-ÿ])", re.MULTILINE
)
STRAY_CAP = re.compile(r"[ \t]\*\*[A-ZÀ-Ý]\*\*(?=[ \t])")
BOLD_ONLY_LINE = re.compile(r"^\*\*([^*\n]+)\*\*[ \t]*$", re.MULTILINE)
HEADING_LINE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")
FENCE = re.compile(r"^\s*(```|~~~)")
LIST_ITEM = re.compile(r"^( *)[-*+] (.*)$")
SPLIT_URL = re.compile(r"((?:https?://|www\.)\S*[./\-])[ \t]+(?=[a-z0-9])")
WWW_URL = re.compile(r"(?<![<(\[/\w.@])(www\.[^\s<>\]\)]+)")
BARE_URL = re.compile(r"(?<![<(\[\"'=])(https?://[^\s<>\]\)]+)")


def convert_sup_tags(markdown: str) -> str:
    """Troca <sup>a</sup>/<sup>1</sup> por ª, º e dígitos sobrescritos (sem HTML)."""

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1).replace("*", "").strip()
        return "".join(SUPERSCRIPTS.get(char, char) for char in raw)

    return SUP_TAG.sub(replace, markdown)


def convert_picture_blocks(markdown: str) -> str:
    """Transforma o "texto de figura" extraído do PDF em um callout dobrável."""

    def replace(match: re.Match[str]) -> str:
        lines = [
            part.strip()
            for part in re.split(r"<br\s*/?>|\n", match.group(1))
            if part.strip()
        ]
        if not lines:
            return ""
        body = "\n".join(f"> {line}" for line in lines)
        return "> [!note]- Texto extraído de figura\n" + body

    return PICTURE_BLOCK.sub(replace, markdown)


def normalize_headings(markdown: str) -> str:
    """Normaliza a hierarquia de títulos.

    * remove negrito dentro de títulos;
    * mapeia os níveis usados para H2, H3, H4... (o H1 é o title do frontmatter);
    * impede saltos de nível (H2 -> H4).
    """
    lines = markdown.split("\n")
    in_fence = False
    levels: set[int] = set()
    for line in lines:
        if FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and (match := HEADING_LINE.match(line)):
            levels.add(len(match.group(1)))
    mapping = {level: min(2 + rank, 6) for rank, level in enumerate(sorted(levels))}

    result: list[str] = []
    in_fence = False
    previous = 1
    for line in lines:
        if FENCE.match(line):
            in_fence = not in_fence
            result.append(line)
            continue
        match = None if in_fence else HEADING_LINE.match(line)
        if not match:
            result.append(line)
            continue
        text = match.group(2).replace("**", "").replace("__", "").strip()
        if not text:
            continue  # título vazio vindo de lixo do PDF
        level = min(mapping[len(match.group(1))], previous + 1)
        previous = level
        result.append(f"{'#' * level} {text}")
    return "\n".join(result)


def normalize_lists(markdown: str) -> str:
    """Usa "-" como marcador e recuos de 2 espaços (padrão do markdownlint)."""
    result: list[str] = []
    for line in markdown.split("\n"):
        match = LIST_ITEM.match(line)
        if match and not line.startswith("**"):
            indent = len(match.group(1))
            line = " " * (indent // 3 * 2 + indent % 3) + "- " + match.group(2)
        result.append(line)
    return "\n".join(result)


def fix_and_wrap_urls(markdown: str) -> str:
    """Reconecta URLs quebradas por espaço e transforma URLs soltas em <url>."""
    previous = None
    while previous != markdown:
        previous = markdown
        markdown = SPLIT_URL.sub(r"\1", markdown)

    def wrap(match: re.Match[str]) -> str:
        url, trailing = match.group(1), ""
        while url and url[-1] in ".,;:":
            trailing = url[-1] + trailing
            url = url[:-1]
        return f"<{url}>{trailing}"

    def wrap_www(match: re.Match[str]) -> str:
        url, trailing = match.group(1), ""
        while url and url[-1] in ".,;:":
            trailing = url[-1] + trailing
            url = url[:-1]
        return f"[{url}](https://{url}){trailing}"

    markdown = BARE_URL.sub(wrap, markdown)
    return WWW_URL.sub(wrap_www, markdown)


def merge_split_paragraphs(markdown: str) -> str:
    """Junta parágrafos cortados por quebra de página.

    Une dois blocos de texto simples quando o primeiro não termina em pontuação
    final e o segundo começa com letra minúscula.
    """
    sentence_end = tuple('.!?:;…)"”»]')
    special_start = ("#", "|", ">", "-", "*", "!", "<", "[", "`", "---")
    blocks = markdown.split("\n\n")
    merged: list[str] = []
    for block in blocks:
        previous = merged[-1] if merged else ""
        is_plain = bool(block) and not block.lstrip().startswith(special_start)
        previous_plain = bool(previous) and not previous.lstrip().startswith(
            special_start
        )
        if (
            is_plain
            and previous_plain
            and not previous.rstrip().endswith(sentence_end)
            and block[0].islower()
        ):
            merged[-1] = previous.rstrip() + " " + block.lstrip()
        else:
            merged.append(block)
    return "\n\n".join(merged)


def clean_markdown(markdown: str) -> str:
    """Limpa ruídos típicos de PDF e deixa o Markdown "linter-friendly"."""
    markdown = markdown.replace("\r\n", "\n").replace("\r", "\n")
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)  # espaços no fim da linha
    markdown = HYPHEN_BREAK.sub(r"\1\2", markdown)  # "apren-\ndizagem"
    markdown = markdown.replace("\u00a0", " ")
    markdown = convert_picture_blocks(markdown)
    markdown = convert_sup_tags(markdown)
    markdown = BR_TAG.sub(" ", markdown)
    markdown = PAGE_NUMBER_LINE.sub("", markdown)  # **12** isolado = nº de página
    markdown = DROP_CAP.sub(r"\1\2", markdown)  # "**F**\n\nator" -> "Fator"
    markdown = STRAY_CAP.sub("", markdown)
    markdown = normalize_headings(markdown)
    markdown = BOLD_ONLY_LINE.sub(r"\1", markdown)  # negrito usado como "título"
    markdown = normalize_lists(markdown)
    markdown = fix_and_wrap_urls(markdown)
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    markdown = EXTRA_BLANKS.sub("\n\n", markdown).strip()
    return merge_split_paragraphs(markdown)


def convert_images_to_wikilinks(markdown: str) -> str:
    """Troca ![alt](caminho/img.png) por ![[img.png]] (embed do Obsidian/Quartz)."""

    def replace(match: re.Match[str]) -> str:
        filename = Path(match.group(1)).name
        return f"![[{filename}]]"

    return IMAGE_MD.sub(replace, markdown)


def build_frontmatter(
    title: str, description: str, tags: list[str], draft: bool
) -> str:
    """Monta o frontmatter YAML no padrão das notas do vault."""
    lines = [
        "---",
        f"title: {yaml_quote(title)}",
        f"description: {yaml_quote(description)}",
        f"date: '{date.today().isoformat()}'",
        f"draft: {str(draft).lower()}",
        "tags:",
        *[f"  - {tag}" for tag in tags],
        "---",
        "",
    ]
    return "\n".join(lines)


def convert_pdf(
    pdf_path: Path,
    output_dir: Path,
    attachments_dir: Path,
    tags: list[str],
    draft: bool,
    with_images: bool,
    with_ocr: bool = False,
    title_override: str | None = None,
    description_override: str | None = None,
    skip_pages: str | None = None,
) -> Path:
    """Converte um PDF em uma nota Markdown.

    Args:
        pdf_path: Caminho do PDF de origem.
        output_dir: Pasta onde a nota .md será gravada.
        attachments_dir: Pasta para as imagens extraídas.
        tags: Tags a incluir no frontmatter.
        draft: Se True, marca a nota como rascunho.
        with_images: Se True, extrai imagens do PDF.
        with_ocr: Se True, liga o OCR (só para PDFs escaneados).
        title_override: Título fixo (ignora a detecção automática).
        description_override: Descrição fixa (ignora a detecção automática).
        skip_pages: Páginas a ignorar (base 1), ex.: "1-4,10".

    Returns:
        Caminho da nota .md criada.

    Raises:
        ConversionError: Se o PDF não puder ser lido ou estiver sem texto.
    """
    try:
        with pymupdf.open(pdf_path) as doc:
            metadata = dict(doc.metadata or {})
            kwargs: dict = {"show_progress": False}
            if skip_pages:
                skipped = parse_page_spec(skip_pages, doc.page_count)
                kwargs["pages"] = [
                    page for page in range(doc.page_count) if page not in skipped
                ]
            if with_images:
                attachments_dir.mkdir(parents=True, exist_ok=True)
                kwargs.update(
                    write_images=True,
                    image_path=str(attachments_dir),
                    image_format="png",
                    dpi=150,
                )
            try:
                markdown = pymupdf4llm.to_markdown(doc, use_ocr=with_ocr, **kwargs)
            except TypeError:
                # Versões antigas do pymupdf4llm não têm o parâmetro use_ocr.
                markdown = pymupdf4llm.to_markdown(doc, **kwargs)
    except Exception as exc:  # pymupdf lança vários tipos de erro
        raise ConversionError(f"{pdf_path.name}: {exc}") from exc

    if not markdown.strip():
        raise ConversionError(
            f"{pdf_path.name}: sem texto extraível (PDF escaneado? use OCR)."
        )

    markdown = clean_markdown(markdown)
    markdown = convert_images_to_wikilinks(markdown)

    title = title_override or extract_title(markdown, metadata, pdf_path.stem)
    description = description_override or extract_description(markdown)
    note = build_frontmatter(title, description, tags, draft) + "\n" + markdown + "\n"

    output_dir.mkdir(parents=True, exist_ok=True)
    note_path = output_dir / f"{sanitize_name(pdf_path.stem)}.md"
    with note_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(note)
    return note_path


def find_pdfs(source: Path) -> list[Path]:
    """Lista PDFs de um arquivo ou, recursivamente, de uma pasta."""
    if source.is_file():
        return [source] if source.suffix.lower() == ".pdf" else []
    return sorted(source.rglob("*.pdf"))


def parse_args() -> argparse.Namespace:
    """Lê os argumentos de linha de comando."""
    parser = argparse.ArgumentParser(description="Converte PDFs em notas Obsidian.")
    parser.add_argument("source", type=Path, help="PDF ou pasta com PDFs")
    parser.add_argument("-o", "--output", type=Path, default=Path("notas"))
    parser.add_argument(
        "-a",
        "--attachments",
        type=Path,
        default=None,
        help="Pasta de imagens (padrão: <output>/attachments)",
    )
    parser.add_argument("-t", "--tags", nargs="*", default=["didatica"])
    parser.add_argument("--draft", action="store_true", help="Marca draft: true")
    parser.add_argument(
        "--skip-pages",
        help='Páginas a ignorar, base 1 (ex.: "1-4" ou "1-4,60-" para até o fim)',
    )
    parser.add_argument("--title", help="Título fixo (use com um único PDF)")
    parser.add_argument("--description", help="Descrição fixa (um único PDF)")
    parser.add_argument("--no-images", action="store_true", help="Não extrai imagens")
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="Liga OCR (apenas para PDFs escaneados; padrão: desligado)",
    )
    return parser.parse_args()


def main() -> int:
    """Ponto de entrada. Retorna 0 se tudo deu certo, 1 se houve falhas."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()

    if not args.source.exists():
        logger.error("Origem não encontrada: %s", args.source)
        return 1

    pdfs = find_pdfs(args.source)
    if not pdfs:
        logger.error("Nenhum PDF encontrado em %s", args.source)
        return 1

    attachments = args.attachments or args.output / "attachments"
    failures = 0
    for pdf in pdfs:
        try:
            note = convert_pdf(
                pdf,
                args.output,
                attachments,
                args.tags,
                args.draft,
                with_images=not args.no_images,
                with_ocr=args.ocr,
                title_override=args.title,
                skip_pages=args.skip_pages,
                description_override=args.description,
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

# pdf2obsidian

Converte PDFs em notas Markdown prontas para o **Obsidian** e para publicação no **Quartz**.

O script lê um PDF (ou uma pasta inteira), extrai o texto como Markdown, limpa os ruídos típicos de PDF e grava uma nota `.md` com frontmatter no padrão do vault. A saída já passa no `markdownlint` com as regras padrão, exceto o limite de largura de linha (MD013), que não faz sentido para parágrafos corridos.

## Sumário

- [pdf2obsidian](#pdf2obsidian)
  - [Sumário](#sumário)
  - [Requisitos](#requisitos)
  - [Instalação](#instalação)
  - [Uso rápido](#uso-rápido)
  - [Opções](#opções)
  - [O que o script faz](#o-que-o-script-faz)
    - [Frontmatter](#frontmatter)
    - [Limpeza do texto](#limpeza-do-texto)
    - [Imagens](#imagens)
    - [Nomes de arquivo](#nomes-de-arquivo)
  - [Exemplo de saída](#exemplo-de-saída)
  - [Solução de problemas](#solução-de-problemas)
  - [Limitações](#limitações)

## Requisitos

- Python 3.10 ou superior
- [`pymupdf4llm`](https://pypi.org/project/pymupdf4llm/) (instala o `pymupdf` junto)

## Instalação

```bash
uv add pymupdf4llm        # ou: pip install pymupdf4llm
```

Não há mais nada para instalar: o script é um arquivo único, `pdf2obsidian.py`.

## Uso rápido

```bash
# um PDF
python pdf2obsidian.py manual.pdf -o vault/docs

# uma pasta inteira (busca PDFs recursivamente)
python pdf2obsidian.py pasta_pdfs/ -o vault/docs --tags didatica senac

# sem imagens e como rascunho para revisar antes de publicar
python pdf2obsidian.py pasta_pdfs/ -o vault/docs --no-images --draft
```

Caso típico de um documento institucional (capa e sumário fora, título e descrição definidos à mão):

```powershell
python .\pdf2obsidian.py docs\DocTec1_ConcepcoesPrincipios_2022.pdf -o vault\docs --no-images `
  --skip-pages "1-5" `
  --title "Concepções e Princípios" `
  --description "Documento Técnico 1 do Modelo Pedagógico Senac: concepções e princípios que orientam a educação profissional."
```

O código de saída é `0` quando todos os PDFs foram convertidos e `1` se algum falhou, o que facilita o uso em scripts e CI.

## Opções

| Opção                  | Padrão                 | Descrição                                                                              |
| :--------------------- | :--------------------- | :------------------------------------------------------------------------------------- |
| `source`               | —                      | PDF ou pasta com PDFs (busca recursiva).                                               |
| `-o`, `--output`       | `notas`                | Pasta onde as notas `.md` serão gravadas.                                              |
| `-a`, `--attachments`  | `<output>/attachments` | Pasta das imagens extraídas.                                                           |
| `-t`, `--tags`         | `didatica`             | Tags do frontmatter, separadas por espaço.                                             |
| `--draft`              | desligado              | Grava `draft: true` no frontmatter.                                                    |
| `--title`              | automático             | Título fixo. Com uma pasta, vale para todos os PDFs.                                   |
| `--description`        | automática             | Descrição fixa. Com uma pasta, vale para todos os PDFs.                                |
| `--skip-pages`         | nenhuma                | Páginas a ignorar, contadas a partir de 1: `"1-4"`, `"1-4,10"` ou `"60-"` (até o fim). |
| `--duplicate-headings` | `callout`              | `callout` troca títulos repetidos por callouts; `keep` mantém todos como títulos.      |
| `--no-images`          | desligado              | Não extrai imagens.                                                                    |
| `--ocr`                | desligado              | Liga o OCR, só para PDFs escaneados.                                                   |

## O que o script faz

### Frontmatter

Cada nota começa com o frontmatter no padrão do vault:

```yaml
---
title: 'Concepções e Princípios'
description: 'Documento Técnico 1 do Modelo Pedagógico Senac...'
date: '2026-11-05'
draft: false
tags:
  - didatica
---
```

- **title:** vem de `--title`; se ausente, do metadado do PDF; depois, do primeiro título do texto; por fim, do nome do arquivo. Metadados lixo como `arquivo.indd` ou `arquivo.docx` são ignorados.
- **description:** vem de `--description`; se ausente, do primeiro parágrafo longo (mais de 120 caracteres), cortado em 200.
- **date:** a data da conversão.

### Limpeza do texto

| Problema no PDF                                                                  | Resultado na nota                                                                                    |
| :------------------------------------------------------------------------------- | :--------------------------------------------------------------------------------------------------- |
| Palavras hifenizadas na quebra de linha (`apren-` / `dizagem`)                   | `aprendizagem`                                                                                       |
| Parágrafo cortado por troca de página                                            | Parágrafos unidos                                                                                    |
| Número de página solto (`**12**`)                                                | Removido                                                                                             |
| Letra capitular (`**F**` / `ator`)                                               | `Fator`                                                                                              |
| Títulos com níveis embaralhados (H6, H5, saltos)                                 | Hierarquia H2, H3, H4 sem saltos. O H1 fica com o `title` do frontmatter                             |
| Negrito usado no lugar de título                                                 | Texto simples                                                                                        |
| `<sup>a</sup>`, `<sup>1</sup>`                                                   | `ª`, `¹`                                                                                             |
| `<br>` fora de tabelas e blocos                                                  | Espaço                                                                                               |
| "Texto de figura" extraído como HTML                                             | Callout dobrável `> [!note]- Texto extraído de figura`                                               |
| URLs quebradas por espaço (`https://site.org/ pagina`)                           | URL reconectada                                                                                      |
| URLs soltas                                                                      | `<https://...>` ou `[www.site.br](https://www.site.br)`                                              |
| Título repetido várias vezes (ex.: "Possibilidades de uso" sob cada ferramenta)  | Callout `> [!info] Possibilidades de uso` com o conteúdo dentro. Evita o aviso MD024 do markdownlint |
| Texto riscado ou destacado que o PDF gera sem querer (`~~x~~`, `<mark>x</mark>`) | Marcadores removidos e palavras partidas reconectadas                                                |
| Listas com `*` e recuo de 3 espaços                                              | `-` e recuo de 2 espaços                                                                             |
| Quebras de linha do Windows (CRLF)                                               | LF                                                                                                   |

### Imagens

Quando as imagens estão ligadas, elas são salvas como PNG (150 dpi) na pasta de anexos e referenciadas como `![[arquivo.png]]`, a sintaxe de embed que o Obsidian e o Quartz entendem. Use `--no-images` para PDFs em que as imagens são só decoração.

### Nomes de arquivo

Caracteres que quebram links no Obsidian (`# ^ [ ] | : /` e outros) são trocados por espaço no nome da nota.

## Exemplo de saída

Entrada (trecho do texto extraído do PDF):

```text
**F**

ator de desenvolvimento humano e inclusão social, a educa-
ção profissional, neste século, transcende a finalidade de
estrita preparação de mão de obra.
```

Saída na nota:

```markdown
Fator de desenvolvimento humano e inclusão social, a educação profissional,
neste século, transcende a finalidade de estrita preparação de mão de obra.
```

## Solução de problemas

**`UnicodeDecodeError` em threads (`_readerthread`) no Windows**
Ruído do console: o `pymupdf4llm` dispara um subprocesso que emite `cp1252` e o Python tenta ler como UTF-8. O script já força `PYTHONUTF8=1` antes de importar a biblioteca. Se ainda aparecer e a nota for gerada, pode ignorar.

**`'NoneType' object is not iterable` ao converter**
Vem do RapidOCR, que devolve `None` quando não acha texto em uma imagem. O script desliga o OCR por padrão. Se o erro persistir, a sua versão do `pymupdf4llm` não aceita o parâmetro `use_ocr`; desinstale o plugin com `pip uninstall rapidocr-onnxruntime`.

**`sem texto extraível (PDF escaneado? use OCR)`**
O PDF é só imagem. Rode OCR antes (por exemplo, com `ocrmypdf`) ou tente `--ocr`.

**Título ou descrição genéricos**
PDFs institucionais costumam ter capa, ficha catalográfica e metadados ruins. Corte essas páginas com `--skip-pages` ou defina `--title` e `--description` na mão.

## Limitações

- **Wikilinks:** o script não cria links internos como `[[Aprendizagem]]`.
- **Títulos repetidos:** só viram callout os títulos sem subtítulos. Um título repetido que tenha subtítulos abaixo continua como título e ainda gera o aviso MD024.
- **Callouts consecutivos:** dois callouts separados por uma linha em branco geram o aviso MD028 (linha em branco dentro de citação).
- **Letras capitulares no meio da palavra:** quando o PDF coloca a capitular na posição errada (`eduO cacionais`), o erro vai para a nota. Vale buscar maiúsculas coladas no meio de palavras.
- **Junção de parágrafos:** é heurística. Parágrafos que começam em minúscula e seguem um bloco sem pontuação final são unidos, o que pode errar em listas sem marcador.
- **URLs quebradas:** a reconexão também é heurística. Confira as URLs da bibliografia.
- **Revisão manual:** títulos e tabelas de PDFs mal estruturados costumam precisar de um ajuste. Use `--draft` para revisar antes de publicar.
- **Escopo:** converte o conteúdo, não o layout. Colunas, caixas de texto e figuras complexas podem sair na ordem errada.

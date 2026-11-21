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
  - [senac2obsidian](#senac2obsidian)
    - [Requisitos e uso](#requisitos-e-uso)
    - [Saída](#saída)
    - [Propriedades que se repetem](#propriedades-que-se-repetem)
    - [Dados de alunos](#dados-de-alunos)
    - [Avisos do markdownlint](#avisos-do-markdownlint)
    - [Limitações do layout](#limitações-do-layout)
  - [pdfcap2obsidian](#pdfcap2obsidian)
  - [Uso](#uso)
  - [Opções pdfcap2obsidian](#opções-pdfcap2obsidian)
  - [Cabeçalho (Quartz v5)](#cabeçalho-quartz-v5)
  - [Como detecta o sumário](#como-detecta-o-sumário)
  - [O que o script faz com o texto](#o-que-o-script-faz-com-o-texto)
  - [Verificação](#verificação)
  - [Limitações pdfcap2obsidian](#limitações-pdfcap2obsidian)

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

---

## senac2obsidian

Segundo script do projeto, específico para os PDFs que o sistema acadêmico do Senac exporta: o **Diário de Classe** e o **PTD** (Plano de Trabalho do Docente). Diferente do `pdf2obsidian.py`, ele não faz extração genérica: lê o PDF por posição na página, entende as seções de cada relatório e monta a nota no padrão do vault.

### Requisitos e uso

- Python 3.10 ou superior e `pip install pymupdf`.

```bash
python senac2obsidian.py PTD_2026_12_331.pdf -o vault/docs
python senac2obsidian.py pasta_pdfs/ -o vault/docs --draft
```

O tipo de relatório é reconhecido pelo título da primeira página. Um PDF de outro tipo falha com a sugestão de usar o `pdf2obsidian.py`.

| Opção            | Padrão     | Descrição                                                                                                                      |
| :--------------- | :--------- | :----------------------------------------------------------------------------------------------------------------------------- |
| `source`         | —          | PDF ou pasta com PDFs (busca recursiva).                                                                                       |
| `-o`, `--output` | `notas`    | Pasta de saída.                                                                                                                |
| `-t`, `--tags`   | nenhuma    | Tags extras (além de `ptd`/`diario`, `senac`, `ucN` e o curso).                                                                |
| `--draft`        | desligado  | Grava `draft: true`.                                                                                                           |
| `--ptd`          | automático | PDF do PTD usado para numerar aulas e SAs do Diário. Sem ele, o script procura, na pasta do Diário, o PTD da mesma turma e UC. |
| `--full-cha`     | desligado  | Repete Conhecimentos, Habilidades e Atitudes em todas as aulas.                                                                |

### Saída

O nome do arquivo vem da identificação: `PTD-2026.12.331-UC1-Programador-de-Sistemas.md` e `Diario-2026.12.331-UC1-Programador-de-Sistemas.md`.

**PTD**, no modelo do vault:

- `# Plano de Trabalho do Docente - UC1` e o callout `[!info] Identificação`.
- `## Situações de Aprendizagem` → `### SAx - nome` (callout `[!abstract]`, com planejamento, descrição, recursos, indicadores e CHA) → `#### Aula N - tema` (callout `[!tip]`).
- As aulas ficam na SA indicada pelo próprio PDF, não pela ordem do calendário (por exemplo, SA1 com as aulas 6 a 17 e SA2 com as aulas 1 a 5 e 18).
- `## Avaliações` (callouts `[!warning]`) e `## Contribuição da Unidade Curricular para o Projeto Integrador`.
**Diário de Classe**, com a mesma organização do PTD:

- `## Avaliações` e uma seção com os Conhecimentos, Habilidades e Atitudes da UC.
- `## Aulas` → `### SAx - nome` → `#### Aula N - tema`, com o callout `[!tip] Aula N - dd/mm/aaaa`.
- O número da aula e a SA vêm do PTD da mesma turma e UC, ligados pela data. Sem PTD, as aulas são numeradas pela ordem dos dias e as SAs ficam sem número.
- O texto livre do conteúdo mantém as quebras de linha e as listas, e protege `*args`, `**kwargs` e `__init__` para não virarem formatação.
Nas aulas, Conhecimentos, Habilidades e Atitudes só aparecem quando são um subconjunto da lista completa da UC. Se forem iguais, ficam só na lista completa. Use `--full-cha` para repetir em todas.

### Propriedades que se repetem

Horário, Ambiente Pedagógico, Docente(s), Forma de Execução e Recursos Didáticos costumam ser iguais em todas as aulas. O script mostra o valor mais comum uma única vez, em **Padrão das aulas**, no callout de Identificação. Em cada aula aparecem só os valores diferentes do padrão, e o Docente(s) igual ao da identificação não se repete.

### Dados de alunos

As seções **Feedbacks**, **Observações** e **Histórico de Revisões** do Diário são **ignoradas de propósito**: trazem nomes, menções, desistências e justificativas de alunos. Se a pasta `vault/docs` é publicada (Quartz, GitHub Pages ou repositório público), esses dados não devem ir junto. O script informa no terminal quais seções foram puladas.

### Avisos do markdownlint

As notas saem sem avisos, exceto MD013 (linha longa), MD025 e MD028. O MD025 vem do `title` no frontmatter somado ao `#` do corpo, que é o padrão do modelo do vault. O MD028 aparece quando dois callouts de avaliação ficam em sequência. Se incomodar, no `.markdownlint.json`:

```json
{ "MD013": false, "MD025": { "front_matter_title": "" }, "MD028": false }
```

### Limitações do layout

- O script depende do layout do relatório do sistema (posições das colunas e fontes). Se o sistema mudar o layout do PDF, as seções podem sair vazias ou trocadas.
- O texto é preservado como está no PDF, inclusive erros de digitação do sistema (por exemplo, `busca0se`).
- A junção de linhas quebradas usa a margem direita da página e a pontuação final dos itens, então um item de lista sem ponto final pode ser unido ao seguinte.

## pdfcap2obsidian

Converte **capítulos ou seções** de um PDF em notas Markdown do Obsidian, mantendo o texto **palavra por palavra**. Só muda a forma (parágrafos, títulos, negrito/itálico, callouts, frontmatter).

Requisitos: Python 3.10+ e `pip install pymupdf`. O script é um arquivo só.

## Uso

```bash
# ver o sumário numerado
python pdfcap2obsidian.py livro.pdf --list

# sem -s, o script mostra o sumário e pergunta o que converter
python pdfcap2obsidian.py livro.pdf -o vault/estudos

# por parâmetro (número, intervalo, título, level:N, re:regex ou todos)
python pdfcap2obsidian.py livro.pdf -s "Marcos 1" -o vault/estudos
python pdfcap2obsidian.py livro.pdf -s level:3            # um arquivo por capítulo
python pdfcap2obsidian.py livro.pdf -s 14 16-18 --merge --title "Trecho"

# sem sumário: por páginas
python pdfcap2obsidian.py livro.pdf --pages 14-49 --title "Trecho"
```

Comando usado para gerar o capítulo 1 de Marcos:

```bash
python pdfcap2obsidian.py Marcos__Barclay_.pdf -s "Marcos 1" -o vault/estudos \
  -t Teologia Novo-Testamento Evangelho Comentário Estudo
```

## Opções pdfcap2obsidian

| Opção                                   | Descrição                                                                                                                                                                                                                                                                        |
| :-------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-s`, `--sections`                      | O que converter. Número, intervalo (`14-16`), título (`"Marcos 1"`), `level:N`, `re:regex`, `todos`. Títulos: o exato vence a busca parcial.                                                                                                                                     |
| `--list`, `--level N`                   | Só mostra o sumário (até o nível N).                                                                                                                                                                                                                                             |
| `--pages 14-49`                         | Converte páginas (base 1), sem usar o sumário.                                                                                                                                                                                                                                   |
| `--toc`                                 | `auto` (padrão), `outline`, `printed` ou `fonts`.                                                                                                                                                                                                                                |
| `--book`, `--title`, `--title-template` | Nome do livro (padrão: metadado do PDF, em caixa de frase), título fixo ou modelo (`{book}`, `{title}`, `{n}`). Sem modelo, itens numerados viram `{book} - Capítulo 1` (troque a palavra com `--chapter-word`) e os demais `{book} - {title}`. O nome do arquivo vem do título. |
| `--description`                         | Padrão: início do primeiro parágrafo do texto, até 160 caracteres.                                                                                                                                                                                                               |
| `-t`, `--tags`                          | Tags. São normalizadas para o Quartz: minúsculas, sem acento, hífen no lugar de espaço.                                                                                                                                                                                          |
| `--fm`                                  | `quartz` (padrão), `dg` (Digital Garden: `title`, `tags`, `dg-publish`) ou `none`.                                                                                                                                                                                               |
| `--draft`, `--no-date-draft`            | `draft: true`. Com `--no-date-draft` o Quartz não recebe `date` nem `draft`.                                                                                                                                                                                                     |
| `--panel`                               | Nota do link `← Voltar ao Painel Geral`.                                                                                                                                                                                                                                         |
| `--merge`                               | Junta as seleções em uma nota.                                                                                                                                                                                                                                                   |
| `--no-callouts`                         | Não transforma citações em itálico em callouts.                                                                                                                                                                                                                                  |
| `--link-refs`                           | `**Marcos 1:1-4**` vira `**[[Marcos 1#1\|Marcos 1:1-4]]**` (texto visível idêntico).                                                                                                                                                                                             |
| `--keep-index`                          | Mantém a lista de seções que abre cada capítulo.                                                                                                                                                                                                                                 |
| `--no-verify`                           | Pula a verificação de fidelidade.                                                                                                                                                                                                                                                |

Código de saída: `0` ok, `1` erro, `2` se o texto gerado diferir do PDF.

## Cabeçalho (Quartz v5)

```yaml
---
title: "Marcos (William Barclay) - Capítulo 1"
description: "Marcos começa a história de Jesus desde muito atrás. A história de…"
date: 2026-11-21
draft: false
tags:
  - teologia
  - novo-testamento
aliases:
  - "Marcos 1"
---
```

- O título aparece só no frontmatter. O corpo não repete `# H1`, porque o Quartz já o renderiza.
- `aliases` guarda o nome do item no sumário (`Marcos 1`), para que `[[Marcos 1]]` resolva para a nota.
- A descrição é um trecho do próprio texto, cortado na última palavra inteira.

## Como detecta o sumário

1. **Marcadores embutidos** do PDF (o caso mais confiável).
2. **Sumário impresso** (`Título ..... 12`) nas primeiras páginas, com a diferença entre página impressa e do PDF calculada sozinha (`--page-offset` para forçar).
3. **Títulos por fonte** (linhas curtas, maiores que o corpo ou em negrito).

Cada título é localizado na página pela linha exata, então várias seções que dividem a mesma página são cortadas no ponto certo. Se o título do marcador tiver erro de digitação em relação ao texto, usa busca aproximada e avisa.

## O que o script faz com o texto

| No PDF                                                                    | Na nota                                             |
| :------------------------------------------------------------------------ | :-------------------------------------------------- |
| Cabeçalho e número de página repetidos                                    | Removidos                                           |
| Linhas quebradas                                                          | Parágrafos (por recuo, espaço e fim de linha curto) |
| Parágrafo cortado pela troca de página                                    | Unido                                               |
| `confessá-` / `lo` na quebra de linha                                     | `confessá-lo` (o hífen é mantido)                   |
| Título em caixa alta                                                      | `###` em caixa de frase                             |
| Negrito e itálico                                                         | `**` e `*`                                          |
| Parágrafo todo em itálico, ou frase inicial em itálico com 50+ caracteres | Callout `> [!quote]`                                |
| `*` de nota de rodapé                                                     | `[^1]` e definição no fim                           |
| `*`, `_`, `<`, `[[` e outros do texto                                     | Escapados                                           |
| Lista de seções que abre o capítulo                                       | Omitida (é só o índice)                             |

Erros de digitação do original (`pelo. momento`, `séria`, `SUS AMIGOS`) são mantidos de propósito.

## Verificação

Depois de gerar cada nota, o script remove só a sintaxe que ele mesmo adicionou e compara, caractere a caractere (sem espaços e sem caixa), com o texto extraído do PDF para o mesmo trecho. Qualquer diferença é apontada com o contexto.

## Limitações pdfcap2obsidian

- PDF escaneado precisa de OCR antes. Imagens não são extraídas.
- Só vira callout o que está em itálico no PDF. Rótulos e títulos de callout (`[!example] Título`) exigiriam texto novo, então não são criados.
- A detecção de parágrafos é heurística. Texto sem recuo e sem linha final curta pode ser unido.
- Colunas e tabelas complexas não são reconstruídas.

# pdf2obsidian

O `pdf2obsidian.py` converte um PDF, ou uma pasta inteira de PDFs, em notas `.md` prontas para o vault do obsidian.

## Como funciona

- Usa a biblioteca `pymupdf4llm` para extrair o texto como Markdown, detectando títulos, listas e tabelas.
- Gera o frontmatter no mesmo padrão das suas notas (`title`, `description`, `date`, `draft`, `tags`).
- O título vem do metadado do PDF, do primeiro título do texto ou do nome do arquivo, nessa ordem. A descrição é o primeiro parágrafo longo, cortado em 200 caracteres.
- Limpa o texto: junta palavras hifenizadas na quebra de linha ("apren-\ndizagem" vira "aprendizagem"), remove espaços estranhos e linhas em branco repetidas.
- Extrai as imagens para uma pasta `attachments` e as referencia como `![[imagem.png]]`, que o Obsidian e o Quartz entendem.
- Troca caracteres que quebram links no Obsidian (`# ^ [ ] | : /`) nos nomes dos arquivos.

## Como usar

```bash
uv add pymupdf4llm      # ou: pip install pymupdf4llm

python pdf2obsidian.py manual.pdf -o vault/docs
python pdf2obsidian.py pasta_pdfs/ -o vault/docs --tags didatica senac
python pdf2obsidian.py pasta_pdfs/ -o vault/docs --no-images --draft
python .\pdf2obsidian.py docs\DocTec1_ConcepcoesPrincipios_2022.pdf -o vault\docs --no-images `
  --skip-pages "1-5" `
  --title "Concepções e Princípios" `
  --description "Documento Técnico 1 do Modelo Pedagógico Senac: concepções e princípios que orientam a educação profissional."
```

## Pontos de atenção

- **PDFs escaneados:** se o PDF for só imagem, o script avisa que não há texto extraível. Nesse caso é preciso rodar OCR antes, por exemplo com `ocrmypdf`.
- **Tags:** o padrão é `didatica`, como nas suas notas. Mude com `--tags`.
- **Rascunho:** use `--draft` para marcar `draft: true` e revisar antes de publicar no Quartz.
- **Revisão manual:** a descrição automática e os títulos detectados em PDFs mal estruturados costumam precisar de um ajuste.
- **Wikilinks:** o script não cria links internos como `[[Aprendizagem]]`.

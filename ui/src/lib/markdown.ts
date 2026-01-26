/**
 * Markdown rendering using marked library
 */

import { marked, type Tokens } from "marked"

// Configure marked
marked.setOptions({
  breaks: true, // Convert \n to <br>
  gfm: true, // GitHub Flavored Markdown (tables, strikethrough, etc.)
})

// Custom renderer to add CSS classes
const renderer = new marked.Renderer()

// Add classes to headers
renderer.heading = function({ tokens, depth }: Tokens.Heading): string {
  const text = this.parser.parseInline(tokens)
  return `<h${depth} class="markdown-h${depth}">${text}</h${depth}>\n`
}

// Add class to tables
renderer.table = function(token: Tokens.Table): string {
  let header = "<tr>"
  for (const cell of token.header) {
    const content = this.parser.parseInline(cell.tokens)
    const align = cell.align ? ` style="text-align:${cell.align}"` : ""
    header += `<th${align}>${content}</th>`
  }
  header += "</tr>"

  let body = ""
  for (const row of token.rows) {
    body += "<tr>"
    for (const cell of row) {
      const content = this.parser.parseInline(cell.tokens)
      const align = cell.align ? ` style="text-align:${cell.align}"` : ""
      body += `<td${align}>${content}</td>`
    }
    body += "</tr>"
  }

  return `<table class="markdown-table"><thead>${header}</thead><tbody>${body}</tbody></table>\n`
}

// Add class to lists
renderer.list = function(token: Tokens.List): string {
  const tag = token.ordered ? "ol" : "ul"
  let body = ""
  for (const item of token.items) {
    body += this.listitem(item)
  }
  return `<${tag} class="markdown-list">${body}</${tag}>\n`
}

// Add class to code blocks
renderer.code = function({ text, lang }: Tokens.Code): string {
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
  const langClass = lang ? ` language-${lang}` : ""
  return `<pre class="markdown-code${langClass}"><code>${escaped}</code></pre>\n`
}

// Add class to blockquotes
renderer.blockquote = function({ tokens }: Tokens.Blockquote): string {
  const inner = this.parser.parse(tokens)
  return `<blockquote class="markdown-blockquote">${inner}</blockquote>\n`
}

marked.use({ renderer })

/**
 * Render markdown source to HTML
 */
export function renderMarkdown(source: string): string {
  // Escape < and > to prevent XSS, but keep markdown functional
  const escaped = source
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")

  const result = marked.parse(escaped, { async: false }) as string
  return result
}

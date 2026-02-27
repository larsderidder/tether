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

  return `<div class="markdown-table-wrapper"><table class="markdown-table"><thead>${header}</thead><tbody>${body}</tbody></table></div>\n`
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

// Helper to fix double-escaped HTML entities in code
function fixCodeEscaping(text: string): string {
  // Text may already have &lt; &gt; from pre-escaping, decode first then re-escape
  const decoded = text
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
  return decoded
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
}

// Add class to code blocks
renderer.code = function({ text, lang }: Tokens.Code): string {
  const escaped = fixCodeEscaping(text)
  const langClass = lang ? ` language-${lang}` : ""
  return `<pre class="markdown-code${langClass}"><code>${escaped}</code></pre>\n`
}

// Handle inline code (backticks)
renderer.codespan = function({ text }: Tokens.Codespan): string {
  const escaped = fixCodeEscaping(text)
  return `<code>${escaped}</code>`
}

// Add class to blockquotes
renderer.blockquote = function({ tokens }: Tokens.Blockquote): string {
  const inner = this.parser.parse(tokens)
  return `<blockquote class="markdown-blockquote">${inner}</blockquote>\n`
}

marked.use({ renderer })

/**
 * Decode HTML entities in a string
 */
export function decodeHtmlEntities(text: string): string {
  const textarea = document.createElement("textarea")
  textarea.innerHTML = text
  return textarea.value
}

/**
 * Render markdown source to HTML
 */
export function renderMarkdown(source: string): string {
  // First decode any HTML entities that came from the server
  const decoded = decodeHtmlEntities(source)

  // Escape < and > to prevent XSS, but keep markdown functional
  const escaped = decoded
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")

  const result = marked.parse(escaped, { async: false }) as string
  return result
}

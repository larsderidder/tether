/**
 * Shared markdown rendering utility
 * Supports: inline code, bold, italics, and tables
 */

/** Escape HTML entities for safe rendering */
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
}

/** Parse a table row into cells */
function parseTableRow(row: string): string[] {
  // Remove leading/trailing pipes and split by |
  const trimmed = row.trim()
  const withoutPipes = trimmed.startsWith("|") ? trimmed.slice(1) : trimmed
  const withoutEnd = withoutPipes.endsWith("|") ? withoutPipes.slice(0, -1) : withoutPipes
  return withoutEnd.split("|").map((cell) => cell.trim())
}

/** Check if a line is a table separator row (e.g., |---|---|) */
function isSeparatorRow(line: string): boolean {
  const cells = parseTableRow(line)
  return cells.every((cell) => /^:?-+:?$/.test(cell))
}

/** Check if a line looks like a table row */
function isTableRow(line: string): boolean {
  const trimmed = line.trim()
  return trimmed.startsWith("|") && trimmed.includes("|", 1)
}

/** Render inline markdown (code, bold, italic) */
function renderInline(text: string): string {
  let out = text.replace(/`([^`]+)`/g, "<code>$1</code>")
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
  out = out.replace(/(^|\s)\*([^*]+)\*/g, "$1<em>$2</em>")
  return out
}

/** Render a markdown table to HTML */
function renderTable(lines: string[]): string {
  if (lines.length < 2) return lines.map(renderInline).join("<br />")

  // Check if second line is separator
  if (!isSeparatorRow(lines[1])) {
    return lines.map(renderInline).join("<br />")
  }

  const headerCells = parseTableRow(lines[0])
  const bodyRows = lines.slice(2)

  let html = '<table class="markdown-table">'

  // Render header
  html += "<thead><tr>"
  for (const cell of headerCells) {
    html += `<th>${renderInline(cell)}</th>`
  }
  html += "</tr></thead>"

  // Render body
  if (bodyRows.length > 0) {
    html += "<tbody>"
    for (const row of bodyRows) {
      const cells = parseTableRow(row)
      html += "<tr>"
      for (const cell of cells) {
        html += `<td>${renderInline(cell)}</td>`
      }
      html += "</tr>"
    }
    html += "</tbody>"
  }

  html += "</table>"
  return html
}

/**
 * Render markdown source to HTML
 * Supports: inline code, bold, italics, and tables
 */
export function renderMarkdown(source: string): string {
  const escaped = escapeHtml(source)
  const lines = escaped.split("\n")

  const result: string[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]

    // Check for table start
    if (isTableRow(line)) {
      // Collect consecutive table rows
      const tableLines: string[] = []
      while (i < lines.length && isTableRow(lines[i])) {
        tableLines.push(lines[i])
        i++
      }
      result.push(renderTable(tableLines))
    } else {
      // Regular line processing
      const trimmed = line.trim()
      if (trimmed.toLowerCase() === "thinking") {
        result.push("<em>thinking</em>")
      } else {
        result.push(renderInline(line))
      }
      i++
    }
  }

  return result.join("<br />")
}

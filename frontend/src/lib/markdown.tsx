import type { ReactNode } from "react";

// A deliberately small Markdown renderer: headings, paragraphs, lists,
// tables, blockquotes, fenced code, inline code, bold/italic/strike and links.
// It never uses dangerouslySetInnerHTML, so model output cannot inject markup.

type Block =
  | { kind: "code"; text: string; lang: string }
  | { kind: "heading"; level: number; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "table"; header: string[]; rows: string[][] }
  | { kind: "quote"; text: string }
  | { kind: "hr" }
  | { kind: "p"; text: string };

const SAFE_HREF = /^(https?:\/\/|mailto:)/i;

function splitRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((cell) => cell.trim());
}

function isTableSeparator(line: string): boolean {
  return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(line);
}

function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (!line.trim()) {
      index += 1;
      continue;
    }

    // Fenced code.
    const fence = line.match(/^\s*```([\w+-]*)\s*$/);
    if (fence) {
      const lang = fence[1] || "";
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1; // closing fence
      blocks.push({ kind: "code", text: body.join("\n"), lang });
      continue;
    }

    // Horizontal rule.
    if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) {
      blocks.push({ kind: "hr" });
      index += 1;
      continue;
    }

    // Heading.
    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/);
    if (heading) {
      blocks.push({
        kind: "heading",
        level: Math.min(6, heading[1].length),
        text: heading[2].trim(),
      });
      index += 1;
      continue;
    }

    // Blockquote.
    if (/^\s*>\s?/.test(line)) {
      const body: string[] = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        body.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      blocks.push({ kind: "quote", text: body.join(" ") });
      continue;
    }

    // Table: header row followed by a separator row.
    if (
      line.includes("|") &&
      index + 1 < lines.length &&
      isTableSeparator(lines[index + 1])
    ) {
      const header = splitRow(line);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(splitRow(lines[index]));
        index += 1;
      }
      blocks.push({ kind: "table", header, rows });
      continue;
    }

    // Lists (consecutive items of the same kind).
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || ordered) {
      const isOrdered = Boolean(ordered);
      const items: string[] = [];
      while (index < lines.length) {
        const current = lines[index];
        const next = isOrdered
          ? current.match(/^\s*\d+[.)]\s+(.*)$/)
          : current.match(/^\s*[-*+]\s+(.*)$/);
        if (!next) break;
        items.push(next[1]);
        index += 1;
        // Continuation lines are indented; fold them into the item.
        while (
          index < lines.length &&
          /^\s{2,}\S/.test(lines[index]) &&
          !/^\s*([-*+]|\d+[.)])\s+/.test(lines[index])
        ) {
          items[items.length - 1] += ` ${lines[index].trim()}`;
          index += 1;
        }
      }
      blocks.push({ kind: "list", ordered: isOrdered, items });
      continue;
    }

    // Paragraph: gather until a blank line or the start of another block.
    const body: string[] = [];
    while (index < lines.length && lines[index].trim()) {
      const current = lines[index];
      if (
        body.length > 0 &&
        (/^\s*```/.test(current) ||
          /^\s{0,3}#{1,6}\s+/.test(current) ||
          /^\s*>\s?/.test(current) ||
          /^\s*[-*+]\s+/.test(current) ||
          /^\s*\d+[.)]\s+/.test(current) ||
          /^\s*([-*_])\s*(\1\s*){2,}$/.test(current) ||
          (current.includes("|") &&
            index + 1 < lines.length &&
            isTableSeparator(lines[index + 1])))
      ) {
        break;
      }
      body.push(current.trim());
      index += 1;
    }
    blocks.push({ kind: "p", text: body.join(" ") });
  }

  return blocks;
}

const INLINE_SOURCE = [
  "(`[^`\\n]+`)", // 1 inline code
  "(\\*\\*[^*\\n]+\\*\\*|__[^_\\n]+__)", // 2 bold
  "(~~[^~\\n]+~~)", // 3 strike
  "(\\[[^\\]\\n]+\\]\\([^)\\s]+\\))", // 4 link
  "(https?://[^\\s<>()]+)", // 5 bare url
  "(\\*[^*\\n]+\\*|_[^_\\n]+_)", // 6 italic
].join("|");

function renderInline(text: string, prefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  // A fresh regex per call: renderInline recurses for bold/italic/link text,
  // and a shared global regex would have its lastIndex reset by the inner
  // call, making the outer loop re-match the same token forever.
  const inline = new RegExp(INLINE_SOURCE, "g");

  while ((match = inline.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const token = match[0];
    const key = `${prefix}-${match.index}`;

    if (match[1]) {
      nodes.push(
        <code className="md-inline-code" key={key}>
          {token.slice(1, -1)}
        </code>,
      );
    } else if (match[2]) {
      const inner = token.startsWith("**")
        ? token.slice(2, -2)
        : token.slice(2, -2);
      nodes.push(<strong key={key}>{renderInline(inner, key)}</strong>);
    } else if (match[3]) {
      nodes.push(<del key={key}>{renderInline(token.slice(2, -2), key)}</del>);
    } else if (match[4]) {
      const link = token.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
      if (link) {
        const label = link[1];
        const href = link[2];
        nodes.push(
          SAFE_HREF.test(href) ? (
            <a key={key} href={href} target="_blank" rel="noreferrer">
              {renderInline(label, key)}
            </a>
          ) : (
            <span key={key}>{renderInline(label, key)}</span>
          ),
        );
      } else {
        nodes.push(token);
      }
    } else if (match[5]) {
      nodes.push(
        <a key={key} href={token} target="_blank" rel="noreferrer">
          {token}
        </a>,
      );
    } else if (match[6]) {
      nodes.push(<em key={key}>{renderInline(token.slice(1, -1), key)}</em>);
    } else {
      nodes.push(token);
    }
    last = match.index + token.length;
  }

  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function renderBlocks(blocks: Block[]): ReactNode[] {
  return blocks.map((block, index) => {
    const key = `md-${index}`;
    switch (block.kind) {
      case "code":
        return (
          <pre className="md-pre" key={key}>
            <code>{block.text}</code>
          </pre>
        );
      case "heading": {
        const level = Math.min(5, Math.max(2, block.level + 1));
        const Tag = `h${level}` as "h2" | "h3" | "h4" | "h5";
        return <Tag key={key}>{renderInline(block.text, key)}</Tag>;
      }
      case "list": {
        const Tag = block.ordered ? "ol" : "ul";
        return (
          <Tag key={key}>
            {block.items.map((item, itemIndex) => (
              <li key={`${key}-${itemIndex}`}>
                {renderInline(item, `${key}-${itemIndex}`)}
              </li>
            ))}
          </Tag>
        );
      }
      case "table":
        return (
          <div className="md-table-wrap" key={key}>
            <table className="md-table">
              <thead>
                <tr>
                  {block.header.map((cell, cellIndex) => (
                    <th key={`${key}-h-${cellIndex}`}>
                      {renderInline(cell, `${key}-h-${cellIndex}`)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, rowIndex) => (
                  <tr key={`${key}-r-${rowIndex}`}>
                    {row.map((cell, cellIndex) => (
                      <td key={`${key}-r-${rowIndex}-${cellIndex}`}>
                        {renderInline(cell, `${key}-r-${rowIndex}-${cellIndex}`)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      case "quote":
        return <blockquote key={key}>{renderInline(block.text, key)}</blockquote>;
      case "hr":
        return <hr key={key} />;
      default:
        return <p key={key}>{renderInline(block.text, key)}</p>;
    }
  });
}

export default function Markdown({ text }: { text?: string | null }) {
  const source = String(text || "").trim();
  if (!source) return null;
  return <div className="md">{renderBlocks(parseBlocks(source))}</div>;
}

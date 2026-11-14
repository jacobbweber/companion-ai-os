import React from 'react';
import type { FlashcardDeckPayload } from '../types';
import { FlashcardDeckWidget } from './FlashcardDeckWidget';

interface MarkdownViewProps {
  content: string;
  onOpenWikiNote?: (target: string) => void;
  onFilterTag?: (tag: string) => void;
  onFlashcardGraded?: () => void;
}

function renderInline(
  text: string,
  onOpenWikiNote?: (target: string) => void,
  onFilterTag?: (tag: string) => void
): React.ReactNode[] {
  // Tokenize inline code, [[wikilinks]], **bold**, local ![alt](/api/attachments/... | /api/wiki/assets/... | 03_resources/assets/...), [label](url), and #tags
  const tokenRe =
    /(`[^`\n]+`)|(\[\[[^\]\n]+\]\])|(\*\*[^*\n]+\*\*)|(!\[[^\]\n]*\]\((?:\/api\/attachments\/|\/api\/wiki\/assets\/|(?:\.\/|\.\.\/)*03_resources\/assets\/)[^)\s]+\))|(\[[^\]\n]+\]\((?:https?:\/\/|\/)[^)\s]+\))|(#[a-zA-Z0-9_-]{2,32})/g;
  const nodes: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;

  while ((match = tokenRe.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }
    const raw = match[0];
    if (match[1]) {
      nodes.push(
        <code key={key++} className="md-inline-code">
          {raw.slice(1, -1)}
        </code>
      );
    } else if (match[2]) {
      const inner = raw.slice(2, -2);
      const [target, alias] = inner.split('|').map((s) => s.trim());
      nodes.push(
        <button
          key={key++}
          type="button"
          className="md-wikilink"
          onClick={() => onOpenWikiNote?.(target)}
          title={`Open Wiki note: ${target}`}
        >
          [[{alias || target}]]
        </button>
      );
    } else if (match[3]) {
      nodes.push(<strong key={key++}>{raw.slice(2, -2)}</strong>);
    } else if (match[4]) {
      const mImg = /^!\[([^\]]*)\]\(([^)]+)\)$/.exec(raw);
      if (mImg) {
        const alt = mImg[1] || 'Attached image';
        const rawSrc = mImg[2];
        const src = rawSrc.includes('03_resources/assets/')
          ? `/api/wiki/assets/${rawSrc.split('03_resources/assets/')[1]}`
          : rawSrc;
        nodes.push(
          <a
            key={key++}
            href={src}
            target="_blank"
            rel="noreferrer"
            className="md-attachment-img-card"
            title={`Open full image: ${alt}`}
          >
            <img src={src} alt={alt} loading="lazy" className="md-attachment-img" />
            <span className="md-attachment-caption">{alt}</span>
          </a>
        );
      } else {
        nodes.push(raw);
      }
    } else if (match[5]) {
      const mLink = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(raw);
      if (mLink) {
        const isAtt = mLink[2].startsWith('/api/attachments/');
        nodes.push(
          <a
            key={key++}
            href={mLink[2]}
            target="_blank"
            rel="noreferrer"
            className={isAtt ? 'md-attachment-chip' : 'md-link'}
          >
            {mLink[1]}
          </a>
        );
      } else {
        nodes.push(raw);
      }
    } else if (match[6]) {
      const tag = raw.slice(1);
      nodes.push(
        <button
          key={key++}
          type="button"
          className="md-tag-pill"
          onClick={() => onFilterTag?.(tag)}
        >
          #{tag}
        </button>
      );
    }
    lastIndex = match.index + raw.length;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }
  return nodes;
}

export const MarkdownView: React.FC<MarkdownViewProps> = ({
  content,
  onOpenWikiNote,
  onFilterTag,
  onFlashcardGraded,
}) => {
  if (!content) return null;
  const lines = content.replace(/\r\n/g, '\n').split('\n');
  const blocks: React.ReactNode[] = [];
  let i = 0;
  let bKey = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block
    if (line.trimStart().startsWith('```')) {
      const lang = line.trim().slice(3).trim() || 'code';
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trimStart().startsWith('```')) {
        codeLines.push(lines[i]);
        i++;
      }
      i++;
      const rawBlock = codeLines.join('\n');
      if (lang.toLowerCase() === 'companion-flashcards') {
        try {
          const parsed = JSON.parse(rawBlock) as FlashcardDeckPayload;
          if (parsed && Array.isArray(parsed.cards) && parsed.cards.length > 0) {
            blocks.push(
              <FlashcardDeckWidget key={bKey++} deck={parsed} onGraded={onFlashcardGraded} />
            );
            continue;
          }
        } catch {
          // Still streaming JSON — fall back to code block until closing fence completes
        }
      }
      blocks.push(
        <div key={bKey++} className="md-code-block">
          <div className="md-code-header">
            <span>{lang.toUpperCase()}</span>
          </div>
          <pre>
            <code>{rawBlock}</code>
          </pre>
        </div>
      );
      continue;
    }

    // Markdown table
    if (
      line.includes('|') &&
      i + 1 < lines.length &&
      /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1])
    ) {
      const parseCells = (row: string) =>
        row
          .trim()
          .replace(/^\|/, '')
          .replace(/\|$/, '')
          .split('|')
          .map((c) => c.trim());
      const headers = parseCells(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
        rows.push(parseCells(lines[i]));
        i++;
      }
      blocks.push(
        <div key={bKey++} className="md-table-wrap">
          <table className="md-table">
            <thead>
              <tr>
                {headers.map((h, hi) => (
                  <th key={hi}>{renderInline(h, onOpenWikiNote, onFilterTag)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>
                  {r.map((c, ci) => (
                    <td key={ci}>{renderInline(c, onOpenWikiNote, onFilterTag)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
      continue;
    }

    // Headings
    const hMatch = /^(#{1,4})\s+(.+)$/.exec(line);
    if (hMatch) {
      const level = hMatch[1].length;
      const inner = renderInline(hMatch[2], onOpenWikiNote, onFilterTag);
      if (level === 1) blocks.push(<h1 key={bKey++}>{inner}</h1>);
      else if (level === 2) blocks.push(<h2 key={bKey++}>{inner}</h2>);
      else if (level === 3) blocks.push(<h3 key={bKey++}>{inner}</h3>);
      else blocks.push(<h4 key={bKey++}>{inner}</h4>);
      i++;
      continue;
    }

    // Horizontal rule
    if (/^\s*([-*_]){3,}\s*$/.test(line)) {
      blocks.push(<hr key={bKey++} className="md-hr" />);
      i++;
      continue;
    }

    // Blockquote
    if (line.trimStart().startsWith('> ')) {
      const qLines: string[] = [];
      while (i < lines.length && lines[i].trimStart().startsWith('>')) {
        qLines.push(lines[i].replace(/^\s*>\s?/, ''));
        i++;
      }
      blocks.push(
        <blockquote key={bKey++} className="md-quote">
          {renderInline(qLines.join(' '), onOpenWikiNote, onFilterTag)}
        </blockquote>
      );
      continue;
    }

    // Unordered or ordered list
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items: React.ReactNode[] = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        const cleaned = lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, '');
        items.push(<li key={items.length}>{renderInline(cleaned, onOpenWikiNote, onFilterTag)}</li>);
        i++;
      }
      blocks.push(
        ordered ? (
          <ol key={bKey++} className="md-list">
            {items}
          </ol>
        ) : (
          <ul key={bKey++} className="md-list">
            {items}
          </ul>
        )
      );
      continue;
    }

    // Blank line
    if (!line.trim()) {
      i++;
      continue;
    }

    // Paragraph
    const pLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== '' &&
      !lines[i].trimStart().startsWith('```') &&
      !/^(#{1,4})\s+/.test(lines[i]) &&
      !/^\s*([-*+]|\d+\.)\s+/.test(lines[i])
    ) {
      pLines.push(lines[i]);
      i++;
    }
    blocks.push(
      <p key={bKey++} className="md-p">
        {renderInline(pLines.join(' '), onOpenWikiNote, onFilterTag)}
      </p>
    );
  }

  return <div className="md-body">{blocks}</div>;
};

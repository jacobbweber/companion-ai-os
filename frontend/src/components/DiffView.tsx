import React, { useState } from 'react';
import { Columns2, FileDiff } from 'lucide-react';
import type { WikiDiffPayload, WikiDiffRow } from '../types';

interface DiffViewProps {
  diff: WikiDiffPayload;
  compact?: boolean;
  onClose?: () => void;
}

function renderUnifiedRows(rows: WikiDiffRow[]): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let idx = 0;
  for (const r of rows) {
    if (r.kind === 'hunk') {
      out.push(
        <div key={idx++} className="diff-row diff-hunk mono">
          <span className="diff-ln">···</span>
          <span className="diff-ln">···</span>
          <span className="diff-code">{r.old_text}</span>
        </div>
      );
    } else if (r.kind === 'context') {
      out.push(
        <div key={idx++} className="diff-row diff-ctx mono">
          <span className="diff-ln">{r.old_ln ?? ''}</span>
          <span className="diff-ln">{r.new_ln ?? ''}</span>
          <span className="diff-code">  {r.old_text}</span>
        </div>
      );
    } else if (r.kind === 'del') {
      out.push(
        <div key={idx++} className="diff-row diff-del mono">
          <span className="diff-ln">{r.old_ln ?? ''}</span>
          <span className="diff-ln" />
          <span className="diff-code">- {r.old_text}</span>
        </div>
      );
    } else if (r.kind === 'add') {
      out.push(
        <div key={idx++} className="diff-row diff-add mono">
          <span className="diff-ln" />
          <span className="diff-ln">{r.new_ln ?? ''}</span>
          <span className="diff-code">+ {r.new_text}</span>
        </div>
      );
    } else if (r.kind === 'change') {
      out.push(
        <div key={idx++} className="diff-row diff-del mono">
          <span className="diff-ln">{r.old_ln ?? ''}</span>
          <span className="diff-ln" />
          <span className="diff-code">- {r.old_text}</span>
        </div>,
        <div key={idx++} className="diff-row diff-add mono">
          <span className="diff-ln" />
          <span className="diff-ln">{r.new_ln ?? ''}</span>
          <span className="diff-code">+ {r.new_text}</span>
        </div>
      );
    }
  }
  return out;
}

export const DiffView: React.FC<DiffViewProps> = ({ diff, compact = false, onClose }) => {
  const defaultLayout =
    compact || (typeof window !== 'undefined' && window.innerWidth < 860) ? 'unified' : 'split';
  const [layout, setLayout] = useState<'unified' | 'split'>(defaultLayout);

  return (
    <div className="diff-container" data-testid="wiki-diff-view">
      <div className="diff-toolbar">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span className="badge accent">
            {(diff.mode || 'lineage_diff').replace(/_/g, ' ').toUpperCase()}
          </span>
          <span className="badge ok">+{diff.total_additions}</span>
          <span className="badge warn">-{diff.total_deletions}</span>
          {diff.summary && (
            <span style={{ fontSize: 11.5, color: 'var(--text-secondary)' }}>{diff.summary}</span>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <button
            type="button"
            className={`btn ${layout === 'unified' ? 'btn-primary' : ''}`}
            style={{ padding: '3px 8px', fontSize: 11 }}
            onClick={() => setLayout('unified')}
          >
            <FileDiff size={12} /> Unified
          </button>
          <button
            type="button"
            className={`btn ${layout === 'split' ? 'btn-primary' : ''}`}
            style={{ padding: '3px 8px', fontSize: 11 }}
            onClick={() => setLayout('split')}
          >
            <Columns2 size={12} /> Side-by-Side
          </button>
          {onClose && (
            <button
              type="button"
              className="btn"
              style={{ padding: '3px 8px', fontSize: 11 }}
              onClick={onClose}
            >
              Close Diff
            </button>
          )}
        </div>
      </div>

      {diff.files.map((f, fIdx) => (
        <div key={`${f.title}-${fIdx}`} className="diff-file-card">
          <div className="diff-file-head mono">
            <span>
              {f.old_label} &rarr; <strong>{f.new_label}</strong>
            </span>
            <span>
              +{f.additions} / -{f.deletions}
            </span>
          </div>

          {f.identical ? (
            <div style={{ padding: '12px 14px', fontSize: 12, color: 'var(--text-muted)' }}>
              No line differences between these revisions.
            </div>
          ) : layout === 'unified' ? (
            <div className="diff-table">{renderUnifiedRows(f.rows)}</div>
          ) : (
            <div className="diff-split-grid">
              {f.rows.map((r, rIdx) =>
                r.kind === 'hunk' ? (
                  <div key={rIdx} className="diff-split-hunk mono">
                    {r.old_text}
                  </div>
                ) : (
                  <React.Fragment key={rIdx}>
                    <div
                      className={`diff-split-cell mono ${
                        r.kind === 'del' || r.kind === 'change' ? 'diff-del' : 'diff-ctx'
                      }`}
                    >
                      <span className="diff-ln">{r.old_ln ?? ''}</span>
                      <span className="diff-code">
                        {r.kind === 'del' || r.kind === 'change'
                          ? `- ${r.old_text}`
                          : r.old_ln
                            ? `  ${r.old_text}`
                            : ''}
                      </span>
                    </div>
                    <div
                      className={`diff-split-cell mono ${
                        r.kind === 'add' || r.kind === 'change' ? 'diff-add' : 'diff-ctx'
                      }`}
                    >
                      <span className="diff-ln">{r.new_ln ?? ''}</span>
                      <span className="diff-code">
                        {r.kind === 'add' || r.kind === 'change'
                          ? `+ ${r.new_text}`
                          : r.new_ln
                            ? `  ${r.new_text}`
                            : ''}
                      </span>
                    </div>
                  </React.Fragment>
                )
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
};

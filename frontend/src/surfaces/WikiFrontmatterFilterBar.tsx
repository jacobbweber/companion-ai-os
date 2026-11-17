import React from 'react';
import { Code2, X } from 'lucide-react';
import type { WikiFrontmatterFacets, WikiNoteDetail } from '../types';

interface WikiFrontmatterDrawerProps {
  activeTokens: string[];
  showFilters: boolean;
  facets: WikiFrontmatterFacets | null;
  query: string;
  onApplyToken: (token: string, openListOnMobile?: boolean) => void;
  onInsertKeyPrefix: (keyName: string) => void;
  onClearAll: () => void;
}

export const WikiFrontmatterDrawer: React.FC<WikiFrontmatterDrawerProps> = ({
  activeTokens,
  showFilters,
  facets,
  query,
  onApplyToken,
  onInsertKeyPrefix,
  onClearAll,
}) => {
  if (!showFilters && activeTokens.length === 0) return null;

  return (
    <div className="wiki-fm-drawer" data-testid="wiki-frontmatter-drawer">
      {activeTokens.length > 0 && (
        <div className="wiki-fm-row">
          <span className="wiki-fm-row-label">Active YAML:</span>
          {activeTokens.map((tok) => (
            <button
              key={tok}
              type="button"
              className="wiki-fm-chip active"
              onClick={() => onApplyToken(tok)}
            >
              <span className="mono">{tok}</span>
              <X size={10} />
            </button>
          ))}
          <button type="button" className="wiki-clear-link" onClick={onClearAll}>
            Clear all
          </button>
        </div>
      )}

      {showFilters && facets && (
        <div className="wiki-fm-facets-grid">
          {facets.domains.length > 0 && (
            <div className="wiki-fm-row">
              <span className="wiki-fm-row-label">domain:</span>
              {facets.domains.map((d) => {
                const tok = `domain:${d.value}`;
                return (
                  <button
                    key={tok}
                    type="button"
                    className={`wiki-fm-chip ${query.includes(tok) ? 'active' : ''}`}
                    onClick={() => onApplyToken(tok, true)}
                  >
                    {d.value} <span className="wiki-fm-cnt">{d.count}</span>
                  </button>
                );
              })}
            </div>
          )}

          {facets.templates.length > 0 && (
            <div className="wiki-fm-row">
              <span className="wiki-fm-row-label">template:</span>
              {facets.templates.map((t) => {
                const tok = `template:${t.value}`;
                return (
                  <button
                    key={tok}
                    type="button"
                    className={`wiki-fm-chip ${query.includes(tok) ? 'active' : ''}`}
                    onClick={() => onApplyToken(tok, true)}
                  >
                    {t.value} <span className="wiki-fm-cnt">{t.count}</span>
                  </button>
                );
              })}
            </div>
          )}

          {facets.tags.length > 0 && (
            <div className="wiki-fm-row">
              <span className="wiki-fm-row-label">tag:</span>
              {facets.tags.slice(0, 14).map((tg) => {
                const tok = `tag:${tg.value}`;
                return (
                  <button
                    key={tok}
                    type="button"
                    className={`wiki-fm-chip ${query.includes(tok) ? 'active' : ''}`}
                    onClick={() => onApplyToken(tok, true)}
                  >
                    #{tg.value} <span className="wiki-fm-cnt">{tg.count}</span>
                  </button>
                );
              })}
            </div>
          )}

          {facets.ontology.length > 0 && (
            <div className="wiki-fm-row">
              <span className="wiki-fm-row-label">ontology:</span>
              {facets.ontology.slice(0, 10).map((ont) => {
                const tok = `ontology:${ont.value}`;
                return (
                  <button
                    key={tok}
                    type="button"
                    className={`wiki-fm-chip ${query.includes(tok) ? 'active' : ''}`}
                    onClick={() => onApplyToken(tok, true)}
                  >
                    ◆ {ont.value} <span className="wiki-fm-cnt">{ont.count}</span>
                  </button>
                );
              })}
            </div>
          )}

          {facets.keys.length > 0 && (
            <div className="wiki-fm-row">
              <span className="wiki-fm-row-label">YAML keys:</span>
              {facets.keys.map((k) => (
                <button
                  key={k}
                  type="button"
                  className="wiki-fm-chip subtle mono"
                  title={`Insert ${k}: filter`}
                  onClick={() => onInsertKeyPrefix(k)}
                >
                  {k}:
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

interface WikiNoteFrontmatterBarProps {
  note: WikiNoteDetail;
  expanded: boolean;
  onToggleExpanded: () => void;
  onApplyToken: (token: string, openListOnMobile?: boolean) => void;
}

export const WikiNoteFrontmatterBar: React.FC<WikiNoteFrontmatterBarProps> = ({
  note,
  expanded,
  onToggleExpanded,
  onApplyToken,
}) => {
  const fmEntries = Object.entries(note.frontmatter || {}).filter(
    ([, v]) => v !== null && v !== undefined && v !== '' && !(Array.isArray(v) && v.length === 0)
  );
  if (fmEntries.length === 0) return null;

  return (
    <div className="wiki-fm-props-bar" data-testid="wiki-note-frontmatter-bar">
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, alignItems: 'center' }}>
        {note.domain && (
          <button
            type="button"
            className="wiki-fm-chip"
            onClick={() => onApplyToken(`domain:${note.domain}`, true)}
          >
            domain:{note.domain}
          </button>
        )}
        {note.template && (
          <button
            type="button"
            className="wiki-fm-chip"
            onClick={() => onApplyToken(`template:${note.template}`, true)}
          >
            tpl:{note.template}
          </button>
        )}
        {(note.tags || []).map((tg) => (
          <button
            key={tg}
            type="button"
            className="wiki-fm-chip"
            onClick={() => onApplyToken(`tag:${tg}`, true)}
          >
            #{tg}
          </button>
        ))}
        {(note.ontology || []).map((ont) => (
          <button
            key={ont}
            type="button"
            className="wiki-fm-chip"
            onClick={() => onApplyToken(`ontology:${ont}`, true)}
          >
            ◆ {ont}
          </button>
        ))}
        <button type="button" className="wiki-fm-chip subtle" onClick={onToggleExpanded}>
          <Code2 size={11} />
          <span>YAML ({fmEntries.length})</span>
        </button>
      </div>

      {expanded && (
        <div className="wiki-fm-table">
          {fmEntries.map(([k, v]) => {
            const formatted = Array.isArray(v) ? v.join(', ') : String(v);
            const filterTok =
              !Array.isArray(v) && formatted.length <= 40 && !formatted.includes(' ')
                ? `${k}:${formatted}`
                : '';
            return (
              <div key={k} className="wiki-fm-kv-row">
                <span className="wiki-fm-k mono">{k}</span>
                <span className="wiki-fm-v">
                  {filterTok ? (
                    <button
                      type="button"
                      className="wiki-fm-val-link mono"
                      onClick={() => onApplyToken(filterTok, true)}
                    >
                      {formatted}
                    </button>
                  ) : (
                    <span className="mono">{formatted}</span>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

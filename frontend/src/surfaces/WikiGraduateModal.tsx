import React, { useEffect, useState } from 'react';
import { CheckCircle2, FileDiff, FolderTree, Sparkles, X } from 'lucide-react';
import { callAction } from '../api';
import { DiffView } from '../components/DiffView';
import type { WikiDiffPayload } from '../types';

interface AvailableTemplate {
  slug: string;
  document_type: string;
  required_sections: string[];
  when_to_use: string;
}

interface ProposedGraduation {
  path: string;
  title: string;
  summary: string;
  template: string;
  domain: string;
  topic: string;
  tags: string[];
  ontology: string[];
  related: string[];
  content: string;
  strategy?: string;
  merge_target?: string;
  rendered_markdown?: string;
  rationale: string;
}

interface GraduationProposalResponse {
  ok: boolean;
  curation_id?: string;
  inbox_path?: string;
  used_tier?: string;
  validation_ok?: boolean;
  validation_errors?: string[];
  proposed?: ProposedGraduation;
  available_templates?: AvailableTemplate[];
  existing_folders?: string[];
  sibling_inbox_count?: number;
  diff?: WikiDiffPayload;
  error?: string;
}

interface WikiGraduateModalProps {
  inboxPath: string;
  onCommitted: (graduatedPath: string, summaryMsg: string) => Promise<void>;
  onCancel: () => void;
}

const slugifyPart = (raw: string): string =>
  (raw || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 72);

export const WikiGraduateModal: React.FC<WikiGraduateModalProps> = ({
  inboxPath,
  onCommitted,
  onCancel,
}) => {
  const [loading, setLoading] = useState(true);
  const [committing, setCommitting] = useState(false);
  const [error, setError] = useState('');
  const [curationId, setCurationId] = useState('');
  const [usedTier, setUsedTier] = useState('reasoning');
  const [rationale, setRationale] = useState('');
  const [templates, setTemplates] = useState<AvailableTemplate[]>([]);
  const [existingFolders, setExistingFolders] = useState<string[]>([]);
  const [diff, setDiff] = useState<WikiDiffPayload | null>(null);
  const [showDiff, setShowDiff] = useState(false);

  const [domain, setDomain] = useState('');
  const [topic, setTopic] = useState('');
  const [slug, setSlug] = useState('');
  const [title, setTitle] = useState('');
  const [summary, setSummary] = useState('');
  const [template, setTemplate] = useState('standard-note');
  const [tagsText, setTagsText] = useState('');
  const [ontologyText, setOntologyText] = useState('');
  const [relatedText, setRelatedText] = useState('');
  const [bodyContent, setBodyContent] = useState('');
  const [strategy, setStrategy] = useState('CREATE_NEW');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    setShowDiff(false);
    void callAction<GraduationProposalResponse>('wiki', {
      subaction: 'propose_graduation',
      path: inboxPath,
    }).then((res) => {
      if (cancelled) return;
      setLoading(false);
      const data = res as unknown as GraduationProposalResponse;
      if (!data.ok || !data.proposed || !data.curation_id) {
        setError(data.error || 'Could not generate AI graduation proposal.');
        return;
      }
      const p = data.proposed;
      const pathParts = (p.path || '').split('/');
      const inferredSlug = (pathParts[pathParts.length - 1] || '').replace(/\.md$/i, '');
      setCurationId(data.curation_id);
      setUsedTier(data.used_tier || 'reasoning');
      setRationale(p.rationale || '');
      setTemplates(data.available_templates || []);
      setExistingFolders(data.existing_folders || []);
      setDiff(data.diff || null);
      setDomain(p.domain || 'general');
      setTopic(p.topic || '');
      setSlug(inferredSlug || slugifyPart(p.title) || 'note');
      setTitle(p.title || '');
      setSummary(p.summary || '');
      setTemplate(p.template || 'standard-note');
      setTagsText((p.tags || []).join(', '));
      setOntologyText((p.ontology || []).join(', '));
      setRelatedText((p.related || []).join(', '));
      setBodyContent(p.content || '');
      setStrategy(p.strategy || 'CREATE_NEW');
    });
    return () => {
      cancelled = true;
    };
  }, [inboxPath]);

  const cleanDomain = slugifyPart(domain) || 'general';
  const cleanTopic = slugifyPart(topic);
  const cleanSlug = slugifyPart(slug) || slugifyPart(title) || 'note';
  const targetPath = cleanTopic
    ? `02_knowledge/${cleanDomain}/${cleanTopic}/${cleanSlug}.md`
    : `02_knowledge/${cleanDomain}/${cleanSlug}.md`;

  const confirmGraduation = async () => {
    if (!curationId || committing) return;
    if (['inbox', '01-inbox', '01_inbox'].includes(cleanDomain)) {
      setError('Domain folder cannot be "inbox" — choose a meaningful domain folder such as "cooking" or "homelab".');
      return;
    }
    setCommitting(true);
    setError('');
    const parseList = (raw: string) =>
      raw
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);

    const res = await callAction<{ ok: boolean; output_paths?: string[]; summary?: string; error?: string }>('wiki', {
      subaction: 'commit_curation',
      curation_id: curationId,
      confirmed: true,
      plan_override: {
        strategy,
        rationale: rationale || `Curated into ${targetPath}`,
        outputs: [
          {
            path: targetPath,
            title: title.trim() || cleanSlug,
            summary: summary.trim(),
            template,
            domain: cleanDomain,
            topic: cleanTopic,
            tags: parseList(tagsText),
            ontology: parseList(ontologyText),
            related: parseList(relatedText),
            content: bodyContent,
          },
        ],
      },
    });
    setCommitting(false);
    if (!res.ok) {
      setError(res.error || 'Graduation validation failed.');
      return;
    }
    const finalPath = (res.output_paths && res.output_paths[0]) || targetPath;
    await onCommitted(finalPath, res.summary || `Graduated into ${finalPath}`);
  };

  return (
    <div
      className="card"
      data-testid="wiki-graduate-modal"
      style={{
        marginBottom: 16,
        padding: 16,
        border: '1px solid var(--accent-border, var(--border-strong))',
        background: 'var(--bg-elevated)',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, marginBottom: 10 }}>
        <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, fontWeight: 600, fontSize: 14 }}>
          <Sparkles size={15} style={{ color: 'var(--accent)' }} />
          <span>AI Knowledge Librarian — Curate &amp; Graduate to 02_knowledge/</span>
        </div>
        <button type="button" className="icon-btn" onClick={onCancel} aria-label="Close graduation review">
          <X size={15} />
        </button>
      </div>

      {loading ? (
        <div
          data-testid="wiki-graduate-loading"
          style={{ padding: '18px 8px', fontSize: 13, color: 'var(--text-secondary)', display: 'grid', gap: 6 }}
        >
          <div style={{ fontWeight: 500, color: 'var(--text-primary)' }}>
            Analyzing note content, existing 02_knowledge/ folders, sibling inbox items &amp; YAML schema...
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
            Selecting domain hierarchy, template structure, tags, ontology concepts, and cross-links.
          </div>
        </div>
      ) : (
        <div style={{ display: 'grid', gap: 12 }}>
          {rationale && (
            <div
              style={{
                padding: '9px 12px',
                borderRadius: 'var(--radius-sm)',
                background: 'var(--bg-subtle)',
                fontSize: 12.5,
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                gap: 10,
                flexWrap: 'wrap',
              }}
            >
              <span>{rationale}</span>
              <span style={{ display: 'inline-flex', gap: 6 }}>
                <span className="badge ok">
                  <CheckCircle2 size={11} /> Validated ({strategy})
                </span>
                <span className="badge mono">tier:{usedTier}</span>
              </span>
            </div>
          )}

          {error && (
            <div
              data-testid="wiki-graduate-error"
              style={{
                padding: '8px 10px',
                borderRadius: 'var(--radius-sm)',
                background: 'rgba(239, 68, 68, 0.12)',
                color: 'var(--status-danger)',
                fontSize: 12.5,
                whiteSpace: 'pre-wrap',
              }}
            >
              {error}
            </div>
          )}

          <div
            style={{
              padding: '7px 10px',
              borderRadius: 'var(--radius-sm)',
              border: '1px solid var(--border-hairline)',
              fontSize: 12,
              display: 'flex',
              alignItems: 'center',
              gap: 6,
            }}
          >
            <FolderTree size={13} style={{ color: 'var(--accent)' }} />
            <span style={{ color: 'var(--text-muted)' }}>Target Path:</span>
            <span className="mono" data-testid="wiki-graduate-target-path" style={{ fontWeight: 600 }}>
              {targetPath}
            </span>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 10 }}>
            <div>
              <label className="field-label">Domain Folder</label>
              <input
                className="input mono"
                list="wiki-existing-domains-list"
                data-testid="wiki-graduate-domain-input"
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
                placeholder="e.g. cooking"
              />
              <datalist id="wiki-existing-domains-list">
                {existingFolders.map((f) => (
                  <option key={f} value={f.split('/')[0]} />
                ))}
              </datalist>
            </div>
            <div>
              <label className="field-label">Subfolder / Topic (Optional)</label>
              <input
                className="input mono"
                data-testid="wiki-graduate-topic-input"
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
                placeholder="e.g. one-pot, sheet-pan, stir-fry"
              />
            </div>
            <div>
              <label className="field-label">Filename Slug</label>
              <input
                className="input mono"
                data-testid="wiki-graduate-slug-input"
                value={slug}
                onChange={(e) => setSlug(e.target.value)}
                placeholder="e.g. chicken-stir-fry"
              />
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 10 }}>
            <div>
              <label className="field-label">Curated Title</label>
              <input
                className="input"
                data-testid="wiki-graduate-title-input"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
              />
            </div>
            <div>
              <label className="field-label">Governed Template</label>
              <select
                className="select mono"
                data-testid="wiki-graduate-template-select"
                value={template}
                onChange={(e) => setTemplate(e.target.value)}
              >
                {templates.map((t) => (
                  <option key={t.slug} value={t.slug}>
                    {t.slug} ({t.document_type})
                  </option>
                ))}
                {!templates.some((t) => t.slug === template) && (
                  <option value={template}>{template}</option>
                )}
              </select>
            </div>
          </div>

          <div>
            <label className="field-label">Executive Summary (YAML Frontmatter)</label>
            <textarea
              className="textarea"
              rows={2}
              data-testid="wiki-graduate-summary-input"
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
            />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 10 }}>
            <div>
              <label className="field-label">Tags (comma-separated)</label>
              <input
                className="input mono"
                data-testid="wiki-graduate-tags-input"
                value={tagsText}
                onChange={(e) => setTagsText(e.target.value)}
              />
            </div>
            <div>
              <label className="field-label">Ontology Concepts</label>
              <input
                className="input mono"
                data-testid="wiki-graduate-ontology-input"
                value={ontologyText}
                onChange={(e) => setOntologyText(e.target.value)}
              />
            </div>
            <div>
              <label className="field-label">Related [[Wikilinks]]</label>
              <input
                className="input mono"
                data-testid="wiki-graduate-related-input"
                value={relatedText}
                onChange={(e) => setRelatedText(e.target.value)}
                placeholder="[[related-note]]"
              />
            </div>
          </div>

          <div>
            <label className="field-label">Curated Markdown Body (Template Sections)</label>
            <textarea
              className="textarea mono"
              rows={8}
              data-testid="wiki-graduate-body-input"
              value={bodyContent}
              onChange={(e) => setBodyContent(e.target.value)}
            />
          </div>

          {showDiff && diff && <DiffView diff={diff} onClose={() => setShowDiff(false)} />}

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginTop: 2 }}>
            <div>
              {diff && (
                <button type="button" className="btn btn-sm" onClick={() => setShowDiff((v) => !v)}>
                  <FileDiff size={12} /> {showDiff ? 'Hide YAML Diff' : 'Preview YAML + Diff'}
                </button>
              )}
            </div>
            <div style={{ display: 'inline-flex', gap: 8 }}>
              <button type="button" className="btn" onClick={onCancel} disabled={committing}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                data-testid="wiki-confirm-graduate-btn"
                disabled={committing || !curationId}
                onClick={() => void confirmGraduation()}
              >
                <Sparkles size={13} /> {committing ? 'Graduating...' : 'Confirm & Graduate'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

import React, { useEffect, useRef, useState } from 'react';
import {
  ArrowUpRight,
  BookOpen,
  CheckCircle2,
  FileCheck2,
  FileDiff,
  FolderGit2,
  GitBranch,
  History,
  Image as ImageIcon,
  Layers,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { callAction } from '../api';
import { DiffView } from '../components/DiffView';
import { MarkdownView } from '../components/MarkdownView';
import { MindMapCanvas } from '../components/MindMapCanvas';
import type {
  CurationHistoryEntry,
  CurationInspection,
  MindMapGraph,
  WikiDiffPayload,
  WikiNoteDetail,
  WikiNoteSummary,
  WikiTemplateSpec,
} from '../types';

interface WikiStudioProps {
  initialNotePath?: string;
  onToast: (msg: string) => void;
}

const FOLDERS = [
  { id: '', label: 'All Active' },
  { id: '01_inbox', label: '01_inbox' },
  { id: '02_knowledge', label: '02_knowledge' },
  { id: '03_resources', label: '03_resources' },
  { id: '04_archive', label: '04_archive' },
  { id: '__stale__', label: 'Stale' },
  { id: '__templates__', label: 'Templates' },
  { id: '__lineage__', label: 'Lineage' },
  { id: '__trash__', label: 'Trash' },
];

export const WikiStudio: React.FC<WikiStudioProps> = ({ initialNotePath, onToast }) => {
  const [folder, setFolder] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [notes, setNotes] = useState<WikiNoteSummary[]>([]);
  const [templates, setTemplates] = useState<WikiTemplateSpec[]>([]);
  const [selectedTemplate, setSelectedTemplate] = useState<WikiTemplateSpec | null>(null);
  const [authorityInfo, setAuthorityInfo] = useState<{
    domains: string[];
    ontology: string[];
    tags: string[];
    aliases: Record<string, string>;
  } | null>(null);
  const [aliasRuleInput, setAliasRuleInput] = useState('');
  const [historyItems, setHistoryItems] = useState<CurationHistoryEntry[]>([]);
  const [trashItems, setTrashItems] = useState<
    Array<{ trash_id: string; original_path: string; trashed_at: string }>
  >([]);

  const [selectedNote, setSelectedNote] = useState<WikiNoteDetail | null>(null);
  const [editTitle, setEditTitle] = useState('');
  const [editSummary, setEditSummary] = useState('');
  const [editTemplate, setEditTemplate] = useState('standard-note');
  const [editTags, setEditTags] = useState('');
  const [editOntology, setEditOntology] = useState('');
  const [editBody, setEditBody] = useState('');

  const [viewMode, setViewMode] = useState<'split' | 'edit' | 'preview' | 'diff' | 'graph'>('split');
  const [activeDiff, setActiveDiff] = useState<WikiDiffPayload | null>(null);
  const [uploadingImage, setUploadingImage] = useState(false);
  const editorRef = useRef<HTMLTextAreaElement | null>(null);
  const wikiImgInputRef = useRef<HTMLInputElement | null>(null);
  const [mobileShowList, setMobileShowList] = useState(true);
  const [graph, setGraph] = useState<MindMapGraph | null>(null);

  // Quick capture & Curation audit state
  const [showCapture, setShowCapture] = useState(false);
  const [capTitle, setCapTitle] = useState('');
  const [capTemplate, setCapTemplate] = useState('inbox-raw');
  const [capSummary, setCapSummary] = useState('');
  const [capTags, setCapTags] = useState('notes');
  const [capOntology, setCapOntology] = useState('');
  const [capBody, setCapBody] = useState('');

  const [gradDomain, setGradDomain] = useState('homelab');
  const [gradTopic, setGradTopic] = useState('');
  const [curationAudit, setCurationAudit] = useState<CurationInspection | null>(null);
  const [inspecting, setInspecting] = useState(false);

  const loadTemplatesAndAuthority = async () => {
    const [tplRes, authRes] = await Promise.all([
      callAction<{ templates?: WikiTemplateSpec[] }>('wiki', { subaction: 'list_templates' }),
      callAction<{
        domains?: string[];
        ontology?: string[];
        tags?: string[];
        aliases?: Record<string, string>;
      }>('wiki', {
        subaction: 'tag_authority',
      }),
    ]);
    if (tplRes.templates) setTemplates(tplRes.templates);
    if (authRes.ok) {
      setAuthorityInfo({
        domains: authRes.domains || [],
        ontology: authRes.ontology || [],
        tags: authRes.tags || [],
        aliases: authRes.aliases || {},
      });
    }
  };

  const refreshNotes = async (targetFolder = folder, q = searchQuery) => {
    if (targetFolder === '__trash__') {
      const tRes = await callAction<{
        trash?: Array<{ trash_id: string; original_path: string; trashed_at: string }>;
      }>('wiki', { subaction: 'list_trash' });
      setTrashItems(tRes.trash || []);
      return;
    }
    if (targetFolder === '__templates__') {
      await loadTemplatesAndAuthority();
      return;
    }
    if (targetFolder === '__lineage__') {
      const hRes = await callAction<{ history?: CurationHistoryEntry[] }>('wiki', {
        subaction: 'curation_history',
      });
      setHistoryItems(hRes.history || []);
      return;
    }
    if (targetFolder === '__stale__') {
      const stRes = await callAction<{ stale_notes?: WikiNoteSummary[] }>('wiki', {
        subaction: 'list_stale',
      });
      setNotes(stRes.stale_notes || []);
      return;
    }
    if (q.trim()) {
      const sRes = await callAction<{ hits?: WikiNoteSummary[] }>('wiki', {
        subaction: 'search',
        query: q.trim(),
        include_archive: targetFolder === '04_archive',
        include_ontology_bridges: true,
      });
      setNotes(sRes.hits || []);
    } else {
      const lRes = await callAction<{ notes?: WikiNoteSummary[] }>('wiki', {
        subaction: 'list',
        folder: targetFolder,
        limit: 140,
      });
      setNotes(lRes.notes || []);
    }
  };

  const refreshGraph = async () => {
    const gRes = await callAction<MindMapGraph>('wiki', { subaction: 'graph' });
    if (gRes.ok) setGraph(gRes);
  };

  const openNote = async (pathOrStem: string) => {
    const res = await callAction<WikiNoteDetail>('wiki', {
      subaction: 'read',
      path: pathOrStem,
    });
    if (res.ok) {
      setSelectedTemplate(null);
      setSelectedNote(res);
      setEditTitle(res.title || '');
      const fmSummary =
        typeof res.note_summary === 'string'
          ? res.note_summary
          : typeof res.frontmatter?.summary === 'string'
            ? res.frontmatter.summary
            : '';
      setEditSummary(fmSummary);
      setEditTemplate(res.template || 'standard-note');
      setEditTags((res.tags || []).join(', '));
      setEditOntology((res.ontology || []).join(', '));
      setEditBody(res.body || '');
      if (res.domain && res.domain !== 'inbox') setGradDomain(res.domain);
      if (res.topic) setGradTopic(res.topic);
      setCurationAudit(null);
      setMobileShowList(false);
    } else {
      onToast(res.error || `Note "${pathOrStem}" not found yet.`);
    }
  };

  useEffect(() => {
    void loadTemplatesAndAuthority();
  }, []);

  useEffect(() => {
    void refreshNotes();
    void refreshGraph();
  }, [folder]);

  useEffect(() => {
    if (initialNotePath) {
      void openNote(initialNotePath);
    }
  }, [initialNotePath]);

  const applyTemplateMissingSections = (slug: string) => {
    const tpl = templates.find((t) => t.slug === slug);
    if (!tpl) return;
    setEditTemplate(slug);
    let nextBody = editBody;
    for (const sec of tpl.required_sections) {
      const headingRegex = new RegExp(`^##\\s+${sec.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'im');
      if (!headingRegex.test(nextBody)) {
        nextBody = `${nextBody.trimEnd()}\n\n## ${sec}\n\n`;
      }
    }
    setEditBody(nextBody.trimStart());
    onToast(`Ensured all required sections for "${slug}".`);
  };

  const handleSave = async () => {
    if (!selectedNote) return;
    const res = await callAction<{ content_hash?: string }>('wiki', {
      subaction: 'save',
      path: selectedNote.path,
      title: editTitle,
      summary: editSummary,
      template: editTemplate,
      content: editBody,
      tags: editTags,
      ontology: editOntology,
      expected_hash: selectedNote.content_hash,
    });
    if (res.ok) {
      onToast(`Saved ${selectedNote.path} (author: user).`);
      await openNote(selectedNote.path);
      void refreshNotes();
      void refreshGraph();
    } else {
      onToast(res.error || 'Save rejected — check required template sections or reload.');
    }
  };

  const handleCapture = async () => {
    if (!capTitle.trim() || !capBody.trim()) return;
    const res = await callAction<{ path?: string }>('wiki', {
      subaction: 'capture_inbox',
      title: capTitle.trim(),
      template: capTemplate,
      summary: capSummary.trim(),
      content: capBody,
      tags: capTags,
      ontology: capOntology,
    });
    if (res.ok && res.path) {
      setShowCapture(false);
      setCapTitle('');
      setCapSummary('');
      setCapBody('');
      onToast(`Staged ${res.path} into 01_inbox/`);
      await refreshNotes();
      await refreshGraph();
      void openNote(res.path);
    } else {
      onToast(res.error || 'Could not capture note.');
    }
  };

  const handleInspectGraduation = async () => {
    if (!selectedNote) return;
    setInspecting(true);
    const res = await callAction<CurationInspection>('wiki', {
      subaction: 'inspect_graduation',
      path: selectedNote.path,
    });
    setInspecting(false);
    if (res.ok) {
      setCurationAudit(res);
      onToast(`Overlap & template audit ready (${res.overlap_candidates.length} candidate(s)).`);
    } else {
      onToast(res.error || 'Could not run curation audit.');
    }
  };

  const handleDeepCurateJob = async (targetPath?: string) => {
    const res = await callAction<{ job_id?: string }>('wiki', {
      subaction: 'deep_curate_job',
      path: targetPath || selectedNote?.path || '',
    });
    if (res.ok) {
      onToast(
        `Dispatched multi-turn Reasoning Curation Job (${res.job_id || 'active'}) — originals protected in 04_archive/.`,
      );
    } else {
      onToast(res.error || 'Could not dispatch deep curation job.');
    }
  };

  const handleGraduate = async () => {
    if (!selectedNote || !gradDomain.trim()) return;
    await handleSave();
    const res = await callAction<{ path?: string }>('wiki', {
      subaction: 'graduate',
      path: selectedNote.path,
      domain: gradDomain.trim(),
      topic: gradTopic.trim(),
      template: editTemplate,
      summary: editSummary,
      ontology: editOntology,
    });
    if (res.ok && res.path) {
      onToast(`Graduated -> ${res.path}`);
      await refreshNotes();
      await refreshGraph();
      void openNote(res.path);
    } else {
      onToast(res.error || 'Graduation blocked by template/YAML/anti-orphan guard.');
    }
  };

  const handleRevertCuration = async (txOrPath: string) => {
    const res = await callAction<{ restored_inbox?: string[] }>('wiki', {
      subaction: 'revert_curation',
      tx_id: txOrPath,
    });
    if (res.ok) {
      onToast(res.summary || 'Reverted curation and re-staged note into 01_inbox/.');
      await refreshNotes();
      await refreshGraph();
      if (res.restored_inbox?.[0]) {
        void openNote(res.restored_inbox[0]);
      }
    } else {
      onToast(res.error || 'No active curation transaction found for rollback.');
    }
  };

  const handleTrash = async () => {
    if (!selectedNote) return;
    const res = await callAction('wiki', { subaction: 'trash', path: selectedNote.path });
    if (res.ok) {
      onToast(res.summary || 'Moved note to undoable Trash.');
      setSelectedNote(null);
      void refreshNotes();
      void refreshGraph();
    } else {
      onToast(res.error || 'Trash failed.');
    }
  };

  const handleVerifyNote = async () => {
    if (!selectedNote) return;
    const res = await callAction('wiki', {
      subaction: 'verify_note',
      path: selectedNote.path,
    });
    if (res.ok) {
      onToast(res.summary || `Marked ${selectedNote.path} verified.`);
      await openNote(selectedNote.path);
      void refreshNotes();
    } else {
      onToast(res.error || 'Could not verify note freshness.');
    }
  };

  const openLineageOrDraftDiff = async (pathOverride?: string, compareTo?: string) => {
    const targetPath = pathOverride || selectedNote?.path || '';
    if (!targetPath) return;
    const useDraft =
      !pathOverride &&
      !compareTo &&
      selectedNote &&
      editBody !== selectedNote.body &&
      !selectedNote.supersedes?.length;
    const res = await callAction<WikiDiffPayload>('wiki', {
      subaction: 'diff',
      path: targetPath,
      compare_to: compareTo || '',
      proposed_content: useDraft ? editBody : '',
    });
    if (res.ok && Array.isArray(res.files)) {
      if (pathOverride && pathOverride !== selectedNote?.path) {
        await openNote(pathOverride);
      }
      setActiveDiff(res);
      setMobileShowList(false);
      setViewMode('diff');
    } else {
      onToast(res.error || 'No archived revision or draft diff available for this note yet.');
    }
  };

  const handleConsolidateOntology = async () => {
    const res = await callAction<{
      updated_notes?: Array<{ path: string }>;
      near_duplicate_suggestions?: Array<{ alias: string; suggested_canonical: string }>;
    }>('wiki', {
      subaction: 'consolidate_ontology',
      aliases: aliasRuleInput.trim(),
    });
    if (res.ok) {
      setAliasRuleInput('');
      onToast(res.summary || 'Consolidated ontology synonyms across vault.');
      await loadTemplatesAndAuthority();
      void refreshNotes();
      void refreshGraph();
    } else {
      onToast(res.error || 'Ontology consolidation failed.');
    }
  };

  const uploadWikiDiagramFile = async (file: File) => {
    if (!file || !file.type.startsWith('image/')) {
      onToast('Select a PNG, JPEG, WEBP, or GIF image/diagram.');
      return;
    }
    setUploadingImage(true);
    try {
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = () => reject(reader.error || new Error('Read failed'));
        reader.readAsDataURL(file);
      });
      const res = await callAction<{ markdown?: string; asset_rel_path?: string }>('wiki', {
        subaction: 'upload_asset',
        filename: file.name,
        data_base64: dataUrl,
        alt_text: file.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' '),
      });
      if (res.ok && res.markdown) {
        const el = editorRef.current;
        const snippet = `\n${res.markdown}\n`;
        if (el && typeof el.selectionStart === 'number') {
          const start = el.selectionStart;
          const end = el.selectionEnd ?? start;
          setEditBody((prev) => prev.slice(0, start) + snippet + prev.slice(end));
        } else {
          setEditBody((prev) => `${prev.trimEnd()}\n\n${res.markdown}\n`);
        }
        onToast(`Inserted ${res.asset_rel_path} into note.`);
      } else {
        onToast(res.error || 'Image upload failed.');
      }
    } catch (err) {
      onToast(`Image upload error: ${String(err)}`);
    } finally {
      setUploadingImage(false);
    }
  };

  const isSpecialList =
    folder === '__trash__' || folder === '__templates__' || folder === '__lineage__';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <div className="studio-header">
        <div className="studio-title-group">
          <h1>Co-Owned Knowledge Wiki 3.0 &amp; Ontology Mind Map</h1>
          <p>
            Inbox-First Warehouse • 11 Governed Templates • Multi-Turn Absorption •{' '}
            <code>ontology</code> Cross-Domain Bridges • Never-Delete <code>04_archive/</code>
          </p>
        </div>
        <div className="studio-actions">
          <button
            type="button"
            className={`btn mobile-only ${mobileShowList && viewMode !== 'graph' ? 'btn-primary' : ''}`}
            onClick={() => {
              if (viewMode === 'graph') setViewMode('preview');
              setMobileShowList((v) => !v);
            }}
          >
            <FolderGit2 size={13} /> Notes ({notes.length})
          </button>
          <button
            type="button"
            className={`btn desktop-only ${viewMode === 'split' ? 'btn-primary' : ''}`}
            onClick={() => setViewMode('split')}
          >
            <BookOpen size={13} /> Editor &amp; Reader
          </button>
          <button
            type="button"
            className={`btn mobile-only ${!mobileShowList && (viewMode === 'preview' || viewMode === 'split') ? 'btn-primary' : ''}`}
            onClick={() => {
              setMobileShowList(false);
              setViewMode('preview');
            }}
          >
            <BookOpen size={13} /> Reader
          </button>
          <button
            type="button"
            className={`btn mobile-only ${!mobileShowList && viewMode === 'edit' ? 'btn-primary' : ''}`}
            onClick={() => {
              setMobileShowList(false);
              setViewMode('edit');
            }}
          >
            <BookOpen size={13} /> Editor
          </button>
          <button
            type="button"
            className={`btn ${viewMode === 'graph' ? 'btn-primary' : ''}`}
            onClick={() => {
              setMobileShowList(false);
              setViewMode('graph');
              void refreshGraph();
            }}
          >
            <GitBranch size={13} /> 2D Mind Map ({graph?.stats.note_count ?? 0})
          </button>
          <button type="button" className="btn" onClick={() => setShowCapture((v) => !v)}>
            <Plus size={13} /> New Inbox Note
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => void handleDeepCurateJob('')}
            title="Dispatch Companion's reasoning curator on staged 01_inbox/ notes"
          >
            <Sparkles size={13} /> Deep Curate Inbox
          </button>
          <button
            type="button"
            className="btn"
            onClick={async () => {
              const r = await callAction('wiki', { subaction: 'reindex' });
              onToast(r.summary || 'Rebuilt Wiki SQLite sidecar index.');
              void refreshNotes();
              void refreshGraph();
            }}
          >
            <RefreshCw size={13} /> Reindex
          </button>
        </div>
      </div>

      {showCapture && (
        <div className="panel chat-drawer-panel">
          <div className="panel-head">
            <span className="panel-title">Stage New Note into 01_inbox/ (Option A Inbox-First)</span>
            <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
              <select
                aria-label="Capture template"
                className="select"
                style={{ padding: '4px 8px', fontSize: 11.5 }}
                value={capTemplate}
                onChange={(e) => {
                  const slug = e.target.value;
                  setCapTemplate(slug);
                  const found = templates.find((t) => t.slug === slug);
                  if (found && !capBody.trim()) {
                    setCapBody(found.skeleton);
                  }
                }}
              >
                <option value="inbox-raw">inbox-raw (Fast Unstructured Scratch)</option>
                {templates.map((t) => (
                  <option key={t.slug} value={t.slug}>
                    {t.slug} [{t.document_type}]
                  </option>
                ))}
              </select>
              {capTemplate !== 'inbox-raw' && (
                <button
                  type="button"
                  className="btn"
                  style={{ padding: '4px 8px', fontSize: 11 }}
                  onClick={() => {
                    const found = templates.find((t) => t.slug === capTemplate);
                    if (found) setCapBody(found.skeleton);
                  }}
                >
                  Load Skeleton
                </button>
              )}
            </div>
          </div>
          <div className="grid-2" style={{ marginBottom: 8 }}>
            <input
              className="input"
              placeholder="Note Title (e.g. Honda HRX217 Seasonal Maintenance)"
              value={capTitle}
              onChange={(e) => setCapTitle(e.target.value)}
            />
            <input
              className="input"
              placeholder="1-sentence Executive Summary (optional in inbox, required on graduation)"
              value={capSummary}
              onChange={(e) => setCapSummary(e.target.value)}
            />
          </div>
          <div className="grid-2" style={{ marginBottom: 8 }}>
            <input
              className="input"
              placeholder="Tags comma-separated (e.g. lawnmower, honda, maintenance)"
              value={capTags}
              onChange={(e) => setCapTags(e.target.value)}
            />
            <input
              className="input"
              placeholder="Ontology bridges (e.g. preventive-maintenance, equipment-lifecycle)"
              value={capOntology}
              onChange={(e) => setCapOntology(e.target.value)}
            />
          </div>
          <textarea
            className="textarea"
            style={{ width: '100%', marginBottom: 8, minHeight: 120 }}
            placeholder="Write raw rough thoughts or governed Markdown with [[wikilinks]]..."
            value={capBody}
            onChange={(e) => setCapBody(e.target.value)}
          />
          <button type="button" className="btn btn-primary" onClick={() => void handleCapture()}>
            Capture to 01_inbox/
          </button>
        </div>
      )}

      <div className="wiki-shell" style={{ flex: 1 }}>
        {/* Left Warehouse Explorer */}
        <div
          className={`wiki-sidebar ${
            mobileShowList && viewMode !== 'graph' ? 'mobile-open' : 'mobile-closed'
          }`}
        >
          <div style={{ padding: 8, borderBottom: '1px solid var(--border-hairline)' }}>
            <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginBottom: 8 }}>
              {FOLDERS.map((f) => (
                <button
                  key={f.id}
                  type="button"
                  className={`btn ${folder === f.id ? 'btn-primary' : ''}`}
                  style={{ padding: '3px 7px', fontSize: 11 }}
                  onClick={() => setFolder(f.id)}
                >
                  {f.label}
                </button>
              ))}
            </div>
            {!isSpecialList && (
              <div style={{ display: 'flex', gap: 4 }}>
                <input
                  className="input"
                  style={{ flex: 1, padding: '5px 8px' }}
                  placeholder="Hybrid + Ontology 2-hop search..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && void refreshNotes(folder, searchQuery)}
                />
                <button
                  type="button"
                  className="btn"
                  onClick={() => void refreshNotes(folder, searchQuery)}
                >
                  <Search size={12} />
                </button>
              </div>
            )}
          </div>

          <div className="wiki-note-list">
            {folder === '__templates__' ? (
              <>
                {authorityInfo && (
                  <div
                    className="wiki-note-item"
                    style={{ marginBottom: 8, cursor: 'default', borderStyle: 'dashed' }}
                  >
                    <div className="mono" style={{ fontSize: 11, color: 'var(--accent-primary)' }}>
                      ONTOLOGY AUTHORITY ({authorityInfo.ontology.length}) • ALIASES (
                      {Object.keys(authorityInfo.aliases || {}).length})
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 4 }}>
                      {authorityInfo.ontology.slice(0, 10).join(' • ')}
                    </div>
                    <div style={{ display: 'flex', gap: 4, marginTop: 8, flexWrap: 'wrap' }}>
                      <input
                        className="input"
                        style={{ flex: '1 1 130px', padding: '3px 6px', fontSize: 11 }}
                        placeholder="alias -> canonical (opt)"
                        value={aliasRuleInput}
                        onChange={(e) => setAliasRuleInput(e.target.value)}
                      />
                      <button
                        type="button"
                        className="btn"
                        style={{ padding: '3px 8px', fontSize: 11 }}
                        onClick={() => void handleConsolidateOntology()}
                      >
                        Consolidate Ontology
                      </button>
                    </div>
                  </div>
                )}
                {templates.map((t) => (
                  <button
                    key={t.slug}
                    type="button"
                    className={`wiki-note-item ${selectedTemplate?.slug === t.slug ? 'active' : ''}`}
                    onClick={() => {
                      setSelectedNote(null);
                      setSelectedTemplate(t);
                      setMobileShowList(false);
                      if (viewMode === 'graph') setViewMode('split');
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        gap: 6,
                      }}
                    >
                      <strong style={{ fontSize: 12 }}>{t.slug}</strong>
                      <span className="badge">{t.document_type}</span>
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 3 }}>
                      {t.when_to_use.slice(0, 88)}
                    </div>
                  </button>
                ))}
              </>
            ) : folder === '__lineage__' ? (
              historyItems.length === 0 ? (
                <div style={{ padding: 12, color: 'var(--text-muted)', fontSize: 12 }}>
                  No curation transactions yet. Graduate or Deep Curate an inbox note to build
                  reversible lineage.
                </div>
              ) : (
                historyItems.map((h) => (
                  <div key={h.tx_id} className="wiki-note-item" style={{ cursor: 'default' }}>
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        gap: 6,
                      }}
                    >
                      <code className="mono" style={{ fontSize: 11 }}>
                        {h.tx_id}
                      </code>
                      <span className={`badge ${h.reverted ? 'warn' : 'ok'}`}>
                        {h.reverted ? 'REVERTED' : h.strategy}
                      </span>
                    </div>
                    <div style={{ fontSize: 11.5, color: 'var(--text-secondary)', marginTop: 4 }}>
                      {h.output_paths.map((op) => (
                        <button
                          key={op}
                          type="button"
                          className="md-wikilink"
                          style={{ marginRight: 4, marginBottom: 3 }}
                          onClick={() => void openNote(op)}
                        >
                          {op}
                        </button>
                      ))}
                    </div>
                    {!h.reverted && (
                      <div style={{ display: 'flex', gap: 6, marginTop: 6, flexWrap: 'wrap' }}>
                        {h.output_paths[0] && (
                          <button
                            type="button"
                            className="btn"
                            style={{ padding: '2px 8px', fontSize: 11 }}
                            onClick={() => void openLineageOrDraftDiff(h.output_paths[0])}
                          >
                            <FileDiff size={11} /> View Diff
                          </button>
                        )}
                        <button
                          type="button"
                          className="btn"
                          style={{ padding: '2px 8px', fontSize: 11 }}
                          onClick={() => void handleRevertCuration(h.tx_id)}
                        >
                          <RotateCcw size={11} /> Revert -&gt; 01_inbox/
                        </button>
                      </div>
                    )}
                  </div>
                ))
              )
            ) : folder === '__trash__' ? (
              trashItems.length === 0 ? (
                <div style={{ padding: 12, color: 'var(--text-muted)', fontSize: 12 }}>
                  Trash is empty.
                </div>
              ) : (
                trashItems.map((t) => (
                  <div key={t.trash_id} className="wiki-note-item">
                    <div className="mono" style={{ fontSize: 11.5 }}>
                      {t.original_path}
                    </div>
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        marginTop: 6,
                      }}
                    >
                      <span style={{ fontSize: 10.5, color: 'var(--text-muted)' }}>
                        {t.trashed_at.slice(0, 16)}
                      </span>
                      <button
                        type="button"
                        className="btn"
                        style={{ padding: '2px 7px', fontSize: 11 }}
                        onClick={async () => {
                          const r = await callAction('wiki', {
                            subaction: 'restore',
                            trash_id: t.trash_id,
                          });
                          onToast(r.summary || 'Restored note.');
                          void refreshNotes('__trash__');
                        }}
                      >
                        <RotateCcw size={11} /> Restore
                      </button>
                    </div>
                  </div>
                ))
              )
            ) : notes.length === 0 ? (
              <div style={{ padding: 12, color: 'var(--text-muted)', fontSize: 12 }}>
                No notes in this view yet. Click <strong>New Inbox Note</strong> or ask Companion to
                capture research.
              </div>
            ) : (
              notes.map((n) => (
                <button
                  key={n.rel_path}
                  type="button"
                  className={`wiki-note-item ${selectedNote?.path === n.rel_path ? 'active' : ''}`}
                  onClick={() => {
                    void openNote(n.rel_path);
                    if (viewMode === 'graph') setViewMode('split');
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      gap: 6,
                    }}
                  >
                    <strong style={{ fontSize: 12.5 }}>{n.title}</strong>
                    <span className={`badge ${n.author === 'user' ? 'accent' : 'ok'}`}>
                      {n.author}
                    </span>
                  </div>
                  <div
                    className="mono"
                    style={{ fontSize: 10.5, color: 'var(--text-muted)', marginTop: 2 }}
                  >
                    {n.rel_path}
                  </div>
                  <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 4 }}>
                    {n.template && (
                      <span className="badge" style={{ fontSize: 9.5 }}>
                        {n.template}
                      </span>
                    )}
                    {n.stale && (
                      <span className="badge warn" style={{ fontSize: 9.5 }}>
                        STALE • {n.days_since_verified}d
                      </span>
                    )}
                    {n.match_kind === 'ontology_bridge' && (
                      <span
                        className="badge"
                        style={{
                          fontSize: 9.5,
                          borderColor: '#4cc9f0',
                          color: '#4cc9f0',
                        }}
                      >
                        BRIDGE via {(n.shared_ontology || []).join(', ')}
                      </span>
                    )}
                  </div>
                </button>
              ))
            )}
          </div>
        </div>

        {/* Right Main Area */}
        <div
          className={`wiki-main ${
            mobileShowList && viewMode !== 'graph' ? 'mobile-hidden' : ''
          }`}
        >
          {viewMode === 'graph' ? (
            <MindMapCanvas
              graph={graph}
              selectedId={selectedNote?.path}
              onSelectNode={(id) => {
                void openNote(id);
                setViewMode('split');
              }}
            />
          ) : selectedTemplate ? (
            <div style={{ padding: 22, overflowY: 'auto' }}>
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  gap: 10,
                  flexWrap: 'wrap',
                  marginBottom: 12,
                }}
              >
                <div>
                  <h2 style={{ margin: 0, fontSize: 16 }}>
                    Template: <code>{selectedTemplate.slug}</code> — {selectedTemplate.title}
                  </h2>
                  <p style={{ margin: '4px 0 0', fontSize: 12.5, color: 'var(--text-secondary)' }}>
                    {selectedTemplate.when_to_use}
                  </p>
                </div>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => {
                    setCapTemplate(selectedTemplate.slug);
                    setCapBody(selectedTemplate.skeleton);
                    setShowCapture(true);
                  }}
                >
                  <Plus size={13} /> Stage Inbox Note with {selectedTemplate.slug}
                </button>
              </div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 12 }}>
                <span className="badge accent">TYPE: {selectedTemplate.document_type}</span>
                {selectedTemplate.required_sections.map((sec) => (
                  <span key={sec} className="badge">
                    ## {sec}
                  </span>
                ))}
              </div>
              <pre
                className="mono"
                style={{
                  padding: 14,
                  background: 'var(--code-bg)',
                  border: '1px solid var(--border-hairline)',
                  fontSize: 12,
                  whiteSpace: 'pre-wrap',
                }}
              >
                {selectedTemplate.skeleton}
              </pre>
            </div>
          ) : !selectedNote ? (
            <div style={{ padding: 28, color: 'var(--text-secondary)', overflowY: 'auto' }}>
              <FolderGit2 size={24} style={{ color: 'var(--accent-primary)', marginBottom: 8 }} />
              <h3 style={{ margin: '0 0 6px' }}>Select, Capture, or Deliberately Curate a Note</h3>
              <p style={{ maxWidth: 580, lineHeight: 1.55 }}>
                All rough ideas stage into <code>01_inbox/</code> first. Graduating into{' '}
                <code>02_knowledge/</code> enforces one of the <strong>11 governed templates</strong>
                , rich YAML frontmatter, overlap synthesis (no lazy dumps),{' '}
                <code>ontology</code> cross-domain bridges, and Never-Delete{' '}
                <code>04_archive/</code> rollback lineage.
              </p>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 12 }}>
                <button
                  type="button"
                  className="btn"
                  onClick={() => {
                    setFolder('__templates__');
                    setMobileShowList(true);
                  }}
                >
                  <Layers size={13} /> Browse 11 Templates ({templates.length})
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => {
                    setFolder('__lineage__');
                    setMobileShowList(true);
                  }}
                >
                  <History size={13} /> Curation Lineage &amp; Rollback
                </button>
              </div>
            </div>
          ) : (
            <>
              <div className="wiki-note-toolbar">
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <code className="mono">{selectedNote.path}</code>
                  <span className={`badge ${selectedNote.author === 'user' ? 'accent' : 'ok'}`}>
                    AUTHOR: {selectedNote.author}
                  </span>
                  {selectedNote.template && (
                    <span className="badge">TPL: {selectedNote.template}</span>
                  )}
                  {selectedNote.path.startsWith('02_knowledge/') && (
                    <span className={`badge ${selectedNote.stale ? 'warn' : 'ok'}`}>
                      {selectedNote.stale
                        ? `STALE • ${selectedNote.days_since_verified ?? 0}d / ${selectedNote.review_after_days ?? 90}d`
                        : `VERIFIED • ${selectedNote.days_since_verified ?? 0}d / ${selectedNote.review_after_days ?? 365}d`}
                    </span>
                  )}
                  <span className="badge desktop-only">HASH: {selectedNote.content_hash}</span>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                  {selectedNote.path.startsWith('02_knowledge/') && (
                    <button
                      type="button"
                      className="btn"
                      onClick={() => void handleVerifyNote()}
                      title="Stamp last_verified_at today to confirm hardware/SOP accuracy"
                    >
                      <CheckCircle2 size={12} /> Mark Verified
                    </button>
                  )}
                  {(Boolean(selectedNote.supersedes?.length || selectedNote.superseded_by) ||
                    editBody !== selectedNote.body) && (
                    <button
                      type="button"
                      className={`btn ${viewMode === 'diff' ? 'btn-primary' : ''}`}
                      onClick={() =>
                        viewMode === 'diff'
                          ? setViewMode('split')
                          : void openLineageOrDraftDiff()
                      }
                    >
                      <FileDiff size={12} /> {viewMode === 'diff' ? 'Exit Diff' : 'Diff'}
                    </button>
                  )}
                  {selectedNote.path.startsWith('01_inbox/') && (
                    <>
                      <button
                        type="button"
                        className="btn"
                        disabled={inspecting}
                        onClick={() => void handleInspectGraduation()}
                      >
                        <FileCheck2 size={12} /> {inspecting ? 'Auditing...' : 'Inspect Overlaps'}
                      </button>
                      <button
                        type="button"
                        className="btn"
                        onClick={() => void handleDeepCurateJob(selectedNote.path)}
                      >
                        <Sparkles size={12} /> Deep Curate (AI)
                      </button>
                      <input
                        className="input"
                        style={{ width: 105, padding: '4px 7px' }}
                        placeholder="domain"
                        value={gradDomain}
                        onChange={(e) => setGradDomain(e.target.value)}
                      />
                      <input
                        className="input"
                        style={{ width: 90, padding: '4px 7px' }}
                        placeholder="topic (opt)"
                        value={gradTopic}
                        onChange={(e) => setGradTopic(e.target.value)}
                      />
                      <button
                        type="button"
                        className="btn"
                        onClick={() => void handleGraduate()}
                      >
                        <ArrowUpRight size={12} /> Graduate
                      </button>
                    </>
                  )}
                  {(selectedNote.path.startsWith('02_knowledge/') ||
                    selectedNote.path.startsWith('04_archive/')) &&
                    Boolean(selectedNote.supersedes?.length || selectedNote.superseded_by) && (
                      <button
                        type="button"
                        className="btn"
                        onClick={() => void handleRevertCuration(selectedNote.path)}
                      >
                        <RotateCcw size={12} /> Revert Curation
                      </button>
                    )}
                  <button type="button" className="btn btn-primary" onClick={() => void handleSave()}>
                    <Save size={12} /> Save Note
                  </button>
                  <button type="button" className="btn btn-danger" onClick={() => void handleTrash()}>
                    <Trash2 size={12} /> Trash
                  </button>
                </div>
              </div>

              {/* Deliberate Curation Audit Drawer */}
              {curationAudit && (
                <div className="wiki-curation-banner">
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      gap: 8,
                      flexWrap: 'wrap',
                    }}
                  >
                    <strong className="mono" style={{ fontSize: 11.5 }}>
                      CURATION AUDIT ({curationAudit.curation_id})
                    </strong>
                    <button
                      type="button"
                      className="btn"
                      style={{ padding: '2px 7px', fontSize: 10.5 }}
                      onClick={() => setCurationAudit(null)}
                    >
                      Dismiss
                    </button>
                  </div>
                  <div style={{ fontSize: 12, marginTop: 6 }}>
                    <strong>Overlap Candidates ({curationAudit.overlap_candidates.length}):</strong>{' '}
                    {curationAudit.overlap_candidates.length === 0 ? (
                      <span style={{ color: 'var(--text-muted)' }}>
                        None — safe to CREATE_NEW or SPLIT_MULTI.
                      </span>
                    ) : (
                      curationAudit.overlap_candidates.map((c) => (
                        <button
                          key={c.path}
                          type="button"
                          className="md-wikilink"
                          style={{ marginRight: 6 }}
                          onClick={() => void openNote(c.path)}
                        >
                          {c.path} ({Math.round(c.overlap_score * 100)}%
                          {c.must_read ? ' MUST-READ' : ''})
                        </button>
                      ))
                    )}
                  </div>
                  <div style={{ fontSize: 12, marginTop: 5 }}>
                    <strong>Recommended Templates:</strong>{' '}
                    {curationAudit.recommended_templates.map((rt) => (
                      <button
                        key={rt.slug}
                        type="button"
                        className="btn"
                        style={{ padding: '1px 7px', fontSize: 10.5, marginRight: 5 }}
                        onClick={() => applyTemplateMissingSections(rt.slug)}
                      >
                        Use {rt.slug}
                      </button>
                    ))}
                  </div>
                  {curationAudit.ontology_bridges.length > 0 && (
                    <div style={{ fontSize: 11.5, marginTop: 5, color: '#4cc9f0' }}>
                      <strong>2-Hop Ontology Bridges:</strong>{' '}
                      {curationAudit.ontology_bridges
                        .map((b) => `${b.rel_path} (${b.shared_ontology.join(', ')})`)
                        .join(' • ')}
                    </div>
                  )}
                </div>
              )}

              {/* Lineage Banner when note has archive provenance */}
              {Boolean(selectedNote.supersedes?.length || selectedNote.superseded_by) && (
                <div className="wiki-lineage-bar mono">
                  {selectedNote.superseded_by && (
                    <span>
                      ARCHIVED — Superseded by:{' '}
                      <button
                        type="button"
                        className="md-wikilink"
                        onClick={() => void openNote(selectedNote.superseded_by!)}
                      >
                        {selectedNote.superseded_by}
                      </button>
                    </span>
                  )}
                  {Boolean(selectedNote.supersedes?.length) && (
                    <span>
                      LINEAGE ORIGINS ({selectedNote.supersedes!.length}):{' '}
                      {selectedNote.supersedes!.map((archPath) => (
                        <button
                          key={archPath}
                          type="button"
                          className="md-wikilink"
                          style={{ marginRight: 5 }}
                          onClick={() => void openNote(archPath)}
                        >
                          {archPath}
                        </button>
                      ))}
                      <button
                        type="button"
                        className="btn"
                        style={{ padding: '1px 7px', fontSize: 10.5, marginLeft: 4 }}
                        onClick={() => void openLineageOrDraftDiff(selectedNote.path)}
                      >
                        <FileDiff size={11} /> Compare Diff
                      </button>
                    </span>
                  )}
                </div>
              )}

              {viewMode === 'diff' && activeDiff ? (
                <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: 16 }}>
                  <DiffView diff={activeDiff} onClose={() => setViewMode('split')} />
                </div>
              ) : (
                <div className={`wiki-split mode-${viewMode}`}>
                <div className="wiki-pane wiki-pane-edit">
                  <div style={{ display: 'flex', gap: 6, marginBottom: 6, flexWrap: 'wrap' }}>
                    <input
                      className="input"
                      style={{ flex: '1 1 190px' }}
                      value={editTitle}
                      onChange={(e) => setEditTitle(e.target.value)}
                      placeholder="Title"
                    />
                    <select
                      aria-label="Note template"
                      className="select"
                      style={{ flex: '0 1 185px' }}
                      value={editTemplate}
                      onChange={(e) => setEditTemplate(e.target.value)}
                    >
                      {selectedNote.path.startsWith('01_inbox/') && (
                        <option value="inbox-raw">inbox-raw</option>
                      )}
                      {templates.map((t) => (
                        <option key={t.slug} value={t.slug}>
                          {t.slug}
                        </option>
                      ))}
                    </select>
                    {editTemplate !== 'inbox-raw' && (
                      <button
                        type="button"
                        className="btn"
                        style={{ padding: '4px 8px', fontSize: 11 }}
                        onClick={() => applyTemplateMissingSections(editTemplate)}
                        title="Append any missing ## Required Sections for this template"
                      >
                        + Sections
                      </button>
                    )}
                    <input
                      ref={wikiImgInputRef}
                      type="file"
                      accept="image/png,image/jpeg,image/webp,image/gif"
                      style={{ display: 'none' }}
                      aria-label="Upload Wiki diagram"
                      onChange={(e) => {
                        const f = e.target.files?.[0];
                        if (f) void uploadWikiDiagramFile(f);
                        e.currentTarget.value = '';
                      }}
                    />
                    <button
                      type="button"
                      className="btn"
                      disabled={uploadingImage}
                      style={{ padding: '4px 8px', fontSize: 11 }}
                      onClick={() => wikiImgInputRef.current?.click()}
                      title="Upload or paste an inline diagram into 03_resources/assets/"
                    >
                      <ImageIcon size={12} /> {uploadingImage ? 'Uploading...' : '+ Image'}
                    </button>
                  </div>
                  <div style={{ display: 'flex', gap: 6, marginBottom: 8, flexWrap: 'wrap' }}>
                    <input
                      className="input"
                      style={{ flex: '2 1 220px' }}
                      value={editSummary}
                      onChange={(e) => setEditSummary(e.target.value)}
                      placeholder="Executive summary (required in 02_knowledge/)"
                    />
                    <input
                      className="input"
                      style={{ flex: '1 1 130px' }}
                      value={editTags}
                      onChange={(e) => setEditTags(e.target.value)}
                      placeholder="tags"
                    />
                    <input
                      className="input"
                      style={{ flex: '1 1 150px' }}
                      value={editOntology}
                      onChange={(e) => setEditOntology(e.target.value)}
                      placeholder="ontology bridges"
                    />
                  </div>
                  <textarea
                    ref={editorRef}
                    className="wiki-editor-area"
                    value={editBody}
                    onChange={(e) => setEditBody(e.target.value)}
                    onPaste={(e) => {
                      const items = Array.from(e.clipboardData?.items || []);
                      const imgItem = items.find((it) => it.type.startsWith('image/'));
                      if (imgItem) {
                        const file = imgItem.getAsFile();
                        if (file) {
                          e.preventDefault();
                          void uploadWikiDiagramFile(file);
                        }
                      }
                    }}
                    onDragOver={(e) => {
                      if (Array.from(e.dataTransfer?.types || []).includes('Files')) {
                        e.preventDefault();
                      }
                    }}
                    onDrop={(e) => {
                      const file = Array.from(e.dataTransfer?.files || []).find((f) =>
                        f.type.startsWith('image/')
                      );
                      if (file) {
                        e.preventDefault();
                        void uploadWikiDiagramFile(file);
                      }
                    }}
                  />
                </div>

                <div className="wiki-pane wiki-pane-preview" style={{ borderRight: 'none' }}>
                  {editSummary && (
                    <div
                      style={{
                        padding: '8px 10px',
                        marginBottom: 12,
                        borderLeft: '2px solid var(--accent-primary)',
                        background: 'var(--bg-elevated)',
                        fontSize: 12.5,
                        color: 'var(--text-secondary)',
                      }}
                    >
                      {editSummary}
                    </div>
                  )}
                  <MarkdownView
                    content={editBody}
                    onOpenWikiNote={(target) => void openNote(target)}
                    onFilterTag={(t) => {
                      setFolder('');
                      setSearchQuery(t);
                      void refreshNotes('', t);
                    }}
                  />

                  <div
                    style={{
                      marginTop: 22,
                      paddingTop: 12,
                      borderTop: '1px solid var(--border-hairline)',
                    }}
                  >
                    {editOntology.trim() && (
                      <div style={{ marginBottom: 10 }}>
                        <div className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                          ONTOLOGY BRIDGE CONCEPTS
                        </div>
                        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 5 }}>
                          {editOntology
                            .split(',')
                            .map((x) => x.trim())
                            .filter(Boolean)
                            .map((ont) => (
                              <button
                                key={ont}
                                type="button"
                                className="badge"
                                style={{
                                  borderColor: '#4cc9f0',
                                  color: '#4cc9f0',
                                  cursor: 'pointer',
                                }}
                                onClick={() => {
                                  setFolder('');
                                  setSearchQuery(ont);
                                  void refreshNotes('', ont);
                                }}
                              >
                                ~ {ont}
                              </button>
                            ))}
                        </div>
                      </div>
                    )}

                    <div className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                      INBOUND BACKLINKS ({selectedNote.backlinks?.length || 0})
                    </div>
                    {selectedNote.backlinks?.length ? (
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 6 }}>
                        {selectedNote.backlinks.map((b) => (
                          <button
                            key={b}
                            type="button"
                            className="md-wikilink"
                            onClick={() => void openNote(b)}
                          >
                            &larr; {b}
                          </button>
                        ))}
                      </div>
                    ) : (
                      <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4 }}>
                        No inbound wikilinks reference this note yet.
                      </div>
                    )}
                  </div>
                </div>
              </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
};

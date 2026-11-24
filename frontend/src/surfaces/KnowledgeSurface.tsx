import React, { useEffect, useRef, useState } from 'react';
import {
  ArchiveRestore,
  ArrowLeft,
  CheckCircle2,
  Edit3,
  Eye,
  FileDiff,
  FolderTree,
  GraduationCap,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Search,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import { callAction } from '../api';
import { DiffView } from '../components/DiffView';
import { MarkdownView } from '../components/MarkdownView';
import { MindMapCanvas } from '../components/MindMapCanvas';
import type {
  MindMapGraph,
  WikiDiffPayload,
  WikiFrontmatterFacets,
  WikiNoteDetail,
  WikiNoteSummary,
  WikiTreeNode,
} from '../types';
import { KnowledgeStudyView } from './KnowledgeStudyView';
import {
  WikiExplorerSidebar,
  ancestorFoldersFor,
  collectAllFolderPaths,
  collectDefaultExpanded,
  findFirstReadableFile,
} from './WikiExplorerSidebar';
import { WikiFrontmatterDrawer, WikiNoteFrontmatterBar } from './WikiFrontmatterFilterBar';
import { WikiGraduateModal } from './WikiGraduateModal';

interface KnowledgeSurfaceProps {
  initialNotePath?: string;
  openNonce?: number;
  initialMode?: 'notes' | 'mindmap' | 'study';
  onRefreshStatus: () => Promise<void>;
  onToast: (msg: string) => void;
}

const FM_TOKEN_RE = /(?:^|\s)(?:#([a-zA-Z0-9_-]+)|([a-zA-Z_][a-zA-Z0-9_-]*):(?:"([^"]+)"|'([^']+)'|([^\s]+)))/g;

function extractActiveTokens(q: string): string[] {
  const tokens: string[] = [];
  const re = new RegExp(FM_TOKEN_RE.source, 'g');
  let m: RegExpExecArray | null;
  while ((m = re.exec(q)) !== null) {
    const raw = m[0].trim();
    if (!raw.startsWith('http:') && !raw.startsWith('https:')) tokens.push(raw);
  }
  return tokens;
}

export const KnowledgeSurface: React.FC<KnowledgeSurfaceProps> = ({
  initialNotePath = '',
  openNonce = 0,
  initialMode = 'notes',
  onRefreshStatus,
  onToast,
}) => {
  const [mode, setMode] = useState<'notes' | 'mindmap' | 'study'>(initialMode);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(() =>
    typeof window !== 'undefined' ? window.localStorage.getItem('companion_wiki_tree_open') !== '0' : true
  );
  const [mobilePane, setMobilePane] = useState<'list' | 'reading'>(initialNotePath ? 'reading' : 'list');
  const [tree, setTree] = useState<WikiTreeNode[]>([]);
  const [totalFiles, setTotalFiles] = useState(0);
  const [facets, setFacets] = useState<WikiFrontmatterFacets | null>(null);
  const [expandedPaths, setExpandedPaths] = useState<Record<string, boolean>>({});
  const [query, setQuery] = useState('');
  const [showFilters, setShowFilters] = useState(false);
  const [searchHits, setSearchHits] = useState<WikiNoteSummary[]>([]);
  const [selectedNote, setSelectedNote] = useState<WikiNoteDetail | null>(null);
  const [selectedAsset, setSelectedAsset] = useState<{ rel_path: string; asset_url: string; title: string } | null>(null);
  const [showFrontmatterTable, setShowFrontmatterTable] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editBody, setEditBody] = useState('');
  const [diff, setDiff] = useState<WikiDiffPayload | null>(null);
  const [graduatingPath, setGraduatingPath] = useState('');
  const [freshAudit, setFreshAudit] = useState<{ path: string; summary: string; flagged: string[] } | null>(null);
  const [verifyingFresh, setVerifyingFresh] = useState(false);
  const [graph, setGraph] = useState<MindMapGraph | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);

  const [showCapture, setShowCapture] = useState(false);
  const [capTitle, setCapTitle] = useState('');
  const [capBody, setCapBody] = useState('');
  const [showTrash, setShowTrash] = useState(false);
  const [trashItems, setTrashItems] = useState<Array<{ trash_id: string; original_path?: string; trashed_at?: string }>>([]);

  const loadTrash = async () => {
    const res = await callAction<{ trash?: Array<{ trash_id: string; original_path?: string; trashed_at?: string }> }>('wiki', {
      subaction: 'list_trash',
    });
    if (res.trash) setTrashItems(res.trash);
  };

  const toggleExplorerPanel = () => {
    if (typeof window !== 'undefined' && window.innerWidth <= 768) {
      setMobilePane((cur) => (cur === 'list' ? 'reading' : 'list'));
      return;
    }
    setSidebarOpen((cur) => {
      const next = !cur;
      window.localStorage.setItem('companion_wiki_tree_open', next ? '1' : '0');
      return next;
    });
  };

  const loadTreeAndFacets = async (autoSelectFirst = false) => {
    const res = await callAction<{ tree?: WikiTreeNode[]; facets?: WikiFrontmatterFacets; total_files?: number }>(
      'wiki',
      { subaction: 'tree' }
    );
    if (res.ok && res.tree) {
      setTree(res.tree);
      setTotalFiles(res.total_files ?? 0);
      if (res.facets) setFacets(res.facets);
      setExpandedPaths((prev) => (Object.keys(prev).length > 0 ? prev : collectDefaultExpanded(res.tree || [])));
      if (autoSelectFirst && !selectedNote && !selectedAsset) {
        const target = initialNotePath || findFirstReadableFile(res.tree);
        if (target) void openNote(target, Boolean(initialNotePath));
      }
    }
  };

  const runSearch = async (rawQuery: string) => {
    const q = rawQuery.trim();
    if (!q) {
      setSearchHits([]);
      return;
    }
    const res = await callAction<{ hits?: WikiNoteSummary[]; notes?: WikiNoteSummary[]; results?: WikiNoteSummary[] }>(
      'wiki',
      { subaction: 'search', query: q, limit: 30 }
    );
    setSearchHits(res.hits || res.notes || res.results || []);
  };

  const openNote = async (path: string, switchMobile = true, skipDirtyGuard = false) => {
    if (!path) return;
    if (
      !skipDirtyGuard &&
      editing &&
      selectedNote &&
      selectedNote.path !== path &&
      editBody !== (selectedNote.raw || selectedNote.body || '') &&
      typeof window.confirm === 'function' &&
      !window.confirm('Discard unsaved edits to this note?')
    ) {
      return;
    }
    setDiff(null);
    setGraduatingPath('');
    setFreshAudit(null);
    setEditing(false);
    setSelectedAsset(null);
    if (switchMobile) setMobilePane('reading');
    const res = await callAction<WikiNoteDetail>('wiki', { subaction: 'read', path });
    if (res.ok) {
      const detail = res as unknown as WikiNoteDetail;
      setSelectedNote(detail);
      setEditBody(detail.raw || detail.body || '');
      const ancestors = ancestorFoldersFor(detail.path);
      if (ancestors.length > 0) {
        setExpandedPaths((prev) => {
          const next = { ...prev };
          for (const a of ancestors) next[a] = true;
          return next;
        });
      }
    }
  };

  const applyFilterToken = (token: string, openListOnMobile = false) => {
    const cleanTok = token.trim();
    if (!cleanTok) return;
    setMode('notes');
    if (!sidebarOpen) {
      setSidebarOpen(true);
      window.localStorage.setItem('companion_wiki_tree_open', '1');
    }
    if (openListOnMobile) setMobilePane('list');
    setQuery((cur) => {
      const parts = cur.trim().split(/\s+/).filter(Boolean);
      const exists = parts.some((p) => p.toLowerCase() === cleanTok.toLowerCase());
      return (exists ? parts.filter((p) => p.toLowerCase() !== cleanTok.toLowerCase()) : [...parts, cleanTok]).join(' ');
    });
  };

  useEffect(() => {
    setMode(initialMode);
  }, [initialMode]);

  useEffect(() => {
    void loadTreeAndFacets(true);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void runSearch(query), 150);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    if (initialNotePath) {
      setMode('notes');
      void openNote(initialNotePath, true, true);
    }
  }, [initialNotePath, openNonce]);

  useEffect(() => {
    if (mode === 'mindmap') {
      void callAction<MindMapGraph>('wiki', { subaction: 'graph' }).then((res) => {
        if (res.ok) setGraph(res as unknown as MindMapGraph);
      });
    }
  }, [mode]);

  const saveEdit = async () => {
    if (!selectedNote) return;
    const res = await callAction('wiki', {
      subaction: 'save',
      path: selectedNote.path,
      content: editBody,
      expected_hash: selectedNote.content_hash,
      confirmed: true,
    });
    if (res.ok) {
      setEditing(false);
      await Promise.all([openNote(selectedNote.path, false), loadTreeAndFacets(false)]);
      if (query.trim()) await runSearch(query);
      onToast(res.summary || 'Saved Wiki note & YAML frontmatter.');
    } else {
      onToast(res.error || 'Could not save note.');
    }
  };

  const trashSelectedNote = async () => {
    if (!selectedNote) return;
    const res = await callAction('wiki', { subaction: 'trash', path: selectedNote.path, confirmed: true });
    if (res.ok) {
      setSelectedNote(null);
      setMobilePane('list');
      await loadTreeAndFacets(true);
      if (query.trim()) await runSearch(query);
      onToast(res.summary || 'Moved note to recoverable Trash.');
    } else {
      onToast(res.error || 'Could not trash note.');
    }
  };

  const inspectNoteDiff = async () => {
    if (!selectedNote) return;
    const res = await callAction<WikiDiffPayload>('wiki', { subaction: 'diff', path: selectedNote.path });
    if (res.ok) setDiff(res as unknown as WikiDiffPayload);
    else onToast(res.error || 'No archived revision diff available yet.');
  };

  const captureInbox = async () => {
    if (!capTitle.trim() || !capBody.trim()) return;
    const res = await callAction<{ path?: string }>('wiki', {
      subaction: 'capture_inbox',
      title: capTitle.trim(),
      content: capBody,
    });
    if (res.ok) {
      setCapTitle('');
      setCapBody('');
      setShowCapture(false);
      await loadTreeAndFacets(false);
      if (res.path) await openNote(res.path);
      onToast('Captured note to 01_inbox/.');
    }
  };

  const activeTokens = extractActiveTokens(query);
  const isArchivedNote = Boolean(selectedNote?.path.startsWith('04_archive/'));

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }} data-testid="knowledge-surface">
      <div className="wiki-note-toolbar">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          {mode === 'notes' && (
            <button
              type="button"
              className={`btn btn-sm ${sidebarOpen ? 'active-subtle' : ''}`}
              onClick={toggleExplorerPanel}
              title={sidebarOpen ? 'Collapse folder & file explorer' : 'Open folder & file explorer'}
              aria-expanded={sidebarOpen}
              data-testid="wiki-toggle-explorer-btn"
            >
              {sidebarOpen ? <PanelLeftClose size={13} /> : <PanelLeftOpen size={13} />}
              <span>Files</span>
            </button>
          )}

          <div className="segmented-pills">
            <button type="button" className={`segmented-pill ${mode === 'notes' ? 'active' : ''}`} onClick={() => setMode('notes')}>
              Notes
            </button>
            <button type="button" className={`segmented-pill ${mode === 'mindmap' ? 'active' : ''}`} onClick={() => setMode('mindmap')}>
              <Network size={12} style={{ marginRight: 4 }} /> Mind Map
            </button>
            <button type="button" className={`segmented-pill ${mode === 'study' ? 'active' : ''}`} onClick={() => setMode('study')}>
              <GraduationCap size={12} style={{ marginRight: 4 }} /> Study
            </button>
          </div>
        </div>

        {mode === 'notes' && (
          <div className="wiki-toolbar-search-wrap">
            <div className="wiki-fm-search-box">
              <Search size={13} className="wiki-fm-search-icon" />
              <input
                ref={searchInputRef}
                className="wiki-fm-search-input"
                placeholder="Search YAML frontmatter & notes (tag:, domain:, template:, key:val)..."
                value={query}
                onChange={(e) => {
                  const val = e.target.value;
                  setQuery(val);
                  if (val.trim() && !sidebarOpen) {
                    setSidebarOpen(true);
                    window.localStorage.setItem('companion_wiki_tree_open', '1');
                  }
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void runSearch(query);
                  if (e.key === 'Escape' && query) setQuery('');
                }}
                data-testid="wiki-frontmatter-search-input"
              />
              {query && (
                <button type="button" className="wiki-search-clear-btn" onClick={() => setQuery('')} aria-label="Clear search">
                  <X size={12} />
                </button>
              )}
              <button
                type="button"
                className={`wiki-filter-toggle-btn ${showFilters || activeTokens.length > 0 ? 'active' : ''}`}
                title="Browse YAML frontmatter properties & filters"
                onClick={() => setShowFilters((v) => !v)}
                data-testid="wiki-frontmatter-filters-btn"
              >
                <SlidersHorizontal size={12} />
                <span>YAML</span>
                {activeTokens.length > 0 && <span className="wiki-filter-badge">{activeTokens.length}</span>}
              </button>
            </div>

            <button
              type="button"
              className={`btn btn-sm ${showTrash ? 'active-subtle' : ''}`}
              data-testid="wiki-trash-drawer-btn"
              onClick={() => {
                const next = !showTrash;
                setShowTrash(next);
                if (next) void loadTrash();
              }}
            >
              <ArchiveRestore size={12} /> Trash
            </button>
            <button type="button" className="btn btn-primary btn-sm" onClick={() => setShowCapture((v) => !v)}>
              <Plus size={12} /> Capture
            </button>
          </div>
        )}
      </div>

      {showTrash && mode === 'notes' && (
        <div
          data-testid="wiki-trash-drawer"
          style={{ padding: '10px 16px', borderBottom: '1px solid var(--border-hairline)', background: 'var(--bg-panel)' }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
            <strong style={{ fontSize: 12.5 }}>Recoverable Wiki Trash ({trashItems.length})</strong>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowTrash(false)}>
              Close
            </button>
          </div>
          {trashItems.length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>Trash is empty.</div>
          ) : (
            <div className="card-group" style={{ marginBottom: 0 }}>
              {trashItems.slice(0, 8).map((ti) => (
                <div key={ti.trash_id} className="card-row" style={{ padding: '7px 12px' }}>
                  <span className="mono" style={{ fontSize: 12 }}>{ti.original_path || ti.trash_id}</span>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={async () => {
                      const r = await callAction<{ path?: string; summary?: string; error?: string }>('wiki', {
                        subaction: 'restore',
                        trash_id: ti.trash_id,
                      });
                      await Promise.all([loadTrash(), loadTreeAndFacets(false)]);
                      if (r.path) await openNote(r.path, false, true);
                      onToast(r.ok === false ? r.error || 'Could not restore note.' : r.summary || 'Restored note.');
                    }}
                  >
                    <ArchiveRestore size={12} /> Restore
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {mode === 'notes' && (
        <WikiFrontmatterDrawer
          activeTokens={activeTokens}
          showFilters={showFilters}
          facets={facets}
          query={query}
          onApplyToken={applyFilterToken}
          onInsertKeyPrefix={(k) => {
            const prefix = `${k}:`;
            setQuery((cur) => (cur.trim() ? `${cur.trim()} ${prefix}` : prefix));
            searchInputRef.current?.focus();
          }}
          onClearAll={() => setQuery('')}
        />
      )}

      {showCapture && mode === 'notes' && (
        <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--border-hairline)', background: 'var(--bg-panel)' }}>
          <div style={{ maxWidth: 680, display: 'grid', gap: 8 }}>
            <input className="input" placeholder="Note title..." value={capTitle} onChange={(e) => setCapTitle(e.target.value)} />
            <textarea className="textarea" rows={3} placeholder="Markdown content (supports [[wikilinks]] & YAML frontmatter)..." value={capBody} onChange={(e) => setCapBody(e.target.value)} />
            <div>
              <button type="button" className="btn btn-primary btn-sm" onClick={() => void captureInbox()}>
                Save to 01_inbox/
              </button>
            </div>
          </div>
        </div>
      )}

      {mode === 'mindmap' && (
        <MindMapCanvas
          graph={graph}
          selectedId={selectedNote?.path}
          onSelectNode={(path) => {
            setMode('notes');
            void openNote(path);
          }}
        />
      )}

      {mode === 'study' && <KnowledgeStudyView onRefreshStatus={onRefreshStatus} onToast={onToast} />}

      {mode === 'notes' && (
        <div className={`wiki-shell ${sidebarOpen ? '' : 'sidebar-collapsed'} mobile-${mobilePane}`}>
          <WikiExplorerSidebar
            tree={tree}
            totalFiles={totalFiles}
            expandedPaths={expandedPaths}
            selectedPath={selectedAsset?.rel_path || selectedNote?.path || ''}
            query={query}
            searchHits={searchHits}
            onToggleFolder={(relPath) => setExpandedPaths((prev) => ({ ...prev, [relPath]: !prev[relPath] }))}
            onExpandAll={() => setExpandedPaths(collectAllFolderPaths(tree))}
            onCollapseAll={() => setExpandedPaths({})}
            onSelectFile={(node) => {
              if (node.is_asset && node.asset_url) {
                setSelectedNote(null);
                setSelectedAsset({ rel_path: node.rel_path, asset_url: node.asset_url, title: node.title || node.rel_path });
                setMobilePane('reading');
              } else {
                void openNote(node.rel_path, true);
              }
            }}
            onClearQuery={() => setQuery('')}
            onApplyToken={(tok) => applyFilterToken(tok, false)}
            onReindex={() => {
              void callAction('wiki', { subaction: 'reindex' }).then(() => {
                void loadTreeAndFacets(false);
                if (query.trim()) void runSearch(query);
                onToast('Reindexed Knowledge Wiki & YAML frontmatter.');
              });
            }}
            onCollapseSidebar={toggleExplorerPanel}
          />

          <div className="wiki-main">
            {selectedAsset ? (
              <div style={{ flex: 1, overflowY: 'auto', padding: '18px 22px' }}>
                <div style={{ maxWidth: 760, margin: '0 auto' }}>
                  <button type="button" className="btn btn-ghost btn-sm mobile-only-btn" style={{ marginBottom: 10 }} onClick={() => setMobilePane('list')}>
                    <ArrowLeft size={13} /> Directory Tree
                  </button>
                  <h2 style={{ margin: '0 0 6px', fontSize: 19 }}>{selectedAsset.title}</h2>
                  <div className="mono" style={{ fontSize: 11.5, color: 'var(--text-muted)', marginBottom: 16 }}>
                    {selectedAsset.rel_path}
                  </div>
                  <img src={selectedAsset.asset_url} alt={selectedAsset.title} style={{ maxWidth: '100%', borderRadius: 8, border: '1px solid var(--border-hairline)' }} />
                </div>
              </div>
            ) : selectedNote ? (
              <div style={{ flex: 1, overflowY: 'auto', padding: '18px 22px' }}>
                <div style={{ maxWidth: 780, margin: '0 auto' }}>
                  <button type="button" className="btn btn-ghost btn-sm mobile-only-btn" style={{ marginBottom: 10 }} onClick={() => setMobilePane('list')}>
                    <ArrowLeft size={13} /> Directory Tree
                  </button>

                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 10, marginBottom: 10, flexWrap: 'wrap' }}>
                    <div>
                      <h2 style={{ margin: 0, fontSize: 20 }}>{selectedNote.title}</h2>
                      <div style={{ fontSize: 11.5, color: 'var(--text-muted)', marginTop: 3 }}>
                        <span className="mono">{selectedNote.path}</span> • Author: {selectedNote.author}
                        {isArchivedNote && ' • Archived Snapshot'}
                      </div>
                    </div>
                    <div style={{ display: 'inline-flex', gap: 6, flexWrap: 'wrap' }}>
                      {selectedNote.path.startsWith('01_inbox/') && (
                        <button
                          type="button"
                          className="btn btn-primary btn-sm"
                          data-testid="wiki-graduate-btn"
                          onClick={() => setGraduatingPath(selectedNote.path)}
                        >
                          <Sparkles size={12} /> Graduate
                        </button>
                      )}
                      {!isArchivedNote && selectedNote.path.startsWith('02_knowledge/') && (
                        <button
                          type="button"
                          className="btn btn-sm"
                          data-testid="wiki-verify-fresh-btn"
                          title="Run AI claim & freshness audit"
                          disabled={verifyingFresh}
                          onClick={async () => {
                            setVerifyingFresh(true);
                            const r = await callAction<{
                              audit_summary?: string;
                              flagged_claims?: string[];
                              summary?: string;
                              error?: string;
                            }>('wiki', {
                              subaction: 'verify_note',
                              path: selectedNote.path,
                            });
                            setVerifyingFresh(false);
                            await Promise.all([openNote(selectedNote.path, false, true), loadTreeAndFacets(false)]);
                            if (r.ok && (r.audit_summary || (r.flagged_claims && r.flagged_claims.length > 0))) {
                              setFreshAudit({
                                path: selectedNote.path,
                                summary: r.audit_summary || r.summary || 'Verified note freshness.',
                                flagged: r.flagged_claims || [],
                              });
                            }
                            onToast(r.ok === false ? r.error || 'Could not verify note.' : r.summary || 'Verified note freshness.');
                          }}
                        >
                          <CheckCircle2 size={12} /> {verifyingFresh ? 'Auditing...' : 'Verify Fresh'}
                        </button>
                      )}
                      <button type="button" className="btn btn-sm" onClick={() => void inspectNoteDiff()}>
                        <FileDiff size={12} /> Diff
                      </button>
                      {!isArchivedNote && (
                        <>
                          <button type="button" className="btn btn-sm" onClick={() => setEditing((v) => !v)}>
                            {editing ? <Eye size={12} /> : <Edit3 size={12} />}
                            {editing ? 'Preview' : 'Edit'}
                          </button>
                          <button type="button" className="btn btn-danger btn-sm" title="Move note to recoverable Trash" onClick={() => void trashSelectedNote()}>
                            <Trash2 size={12} />
                          </button>
                        </>
                      )}
                    </div>
                  </div>

                  {!editing && (
                    <WikiNoteFrontmatterBar
                      note={selectedNote}
                      expanded={showFrontmatterTable}
                      onToggleExpanded={() => setShowFrontmatterTable((v) => !v)}
                      onApplyToken={applyFilterToken}
                    />
                  )}

                  {graduatingPath === selectedNote.path && selectedNote.path.startsWith('01_inbox/') && (
                    <WikiGraduateModal
                      key={graduatingPath}
                      inboxPath={graduatingPath}
                      onCancel={() => setGraduatingPath('')}
                      onCommitted={async (gradPath, msg) => {
                        setGraduatingPath('');
                        await loadTreeAndFacets(false);
                        await openNote(gradPath, false, true);
                        onToast(msg);
                      }}
                    />
                  )}

                  {freshAudit && freshAudit.path === selectedNote.path && (
                    <div
                      data-testid="wiki-freshness-audit-banner"
                      style={{
                        marginBottom: 14,
                        padding: '9px 12px',
                        borderRadius: 'var(--radius-sm)',
                        background: 'var(--bg-elevated)',
                        border: `1px solid ${freshAudit.flagged.length > 0 ? 'var(--status-warn)' : 'var(--border-hairline)'}`,
                        fontSize: 12.5,
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                        <strong>AI Freshness Audit</strong>
                        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setFreshAudit(null)}>
                          Dismiss
                        </button>
                      </div>
                      <div style={{ marginTop: 4, color: 'var(--text-secondary)' }}>{freshAudit.summary}</div>
                      {freshAudit.flagged.length > 0 && (
                        <ul style={{ margin: '6px 0 0 18px', padding: 0, color: 'var(--status-warn)' }}>
                          {freshAudit.flagged.map((fc, i) => (
                            <li key={i}>{fc}</li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}

                  {diff && (
                    <div style={{ marginBottom: 16 }}>
                      <DiffView diff={diff} onClose={() => setDiff(null)} />
                    </div>
                  )}

                  {editing ? (
                    <div style={{ display: 'grid', gap: 10 }}>
                      <div style={{ fontSize: 11.5, color: 'var(--text-muted)' }}>
                        Editing full Markdown + YAML frontmatter (<span className="mono">--- ... ---</span>)
                      </div>
                      <textarea className="textarea mono" rows={20} value={editBody} onChange={(e) => setEditBody(e.target.value)} />
                      <div style={{ display: 'flex', gap: 8 }}>
                        <button type="button" className="btn btn-primary" onClick={() => void saveEdit()}>
                          Save Note & Frontmatter
                        </button>
                        <button
                          type="button"
                          className="btn"
                          onClick={() => {
                            setEditing(false);
                            setEditBody(selectedNote.raw || selectedNote.body || '');
                          }}
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="md-body">
                      <MarkdownView
                        content={selectedNote.body || selectedNote.raw}
                        onOpenWikiNote={(target) => void openNote(target, true)}
                        onFilterTag={(tag) => applyFilterToken(`tag:${tag.replace(/^#/, '')}`, true)}
                      />
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <div className="chat-empty-hero">
                <FolderTree size={26} style={{ color: 'var(--text-muted)', marginBottom: 6 }} />
                <div style={{ color: 'var(--text-muted)' }}>
                  Select a file from the directory tree or search YAML frontmatter above.
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

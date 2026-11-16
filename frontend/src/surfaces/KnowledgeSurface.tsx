import React, { useEffect, useState } from 'react';
import {
  ArrowLeft,
  Edit3,
  Eye,
  FileDiff,
  GraduationCap,
  Network,
  Play,
  Plus,
  Search,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { callAction } from '../api';
import { DiffView } from '../components/DiffView';
import { FlashcardDeckWidget } from '../components/FlashcardDeckWidget';
import { MarkdownView } from '../components/MarkdownView';
import { MindMapCanvas } from '../components/MindMapCanvas';
import type {
  EducationCourse,
  FlashcardDeckPayload,
  MindMapGraph,
  WikiDiffPayload,
  WikiNoteDetail,
  WikiNoteSummary,
} from '../types';

interface KnowledgeSurfaceProps {
  initialNotePath?: string;
  initialMode?: 'notes' | 'mindmap' | 'study';
  onRefreshStatus: () => Promise<void>;
  onToast: (msg: string) => void;
}

export const KnowledgeSurface: React.FC<KnowledgeSurfaceProps> = ({
  initialNotePath = '',
  initialMode = 'notes',
  onRefreshStatus,
  onToast,
}) => {
  const [mode, setMode] = useState<'notes' | 'mindmap' | 'study'>(initialMode);
  const [mobilePane, setMobilePane] = useState<'list' | 'reading'>(initialNotePath ? 'reading' : 'list');
  const [folder, setFolder] = useState<string>('');
  const [query, setQuery] = useState('');
  const [notes, setNotes] = useState<WikiNoteSummary[]>([]);
  const [selectedNote, setSelectedNote] = useState<WikiNoteDetail | null>(null);
  const [editing, setEditing] = useState(false);
  const [editBody, setEditBody] = useState('');
  const [diff, setDiff] = useState<WikiDiffPayload | null>(null);
  const [graph, setGraph] = useState<MindMapGraph | null>(null);

  // Quick capture
  const [showCapture, setShowCapture] = useState(false);
  const [capTitle, setCapTitle] = useState('');
  const [capBody, setCapBody] = useState('');

  // Education & Spaced Repetition
  const [courses, setCourses] = useState<EducationCourse[]>([]);
  const [studyDeck, setStudyDeck] = useState<FlashcardDeckPayload | null>(null);
  const [newCourseTitle, setNewCourseTitle] = useState('');

  const loadNotes = async (overrideQuery?: string) => {
    const q = (overrideQuery !== undefined ? overrideQuery : query).trim();
    const res = q
      ? await callAction<{ hits?: WikiNoteSummary[]; results?: WikiNoteSummary[]; notes?: WikiNoteSummary[] }>('wiki', {
          subaction: 'search',
          query: q,
        })
      : await callAction<{ hits?: WikiNoteSummary[]; results?: WikiNoteSummary[]; notes?: WikiNoteSummary[] }>('wiki', {
          subaction: 'list',
          folder,
        });
    const list = res.hits || res.notes || res.results || [];
    setNotes(list);
    if (!selectedNote && list.length > 0) {
      void openNote(initialNotePath || list[0].rel_path, false);
    }
  };

  const openNote = async (path: string, switchMobile = true) => {
    if (!path) return;
    setDiff(null);
    setEditing(false);
    if (switchMobile) setMobilePane('reading');
    const res = await callAction<WikiNoteDetail>('wiki', { subaction: 'read', path });
    if (res.ok) {
      const detail = res as unknown as WikiNoteDetail;
      setSelectedNote(detail);
      setEditBody(detail.body || detail.raw || '');
    }
  };

  const loadStudyState = async (courseId = '') => {
    const prog = await callAction<{ courses?: EducationCourse[]; active_course?: EducationCourse }>('education', {
      subaction: 'progress',
    });
    const list = prog.courses || [];
    setCourses(list);
    const targetId = courseId || prog.active_course?.course_id || list[0]?.course_id || '';
    if (targetId) {
      const dRes = await callAction<{ ok?: boolean; deck?: FlashcardDeckPayload }>('education', {
        subaction: 'flashcard_deck',
        course_id: targetId,
        limit: 8,
      });
      setStudyDeck(dRes.ok && dRes.deck ? dRes.deck : null);
    } else {
      setStudyDeck(null);
    }
  };

  useEffect(() => {
    setMode(initialMode);
  }, [initialMode]);

  useEffect(() => {
    void loadNotes();
  }, [folder]);

  useEffect(() => {
    if (initialNotePath) {
      setMode('notes');
      void openNote(initialNotePath, true);
    }
  }, [initialNotePath]);

  useEffect(() => {
    if (mode === 'mindmap') {
      void callAction<MindMapGraph>('wiki', { subaction: 'graph' }).then((res) => {
        if (res.ok) setGraph(res as unknown as MindMapGraph);
      });
    } else if (mode === 'study') {
      void loadStudyState();
    }
  }, [mode]);

  const saveEdit = async () => {
    if (!selectedNote) return;
    const res = await callAction('wiki', {
      subaction: 'save',
      path: selectedNote.path,
      title: selectedNote.title,
      content: editBody,
      expected_hash: selectedNote.content_hash,
      confirmed: true,
    });
    if (res.ok) {
      setEditing(false);
      await openNote(selectedNote.path, false);
      onToast(res.summary || 'Saved Wiki note.');
    } else {
      onToast(res.error || 'Could not save note.');
    }
  };

  const trashSelectedNote = async () => {
    if (!selectedNote) return;
    const res = await callAction('wiki', {
      subaction: 'trash',
      path: selectedNote.path,
      confirmed: true,
    });
    if (res.ok) {
      setSelectedNote(null);
      setMobilePane('list');
      await loadNotes();
      onToast(res.summary || 'Moved note to recoverable Trash.');
    } else {
      onToast(res.error || 'Could not trash note.');
    }
  };

  const createQuickCourse = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newCourseTitle.trim()) return;
    const res = await callAction('education', {
      subaction: 'save_course',
      title: newCourseTitle.trim(),
      set_active: true,
    });
    if (res.ok) {
      setNewCourseTitle('');
      await loadStudyState();
      await onRefreshStatus();
      onToast(res.summary || 'Created study course.');
    }
  };

  const inspectNoteDiff = async () => {
    if (!selectedNote) return;
    const res = await callAction<WikiDiffPayload>('wiki', {
      subaction: 'diff',
      path: selectedNote.path,
    });
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
      await loadNotes();
      if (res.path) await openNote(res.path);
      onToast('Captured note to 01_inbox/.');
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }} data-testid="knowledge-surface">
      <div className="wiki-note-toolbar">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <div className="segmented-pills">
            <button
              type="button"
              className={`segmented-pill ${mode === 'notes' ? 'active' : ''}`}
              onClick={() => setMode('notes')}
            >
              Notes
            </button>
            <button
              type="button"
              className={`segmented-pill ${mode === 'mindmap' ? 'active' : ''}`}
              onClick={() => setMode('mindmap')}
            >
              <Network size={12} style={{ marginRight: 4 }} /> Mind Map
            </button>
            <button
              type="button"
              className={`segmented-pill ${mode === 'study' ? 'active' : ''}`}
              onClick={() => setMode('study')}
            >
              <GraduationCap size={12} style={{ marginRight: 4 }} /> Study
            </button>
          </div>

          {mode === 'notes' && (
            <select
              className="select"
              style={{ width: 'auto' }}
              value={folder}
              onChange={(e) => setFolder(e.target.value)}
            >
              <option value="">All Folders</option>
              <option value="01_inbox">01_inbox (Staged)</option>
              <option value="02_knowledge">02_knowledge (Curated)</option>
              <option value="03_resources">03_resources</option>
              <option value="04_archive">04_archive</option>
            </select>
          )}
        </div>

        {mode === 'notes' && (
          <div style={{ display: 'inline-flex', gap: 8, alignItems: 'center' }}>
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              <input
                className="input"
                style={{ width: 190, padding: '5px 9px' }}
                placeholder="Search Wiki & ontology..."
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void loadNotes();
                }}
              />
              <button type="button" className="icon-btn" onClick={() => void loadNotes()} aria-label="Search wiki">
                <Search size={14} />
              </button>
            </div>
            <button type="button" className="btn btn-primary btn-sm" onClick={() => setShowCapture((v) => !v)}>
              <Plus size={12} /> Capture Note
            </button>
          </div>
        )}
      </div>

      {showCapture && mode === 'notes' && (
        <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--border-hairline)', background: 'var(--bg-panel)' }}>
          <div style={{ maxWidth: 680, display: 'grid', gap: 8 }}>
            <input className="input" placeholder="Note title..." value={capTitle} onChange={(e) => setCapTitle(e.target.value)} />
            <textarea className="textarea" rows={3} placeholder="Markdown content (supports [[wikilinks]])..." value={capBody} onChange={(e) => setCapBody(e.target.value)} />
            <div>
              <button type="button" className="btn btn-primary btn-sm" onClick={() => void captureInbox()}>
                Save to Inbox
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

      {mode === 'study' && (
        <div className="surface-scroll">
          <div className="surface-container" style={{ maxWidth: 780 }}>
            <form onSubmit={(e) => void createQuickCourse(e)} style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
              <input
                className="input"
                placeholder="Start a new study course (e.g. Rust Ownership, ZFS Storage)..."
                value={newCourseTitle}
                onChange={(e) => setNewCourseTitle(e.target.value)}
              />
              <button type="submit" className="btn btn-primary btn-sm" style={{ flexShrink: 0 }}>
                <Plus size={12} /> Add Course
              </button>
            </form>

            {studyDeck && studyDeck.cards.length > 0 && (
              <FlashcardDeckWidget
                deck={studyDeck}
                onGraded={() => {
                  void loadStudyState(studyDeck.course_id);
                  void onRefreshStatus();
                }}
              />
            )}
            <div className="card-group" style={{ marginTop: 14 }}>
              {courses.length === 0 ? (
                <div className="card-row">
                  <span style={{ color: 'var(--text-muted)' }}>
                    No active courses yet — add a topic above or ask Companion in Chat to build a course with flashcards!
                  </span>
                </div>
              ) : (
                courses.map((c) => (
                  <div key={c.course_id} className="card-row">
                    <div className="card-row-info">
                      <div className="card-row-title">{c.title}</div>
                      <div className="card-row-desc">
                        {c.mastered_modules}/{c.total_modules} modules • {c.total_items} card(s) ({c.due_count} due)
                      </div>
                    </div>
                    <div style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
                      <span className="badge accent">{c.mastery_pct}% mastery</span>
                      {c.total_items > 0 && (
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => void loadStudyState(c.course_id)}
                        >
                          <Play size={11} /> Deck
                        </button>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      )}

      {mode === 'notes' && (
        <div className={`wiki-shell mobile-${mobilePane}`}>
          <div className="wiki-sidebar">
            <div className="wiki-note-list">
              {notes.length === 0 ? (
                <div style={{ padding: 14, fontSize: 12.5, color: 'var(--text-muted)' }}>
                  No matching notes found.
                </div>
              ) : (
                notes.map((n) => (
                  <button
                    key={n.rel_path}
                    type="button"
                    className={`wiki-note-item ${selectedNote?.path === n.rel_path ? 'active' : ''}`}
                    onClick={() => void openNote(n.rel_path, true)}
                  >
                    <div style={{ fontWeight: 500, fontSize: 13 }}>{n.title}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                      {n.folder} • {n.author}
                    </div>
                  </button>
                ))
              )}
            </div>
          </div>

          <div className="wiki-main">
            {selectedNote ? (
              <div style={{ flex: 1, overflowY: 'auto', padding: '18px 22px' }}>
                <div style={{ maxWidth: 760, margin: '0 auto' }}>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm mobile-only-btn"
                    style={{ marginBottom: 10 }}
                    onClick={() => setMobilePane('list')}
                  >
                    <ArrowLeft size={13} /> All Notes ({notes.length})
                  </button>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
                    <div>
                      <h2 style={{ margin: 0, fontSize: 20 }}>{selectedNote.title}</h2>
                      <div style={{ fontSize: 11.5, color: 'var(--text-muted)', marginTop: 3 }}>
                        <span className="mono">{selectedNote.path}</span> • Author: {selectedNote.author}
                      </div>
                    </div>
                    <div style={{ display: 'inline-flex', gap: 6, flexWrap: 'wrap' }}>
                      {selectedNote.path.startsWith('01_inbox/') && (
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={async () => {
                            const r = await callAction('wiki', {
                              subaction: 'graduate',
                              path: selectedNote.path,
                              domain: selectedNote.domain || 'general',
                              confirmed: true,
                            });
                            await loadNotes();
                            onToast(r.summary || r.error || 'Graduated note.');
                          }}
                        >
                          <Sparkles size={12} /> Graduate
                        </button>
                      )}
                      <button type="button" className="btn btn-sm" onClick={() => void inspectNoteDiff()}>
                        <FileDiff size={12} /> Diff
                      </button>
                      <button type="button" className="btn btn-sm" onClick={() => setEditing((v) => !v)}>
                        {editing ? <Eye size={12} /> : <Edit3 size={12} />}
                        {editing ? 'Preview' : 'Edit'}
                      </button>
                      <button
                        type="button"
                        className="btn btn-danger btn-sm"
                        title="Move note to recoverable Trash"
                        onClick={() => void trashSelectedNote()}
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  </div>

                  {diff && (
                    <div style={{ marginBottom: 16 }}>
                      <DiffView diff={diff} onClose={() => setDiff(null)} />
                    </div>
                  )}

                  {editing ? (
                    <div style={{ display: 'grid', gap: 10 }}>
                      <textarea
                        className="textarea mono"
                        rows={18}
                        value={editBody}
                        onChange={(e) => setEditBody(e.target.value)}
                      />
                      <div>
                        <button type="button" className="btn btn-primary" onClick={() => void saveEdit()}>
                          Save Note
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="md-body">
                      <MarkdownView
                        content={selectedNote.body || selectedNote.raw}
                        onOpenWikiNote={(target) => void openNote(target, true)}
                        onFilterTag={(tag) => {
                          setQuery(tag);
                          setMobilePane('list');
                          void loadNotes(tag);
                        }}
                      />
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <div className="chat-empty-hero">
                <div style={{ color: 'var(--text-muted)' }}>Select a note from the sidebar to read.</div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

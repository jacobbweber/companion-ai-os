import React, { useEffect, useRef, useState } from 'react';
import {
  Archive,
  BookmarkPlus,
  CheckCircle2,
  Cpu,
  FileOutput,
  FileText,
  GraduationCap,
  Image as ImageIcon,
  Layers,
  Paperclip,
  Search,
  Send,
  Shield,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import { apiPost, callAction, fetchHistory, subscribeJobStream, uploadAttachment } from '../api';
import { MarkdownView } from '../components/MarkdownView';
import type {
  ArchiveSearchHit,
  ChatAttachment,
  ChatMessage,
  EducationCourse,
  PromptTemplate,
  StudioId,
  WebStatus,
} from '../types';

const ACCEPTED_FILE_TYPES =
  'image/png,image/jpeg,image/webp,image/gif,.pdf,.md,.markdown,.txt,.docx,.xlsx,.pptx,.csv,.tsv,.json,.yaml,.yml,.toml,.xml,.html,.htm,.log,.py,.ts,.tsx,.js,.jsx,.css,.sql,.sh,.ps1,.c,.cpp,.h,.rs,.go,.java';

interface ChatStudioProps {
  status: WebStatus | null;
  onRefreshStatus: () => void;
  onOpenWikiNote: (notePath: string) => void;
  onNavigateStudio: (studio: StudioId) => void;
  onOpenSystemTab?: (tab: 'identity' | 'models' | 'autonomy' | 'vault' | 'federation' | 'ops') => void;
  onToast: (msg: string) => void;
}

export const ChatStudio: React.FC<ChatStudioProps> = ({
  status,
  onRefreshStatus,
  onOpenWikiNote,
  onNavigateStudio,
  onOpenSystemTab,
  onToast,
}) => {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [planFirst, setPlanFirst] = useState(false);
  const [sending, setSending] = useState(false);
  const [stagedAttachments, setStagedAttachments] = useState<ChatAttachment[]>([]);
  const [uploadingCount, setUploadingCount] = useState(0);
  const [dragActive, setDragActive] = useState(false);
  const [stagedTurns, setStagedTurns] = useState<Record<string, string>>({});

  // Archive search, Saved Prompts & Study/Mastery drawers
  const [showArchive, setShowArchive] = useState(false);
  const [archiveQuery, setArchiveQuery] = useState('');
  const [archiveHits, setArchiveHits] = useState<ArchiveSearchHit[]>([]);
  const [showPrompts, setShowPrompts] = useState(false);
  const [prompts, setPrompts] = useState<PromptTemplate[]>([]);
  const [newPromptName, setNewPromptName] = useState('');
  const [newPromptText, setNewPromptText] = useState('');
  const [showStudy, setShowStudy] = useState(false);
  const [courses, setCourses] = useState<EducationCourse[]>([]);
  const [dueTotal, setDueTotal] = useState(0);
  const [courseTitle, setCourseTitle] = useState('');
  const [courseDomain, setCourseDomain] = useState('engineering');
  const [courseModulesRaw, setCourseModulesRaw] = useState('');
  const [cardPrompt, setCardPrompt] = useState('');
  const [cardAnswer, setCardAnswer] = useState('');

  const streamEndRef = useRef<HTMLDivElement | null>(null);
  const unsubRef = useRef<(() => void) | null>(null);
  const attachedJobIdRef = useRef<string>('');
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const dragDepthRef = useRef(0);

  const loadHistory = async () => {
    try {
      const res = await fetchHistory(100);
      if (res.ok) setMessages(res.messages || []);
    } catch {
      // ignore transient network error
    }
  };

  const stageTurnToInbox = async (idx: number) => {
    const msg = messages[idx];
    if (!msg || !msg.content.trim()) return;
    let userPrompt = '';
    for (let k = idx - 1; k >= 0; k--) {
      if (messages[k].role === 'user') {
        userPrompt = messages[k].content;
        break;
      }
    }
    const res = await callAction<{ path?: string; template?: string }>('wiki', {
      subaction: 'stage_turn',
      content: msg.content,
      user_prompt: userPrompt,
    });
    if (res.ok && res.path) {
      setStagedTurns((prev) => ({ ...prev, [String(msg.id)]: res.path! }));
      onToast(`Staged turn into ${res.path} (template: ${res.template || 'inbox'}).`);
    } else {
      onToast(res.error || 'Could not stage turn to Wiki Inbox.');
    }
  };

  const loadPrompts = async () => {
    const res = await callAction<{ prompts?: PromptTemplate[] }>('prompts', { subaction: 'list' });
    if (res.prompts) setPrompts(res.prompts);
  };

  const loadEducation = async () => {
    const res = await callAction<{ courses?: EducationCourse[]; due_total?: number }>('education', {
      subaction: 'progress',
    });
    if (Array.isArray(res.courses)) setCourses(res.courses);
    if (typeof res.due_total === 'number') setDueTotal(res.due_total);
  };

  const attachStreamToMessage = (jobId: string, targetMsgId: string) => {
    attachedJobIdRef.current = jobId;
    unsubRef.current?.();
    unsubRef.current = subscribeJobStream(jobId, {
      onTextDelta: (delta) => {
        setMessages((prev) =>
          prev.map((m) => (m.id === targetMsgId ? { ...m, content: m.content + delta } : m))
        );
      },
      onThinkingDelta: (delta) => {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === targetMsgId ? { ...m, thinking: (m.thinking || '') + delta } : m
          )
        );
      },
      onPhase: (phase) => {
        setMessages((prev) =>
          prev.map((m) => (m.id === targetMsgId ? { ...m, activePhase: phase } : m))
        );
      },
      onTool: (tool, active) => {
        setMessages((prev) =>
          prev.map((m) => (m.id === targetMsgId ? { ...m, activeTool: active ? tool : '' } : m))
        );
      },
      onDone: (done) => {
        attachedJobIdRef.current = '';
        setMessages((prev) =>
          prev.map((m) =>
            m.id === targetMsgId
              ? {
                  ...m,
                  content: done.response || m.content || done.error || '(Completed)',
                  streaming: false,
                  activeTool: '',
                }
              : m
          )
        );
        setSending(false);
        onRefreshStatus();
      },
      onError: () => {
        attachedJobIdRef.current = '';
        setSending(false);
        void loadHistory();
        onRefreshStatus();
      },
    });
  };

  useEffect(() => {
    void loadHistory();
    void loadPrompts();
    void loadEducation();
    return () => {
      unsubRef.current?.();
    };
  }, []);

  useEffect(() => {
    const activeJob = status?.active_job;
    if (
      !activeJob ||
      activeJob.status !== 'running' ||
      !activeJob.job_id ||
      attachedJobIdRef.current === activeJob.job_id
    ) {
      return;
    }
    const streamId = `reattach_${activeJob.job_id}`;
    setSending(true);
    setMessages((prev) => {
      if (prev.some((m) => m.id === streamId)) return prev;
      const next = [...prev];
      const last = next[next.length - 1];
      if (activeJob.message && (!last || last.role !== 'user' || last.content !== activeJob.message)) {
        next.push({
          id: `u_${activeJob.job_id}`,
          role: 'user',
          content: activeJob.message,
          timestamp: activeJob.created_at || new Date().toISOString(),
        });
      }
      next.push({
        id: streamId,
        role: 'assistant',
        content: '',
        thinking: '',
        activePhase: activeJob.active_phase || '',
        activeTool: activeJob.active_tool || '',
        timestamp: new Date().toISOString(),
        streaming: true,
      });
      return next;
    });
    attachStreamToMessage(activeJob.job_id, streamId);
  }, [status?.active_job?.job_id, status?.active_job?.status]);

  useEffect(() => {
    streamEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length]);

  const ingestFiles = async (incoming: FileList | File[]) => {
    const list = Array.from(incoming || []).slice(0, 8);
    if (list.length === 0) return;
    setUploadingCount((c) => c + list.length);
    for (const file of list) {
      try {
        const res = await uploadAttachment(file);
        if (res.ok && res.attachment) {
          const att = res.attachment;
          setStagedAttachments((prev) =>
            prev.some((x) => x.id === att.id) ? prev : [...prev, att].slice(-8)
          );
          if (att.wiki_resource_path) {
            onToast(`Attached ${att.filename} • Auto-filed full text to Wiki (${att.wiki_resource_path})`);
          } else if (att.kind === 'image') {
            onToast(`Attached ${att.filename} (${att.meta_label}) • Vision routing ready`);
          }
        } else {
          onToast(res.error || `Could not attach ${file.name}`);
        }
      } catch (err) {
        onToast(`Upload error (${file.name}): ${String(err)}`);
      } finally {
        setUploadingCount((c) => Math.max(0, c - 1));
      }
    }
  };

  const removeStagedAttachment = (id: string) => {
    setStagedAttachments((prev) => prev.filter((a) => a.id !== id));
  };

  const formatOptimisticDisplay = (rawText: string, atts: ChatAttachment[]): string => {
    const base =
      rawText ||
      (atts.length > 0
        ? 'Please inspect the attached file(s) and summarize or extract the key details.'
        : '');
    if (atts.length === 0) return base;
    const badges = atts.map((a) => {
      if (a.kind === 'image') return `![${a.filename}](${a.raw_url})`;
      const wikiTag = a.wiki_resource_path
        ? ` • Wiki: [[${a.wiki_resource_path.replace(/\.md$/i, '')}]]`
        : '';
      return `[📎 ${a.filename} (${a.meta_label})](${a.raw_url})${wikiTag}`;
    });
    return `${base}\n\n${badges.join('\n\n')}`.trim();
  };

  const sendMessage = async (overrideMsg?: string) => {
    const text = (overrideMsg ?? input).trim();
    const activeAttachments = overrideMsg ? [] : stagedAttachments;
    if ((!text && activeAttachments.length === 0) || sending || uploadingCount > 0) return;
    if (!overrideMsg) {
      setInput('');
      setStagedAttachments([]);
    }
    setSending(true);

    const displayText = formatOptimisticDisplay(text, activeAttachments);
    const attachmentIds = activeAttachments.map((a) => a.id);

    const userMsg: ChatMessage = {
      id: `u_${Date.now()}`,
      role: 'user',
      content: displayText,
      timestamp: new Date().toISOString(),
    };
    const streamId = `a_${Date.now()}`;
    const assistantPlaceholder: ChatMessage = {
      id: streamId,
      role: 'assistant',
      content: '',
      thinking: '',
      timestamp: new Date().toISOString(),
      streaming: true,
    };

    setMessages((prev) => [...prev, userMsg, assistantPlaceholder]);

    try {
      const mode = text.startsWith('/') ? 'chat' : planFirst ? 'plan' : 'chat';
      const sub = await apiPost<{
        ok: boolean;
        job_id?: string;
        response?: string;
        error?: string;
      }>('/api/jobs', {
        message: text,
        mode,
        async: true,
        ...(attachmentIds.length > 0 ? { attachment_ids: attachmentIds } : {}),
      });

      if (!sub.ok) {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === streamId
              ? { ...m, content: `Error: ${sub.error || 'Request rejected'}`, streaming: false }
              : m
          )
        );
        setSending(false);
        return;
      }

      // Immediate synchronous slash response
      if (!sub.job_id && typeof sub.response === 'string') {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === streamId ? { ...m, content: sub.response || '', streaming: false } : m
          )
        );
        setSending(false);
        onRefreshStatus();
        return;
      }

      if (sub.job_id) {
        attachStreamToMessage(sub.job_id, streamId);
      }
    } catch (err) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === streamId ? { ...m, content: `Network error: ${String(err)}`, streaming: false } : m
        )
      );
      setSending(false);
    }
  };

  const runArchiveSearch = async () => {
    const res = await callAction<{ hits?: ArchiveSearchHit[] }>('archive', {
      subaction: 'search',
      query: archiveQuery,
      limit: 20,
    });
    setArchiveHits(res.hits || []);
  };

  const runColdArchiveSweep = async () => {
    const res = await callAction('archive', { subaction: 'run', older_than_days: 90 });
    onToast(res.summary || 'Cold archive sweep completed.');
    if (archiveQuery) void runArchiveSearch();
  };

  const savePrompt = async () => {
    if (!newPromptName.trim() || !newPromptText.trim()) return;
    const res = await callAction('prompts', {
      subaction: 'save',
      name: newPromptName.trim(),
      template: newPromptText.trim(),
      category: 'studio',
    });
    if (res.ok) {
      setNewPromptName('');
      setNewPromptText('');
      void loadPrompts();
      onToast(`Saved prompt template "${newPromptName.trim()}".`);
    } else {
      onToast(res.error || 'Could not save prompt.');
    }
  };

  const exportChatToWiki = async () => {
    const res = await callAction<{ path?: string }>('wiki', {
      subaction: 'export_chat',
      title: `Session Export ${new Date().toISOString().slice(0, 10)}`,
      limit: 16,
    });
    if (res.ok && res.path) {
      onToast(`Exported conversation to Wiki Inbox: ${res.path}`);
      onOpenWikiNote(res.path);
    } else {
      onToast(res.error || 'No turns to export yet.');
    }
  };

  const changeTrustMode = async (mode: string) => {
    const res = await callAction('trust', { subaction: 'set', mode });
    if (res.ok) {
      onRefreshStatus();
      onToast(`Trust Mode set to ${mode.toUpperCase()}.`);
    }
  };

  const changeTier = async (tier: string) => {
    await apiPost('/api/settings', { tier });
    onRefreshStatus();
    onToast(`Model routing switched to ${tier.toUpperCase()}.`);
  };

  const pendingApprovals = status?.pending_approvals || [];
  const activeCourse = courses.find((c) => c.active) || courses[0] || null;
  const dueBadgeCount = status?.education_summary?.due_total ?? dueTotal;

  const resolveChatApproval = async (approvalId: string, decision: 'approve' | 'deny') => {
    const res = await callAction('approvals', {
      subaction: decision,
      approval_id: approvalId,
    });
    onRefreshStatus();
    onToast(res.summary || res.error || `Marked ${approvalId} as ${decision}.`);
  };

  const createStudyCourse = async () => {
    if (!courseTitle.trim()) return;
    const mods = courseModulesRaw
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    const res = await callAction('education', {
      subaction: 'save_course',
      title: courseTitle.trim(),
      domain: courseDomain.trim() || 'general',
      modules: mods.length > 0 ? mods : undefined,
      set_active: true,
    });
    if (res.ok) {
      setCourseTitle('');
      setCourseModulesRaw('');
      await loadEducation();
      onRefreshStatus();
      onToast(res.summary || 'Created study course.');
    } else {
      onToast(res.error || 'Could not create course.');
    }
  };

  const switchActiveCourse = async (cid: string) => {
    const res = await callAction('education', { subaction: 'set_active', course_id: cid });
    if (res.ok) {
      await loadEducation();
      onRefreshStatus();
    }
  };

  const addQuickFlashcard = async () => {
    if (!activeCourse || !cardPrompt.trim() || !cardAnswer.trim()) return;
    const res = await callAction('education', {
      subaction: 'upsert_items',
      course_id: activeCourse.course_id,
      items: [
        {
          prompt: cardPrompt.trim(),
          answer: cardAnswer.trim(),
          topic: activeCourse.current_module?.title || activeCourse.title,
        },
      ],
    });
    if (res.ok) {
      setCardPrompt('');
      setCardAnswer('');
      await loadEducation();
      onRefreshStatus();
      onToast(res.summary || 'Saved flashcard.');
    } else {
      onToast(res.error || 'Could not save flashcard.');
    }
  };

  const updateModuleStage = async (moduleSlug: string, stage: string) => {
    if (!activeCourse) return;
    const res = await callAction('education', {
      subaction: 'advance_stage',
      course_id: activeCourse.course_id,
      module_slug: moduleSlug,
      stage,
    });
    if (res.ok) {
      await loadEducation();
      onRefreshStatus();
    }
  };

  const dealFlashcardsToChat = async () => {
    if (!activeCourse) return;
    const res = await callAction<{ markdown_block?: string; chat_message?: ChatMessage }>('education', {
      subaction: 'flashcard_deck',
      course_id: activeCourse.course_id,
      limit: 8,
      persist_to_chat: true,
    });
    if (res.ok && res.markdown_block) {
      const cardMsg: ChatMessage = res.chat_message || {
        id: `fc_${Date.now()}`,
        role: 'assistant',
        content: `Here is your interactive active-recall deck for **${activeCourse.title}**:\n\n${res.markdown_block}`,
        timestamp: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, cardMsg]);
      setShowStudy(false);
    } else {
      setShowStudy(false);
      void sendMessage(
        `Create 5 high-signal flashcards for my active course "${activeCourse.title}" (${activeCourse.current_module?.title || 'current module'}) and deal an interactive flashcard deck right here in chat.`
      );
    }
  };

  return (
    <div
      className={`chat-layout ${dragActive ? 'drag-active' : ''}`}
      onDragEnter={(e) => {
        if (e.dataTransfer?.types?.includes('Files')) {
          e.preventDefault();
          dragDepthRef.current += 1;
          setDragActive(true);
        }
      }}
      onDragOver={(e) => {
        if (e.dataTransfer?.types?.includes('Files')) {
          e.preventDefault();
        }
      }}
      onDragLeave={(e) => {
        if (e.dataTransfer?.types?.includes('Files')) {
          dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
          if (dragDepthRef.current === 0) setDragActive(false);
        }
      }}
      onDrop={(e) => {
        if (e.dataTransfer?.files && e.dataTransfer.files.length > 0) {
          e.preventDefault();
          dragDepthRef.current = 0;
          setDragActive(false);
          void ingestFiles(e.dataTransfer.files);
        }
      }}
    >
      {dragActive && (
        <div className="chat-drop-overlay" aria-hidden="true">
          <div className="chat-drop-box">
            <Paperclip size={22} />
            <strong>DROP IMAGES OR DOCUMENTS TO ATTACH</strong>
            <span className="mono">
              PNG • JPG • WEBP • GIF • PDF • DOCX • XLSX • PPTX • MD • CSV • JSON • CODE
            </span>
          </div>
        </div>
      )}
      <div className="chat-top-zone">
        <div className="studio-header">
          <div className="studio-title-group">
            <h1>Forever Chat &amp; Live Stream Deck</h1>
            <p>
              Continuous hot/cold memory stream • Fast:{' '}
              <code className="mono">{status?.fast_model || 'qwen2.5:7b'}</code> • Reasoning:{' '}
              <code className="mono">{status?.reasoning_model || 'qwen2.5:7b'}</code>
            </p>
          </div>
          <div className="studio-actions">
            <button
              type="button"
              className={`btn ${showStudy ? 'btn-primary' : ''}`}
              onClick={() => {
                const next = !showStudy;
                setShowStudy(next);
                if (next) void loadEducation();
              }}
            >
              <GraduationCap size={13} /> Study
              {dueBadgeCount > 0 ? ` (${dueBadgeCount} due)` : courses.length > 0 ? ` (${courses.length})` : ''}
            </button>
            <button
              type="button"
              className={`btn ${showArchive ? 'btn-primary' : ''}`}
              onClick={() => setShowArchive((v) => !v)}
            >
              <Archive size={13} /> Forever Archive
            </button>
            <button
              type="button"
              className={`btn ${showPrompts ? 'btn-primary' : ''}`}
              onClick={() => setShowPrompts((v) => !v)}
            >
              <BookmarkPlus size={13} /> Prompts ({prompts.length})
            </button>
            <button type="button" className="btn" onClick={() => void exportChatToWiki()}>
              <FileOutput size={13} /> Export to Wiki
            </button>
          </div>
        </div>

        {/* Blueprint Plan-First Approval Banner (also preserves contract ID) */}
        <div
          id="approval-banner"
          className="approval-strip"
          style={{ display: status?.awaiting_approval ? 'flex' : 'none' }}
        >
          <div>
            <strong className="mono">[BLUEPRINT GATE]</strong> Implementation plan is staged and
            awaiting your go-ahead.
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void sendMessage('/approve')}
            >
              <CheckCircle2 size={13} /> Approve &amp; Build
            </button>
          </div>
        </div>

        {/* Action-Only Pending Approval Banner (strictly shown when status='waiting') */}
        {pendingApprovals.length > 0 && (
          <div className="approval-strip" data-testid="chat-action-approval-banner">
            <div style={{ minWidth: 0, flex: 1 }}>
              <strong className="mono">[APPROVAL NEEDED]</strong>{' '}
              <span>
                {pendingApprovals[0].summary ||
                  `${pendingApprovals[0].category}:${pendingApprovals[0].action_name}`}
              </span>
              {pendingApprovals.length > 1 ? ` (+${pendingApprovals.length - 1} more)` : ''}
            </div>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() =>
                  void resolveChatApproval(pendingApprovals[0].approval_id, 'approve')
                }
              >
                <CheckCircle2 size={13} /> Approve
              </button>
              <button
                type="button"
                className="btn btn-danger"
                onClick={() => void resolveChatApproval(pendingApprovals[0].approval_id, 'deny')}
              >
                <X size={13} /> Deny
              </button>
              <button type="button" className="btn" onClick={() => onNavigateStudio('jobs')}>
                Review
              </button>
            </div>
          </div>
        )}

        {/* Conversational Education & Mastery Drawer */}
        {showStudy && (
          <div className="panel chat-drawer-panel" data-testid="chat-study-drawer">
            <div className="panel-head">
              <span className="panel-title">
                <GraduationCap size={13} /> Courses, Socratic Progression &amp; Spaced Repetition
              </span>
              {courses.length > 0 && (
                <select
                  aria-label="Active study course"
                  className="select"
                  value={activeCourse?.course_id || ''}
                  onChange={(e) => void switchActiveCourse(e.target.value)}
                >
                  {courses.map((c) => (
                    <option key={c.course_id} value={c.course_id}>
                      {c.title} ({c.mastery_pct}% • {c.due_count} due)
                    </option>
                  ))}
                </select>
              )}
            </div>

            <div className="grid-2">
              <div>
                {!activeCourse ? (
                  <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                    No active study courses yet. Create a course on the right or simply tell Companion
                    in chat what you want to learn!
                  </div>
                ) : (
                  <div>
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        gap: 8,
                        marginBottom: 8,
                        flexWrap: 'wrap',
                      }}
                    >
                      <div>
                        <strong>{activeCourse.title}</strong>{' '}
                        <span className="badge accent">{activeCourse.mastery_pct}% MASTERY</span>{' '}
                        <span className="badge warn">{activeCourse.due_count} DUE</span>
                      </div>
                    </div>

                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 10 }}>
                      <button
                        type="button"
                        className="btn btn-primary"
                        onClick={() => {
                          setShowStudy(false);
                          void sendMessage(
                            `Tutor me on the current module of "${activeCourse.title}" (${activeCourse.current_module?.title || 'Module 1'}) step by step.`
                          );
                        }}
                      >
                        <GraduationCap size={12} /> Tutor Current Module
                      </button>
                      <button
                        type="button"
                        className="btn"
                        onClick={() => {
                          setShowStudy(false);
                          void sendMessage(
                            `Quiz me conversationally on "${activeCourse.title}" (${activeCourse.current_module?.title || 'due concepts'}) and grade my answers.`
                          );
                        }}
                      >
                        Quiz Me in Chat
                      </button>
                      <button
                        type="button"
                        className="btn"
                        onClick={() => void dealFlashcardsToChat()}
                      >
                        <Layers size={12} /> Deal Flashcards Here
                      </button>
                    </div>

                    <div style={{ display: 'grid', gap: 5, marginBottom: 10 }}>
                      {activeCourse.modules.map((m, mi) => (
                        <div
                          key={m.slug || mi}
                          style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            gap: 8,
                            padding: '5px 8px',
                            border: '1px solid var(--border-hairline)',
                            background: 'var(--bg-elevated)',
                            fontSize: 12,
                          }}
                        >
                          <span>
                            <strong className="mono">{mi + 1}.</strong> {m.title}
                          </span>
                          <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
                            {m.wiki_path && (
                              <button
                                type="button"
                                className="md-wikilink"
                                onClick={() => onOpenWikiNote(m.wiki_path)}
                              >
                                Study Note
                              </button>
                            )}
                            <select
                              aria-label={`Stage for ${m.title}`}
                              className="select"
                              style={{ padding: '2px 6px', fontSize: 11 }}
                              value={m.stage}
                              onChange={(e) => void updateModuleStage(m.slug, e.target.value)}
                            >
                              <option value="priming">priming</option>
                              <option value="dual_coding">dual_coding</option>
                              <option value="elaboration">elaboration</option>
                              <option value="retrieval">retrieval</option>
                              <option value="mastered">mastered</option>
                            </select>
                          </span>
                        </div>
                      ))}
                    </div>

                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      <input
                        className="input"
                        style={{ flex: '1 1 140px' }}
                        placeholder="Quick flashcard prompt..."
                        value={cardPrompt}
                        onChange={(e) => setCardPrompt(e.target.value)}
                      />
                      <input
                        className="input"
                        style={{ flex: '1 1 140px' }}
                        placeholder="Concise answer..."
                        value={cardAnswer}
                        onChange={(e) => setCardAnswer(e.target.value)}
                      />
                      <button
                        type="button"
                        className="btn"
                        disabled={!cardPrompt.trim() || !cardAnswer.trim()}
                        onClick={() => void addQuickFlashcard()}
                      >
                        + Add Card
                      </button>
                    </div>
                  </div>
                )}
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                  CREATE NEW STUDY COURSE
                </div>
                <div style={{ display: 'flex', gap: 6 }}>
                  <input
                    className="input"
                    style={{ flex: 2 }}
                    placeholder="Course Title (e.g. Distributed Consensus)"
                    value={courseTitle}
                    onChange={(e) => setCourseTitle(e.target.value)}
                  />
                  <input
                    className="input"
                    style={{ flex: 1 }}
                    placeholder="Domain"
                    value={courseDomain}
                    onChange={(e) => setCourseDomain(e.target.value)}
                  />
                </div>
                <input
                  className="input"
                  placeholder="Syllabus modules (comma-separated, optional)"
                  value={courseModulesRaw}
                  onChange={(e) => setCourseModulesRaw(e.target.value)}
                />
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={!courseTitle.trim()}
                  onClick={() => void createStudyCourse()}
                >
                  Save Course
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Forever Hot + Cold Archive Drawer */}
        {showArchive && (
          <div className="panel chat-drawer-panel">
            <div className="panel-head">
              <span className="panel-title">
                <Search size={13} /> Unified Hot (&le;90d) + Cold (&gt;90d) Forever History Search
              </span>
              <button type="button" className="btn" onClick={() => void runColdArchiveSweep()}>
                Sweep &gt;90d to Cold Archive
              </button>
            </div>
            <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
              <input
                className="input"
                style={{ flex: 1 }}
                placeholder="Search across your entire conversation lifetime..."
                value={archiveQuery}
                onChange={(e) => setArchiveQuery(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && void runArchiveSearch()}
              />
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void runArchiveSearch()}
              >
                Search
              </button>
            </div>
            {archiveHits.length > 0 && (
              <div style={{ maxHeight: 190, overflowY: 'auto', display: 'grid', gap: 6 }}>
                {archiveHits.map((h, i) => (
                  <div
                    key={`${h.id}_${i}`}
                    style={{
                      padding: '6px 10px',
                      border: '1px solid var(--border-hairline)',
                      background: 'var(--bg-elevated)',
                      fontSize: 12,
                    }}
                  >
                    <span className={`badge ${h.tier === 'Cold Archive' ? 'warn' : 'ok'}`}>
                      {h.tier}
                    </span>{' '}
                    <span className="mono" style={{ color: 'var(--text-muted)' }}>
                      {h.timestamp} • {h.role}
                    </span>
                    <div style={{ marginTop: 4 }}>{h.content.slice(0, 260)}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Saved Prompts Library Drawer */}
        {showPrompts && (
          <div className="panel chat-drawer-panel">
            <div className="panel-head">
              <span className="panel-title">
                <Sparkles size={13} /> Saved Prompts Library
              </span>
            </div>
            <div className="grid-2">
              <div>
                {prompts.length === 0 ? (
                  <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                    No saved prompt templates yet. Add one on the right to insert with 1 click.
                  </div>
                ) : (
                  prompts.map((p) => (
                    <div
                      key={p.name}
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        padding: '6px 8px',
                        border: '1px solid var(--border-hairline)',
                        marginBottom: 5,
                      }}
                    >
                      <button
                        type="button"
                        className="btn btn-ghost"
                        style={{ border: 'none', padding: 0, textAlign: 'left' }}
                        onClick={() => {
                          setInput((cur) => (cur ? `${cur}\n${p.template}` : p.template));
                          setShowPrompts(false);
                        }}
                      >
                        <strong className="mono">{p.name}</strong> —{' '}
                        {p.template.slice(0, 65)}
                      </button>
                      <button
                        type="button"
                        className="btn btn-danger"
                        onClick={async () => {
                          await callAction('prompts', { subaction: 'delete', name: p.name });
                          void loadPrompts();
                        }}
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  ))
                )}
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <input
                  className="input"
                  placeholder="Template slug (e.g. weekly-system-audit)"
                  value={newPromptName}
                  onChange={(e) => setNewPromptName(e.target.value)}
                />
                <textarea
                  className="textarea"
                  placeholder="Reusable prompt text..."
                  value={newPromptText}
                  onChange={(e) => setNewPromptText(e.target.value)}
                />
                <button type="button" className="btn btn-primary" onClick={() => void savePrompt()}>
                  Save Template
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Main Message Stream */}
      <div className="chat-stream">
        <div className="chat-stream-inner">
          {status && status.onboarded === false && (
            <div
              className="panel"
              data-testid="first-boot-welcome-banner"
              style={{ maxWidth: 720, margin: '14px auto 0', borderColor: 'var(--accent-primary)' }}
            >
              <div className="panel-head">
                <span className="panel-title">Welcome to Companion AI OS</span>
                <span className="badge accent">QUICK SETUP</span>
              </div>
              <p style={{ color: 'var(--text-secondary)', margin: '0 0 10px', fontSize: 12.5 }}>
                Ready out of the box with local <strong>Ollama</strong> (<code>127.0.0.1:11434</code>
                ). Take 30 seconds anytime to personalize your Companion&apos;s name &amp; profile or
                connect local/cloud LLM models:
              </p>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() =>
                    onOpenSystemTab ? onOpenSystemTab('identity') : onNavigateStudio('system')
                  }
                >
                  Personalize Identity &amp; Profile
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() =>
                    onOpenSystemTab ? onOpenSystemTab('models') : onNavigateStudio('system')
                  }
                >
                  Configure Ollama / LLM Models
                </button>
              </div>
            </div>
          )}

          {messages.length === 0 && (
            <div className="panel" style={{ maxWidth: 720, margin: '20px auto' }}>
              <div className="panel-head">
                <span className="panel-title">Companion 3.2 Studio Ready</span>
                <span className="badge accent">FOREVER SESSION</span>
              </div>
              <p style={{ color: 'var(--text-secondary)', margin: '0 0 10px' }}>
                Chat continuously without context anxiety. Organize life &amp; software sprints on{' '}
                <strong>Boards</strong>, track appointments &amp; due reviews on{' '}
                <strong>Calendar</strong>, master any subject with Socratic tutoring &amp; inline
                flashcards, and curate lasting notes into your <strong>Knowledge Wiki</strong>.
              </p>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <button
                  type="button"
                  className="btn"
                  onClick={() => onNavigateStudio('boards')}
                >
                  Open Kanban Boards
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => onNavigateStudio('calendar')}
                >
                  Open Unified Calendar
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => setShowStudy(true)}
                >
                  Start a Study Course
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => onNavigateStudio('wiki')}
                >
                  Open Wiki &amp; 2D Mind Map
                </button>
              </div>
            </div>
          )}

          {messages.map((m, idx) => (
            <div key={m.id} className={`msg-card ${m.role}`}>
              <div className="msg-meta">
                <span>
                  {m.role === 'user'
                    ? status?.user_name || 'YOU'
                    : status?.companion_name || 'COMPANION'}
                </span>
                <span className="mono" style={{ display: 'inline-flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <span>
                    {m.activePhase ? `[${m.activePhase}] ` : ''}
                    {m.activeTool ? `⚙ ${m.activeTool} • ` : ''}
                    {m.streaming ? 'LIVE STREAMING' : (m.timestamp || '').slice(11, 19)}
                  </span>
                  {m.role === 'assistant' && !m.streaming && Boolean(m.content?.trim()) && (
                    stagedTurns[String(m.id)] ? (
                      <button
                        type="button"
                        className="msg-stage-btn staged"
                        onClick={() => onOpenWikiNote(stagedTurns[String(m.id)])}
                        title={`Open ${stagedTurns[String(m.id)]} in Wiki`}
                      >
                        <CheckCircle2 size={11} /> Open in Wiki
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="msg-stage-btn"
                        onClick={() => void stageTurnToInbox(idx)}
                        title="Distill and stage this single response into 01_inbox/"
                      >
                        <BookmarkPlus size={11} /> + Stage to Inbox
                      </button>
                    )
                  )}
                </span>
              </div>

              {m.thinking && (
                <details className="thinking-box" open={Boolean(m.streaming && !m.content)}>
                  <summary>Reasoning Trace ({m.thinking.length} chars)</summary>
                  <pre style={{ whiteSpace: 'pre-wrap', margin: '6px 0 0', fontSize: 11.5 }}>
                    {m.thinking}
                  </pre>
                </details>
              )}

              <MarkdownView
                content={m.content || (m.streaming ? '▍' : '')}
                onOpenWikiNote={onOpenWikiNote}
                onFlashcardGraded={() => {
                  void loadEducation();
                  onRefreshStatus();
                }}
              />
            </div>
          ))}
          <div ref={streamEndRef} />
        </div>
      </div>

      {/* Bottom Tactical Composer */}
      <div className="composer-dock">
        <div className="composer-inner">
          <div className="composer-toolbar">
            <div className="composer-controls">
              <button
                type="button"
                className={`btn ${planFirst ? 'btn-primary' : ''}`}
                onClick={() => setPlanFirst((v) => !v)}
              >
                {planFirst ? 'Plan First: ON' : 'Plan First: OFF'}
              </button>

              <label className="hud-chip">
                <Shield size={12} />
                <span>Trust:</span>
                <select
                  aria-label="Trust mode"
                  className="select"
                  style={{ border: 'none', padding: '1px 4px', background: 'transparent' }}
                  value={status?.trust_mode || 'balanced'}
                  onChange={(e) => void changeTrustMode(e.target.value)}
                >
                  <option value="strict">Strict</option>
                  <option value="balanced">Balanced</option>
                  <option value="autonomous">Autonomous</option>
                </select>
              </label>

              <label className="hud-chip">
                <Cpu size={12} />
                <span>Tier:</span>
                <select
                  aria-label="Model tier"
                  className="select"
                  style={{ border: 'none', padding: '1px 4px', background: 'transparent' }}
                  value={status?.active_model_tier || 'auto'}
                  onChange={(e) => void changeTier(e.target.value)}
                >
                  <option value="auto">Auto</option>
                  <option value="fast">Fast ({status?.fast_model || 'qwen2.5:7b'})</option>
                  <option value="reasoning">Reasoning ({status?.reasoning_model || 'qwen2.5:7b'})</option>
                </select>
              </label>
            </div>

            <div className="mono composer-hint desktop-only">
              Enter to send • Paste/Drop images or PDF/DOCX/MD • <code>[[note]]</code> links to Wiki
            </div>
          </div>

          {(stagedAttachments.length > 0 || uploadingCount > 0) && (
            <div className="composer-attachments-strip" aria-label="Staged attachments">
              {uploadingCount > 0 && (
                <div className="attachment-staging-chip uploading">
                  <span className="mono">EXTRACTING ({uploadingCount})...</span>
                </div>
              )}
              {stagedAttachments.map((att) => (
                <div key={att.id} className="attachment-staging-chip">
                  {att.kind === 'image' ? (
                    <img
                      src={att.raw_url}
                      alt={att.filename}
                      className="attachment-staging-thumb"
                    />
                  ) : (
                    <span className="attachment-staging-icon">
                      <FileText size={15} />
                    </span>
                  )}
                  <div className="attachment-staging-meta">
                    <div className="attachment-staging-name" title={att.filename}>
                      {att.filename}
                    </div>
                    <div className="attachment-staging-sub mono">
                      {att.kind === 'image' ? (
                        <span className="badge accent" style={{ padding: '0 4px', fontSize: 9.5 }}>
                          <ImageIcon size={9} /> VISION
                        </span>
                      ) : (
                        <span className="badge" style={{ padding: '0 4px', fontSize: 9.5 }}>
                          {att.kind.toUpperCase()}
                        </span>
                      )}{' '}
                      <span>{att.meta_label}</span>
                      {att.wiki_resource_path && (
                        <span className="badge ok" style={{ padding: '0 4px', fontSize: 9.5 }}>
                          WIKI FILED
                        </span>
                      )}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="attachment-remove-btn"
                    aria-label={`Remove ${att.filename}`}
                    onClick={() => removeStagedAttachment(att.id)}
                  >
                    <X size={13} />
                  </button>
                </div>
              ))}
            </div>
          )}

          <div className="composer-row">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ACCEPTED_FILE_TYPES}
              style={{ display: 'none' }}
              data-testid="chat-file-input"
              onChange={(e) => {
                if (e.target.files && e.target.files.length > 0) {
                  void ingestFiles(e.target.files);
                  e.target.value = '';
                }
              }}
            />
            <button
              type="button"
              className="btn composer-attach-btn"
              aria-label="Attach image or document"
              title="Attach image (vision) or document (PDF, DOCX, XLSX, Markdown, CSV, JSON, Code)"
              disabled={sending}
              onClick={() => fileInputRef.current?.click()}
            >
              <Paperclip size={16} />
            </button>
            <textarea
              rows={1}
              className="textarea"
              placeholder={`Message ${status?.companion_name || 'Companion'} or attach files...`}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onPaste={(e) => {
                const items = Array.from(e.clipboardData?.items || []);
                const files: File[] = [];
                for (const item of items) {
                  if (item.kind === 'file') {
                    const f = item.getAsFile();
                    if (f) {
                      const ext = (f.type.split('/')[1] || 'png').replace(/[^a-z0-9]/gi, '');
                      const named =
                        f.name && f.name !== 'image.png'
                          ? f
                          : new File([f], `pasted-${Date.now()}.${ext}`, { type: f.type });
                      files.push(named);
                    }
                  }
                }
                if (files.length > 0) {
                  e.preventDefault();
                  void ingestFiles(files);
                }
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  void sendMessage();
                }
              }}
            />
            <button
              type="button"
              className="btn btn-primary composer-send-btn"
              disabled={
                sending || uploadingCount > 0 || (!input.trim() && stagedAttachments.length === 0)
              }
              onClick={() => void sendMessage()}
            >
              <Send size={15} />{' '}
              <span>
                {sending ? 'Streaming...' : uploadingCount > 0 ? 'Extracting...' : 'Send'}
              </span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

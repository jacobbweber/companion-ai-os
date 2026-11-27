import React, { useEffect, useRef, useState } from 'react';
import {
  ArrowUp,
  ChevronDown,
  Cpu,
  Folder,
  Paperclip,
  Rocket,
  Shield,
  Sparkles,
  Square,
  X,
} from 'lucide-react';
import { apiPost, callAction, uploadAttachment } from '../api';
import type { ChatAttachment, ProjectGraphEntry, PromptTemplate, WebStatus } from '../types';

export type ComposerExecMode = 'chat' | 'plan' | 'goal' | 'job';

interface ComposerBarProps {
  status: WebStatus | null;
  projects: ProjectGraphEntry[];
  selectedProjectId: string;
  busy: boolean;
  clearedRetryText?: { nonce: number; text: string };
  onSelectProject: (projectId: string) => void;
  onSendMessage: (
    message: string,
    attachmentIds: string[],
    execMode: ComposerExecMode
  ) => Promise<boolean>;
  onStopActiveTurn?: () => Promise<void>;
  onRefreshStatus: () => Promise<void>;
  onToast: (msg: string) => void;
}

interface SlashCommandItem {
  cmd: string;
  desc: string;
  mode?: 'plan' | 'goal';
}

const SDLC_SLASH_COMMANDS: SlashCommandItem[] = [
  { cmd: '/sdlc-next ', desc: 'Auto-detect next SDLC stage & co-design step-by-step (add "express" for fast draft)' },
  { cmd: '/sdlc-1-product ', desc: 'Stage 1 (Product Manager): scope, personas, KPIs -> product.md' },
  { cmd: '/sdlc-2-architecture ', desc: 'Stage 2 (Architect): stack, boundaries & trade-offs -> architecture.md + ADRs' },
  { cmd: '/sdlc-3-backlog ', desc: 'Stage 3 (Delivery Lead): vertical features, increments & stories + Board sync' },
  { cmd: '/sdlc-4-design ', desc: 'Stage 4 (Design Engineer): contracts, schemas, idempotency & hardening pass' },
  { cmd: '/sdlc-5-implement ', desc: 'Stage 5 (Software Engineer): branch isolation & modular source implementation' },
  { cmd: '/sdlc-6-verify ', desc: 'Stage 6 (QA Engineer): adversarial unit/integration tests & clean commit' },
  { cmd: '/sdlc-7-operate ', desc: 'Stage 7 (SRE): Day-2 runbook, observability & recovery -> operations-runbook.md' },
  { cmd: '/sdlc-change ', desc: 'Day-2 Change Ripple: locate Entry-Hat stage & ripple surgical updates' },
];

const CORE_SLASH_COMMANDS: SlashCommandItem[] = [
  { cmd: '/morning', desc: 'Read overnight & idle Autonomy Briefing' },
  { cmd: '/checkin', desc: 'Executive check-in on goals, priorities & health' },
  { cmd: '/plan ', desc: 'Draft an aligned Blueprint before changing code', mode: 'plan' },
  { cmd: '/goal ', desc: 'Run multi-step autonomous execution with Doctor gates', mode: 'goal' },
  { cmd: '/doctor', desc: 'Run 7-stage System Doctor diagnostics' },
  { cmd: '/compact', desc: 'Compact working window into long-term memory (keeps Forever Chat)' },
  { cmd: '/clear', desc: 'Fresh Focus — shift topic cleanly (keeps Forever Chat history)' },
];

export const ComposerBar: React.FC<ComposerBarProps> = ({
  status,
  projects,
  selectedProjectId,
  busy,
  clearedRetryText,
  onSelectProject,
  onSendMessage,
  onStopActiveTurn,
  onRefreshStatus,
  onToast,
}) => {
  const [draft, setDraft] = useState('');
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [openMenu, setOpenMenu] = useState<'none' | 'context' | 'model' | 'posture' | 'project'>('none');
  const [prompts, setPrompts] = useState<PromptTemplate[]>([]);
  const [execMode, setExecMode] = useState<ComposerExecMode>('chat');
  const rootRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const activeTier = status?.active_model_tier || 'auto';
  const trustMode = status?.trust_mode || 'balanced';
  const bgAutonomy = status?.background_autonomy === 'on';

  const currentProj =
    projects.find((p) => p.project_id === selectedProjectId || p.slug === selectedProjectId) ||
    projects[0];

  const isSoftwareProject = Boolean(
    currentProj?.workspace_path ||
      currentProj?.kind === 'software' ||
      (status?.workspace && !status.workspace.is_self_project)
  );
  const orderedSlash =
    isSoftwareProject || draft.toLowerCase().startsWith('/s')
      ? [...SDLC_SLASH_COMMANDS, ...CORE_SLASH_COMMANDS]
      : [...CORE_SLASH_COMMANDS, ...SDLC_SLASH_COMMANDS];
  const showSlashMenu = draft.startsWith('/') && !draft.includes(' ') && draft.length <= 28;
  const filteredSlash = showSlashMenu
    ? orderedSlash.filter((s) => s.cmd.startsWith(draft.toLowerCase()))
    : [];

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    const nextH = Math.min(180, Math.max(36, el.scrollHeight || 36));
    el.style.height = `${nextH}px`;
  }, [draft]);

  useEffect(() => {
    if (openMenu === 'none') return;
    const onPointerDown = (ev: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(ev.target as Node)) {
        setOpenMenu('none');
      }
    };
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === 'Escape') setOpenMenu('none');
    };
    document.addEventListener('mousedown', onPointerDown);
    window.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      window.removeEventListener('keydown', onKey);
    };
  }, [openMenu]);

  const handleUploadFiles = async (files: FileList | File[] | null) => {
    const arr = files ? Array.from(files) : [];
    if (arr.length === 0) return;
    setUploading(true);
    try {
      for (const f of arr) {
        const res = await uploadAttachment(f);
        if (res.ok && res.attachment) {
          setAttachments((prev) => [...prev, res.attachment!]);
        } else {
          onToast(res.error || `Failed to attach ${f.name}`);
        }
      }
    } finally {
      setUploading(false);
      setOpenMenu('none');
    }
  };

  const openContextMenu = async () => {
    const next = openMenu === 'context' ? 'none' : 'context';
    setOpenMenu(next);
    if (next === 'context') {
      const res = await callAction<{ prompts?: PromptTemplate[]; templates?: PromptTemplate[] }>('prompts', {
        subaction: 'list',
      });
      const items = res.prompts || res.templates || [];
      setPrompts(items.slice(0, 6));
    }
  };

  const selectTier = async (tier: string) => {
    setOpenMenu('none');
    const r = await apiPost<{ ok?: boolean; error?: string }>('/api/settings', { tier });
    await onRefreshStatus();
    onToast(r.ok === false ? r.error || 'Could not set model tier.' : `Model routing set to ${tier}.`);
  };

  const selectTrust = async (mode: string) => {
    const r = await callAction('trust', { subaction: 'set', mode });
    await onRefreshStatus();
    onToast(r.ok === false ? r.error || 'Could not update trust mode.' : `Trust mode set to ${mode}.`);
  };

  const toggleBgAutonomy = async () => {
    const res = await callAction('autonomy', { subaction: 'set_autonomy', enabled: !bgAutonomy });
    await onRefreshStatus();
    onToast(res.summary || res.error || `Background Autonomy ${!bgAutonomy ? 'ON' : 'OFF'}.`);
  };

  useEffect(() => {
    if (!clearedRetryText || !clearedRetryText.nonce) return;
    if (draft.trim() === clearedRetryText.text.trim()) {
      setDraft('');
      setAttachments([]);
      setExecMode('chat');
    }
  }, [clearedRetryText?.nonce]);

  const submit = async () => {
    let text = draft.trim();
    if (!text && attachments.length === 0) return;
    if (uploading) {
      onToast('Still uploading attachment — please wait a moment.');
      return;
    }
    if (busy) {
      onToast('Companion is finishing the active turn — click Stop to interrupt or wait a moment.');
      return;
    }
    let chosenMode = execMode;
    if (text.toLowerCase().startsWith('/plan ') && chosenMode === 'chat') {
      chosenMode = 'plan';
      text = text.slice(6).trim();
    } else if (text.toLowerCase().startsWith('/goal ') && chosenMode === 'chat') {
      chosenMode = 'goal';
      text = text.slice(6).trim();
    }
    const ids = attachments.map((a) => a.id);
    setOpenMenu('none');
    const accepted = await onSendMessage(text || 'Inspect attached file(s).', ids, chosenMode);
    if (accepted) {
      setDraft('');
      setAttachments([]);
      setExecMode('chat');
    }
  };

  return (
    <div className="composer-outer" ref={rootRef}>
      <div
        className={`composer-shell ${dragActive ? 'drag-active' : ''}`}
        data-testid="floating-composer"
        onDragOver={(e) => {
          e.preventDefault();
          setDragActive(true);
        }}
        onDragLeave={() => setDragActive(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragActive(false);
          if (e.dataTransfer?.files?.length) {
            void handleUploadFiles(e.dataTransfer.files);
          }
        }}
      >
        {/* Top Context Strip: Project Selector + Staged Attachments */}
        <div className="composer-context-bar">
          <div style={{ position: 'relative' }}>
            <button
              type="button"
              className="composer-chip-trigger"
              style={{ padding: '2px 7px' }}
              onClick={() => setOpenMenu(openMenu === 'project' ? 'none' : 'project')}
              title="Active project context"
            >
              <Folder size={13} />
              <span>{currentProj?.title || status?.workspace?.project_id || 'Companion'}</span>
              <ChevronDown size={12} />
            </button>

            {openMenu === 'project' && (
              <div className="composer-up-menu">
                <div className="popover-group-label">Active Project</div>
                {projects.map((p) => (
                  <button
                    key={p.project_id}
                    type="button"
                    className="popover-row"
                    onClick={() => {
                      onSelectProject(p.project_id);
                      setOpenMenu('none');
                    }}
                  >
                    <span>{p.title}</span>
                    {p.project_id === currentProj?.project_id && <span className="badge accent">Active</span>}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
            {execMode !== 'chat' && (
              <span className="badge accent" data-testid={`composer-mode-pill-${execMode}`}>
                <Rocket size={11} />{' '}
                {execMode === 'plan'
                  ? 'Plan First'
                  : execMode === 'goal'
                    ? 'Autonomous Goal'
                    : 'Background Job'}
                <button
                  type="button"
                  style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
                  onClick={() => setExecMode('chat')}
                  aria-label="Reset execution mode"
                >
                  <X size={11} />
                </button>
              </span>
            )}
            {attachments.map((att) => (
              <span key={att.id} className="badge" data-testid={`composer-att-${att.id}`}>
                {att.filename}
                <button
                  type="button"
                  style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
                  onClick={() => setAttachments((prev) => prev.filter((x) => x.id !== att.id))}
                >
                  <X size={11} />
                </button>
              </span>
            ))}
          </div>
        </div>

        {/* Slash Command Autocomplete Menu */}
        {filteredSlash.length > 0 && (
          <div
            className="composer-up-menu"
            data-testid="slash-autocomplete-menu"
            style={{ left: 12, right: 12, maxWidth: 560, maxHeight: 280, overflowY: 'auto' }}
          >
            <div className="popover-group-label">{isSoftwareProject ? 'SDLC & Actions' : 'Actions'}</div>
            {filteredSlash.map((item) => (
              <button
                key={item.cmd}
                type="button"
                className="popover-row"
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'minmax(136px, auto) 1fr',
                  alignItems: 'baseline',
                  gap: 10,
                }}
                onClick={() => {
                  if (item.mode) {
                    setExecMode(item.mode);
                    setDraft('');
                    textareaRef.current?.focus();
                  } else if (item.cmd.endsWith(' ')) {
                    setDraft(item.cmd);
                    textareaRef.current?.focus();
                  } else {
                    void onSendMessage(item.cmd, [], 'chat').then((ok) => {
                      if (ok) setDraft('');
                    });
                  }
                }}
              >
                <strong className="mono" style={{ flexShrink: 0, whiteSpace: 'nowrap' }}>
                  {item.cmd.trim()}
                </strong>
                <span style={{ fontSize: 11.5, color: 'var(--text-muted)', textAlign: 'left', lineHeight: 1.35 }}>
                  {item.desc}
                </span>
              </button>
            ))}
          </div>
        )}

        {/* Main Prompt Input */}
        <textarea
          ref={textareaRef}
          className="composer-textarea"
          data-testid="chat-composer-input"
          rows={1}
          style={{ maxHeight: 180, overflowY: 'auto' }}
          placeholder="Ask anything, / for actions (paste or drop files)"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onPaste={(e) => {
            const items = Array.from(e.clipboardData?.items || []);
            const pastedFiles = items
              .filter((it) => it.kind === 'file')
              .map((it) => it.getAsFile())
              .filter((f): f is File => Boolean(f));
            if (pastedFiles.length > 0) {
              e.preventDefault();
              void handleUploadFiles(pastedFiles);
            }
          }}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && openMenu !== 'none') {
              e.preventDefault();
              setOpenMenu('none');
              return;
            }
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void submit();
            }
          }}
        />

        {/* Bottom Inline Bar: `+` Context, Model Tier ⌄, Posture ⌄, Send / Stop */}
        <div className="composer-footer-bar">
          <div className="composer-footer-left">
            <input
              ref={fileRef}
              type="file"
              multiple
              hidden
              onChange={(e) => {
                void handleUploadFiles(e.target.files);
                e.currentTarget.value = '';
              }}
            />
            <button
              type="button"
              className="composer-chip-trigger"
              title="Add files, saved prompts, or dispatch mode"
              data-testid="composer-add-context-btn"
              onClick={() => void openContextMenu()}
            >
              <Paperclip size={14} />
            </button>

            {openMenu === 'context' && (
              <div className="composer-up-menu" data-testid="composer-context-sheet">
                <div className="popover-group-label">Add Context &amp; Mode</div>
                <button
                  type="button"
                  className="popover-row"
                  onClick={() => fileRef.current?.click()}
                >
                  <span>Upload file or image...</span>
                </button>
                {(
                  [
                    ['plan', 'Plan First (Blueprint)', 'Align before editing'],
                    ['goal', 'Autonomous Goal', 'Multi-step + Doctor gates'],
                    ['job', 'Background Job', 'Run async in background'],
                  ] as const
                ).map(([mId, mLabel, mSub]) => (
                  <button
                    key={mId}
                    type="button"
                    className="popover-row"
                    onClick={() => {
                      setExecMode((cur) => (cur === mId ? 'chat' : mId));
                      setOpenMenu('none');
                    }}
                  >
                    <span>{mLabel}</span>
                    <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                      {execMode === mId ? 'Active ✓' : mSub}
                    </span>
                  </button>
                ))}
                <div className="popover-group-label">Focus &amp; Memory</div>
                <button
                  type="button"
                  className="popover-row"
                  data-testid="composer-compact-btn"
                  onClick={() => {
                    setOpenMenu('none');
                    void onSendMessage('/compact', [], 'chat');
                  }}
                >
                  <span>Compact Context</span>
                  <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>Distill window</span>
                </button>
                <button
                  type="button"
                  className="popover-row"
                  data-testid="composer-fresh-focus-btn"
                  onClick={() => {
                    setOpenMenu('none');
                    void onSendMessage('/clear', [], 'chat');
                  }}
                >
                  <span>Fresh Focus</span>
                  <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>Shift topic</span>
                </button>
                {prompts.length > 0 && (
                  <>
                    <div className="popover-group-label">Saved Prompts</div>
                    {prompts.map((pt) => (
                      <button
                        key={pt.name}
                        type="button"
                        className="popover-row"
                        onClick={() => {
                          setDraft((cur) => (cur ? `${cur}\n${pt.template}` : pt.template));
                          setOpenMenu('none');
                        }}
                      >
                        <span>{pt.name}</span>
                      </button>
                    ))}
                  </>
                )}
              </div>
            )}

            <button
              type="button"
              className="composer-chip-trigger"
              data-testid="composer-model-btn"
              onClick={() => setOpenMenu(openMenu === 'model' ? 'none' : 'model')}
            >
              <Cpu size={13} />
              <span>
                {activeTier === 'auto'
                  ? `Auto (${status?.fast_model?.split(':')[0] || 'Fast'})`
                  : activeTier}
              </span>
              <ChevronDown size={12} />
            </button>

            {openMenu === 'model' && (
              <div className="composer-up-menu" data-testid="composer-model-sheet">
                <div className="popover-group-label">Model Routing</div>
                {[
                  { id: 'auto', label: 'Auto Triage', sub: `${status?.fast_model || 'fast'} + ${status?.reasoning_model || 'reasoning'}` },
                  { id: 'fast', label: 'Fast Tier', sub: status?.fast_model || 'Low-latency interactive' },
                  { id: 'coding', label: 'Coding Tier', sub: 'Deterministic implementation' },
                  { id: 'reasoning', label: 'Reasoning Tier', sub: status?.reasoning_model || 'Deep architecture & OODA' },
                ].map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    className="popover-row"
                    onClick={() => void selectTier(m.id)}
                  >
                    <div>
                      <div style={{ fontWeight: 500 }}>{m.label}</div>
                      <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{m.sub}</div>
                    </div>
                    {activeTier === m.id && <span className="badge accent">Selected</span>}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="composer-footer-right">
            <button
              type="button"
              className="composer-chip-trigger"
              data-testid="composer-posture-btn"
              onClick={() => setOpenMenu(openMenu === 'posture' ? 'none' : 'posture')}
              title="Trust & Background Autonomy Posture"
            >
              <Shield size={13} />
              <span style={{ textTransform: 'capitalize' }}>
                {trustMode}
                {bgAutonomy ? ' • Idle On' : ''}
              </span>
              <ChevronDown size={12} />
            </button>

            {openMenu === 'posture' && (
              <div className="composer-up-menu align-right" data-testid="composer-posture-sheet" style={{ minWidth: 270 }}>
                <div className="popover-group-label">Trust Policy</div>
                {(['strict', 'balanced', 'autonomous'] as const).map((tm) => (
                  <button
                    key={tm}
                    type="button"
                    className="popover-row"
                    onClick={() => void selectTrust(tm)}
                  >
                    <span style={{ textTransform: 'capitalize' }}>{tm}</span>
                    {trustMode === tm && <span className="badge accent">Active</span>}
                  </button>
                ))}
                <div className="rail-divider" style={{ margin: '6px 2px' }} />
                <div className="popover-row" onClick={() => void toggleBgAutonomy()}>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                    <Sparkles size={13} /> Background Autonomy
                  </span>
                  <span className={`badge ${bgAutonomy ? 'ok' : ''}`}>{bgAutonomy ? 'ON' : 'OFF'}</span>
                </div>
              </div>
            )}

            {busy ? (
              <button
                type="button"
                className="btn btn-danger btn-sm"
                data-testid="composer-stop-btn"
                onClick={() => void onStopActiveTurn?.()}
                aria-label="Stop active turn"
                title="Stop active generation (<100ms)"
                style={{ borderRadius: 999, padding: '5px 11px', fontWeight: 600 }}
              >
                <Square size={11} fill="currentColor" /> Stop
              </button>
            ) : (
              <button
                type="button"
                className="composer-send-circle"
                data-testid="composer-send-btn"
                disabled={(!draft.trim() && attachments.length === 0) || uploading}
                onClick={() => void submit()}
                aria-label="Send message"
              >
                <ArrowUp size={15} />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

import React, { useRef, useState } from 'react';
import {
  ArrowUp,
  ChevronDown,
  Cpu,
  Folder,
  Paperclip,
  Rocket,
  Shield,
  Sparkles,
  X,
} from 'lucide-react';
import { apiPost, callAction, uploadAttachment } from '../api';
import type { ChatAttachment, ProjectGraphEntry, PromptTemplate, WebStatus } from '../types';

interface ComposerBarProps {
  status: WebStatus | null;
  projects: ProjectGraphEntry[];
  selectedProjectId: string;
  busy: boolean;
  onSelectProject: (projectId: string) => void;
  onSendMessage: (message: string, attachmentIds: string[], runAsBgJob: boolean) => Promise<void>;
  onRefreshStatus: () => Promise<void>;
  onToast: (msg: string) => void;
}

const SLASH_COMMANDS = [
  { cmd: '/plan ', desc: 'Draft an aligned Blueprint before changing code' },
  { cmd: '/goal ', desc: 'Run multi-step autonomous execution with Doctor gates' },
  { cmd: '/morning', desc: 'Read overnight & idle Autonomy Briefing' },
  { cmd: '/checkin', desc: 'Executive check-in on goals, priorities & health' },
  { cmd: '/doctor', desc: 'Run 7-stage System Doctor diagnostics' },
  { cmd: '/compact', desc: 'Compact working window into long-term memory (keeps Forever Chat)' },
  { cmd: '/clear', desc: 'Fresh Focus — shift topic cleanly (keeps Forever Chat history)' },
];

export const ComposerBar: React.FC<ComposerBarProps> = ({
  status,
  projects,
  selectedProjectId,
  busy,
  onSelectProject,
  onSendMessage,
  onRefreshStatus,
  onToast,
}) => {
  const [draft, setDraft] = useState('');
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [openMenu, setOpenMenu] = useState<'none' | 'context' | 'model' | 'posture' | 'project'>('none');
  const [prompts, setPrompts] = useState<PromptTemplate[]>([]);
  const [runAsBgJob, setRunAsBgJob] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const activeTier = status?.active_model_tier || 'auto';
  const trustMode = status?.trust_mode || 'balanced';
  const bgAutonomy = status?.background_autonomy === 'on';
  const selfDev = Boolean(status?.self_dev_enabled);

  const currentProj =
    projects.find((p) => p.project_id === selectedProjectId || p.slug === selectedProjectId) ||
    projects[0];

  const showSlashMenu = draft.startsWith('/') && !draft.includes(' ') && draft.length <= 14;
  const filteredSlash = showSlashMenu
    ? SLASH_COMMANDS.filter((s) => s.cmd.startsWith(draft.toLowerCase()))
    : [];

  const handleUploadFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    try {
      for (const f of Array.from(files)) {
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
    if (next === 'context' && prompts.length === 0) {
      const res = await callAction<{ templates?: PromptTemplate[] }>('prompts', { subaction: 'list' });
      if (res.templates) setPrompts(res.templates.slice(0, 6));
    }
  };

  const selectTier = async (tier: string) => {
    setOpenMenu('none');
    await apiPost('/api/settings', { tier });
    await onRefreshStatus();
    onToast(`Model routing set to ${tier}.`);
  };

  const selectTrust = async (mode: string) => {
    await callAction('trust', { subaction: 'set', mode });
    await onRefreshStatus();
    onToast(`Trust mode set to ${mode}.`);
  };

  const toggleBgAutonomy = async () => {
    const res = await callAction('autonomy', { subaction: 'set_autonomy', enabled: !bgAutonomy });
    await onRefreshStatus();
    onToast(res.summary || `Background Autonomy ${!bgAutonomy ? 'ON' : 'OFF'}.`);
  };

  const toggleSelfDev = async () => {
    const res = await callAction('autonomy', { subaction: 'set_self_dev', enabled: !selfDev });
    await onRefreshStatus();
    onToast(res.summary || res.error || `Self-Dev Mode ${!selfDev ? 'ON' : 'OFF'}.`);
  };

  const submit = async () => {
    const text = draft.trim();
    if ((!text && attachments.length === 0) || busy || uploading) return;
    const ids = attachments.map((a) => a.id);
    const asJob = runAsBgJob;
    setDraft('');
    setAttachments([]);
    setRunAsBgJob(false);
    setOpenMenu('none');
    await onSendMessage(text || 'Inspect attached file(s).', ids, asJob);
  };

  return (
    <div className="composer-outer">
      <div className="composer-shell" data-testid="floating-composer">
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

          <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            {runAsBgJob && (
              <span className="badge accent" title="Will run asynchronously in Forever-Chat Background Jobs">
                <Rocket size={11} /> Background Job
                <button
                  type="button"
                  style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
                  onClick={() => setRunAsBgJob(false)}
                >
                  <X size={11} />
                </button>
              </span>
            )}
            {attachments.map((att) => (
              <span key={att.id} className="badge">
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
          <div className="composer-up-menu" style={{ left: 12 }}>
            <div className="popover-group-label">Actions</div>
            {filteredSlash.map((item) => (
              <button
                key={item.cmd}
                type="button"
                className="popover-row"
                onClick={() => setDraft(item.cmd)}
              >
                <strong className="mono">{item.cmd.trim()}</strong>
                <span style={{ fontSize: 11.5, color: 'var(--text-muted)' }}>{item.desc}</span>
              </button>
            ))}
          </div>
        )}

        {/* Main Prompt Input */}
        <textarea
          className="composer-textarea"
          data-testid="chat-composer-input"
          rows={1}
          placeholder="Ask anything, / for actions"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void submit();
            }
          }}
        />

        {/* Bottom Inline Bar: `+` Context, Model Tier ⌄, Posture ⌄, Send */}
        <div className="composer-footer-bar">
          <div className="composer-footer-left">
            <input
              ref={fileRef}
              type="file"
              multiple
              hidden
              onChange={(e) => void handleUploadFiles(e.target.files)}
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
                <div className="popover-group-label">Add Context</div>
                <button
                  type="button"
                  className="popover-row"
                  onClick={() => fileRef.current?.click()}
                >
                  <span>Upload file or image...</span>
                </button>
                <button
                  type="button"
                  className="popover-row"
                  onClick={() => {
                    setRunAsBgJob((v) => !v);
                    setOpenMenu('none');
                  }}
                >
                  <span>{runAsBgJob ? 'Switch to Foreground Turn' : 'Dispatch as Background Job'}</span>
                </button>
                <div className="popover-group-label">Focus &amp; Memory</div>
                <button
                  type="button"
                  className="popover-row"
                  data-testid="composer-compact-btn"
                  onClick={() => {
                    setOpenMenu('none');
                    void onSendMessage('/compact', [], false);
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
                    void onSendMessage('/clear', [], false);
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
              title="Trust, Background Autonomy & Self-Dev Posture"
            >
              <Shield size={13} style={{ color: selfDev ? 'var(--status-warn)' : undefined }} />
              <span style={{ textTransform: 'capitalize' }}>
                {trustMode}
                {bgAutonomy ? ' • Idle On' : ''}
                {selfDev ? ' • Self-Dev' : ''}
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
                <div className="popover-row" onClick={() => void toggleSelfDev()}>
                  <span>Self-Dev Core Unlock</span>
                  <span className={`badge ${selfDev ? 'warn' : ''}`}>{selfDev ? 'ON' : 'OFF (RO)'}</span>
                </div>
              </div>
            )}

            <button
              type="button"
              className="composer-send-circle"
              data-testid="composer-send-btn"
              disabled={(!draft.trim() && attachments.length === 0) || busy || uploading}
              onClick={() => void submit()}
              aria-label="Send message"
            >
              <ArrowUp size={15} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

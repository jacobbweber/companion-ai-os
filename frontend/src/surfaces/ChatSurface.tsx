import React, { useEffect, useRef, useState } from 'react';
import {
  BookmarkPlus,
  Check,
  ChevronDown,
  ChevronRight,
  Cpu,
  Eye,
  ShieldAlert,
  Sparkles,
  X,
} from 'lucide-react';
import { apiPost, callAction, cancelActiveJob, fetchHistory, subscribeJobStream } from '../api';
import { MarkdownView } from '../components/MarkdownView';
import type {
  ActiveWebJob,
  AuxInspectorTarget,
  ChatMessage,
  ProjectGraphEntry,
  ToolStepMeta,
  WebStatus,
} from '../types';
import { ComposerBar, type ComposerExecMode } from './ComposerBar';

interface ChatSurfaceProps {
  status: WebStatus | null;
  projects: ProjectGraphEntry[];
  selectedProjectId: string;
  onSelectProject: (projectId: string) => void;
  onInspect: (target: AuxInspectorTarget) => void;
  onRefreshStatus: () => Promise<void>;
  onToast: (msg: string) => void;
}

function formatTracePillLabel(msg: ChatMessage): string {
  if (msg.activeTool) return `Running ${msg.activeTool}...`;
  if (msg.activePhase && msg.streaming) return `Phase: ${msg.activePhase}`;
  const steps = msg.tool_steps || [];
  const parts: string[] = [];
  if (steps.length > 0) {
    parts.push(`Used ${steps.length} tool${steps.length === 1 ? '' : 's'}`);
  }
  if (msg.thinking) {
    const secs = msg.duration_ms ? Math.max(1, Math.round(msg.duration_ms / 1000)) : 0;
    parts.push(secs > 0 ? `Thought for ${secs}s` : 'Thought trace');
  }
  return parts.join(' · ') || 'Thought & tool trace';
}

export const ChatSurface: React.FC<ChatSurfaceProps> = ({
  status,
  projects,
  selectedProjectId,
  onSelectProject,
  onInspect,
  onRefreshStatus,
  onToast,
}) => {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [hasOlder, setHasOlder] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [sending, setSending] = useState(false);
  const [resolvingApprovalId, setResolvingApprovalId] = useState('');
  const [expandedThinking, setExpandedThinking] = useState<Record<string, boolean>>({});
  const [stagedIds, setStagedIds] = useState<Record<string, boolean>>({});
  const scrollAreaRef = useRef<HTMLDivElement | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);
  const lastTailIdRef = useRef<string>('');
  const unsubRef = useRef<(() => void) | null>(null);
  const attachedJobIdRef = useRef<string>('');
  const prevBusyRef = useRef<boolean>(false);

  const loadHistory = async () => {
    const res = await fetchHistory(80);
    if (res.ok && Array.isArray(res.messages)) {
      setMessages((prev) => {
        const streamingTail = prev.filter((m) => m.streaming);
        if (streamingTail.length === 0) return res.messages;
        return [...res.messages, ...streamingTail];
      });
      setHasOlder(Boolean(res.has_more));
    }
  };

  const loadOlderMessages = async () => {
    if (loadingOlder || !hasOlder) return;
    const firstNumeric = messages.find((m) => typeof m.id === 'number' && m.id > 0);
    if (!firstNumeric || typeof firstNumeric.id !== 'number') {
      setHasOlder(false);
      return;
    }
    const scrollEl = scrollAreaRef.current;
    const prevHeight = scrollEl ? scrollEl.scrollHeight : 0;
    setLoadingOlder(true);
    try {
      const res = await fetchHistory(60, firstNumeric.id);
      if (res.ok && Array.isArray(res.messages)) {
        setHasOlder(Boolean(res.has_more));
        if (res.messages.length > 0) {
          setMessages((prev) => {
            const seen = new Set(prev.map((p) => String(p.id)));
            const older = res.messages.filter((m) => !seen.has(String(m.id)));
            return [...older, ...prev];
          });
          window.requestAnimationFrame(() => {
            if (scrollEl) {
              scrollEl.scrollTop += scrollEl.scrollHeight - prevHeight;
            }
          });
        }
      }
    } finally {
      setLoadingOlder(false);
    }
  };

  const bindStreamToMessage = (jobId: string, streamId: string, cursor = 0) => {
    unsubRef.current?.();
    attachedJobIdRef.current = jobId;
    unsubRef.current = subscribeJobStream(
      jobId,
      {
        onTextDelta: (delta) => {
          setMessages((prev) =>
            prev.map((m) => (m.id === streamId ? { ...m, content: m.content + delta } : m))
          );
        },
        onThinkingDelta: (delta) => {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === streamId ? { ...m, thinking: (m.thinking || '') + delta } : m
            )
          );
        },
        onOrphanThinkClosed: (movedThinking) => {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === streamId
                ? {
                    ...m,
                    thinking: ((m.thinking || '') + (movedThinking || m.content)).trim(),
                    content: '',
                  }
                : m
            )
          );
        },
        onPhase: (phase) => {
          setMessages((prev) =>
            prev.map((m) => (m.id === streamId ? { ...m, activePhase: phase } : m))
          );
        },
        onTool: (toolLabel, active, stepMeta?: ToolStepMeta) => {
          setMessages((prev) =>
            prev.map((m) => {
              if (m.id !== streamId) return m;
              const nextSteps = stepMeta ? [...(m.tool_steps || []), stepMeta] : m.tool_steps;
              return { ...m, activeTool: active ? toolLabel : '', tool_steps: nextSteps };
            })
          );
        },
        onSnapshot: (snap) => {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === streamId
                ? {
                    ...m,
                    content: snap.text || m.content,
                    thinking: snap.thinking || m.thinking,
                    activePhase: snap.phase || m.activePhase,
                    activeTool: snap.activeTool,
                    tool_steps: snap.toolSteps.length > 0 ? snap.toolSteps : m.tool_steps,
                  }
                : m
            )
          );
        },
        onDone: ({ response, error }) => {
          attachedJobIdRef.current = '';
          unsubRef.current = null;
          setMessages((prev) =>
            prev.map((m) =>
              m.id === streamId
                ? {
                    ...m,
                    content: response || m.content || (error ? `Error: ${error}` : ''),
                    streaming: false,
                    activeTool: '',
                  }
                : m
            )
          );
          setSending(false);
          void loadHistory();
          void onRefreshStatus();
        },
        onError: (err) => {
          attachedJobIdRef.current = '';
          unsubRef.current = null;
          setSending(false);
          onToast(err);
          void loadHistory();
        },
      },
      cursor
    );
  };

  useEffect(() => {
    void loadHistory();
    return () => {
      unsubRef.current?.();
    };
  }, []);

  // Auto re-attach to running `status.active_job` across F5 / tab switches + refresh when `busy` clears
  useEffect(() => {
    const aj: ActiveWebJob | null = status?.active_job || null;
    if (aj && aj.status === 'running' && attachedJobIdRef.current !== aj.job_id) {
      const streamId = `job_${aj.job_id}`;
      setSending(true);
      setMessages((prev) => {
        if (prev.some((m) => m.id === streamId)) return prev;
        const tail = prev[prev.length - 1];
        const needsUser =
          !tail || tail.role !== 'user' || !tail.content.includes(aj.message.slice(0, 32));
        const seededUser: ChatMessage[] = needsUser
          ? [
              {
                id: `u_${aj.job_id}`,
                role: 'user',
                content: aj.message,
                timestamp: aj.created_at || new Date().toISOString(),
              },
            ]
          : [];
        const seededAsst: ChatMessage = {
          id: streamId,
          role: 'assistant',
          content: aj.stream_text || '',
          thinking: aj.stream_thinking || '',
          tool_steps: aj.tool_steps || [],
          activePhase: aj.active_phase || '',
          activeTool: aj.active_tool || '',
          timestamp: aj.created_at || new Date().toISOString(),
          streaming: true,
        };
        return [...prev, ...seededUser, seededAsst];
      });
      bindStreamToMessage(aj.job_id, streamId, aj.event_cursor || 0);
    }
    const nowBusy = Boolean(status?.busy);
    if (prevBusyRef.current && !nowBusy && !attachedJobIdRef.current) {
      setSending(false);
      void loadHistory();
    }
    prevBusyRef.current = nowBusy;
  }, [status?.active_job?.job_id, status?.busy]);

  useEffect(() => {
    const tailId = messages.length > 0 ? String(messages[messages.length - 1].id) : '';
    if (tailId && tailId !== lastTailIdRef.current) {
      lastTailIdRef.current = tailId;
      if (typeof endRef.current?.scrollIntoView === 'function') {
        endRef.current.scrollIntoView({ behavior: 'smooth' });
      }
    }
  }, [messages]);

  const handleStopActiveTurn = async () => {
    const jid = status?.active_job?.job_id || attachedJobIdRef.current || 'active';
    const r = await cancelActiveJob(jid);
    if (r.ok === false) {
      onToast(r.error || 'Could not cancel turn.');
      return;
    }
    onToast('Stopped active turn.');
    setSending(false);
    await onRefreshStatus();
    await loadHistory();
  };

  const sendMessage = async (
    text: string,
    attachmentIds: string[],
    execMode: ComposerExecMode
  ): Promise<boolean> => {
    if (execMode === 'job') {
      const jRes = await callAction('jobs', {
        subaction: 'dispatch',
        title: text.slice(0, 72),
        goal: text,
        project_id: selectedProjectId,
        attachment_ids: attachmentIds,
      });
      if (!jRes.ok) {
        onToast(jRes.error || 'Could not dispatch background job.');
        return false;
      }
      await onRefreshStatus();
      onToast(jRes.summary || 'Dispatched to Background Jobs.');
      return true;
    }

    const lowCmd = text.trim().toLowerCase();
    const isFocusMarkerCmd =
      lowCmd === '/clear' ||
      lowCmd === '/reset' ||
      (lowCmd.startsWith('/compact') && !lowCmd.includes('status'));

    if (isFocusMarkerCmd) {
      setSending(true);
      try {
        const mRes = await apiPost<{
          ok?: boolean;
          network_error?: boolean;
          response?: string;
          session_tag?: string;
          error?: string;
        }>('/api/chat', {
          message: text.trim(),
          attachment_ids: [],
          async: true,
        });
        if (mRes.ok) {
          setMessages((prev) => [
            ...prev,
            {
              id: `m_${Date.now()}`,
              role: 'assistant',
              content:
                mRes.response ||
                'Working focus refreshed — Forever Chat history remains saved & searchable.',
              timestamp: new Date().toISOString(),
              session_tag: mRes.session_tag || 'focus_marker',
            },
          ]);
          return true;
        }
        onToast(mRes.error || 'Could not run command.');
        return false;
      } finally {
        setSending(false);
        await onRefreshStatus();
      }
    }

    setSending(true);
    const res = await apiPost<{
      ok?: boolean;
      network_error?: boolean;
      accepted?: boolean;
      job_id?: string;
      display_message?: string;
      response?: string;
      error?: string;
    }>('/api/chat', {
      message: text,
      mode: execMode,
      project_id: selectedProjectId,
      attachment_ids: attachmentIds,
      async: true,
    });

    if (!res.ok || res.network_error) {
      setSending(false);
      onToast(res.error || 'Connection error — your draft was preserved.');
      await onRefreshStatus();
      return false;
    }

    const shownUserText = res.display_message || text;
    const userMsg: ChatMessage = {
      id: `u_${Date.now()}`,
      role: 'user',
      content: shownUserText,
      timestamp: new Date().toISOString(),
    };

    if (res.accepted && res.job_id) {
      const streamId = `job_${res.job_id}`;
      const placeholder: ChatMessage = {
        id: streamId,
        role: 'assistant',
        content: '',
        thinking: '',
        tool_steps: [],
        timestamp: new Date().toISOString(),
        streaming: true,
      };
      setMessages((prev) => [...prev, userMsg, placeholder]);
      bindStreamToMessage(res.job_id, streamId, 0);
      void onRefreshStatus();
      return true;
    }

    const syncReply: ChatMessage = {
      id: `a_${Date.now()}`,
      role: 'assistant',
      content: res.response || 'Completed.',
      timestamp: new Date().toISOString(),
      streaming: false,
    };
    setMessages((prev) => [...prev, userMsg, syncReply]);
    setSending(false);
    await onRefreshStatus();
    return true;
  };

  const stageReplyToWiki = async (msg: ChatMessage) => {
    const key = String(msg.id);
    const res = await callAction('wiki', {
      subaction: 'stage_turn',
      message_id: typeof msg.id === 'number' ? msg.id : undefined,
      title: `Chat Synthesis ${new Date().toISOString().slice(0, 10)}`,
      content: msg.content,
      tags: 'chat-capture',
    });
    if (res.ok) {
      setStagedIds((prev) => ({ ...prev, [key]: true }));
      onToast(res.summary || 'Staged turn into Wiki Inbox (01_inbox/).');
    } else {
      onToast(res.error || 'Could not stage note.');
    }
  };

  const resolvePendingApproval = async (approvalId: string, decision: 'approve' | 'deny') => {
    if (resolvingApprovalId) return;
    setResolvingApprovalId(approvalId);
    try {
      const res = await callAction('approvals', {
        subaction: decision,
        approval_id: approvalId,
      });
      await onRefreshStatus();
      onToast(
        res.ok === false
          ? res.error || `Approval ${decision} failed.`
          : res.summary || `Approval ${decision}d.`
      );
    } finally {
      setResolvingApprovalId('');
    }
  };

  const userName = status?.user_name || 'Partner';
  const inlineApprovals = status?.pending_approvals || [];
  const totalWaitingApprovals = status?.pending_approvals_count ?? inlineApprovals.length;
  const isTurnBusy = sending || Boolean(status?.busy);

  return (
    <div className="chat-canvas-wrap" data-testid="chat-surface">
      {status?.awaiting_approval && (
        <div
          id="approval-banner"
          style={{
            maxWidth: 760,
            margin: '12px auto 0',
            width: 'calc(100% - 32px)',
            padding: '10px 14px',
            borderRadius: 'var(--radius-md)',
            background: 'var(--bg-elevated)',
            border: '1px solid var(--status-warn)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 10,
            flexWrap: 'wrap',
          }}
        >
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
            <ShieldAlert size={15} style={{ color: 'var(--status-warn)' }} />
            <span>Blueprint ready for your alignment &amp; approval</span>
          </span>
          <div style={{ display: 'inline-flex', gap: 6 }}>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => void sendMessage('approve', [], 'chat')}
            >
              <Check size={12} /> Approve &amp; Build
            </button>
            <button
              type="button"
              className="btn btn-danger btn-sm"
              onClick={() => void sendMessage('cancel blueprint', [], 'chat')}
            >
              <X size={12} /> Cancel
            </button>
          </div>
        </div>
      )}

      {inlineApprovals.length > 0 && (
        <div
          data-testid="chat-inline-approvals"
          style={{
            maxWidth: 760,
            margin: '10px auto 0',
            width: 'calc(100% - 32px)',
            padding: '9px 14px',
            borderRadius: 'var(--radius-md)',
            background: 'var(--bg-elevated)',
            border: '1px solid var(--status-warn)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 10,
            flexWrap: 'wrap',
          }}
        >
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, fontSize: 12.5 }}>
            <ShieldAlert size={14} style={{ color: 'var(--status-warn)', flexShrink: 0 }} />
            <span>
              <strong>{inlineApprovals[0].category}:</strong>{' '}
              {(inlineApprovals[0].summary || `${inlineApprovals[0].action_name} (${inlineApprovals[0].target})`).replace(/`/g, '')}
            </span>
          </span>
          <div style={{ display: 'inline-flex', gap: 6, flexWrap: 'wrap' }}>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              data-testid="chat-approvals-view-all"
              onClick={() => onInspect({ kind: 'approvals' })}
            >
              <Eye size={12} /> {totalWaitingApprovals > 1 ? `View all (${totalWaitingApprovals})` : 'Preview'}
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={resolvingApprovalId === inlineApprovals[0].approval_id}
              onClick={() => void resolvePendingApproval(inlineApprovals[0].approval_id, 'approve')}
            >
              <Check size={12} /> Approve
            </button>
            <button
              type="button"
              className="btn btn-danger btn-sm"
              disabled={resolvingApprovalId === inlineApprovals[0].approval_id}
              onClick={() => void resolvePendingApproval(inlineApprovals[0].approval_id, 'deny')}
            >
              <X size={12} /> Deny
            </button>
          </div>
        </div>
      )}

      <div className="chat-scroll-area" ref={scrollAreaRef}>
        {messages.length === 0 ? (
          <div className="chat-empty-hero">
            <div className="chat-hero-glyph">
              <Sparkles size={22} />
            </div>
            <div className="chat-hero-title">Good to see you, {userName}. What&apos;s next?</div>
          </div>
        ) : (
          <div className="chat-column">
            {hasOlder && (
              <div className="chat-load-older-wrap">
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  data-testid="load-older-messages-btn"
                  disabled={loadingOlder}
                  onClick={() => void loadOlderMessages()}
                >
                  {loadingOlder ? 'Loading earlier messages...' : 'Load earlier messages'}
                </button>
              </div>
            )}
            {messages.map((msg) => {
              const mid = String(msg.id);
              if (
                msg.session_tag === 'focus_marker' ||
                msg.session_tag === 'compact_marker'
              ) {
                return (
                  <div
                    key={mid}
                    className="chat-focus-divider"
                    data-testid={`chat-divider-${msg.session_tag}`}
                  >
                    <span>{msg.content}</span>
                  </div>
                );
              }
              if (msg.role === 'user') {
                return (
                  <div key={mid} className="msg-user-bubble md-body">
                    <MarkdownView
                      content={msg.content}
                      onOpenWikiNote={(notePath) =>
                        onInspect({ kind: 'wiki_note', notePath })
                      }
                    />
                  </div>
                );
              }
              const toolSteps = msg.tool_steps || [];
              const hasTrace = Boolean(
                toolSteps.length > 0 || msg.thinking || msg.activeTool || msg.activePhase
              );
              const traceOpen = Boolean(expandedThinking[mid]);
              return (
                <div key={mid} className="msg-assistant-block">
                  {hasTrace && (
                    <div>
                      <button
                        type="button"
                        className="tool-step-pill"
                        data-testid="msg-trace-pill"
                        onClick={() =>
                          setExpandedThinking((p) => ({ ...p, [mid]: !p[mid] }))
                        }
                      >
                        {traceOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                        <Cpu size={12} />
                        <span>{formatTracePillLabel(msg)}</span>
                      </button>
                      {traceOpen && (
                        <div
                          style={{
                            padding: '10px 12px',
                            borderRadius: 'var(--radius-sm)',
                            background: 'var(--bg-panel)',
                            border: '1px solid var(--border-hairline)',
                            marginBottom: 10,
                            display: 'grid',
                            gap: 8,
                          }}
                        >
                          {toolSteps.length > 0 && (
                            <div style={{ display: 'grid', gap: 4, fontSize: 12 }}>
                              {toolSteps.map((st, idx) => (
                                <div
                                  key={`${st.tool}_${idx}`}
                                  className="mono"
                                  style={{
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    gap: 8,
                                    color: st.ok === false ? 'var(--status-err)' : 'var(--text-secondary)',
                                  }}
                                >
                                  <span>
                                    {st.ok === false ? '✗ ' : '✓ '}
                                    {st.summary || st.tool}
                                  </span>
                                  {st.duration_ms !== undefined && (
                                    <span style={{ color: 'var(--text-muted)' }}>
                                      {Math.round(st.duration_ms)}ms
                                    </span>
                                  )}
                                </div>
                              ))}
                            </div>
                          )}
                          {msg.thinking && (
                            <pre
                              className="mono"
                              style={{
                                margin: 0,
                                fontSize: 11.5,
                                color: 'var(--text-secondary)',
                                whiteSpace: 'pre-wrap',
                              }}
                            >
                              {msg.thinking}
                            </pre>
                          )}
                        </div>
                      )}
                    </div>
                  )}

                  <div className="md-body">
                    {msg.content ? (
                      <MarkdownView
                        content={msg.content}
                        onOpenWikiNote={(notePath) =>
                          onInspect({ kind: 'wiki_note', notePath })
                        }
                        onFlashcardGraded={() => void onRefreshStatus()}
                      />
                    ) : msg.streaming ? (
                      <span style={{ color: 'var(--text-muted)', fontSize: 13 }}>Thinking...</span>
                    ) : null}
                  </div>

                  {!msg.streaming && msg.content && (
                    <div className="msg-meta-footer">
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => void stageReplyToWiki(msg)}
                        title="Save synthesis to Knowledge Wiki Inbox"
                      >
                        <BookmarkPlus size={12} />
                        <span>{stagedIds[mid] ? 'Saved to Inbox' : 'Save to Wiki'}</span>
                      </button>
                    </div>
                  )}
                </div>
              );
            })}
            <div ref={endRef} />
          </div>
        )}
      </div>

      <ComposerBar
        status={status}
        projects={projects}
        selectedProjectId={selectedProjectId}
        busy={isTurnBusy}
        onSelectProject={onSelectProject}
        onSendMessage={sendMessage}
        onStopActiveTurn={handleStopActiveTurn}
        onRefreshStatus={onRefreshStatus}
        onToast={onToast}
      />
    </div>
  );
};

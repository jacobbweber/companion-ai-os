import React, { useEffect, useRef, useState } from 'react';
import {
  BookmarkPlus,
  Check,
  ChevronDown,
  ChevronRight,
  Cpu,
  ShieldAlert,
  Sparkles,
  X,
} from 'lucide-react';
import { apiPost, callAction, fetchHistory, subscribeJobStream } from '../api';
import { MarkdownView } from '../components/MarkdownView';
import type {
  AuxInspectorTarget,
  ChatMessage,
  ProjectGraphEntry,
  WebStatus,
} from '../types';
import { ComposerBar } from './ComposerBar';

interface ChatSurfaceProps {
  status: WebStatus | null;
  projects: ProjectGraphEntry[];
  selectedProjectId: string;
  conversationNonce: number;
  onSelectProject: (projectId: string) => void;
  onInspect: (target: AuxInspectorTarget) => void;
  onRefreshStatus: () => Promise<void>;
  onToast: (msg: string) => void;
}

export const ChatSurface: React.FC<ChatSurfaceProps> = ({
  status,
  projects,
  selectedProjectId,
  conversationNonce,
  onSelectProject,
  onInspect,
  onRefreshStatus,
  onToast,
}) => {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sending, setSending] = useState(false);
  const [expandedThinking, setExpandedThinking] = useState<Record<string, boolean>>({});
  const [stagedIds, setStagedIds] = useState<Record<string, boolean>>({});
  const endRef = useRef<HTMLDivElement | null>(null);
  const unsubRef = useRef<(() => void) | null>(null);

  const loadHistory = async () => {
    const res = await fetchHistory(80);
    if (res.ok && Array.isArray(res.messages)) {
      setMessages(res.messages);
    }
  };

  useEffect(() => {
    void loadHistory();
    return () => {
      unsubRef.current?.();
    };
  }, []);

  useEffect(() => {
    if (conversationNonce > 0) {
      setMessages([]);
    }
  }, [conversationNonce]);

  useEffect(() => {
    if (typeof endRef.current?.scrollIntoView === 'function') {
      endRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages.length]);

  const sendMessage = async (text: string, attachmentIds: string[], runAsBgJob: boolean) => {
    if (runAsBgJob) {
      const jRes = await callAction('jobs', {
        subaction: 'dispatch',
        title: text.slice(0, 72),
        goal: text,
      });
      await onRefreshStatus();
      onToast(jRes.summary || 'Dispatched to Background Jobs.');
      return;
    }

    const userMsg: ChatMessage = {
      id: `u_${Date.now()}`,
      role: 'user',
      content: text,
      timestamp: new Date().toISOString(),
    };
    const streamId = `a_${Date.now()}`;
    const placeholder: ChatMessage = {
      id: streamId,
      role: 'assistant',
      content: '',
      thinking: '',
      timestamp: new Date().toISOString(),
      streaming: true,
    };

    setMessages((prev) => [...prev, userMsg, placeholder]);
    setSending(true);

    try {
      const res = await apiPost<{
        ok?: boolean;
        accepted?: boolean;
        job_id?: string;
        response?: string;
        error?: string;
      }>('/api/chat', {
        message: text,
        attachment_ids: attachmentIds,
        async: true,
      });

      if (res.accepted && res.job_id) {
        unsubRef.current?.();
        unsubRef.current = subscribeJobStream(res.job_id, {
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
          onPhase: (phase) => {
            setMessages((prev) =>
              prev.map((m) => (m.id === streamId ? { ...m, activePhase: phase } : m))
            );
          },
          onTool: (tool, active) => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === streamId ? { ...m, activeTool: active ? tool : '' } : m
              )
            );
          },
          onDone: ({ response, error }) => {
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
            void onRefreshStatus();
          },
          onError: (err) => {
            setSending(false);
            onToast(err);
            void loadHistory();
          },
        });
        return;
      }

      setMessages((prev) =>
        prev.map((m) =>
          m.id === streamId
            ? {
                ...m,
                content: res.response || (res.error ? `Error: ${res.error}` : 'Completed.'),
                streaming: false,
              }
            : m
        )
      );
    } finally {
      if (!unsubRef.current) setSending(false);
      await onRefreshStatus();
    }
  };

  const stageReplyToWiki = async (msg: ChatMessage) => {
    const key = String(msg.id);
    const res = await callAction('wiki', {
      subaction: 'capture_inbox',
      title: `Chat Synthesis ${new Date().toISOString().slice(0, 10)}`,
      content: msg.content,
      tags: 'chat-capture',
    });
    if (res.ok) {
      setStagedIds((prev) => ({ ...prev, [key]: true }));
      onToast(res.summary || 'Captured note into Wiki Inbox (01_inbox/).');
    } else {
      onToast(res.error || 'Could not stage note.');
    }
  };

  const userName = status?.user_name || 'Partner';

  return (
    <div className="chat-canvas-wrap" data-testid="chat-surface">
      {status?.awaiting_approval && (
        <div
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
              onClick={() => void sendMessage('approve', [], false)}
            >
              <Check size={12} /> Approve &amp; Build
            </button>
            <button
              type="button"
              className="btn btn-danger btn-sm"
              onClick={() => void sendMessage('cancel blueprint', [], false)}
            >
              <X size={12} /> Cancel
            </button>
          </div>
        </div>
      )}

      <div className="chat-scroll-area">
        {messages.length === 0 ? (
          <div className="chat-empty-hero">
            <div className="chat-hero-glyph">
              <Sparkles size={22} />
            </div>
            <div className="chat-hero-title">Good to see you, {userName}. What&apos;s next?</div>
          </div>
        ) : (
          <div className="chat-column">
            {messages.map((msg) => {
              const mid = String(msg.id);
              if (msg.role === 'user') {
                return (
                  <div key={mid} className="msg-user-bubble">
                    {msg.content}
                  </div>
                );
              }
              const hasTrace = Boolean(msg.thinking || msg.activeTool || msg.activePhase);
              const traceOpen = Boolean(expandedThinking[mid]);
              return (
                <div key={mid} className="msg-assistant-block">
                  {hasTrace && (
                    <div>
                      <button
                        type="button"
                        className="tool-step-pill"
                        onClick={() =>
                          setExpandedThinking((p) => ({ ...p, [mid]: !p[mid] }))
                        }
                      >
                        {traceOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                        <Cpu size={12} />
                        <span>
                          {msg.activeTool
                            ? `Running ${msg.activeTool}...`
                            : msg.activePhase
                              ? `Phase: ${msg.activePhase}`
                              : 'Thought & tool trace'}
                        </span>
                      </button>
                      {traceOpen && msg.thinking && (
                        <pre
                          className="mono"
                          style={{
                            padding: '10px 12px',
                            borderRadius: 'var(--radius-sm)',
                            background: 'var(--bg-panel)',
                            border: '1px solid var(--border-hairline)',
                            fontSize: 11.5,
                            color: 'var(--text-secondary)',
                            whiteSpace: 'pre-wrap',
                            marginBottom: 10,
                          }}
                        >
                          {msg.thinking}
                        </pre>
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
        busy={sending || Boolean(status?.busy)}
        onSelectProject={onSelectProject}
        onSendMessage={sendMessage}
        onRefreshStatus={onRefreshStatus}
        onToast={onToast}
      />
    </div>
  );
};

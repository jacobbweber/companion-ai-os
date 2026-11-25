import React, { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  BookmarkPlus,
  Check,
  ChevronDown,
  ChevronRight,
  Cpu,
  Eye,
  KeyRound,
  RotateCcw,
  ShieldAlert,
  Sparkles,
  X,
} from 'lucide-react';
import {
  apiPost,
  type ApprovalResolvedDetail,
  callAction,
  cancelActiveJob,
  emitApprovalResolved,
  fetchHistory,
  fetchJobSnapshot,
  formatChatErrorBanner,
  subscribeJobStream,
} from '../api';
import { MarkdownView } from '../components/MarkdownView';
import type {
  ActiveWebJob,
  AuxInspectorTarget,
  ChatDeliveryFailure,
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
  onRequireAuth?: () => void;
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
  onRequireAuth,
  onToast,
}) => {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [hasOlder, setHasOlder] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [sending, setSending] = useState(false);
  const [deliveryError, setDeliveryError] = useState<ChatDeliveryFailure | null>(null);
  const [dismissedOrphanId, setDismissedOrphanId] = useState<string>('');
  const [clearedRetryText, setClearedRetryText] = useState<{ nonce: number; text: string }>({
    nonce: 0,
    text: '',
  });
  const [resolvingApprovalId, setResolvingApprovalId] = useState('');
  const [expandedThinking, setExpandedThinking] = useState<Record<string, boolean>>({});
  const [stagedIds, setStagedIds] = useState<Record<string, boolean>>({});
  const [liveElapsedSec, setLiveElapsedSec] = useState(0);
  const turnStartedMsRef = useRef<number>(0);
  const scrollAreaRef = useRef<HTMLDivElement | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);
  const lastTailIdRef = useRef<string>('');
  const unsubRef = useRef<(() => void) | null>(null);
  const attachedJobIdRef = useRef<string>('');
  const inFlightPostRef = useRef<boolean>(false);
  const reconcilingJobRef = useRef<string>('');
  const prevBusyRef = useRef<boolean>(false);
  const busyLiveRef = useRef<boolean>(false);

  const loadHistory = async (): Promise<ChatMessage[]> => {
    const res = await fetchHistory(80);
    if (res.ok && Array.isArray(res.messages)) {
      setMessages((prev) => {
        if (res.messages.length === 0 && prev.some((m) => typeof m.id === 'number' && m.id > 0)) {
          return prev;
        }
        const transientTail = prev.filter((m) => m.streaming);
        if (transientTail.length === 0) return res.messages;
        return [...res.messages, ...transientTail];
      });
      setHasOlder(Boolean(res.has_more));
      return res.messages;
    }
    return [];
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

  const finalizeStreamOutcome = async (
    streamId: string,
    originPayload: { text: string; attachmentIds: string[]; execMode: ComposerExecMode },
    doneStatus: string,
    response: string,
    error: string,
    errorCode = ''
  ) => {
    attachedJobIdRef.current = '';
    unsubRef.current = null;
    setSending(false);

    if (doneStatus === 'interrupted') {
      setMessages((prev) => prev.filter((m) => m.id !== streamId));
      const hist = await loadHistory();
      const tail = hist[hist.length - 1];
      if (!tail || tail.role === 'user') {
        const banner = formatChatErrorBanner({ error_code: 'interrupted', error });
        setDeliveryError({
          id: `err_${Date.now()}`,
          title: banner.title,
          detail: banner.detail,
          errorCode: 'interrupted',
          text: originPayload.text,
          attachmentIds: originPayload.attachmentIds,
          execMode: originPayload.execMode,
          timestamp: new Date().toISOString(),
        });
        onToast(`${banner.title}: ${banner.detail}`);
      }
      void onRefreshStatus();
      return;
    }

    const isTurnError = doneStatus === 'error' || Boolean(error && !response);
    setMessages((prev) =>
      prev.map((m) => {
        if (m.id !== streamId) return m;
        let nextContent = response || m.content || '';
        if (isTurnError && error) {
          nextContent = m.content
            ? `${m.content.trimEnd()}\n\n⚠️ **${error}**`
            : `⚠️ **${error}**`;
        } else if (!nextContent.trim()) {
          nextContent = 'Completed.';
        }
        return {
          ...m,
          content: nextContent,
          streaming: false,
          activeTool: '',
          is_error: isTurnError,
          error_code: errorCode,
          retry_payload: isTurnError ? originPayload : undefined,
        };
      })
    );
    if (isTurnError) {
      const banner = formatChatErrorBanner({ error_code: errorCode, error });
      onToast(`${banner.title}: ${banner.detail}`);
    }
    await loadHistory();
    void onRefreshStatus();
  };

  const bindStreamToMessage = (
    jobId: string,
    streamId: string,
    cursor = 0,
    originPayload: { text: string; attachmentIds: string[]; execMode: ComposerExecMode } = {
      text: '',
      attachmentIds: [],
      execMode: 'chat',
    }
  ) => {
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
        onDone: ({ status: doneStatus, response, error, errorCode }) => {
          void finalizeStreamOutcome(
            streamId,
            originPayload,
            doneStatus,
            response,
            error,
            errorCode || ''
          );
        },
        onError: (err) => {
          void finalizeStreamOutcome(streamId, originPayload, 'interrupted', '', err, 'interrupted');
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

  // Self-Healing Busy State & Auto Re-Attach across F5 / Tab Switches / Dropped LAN SSE
  useEffect(() => {
    const aj: ActiveWebJob | null = status?.active_job || null;
    if (aj && aj.status === 'running' && attachedJobIdRef.current !== aj.job_id) {
      const streamId = `job_${aj.job_id}`;
      setSending(true);
      setDeliveryError(null);
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
      bindStreamToMessage(aj.job_id, streamId, aj.event_cursor || 0, {
        text: aj.message,
        attachmentIds: [],
        execMode: (aj.mode as ComposerExecMode) || 'chat',
      });
      prevBusyRef.current = true;
      return;
    }

    const nowBusy = Boolean(status?.busy) || Boolean(aj && aj.status === 'running');
    const curAttached = attachedJobIdRef.current;

    if (status?.ok && !nowBusy && curAttached && reconcilingJobRef.current !== curAttached) {
      reconcilingJobRef.current = curAttached;
      void (async () => {
        try {
          const snap = await fetchJobSnapshot(curAttached);
          if (attachedJobIdRef.current !== curAttached) return;
          unsubRef.current?.();
          const streamId = `job_${curAttached}`;
          const j = snap.job;
          if (j && j.status && j.status !== 'running') {
            await finalizeStreamOutcome(
              streamId,
              { text: '', attachmentIds: [], execMode: 'chat' },
              String(j.status),
              String(j.response || ''),
              String(j.error || ''),
              String(j.error_code || '')
            );
          } else if (!j || snap.status_code === 404) {
            await finalizeStreamOutcome(
              streamId,
              { text: '', attachmentIds: [], execMode: 'chat' },
              'interrupted',
              '',
              'Active turn ended while connection was offline.',
              'interrupted'
            );
          }
        } finally {
          if (reconcilingJobRef.current === curAttached) {
            reconcilingJobRef.current = '';
          }
        }
      })();
    } else if (status?.ok && !nowBusy && !curAttached && !inFlightPostRef.current && sending) {
      setSending(false);
      void loadHistory();
    } else if (prevBusyRef.current && !nowBusy && !curAttached) {
      setSending(false);
      void loadHistory();
    }
    prevBusyRef.current = nowBusy;
  }, [status]);

  useEffect(() => {
    const tailId = deliveryError
      ? deliveryError.id
      : messages.length > 0
        ? String(messages[messages.length - 1].id)
        : '';
    if (tailId && tailId !== lastTailIdRef.current) {
      lastTailIdRef.current = tailId;
      if (typeof endRef.current?.scrollIntoView === 'function') {
        endRef.current.scrollIntoView({ behavior: 'smooth' });
      }
    }
  }, [messages, deliveryError]);

  const handleStopActiveTurn = async () => {
    const jid = status?.active_job?.job_id || attachedJobIdRef.current || 'active';
    const r = await cancelActiveJob(jid);
    if (r.ok === false) {
      onToast(r.error || 'Could not cancel turn.');
      return;
    }
    unsubRef.current?.();
    unsubRef.current = null;
    attachedJobIdRef.current = '';
    onToast('Stopped active turn.');
    setSending(false);
    await onRefreshStatus();
    await loadHistory();
  };

  const sendMessage = async (
    text: string,
    attachmentIds: string[],
    execMode: ComposerExecMode,
    fromRetry = false
  ): Promise<boolean> => {
    setDeliveryError(null);

    if (execMode === 'job') {
      const jRes = await callAction('jobs', {
        subaction: 'dispatch',
        title: text.slice(0, 72),
        goal: text,
        project_id: selectedProjectId,
        attachment_ids: attachmentIds,
      });
      if (!jRes.ok) {
        const banner = formatChatErrorBanner(jRes);
        setDeliveryError({
          id: `err_${Date.now()}`,
          title: banner.title,
          detail: banner.detail,
          text,
          attachmentIds,
          execMode,
          timestamp: new Date().toISOString(),
        });
        onToast(jRes.error || 'Could not dispatch background job.');
        return false;
      }
      if (fromRetry) setClearedRetryText((p) => ({ nonce: p.nonce + 1, text }));
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
      inFlightPostRef.current = true;
      try {
        const mRes = await apiPost<{
          ok?: boolean;
          status_code?: number;
          error_code?: string;
          auth_required?: boolean;
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
          if (fromRetry) setClearedRetryText((p) => ({ nonce: p.nonce + 1, text }));
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
          await onRefreshStatus();
          return true;
        }
        const banner = formatChatErrorBanner(mRes);
        if (banner.authRequired) onRequireAuth?.();
        setDeliveryError({
          id: `err_${Date.now()}`,
          title: banner.title,
          detail: banner.detail,
          statusCode: mRes.status_code,
          errorCode: mRes.error_code,
          authRequired: banner.authRequired,
          text,
          attachmentIds: [],
          execMode: 'chat',
          timestamp: new Date().toISOString(),
        });
        onToast(`${banner.title}: ${banner.detail}`);
        return false;
      } finally {
        inFlightPostRef.current = false;
        setSending(false);
      }
    }

    setSending(true);
    inFlightPostRef.current = true;
    let res: {
      ok?: boolean;
      status_code?: number;
      error_code?: string;
      auth_required?: boolean;
      network_error?: boolean;
      accepted?: boolean;
      job_id?: string;
      display_message?: string;
      response?: string;
      error?: string;
    };
    try {
      res = await apiPost('/api/chat', {
        message: text,
        mode: execMode,
        project_id: selectedProjectId,
        attachment_ids: attachmentIds,
        async: true,
      });
    } finally {
      inFlightPostRef.current = false;
    }

    if (!res.ok || res.network_error) {
      setSending(false);
      const banner = formatChatErrorBanner(res);
      if (banner.authRequired) onRequireAuth?.();
      setDeliveryError({
        id: `err_${Date.now()}`,
        title: banner.title,
        detail: banner.detail,
        statusCode: res.status_code,
        errorCode: res.error_code,
        authRequired: banner.authRequired,
        text,
        attachmentIds,
        execMode,
        timestamp: new Date().toISOString(),
      });
      onToast(`${banner.title}: ${banner.detail}`);
      if (!banner.authRequired) {
        await onRefreshStatus();
      }
      return false;
    }

    if (fromRetry) {
      setClearedRetryText((p) => ({ nonce: p.nonce + 1, text }));
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
      bindStreamToMessage(res.job_id, streamId, 0, { text, attachmentIds, execMode });
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

  const findPrecedingUserPrompt = (msg: ChatMessage): string => {
    const key = String(msg.id);
    const idx = messages.findIndex((m) => String(m.id) === key);
    for (let i = idx - 1; i >= 0; i -= 1) {
      if (messages[i]?.role === 'user' && messages[i].content.trim()) {
        return messages[i].content.trim();
      }
    }
    return '';
  };

  const stageReplyToWiki = async (msg: ChatMessage) => {
    const key = String(msg.id);
    const userPrompt = findPrecedingUserPrompt(msg);
    const res = await callAction('wiki', {
      subaction: 'stage_turn',
      message_id: typeof msg.id === 'number' ? msg.id : undefined,
      user_prompt: userPrompt,
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
      const res = await callAction<{
        category?: string;
        resumed_background_job?: boolean;
      }>('approvals', {
        subaction: decision,
        approval_id: approvalId,
      });
      await onRefreshStatus();
      onToast(
        res.ok === false
          ? res.error || `Approval ${decision} failed.`
          : res.summary || `Approval ${decision}d.`
      );
      if (res.ok !== false) {
        emitApprovalResolved({
          approvalId,
          decision,
          category: res.category,
          resumedBackgroundJob: Boolean(res.resumed_background_job),
          source: 'chat',
        });
      }
    } finally {
      setResolvingApprovalId('');
    }
  };

  const userName = status?.user_name || 'Partner';
  const inlineApprovals = status?.pending_approvals || [];
  const totalWaitingApprovals = status?.pending_approvals_count ?? inlineApprovals.length;
  const isTurnBusy = sending || Boolean(status?.busy);
  busyLiveRef.current = isTurnBusy || Boolean(attachedJobIdRef.current);

  const autoContinueRef = useRef<(detail: ApprovalResolvedDetail) => void>(() => {});
  autoContinueRef.current = (detail: ApprovalResolvedDetail) => {
    if (detail.decision !== 'approve' || detail.resumedBackgroundJob) return;
    const isInteractiveExec =
      detail.source === 'chat' ||
      detail.category === 'endpoint_exec' ||
      detail.category === 'r3_tool' ||
      detail.category === 'mcp_tool';
    if (!isInteractiveExec) return;
    const prompt = `Approved \`${detail.approvalId}\` — please continue with the results.`;
    void (async () => {
      for (let waitIdx = 0; waitIdx < 12 && busyLiveRef.current; waitIdx += 1) {
        await new Promise((r) => window.setTimeout(r, 250));
      }
      if (busyLiveRef.current) {
        setDeliveryError({
          id: `err_${Date.now()}`,
          title: 'Approval Continuation Queued',
          detail: 'Approval succeeded while an active turn was still busy. Click Retry once ready to continue.',
          errorCode: 'turn_busy',
          text: prompt,
          attachmentIds: [],
          execMode: 'chat',
          timestamp: new Date().toISOString(),
        });
        return;
      }
      await sendMessage(prompt, [], 'chat');
    })();
  };

  useEffect(() => {
    const onResolved = (ev: Event) => {
      const custom = ev as CustomEvent<ApprovalResolvedDetail>;
      if (custom.detail) {
        autoContinueRef.current(custom.detail);
      }
    };
    window.addEventListener('companion:approval-resolved', onResolved);
    return () => window.removeEventListener('companion:approval-resolved', onResolved);
  }, []);

  useEffect(() => {
    if (!isTurnBusy) {
      turnStartedMsRef.current = 0;
      setLiveElapsedSec(0);
      return;
    }
    const srvElapsed = status?.active_job?.elapsed_seconds ?? status?.busy_seconds ?? 0;
    if (!turnStartedMsRef.current) {
      turnStartedMsRef.current = Date.now() - Math.round(Number(srvElapsed || 0) * 1000);
    }
    const updateTick = () => {
      setLiveElapsedSec(Math.max(0, Math.floor((Date.now() - turnStartedMsRef.current) / 1000)));
    };
    updateTick();
    const timer = window.setInterval(updateTick, 1000);
    return () => window.clearInterval(timer);
  }, [isTurnBusy, status?.active_job?.job_id]);

  // Detect orphaned trailing `user` message in persisted SQLite history when server is idle
  const tailMsg = messages.length > 0 ? messages[messages.length - 1] : null;
  const orphanedUserMsg =
    !isTurnBusy &&
    !attachedJobIdRef.current &&
    !deliveryError &&
    tailMsg &&
    tailMsg.role === 'user' &&
    typeof tailMsg.id === 'number' &&
    tailMsg.id > 0 &&
    dismissedOrphanId !== String(tailMsg.id)
      ? tailMsg
      : null;

  const activeErrorCard: ChatDeliveryFailure | null =
    deliveryError ||
    (orphanedUserMsg
      ? {
          id: `orphan_${orphanedUserMsg.id}`,
          title: 'Turn Interrupted Before Completion',
          detail:
            'Companion restarted or lost its upstream connection before finishing a reply to your last message.',
          errorCode: 'interrupted',
          text: orphanedUserMsg.content,
          attachmentIds: [],
          execMode: 'chat',
          timestamp: orphanedUserMsg.timestamp,
        }
      : null);

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
              {(
                inlineApprovals[0].summary ||
                `${inlineApprovals[0].action_name} (${inlineApprovals[0].target})`
              ).replace(/`/g, '')}
            </span>
          </span>
          <div style={{ display: 'inline-flex', gap: 6, flexWrap: 'wrap' }}>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              data-testid="chat-approvals-view-all"
              onClick={() => onInspect({ kind: 'approvals' })}
            >
              <Eye size={12} />{' '}
              {totalWaitingApprovals > 1 ? `View all (${totalWaitingApprovals})` : 'Preview'}
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
        {messages.length === 0 && !activeErrorCard ? (
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
              if (msg.session_tag === 'focus_marker' || msg.session_tag === 'compact_marker') {
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
                      onOpenWikiNote={(notePath) => onInspect({ kind: 'wiki_note', notePath })}
                    />
                  </div>
                );
              }
              const toolSteps = msg.tool_steps || [];
              const hasTrace = Boolean(
                toolSteps.length > 0 || msg.thinking || msg.activeTool || msg.activePhase
              );
              const traceOpen = Boolean(expandedThinking[mid]);
              const isErrRow = Boolean(msg.is_error || msg.content.trim().startsWith('⚠️ **'));
              const retryPrompt = msg.retry_payload?.text || (isErrRow ? findPrecedingUserPrompt(msg) : '');

              return (
                <div key={mid} className="msg-assistant-block">
                  {hasTrace && (
                    <div>
                      <button
                        type="button"
                        className="tool-step-pill"
                        data-testid="msg-trace-pill"
                        onClick={() => setExpandedThinking((p) => ({ ...p, [mid]: !p[mid] }))}
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
                                    color:
                                      st.ok === false
                                        ? 'var(--status-danger)'
                                        : 'var(--text-secondary)',
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
                        onOpenWikiNote={(notePath) => onInspect({ kind: 'wiki_note', notePath })}
                        onFlashcardGraded={() => void onRefreshStatus()}
                      />
                    ) : null}
                    {msg.streaming && (
                      <div
                        data-testid="chat-live-activity"
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 8,
                          marginTop: msg.content ? 10 : 2,
                          padding: '6px 10px',
                          borderRadius: 'var(--radius-sm)',
                          background: 'var(--bg-elevated)',
                          border: '1px solid var(--border-hairline)',
                          color: 'var(--text-secondary)',
                          fontSize: 12.5,
                        }}
                      >
                        <span className="status-dot busy" style={{ flexShrink: 0 }} />
                        <span
                          style={{
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {msg.activeTool
                            ? `Running ${msg.activeTool}...`
                            : msg.activePhase
                              ? `Thinking · ${msg.activePhase}`
                              : 'Thinking...'}
                          {liveElapsedSec > 0 ? ` · ${liveElapsedSec}s` : ''}
                          {msg.thinking && !msg.activeTool
                            ? ` — ${msg.thinking.trim().split('\n').pop()?.slice(0, 88) || ''}`
                            : ''}
                        </span>
                      </div>
                    )}
                  </div>

                  {!msg.streaming && msg.content && (
                    <div className="msg-meta-footer">
                      {isErrRow && retryPrompt && (
                        <button
                          type="button"
                          className="btn btn-primary btn-sm"
                          data-testid="msg-turn-retry-btn"
                          disabled={isTurnBusy}
                          onClick={() =>
                            void sendMessage(
                              retryPrompt,
                              msg.retry_payload?.attachmentIds || [],
                              msg.retry_payload?.execMode || 'chat',
                              true
                            )
                          }
                        >
                          <RotateCcw size={12} /> Retry Turn
                        </button>
                      )}
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

            {activeErrorCard && (
              <div className="chat-delivery-error-card" data-testid="chat-delivery-error-card" role="alert">
                <div className="chat-delivery-error-head">
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, fontWeight: 600 }}>
                    <AlertTriangle size={15} style={{ color: 'var(--status-danger)', flexShrink: 0 }} />
                    <span>{activeErrorCard.title}</span>
                  </span>
                  <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                    {activeErrorCard.authRequired && onRequireAuth && (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        data-testid="chat-error-pair-btn"
                        onClick={() => onRequireAuth()}
                      >
                        <KeyRound size={12} /> Pair Token
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      data-testid="chat-error-retry-btn"
                      disabled={isTurnBusy}
                      onClick={() =>
                        void sendMessage(
                          activeErrorCard.text,
                          activeErrorCard.attachmentIds,
                          activeErrorCard.execMode,
                          true
                        )
                      }
                    >
                      <RotateCcw size={12} /> Retry
                    </button>
                    <button
                      type="button"
                      className="icon-btn"
                      data-testid="chat-error-dismiss-btn"
                      aria-label="Dismiss error notice"
                      onClick={() => {
                        if (orphanedUserMsg) {
                          setDismissedOrphanId(String(orphanedUserMsg.id));
                        }
                        setDeliveryError(null);
                      }}
                    >
                      <X size={13} />
                    </button>
                  </div>
                </div>
                <div className="chat-delivery-error-detail">{activeErrorCard.detail}</div>
                {activeErrorCard.text && !orphanedUserMsg && (
                  <div className="chat-delivery-error-preview">
                    Unsent message preserved: <code>{activeErrorCard.text.slice(0, 140)}</code>
                  </div>
                )}
              </div>
            )}
            <div ref={endRef} />
          </div>
        )}
      </div>

      <ComposerBar
        status={status}
        projects={projects}
        selectedProjectId={selectedProjectId}
        busy={isTurnBusy}
        clearedRetryText={clearedRetryText}
        onSelectProject={onSelectProject}
        onSendMessage={sendMessage}
        onStopActiveTurn={handleStopActiveTurn}
        onRefreshStatus={onRefreshStatus}
        onToast={onToast}
      />
    </div>
  );
};

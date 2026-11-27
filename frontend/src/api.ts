import type {
  ChatAttachment,
  ChatMessage,
  TelemetryPayload,
  ToolStepMeta,
  WebStatus,
} from './types';

let cachedCsrfToken = '';

export function getCsrfToken(): string {
  if (cachedCsrfToken) return cachedCsrfToken;
  const meta = document.querySelector('meta[name="companion-csrf"]');
  const content = meta?.getAttribute('content') || '';
  if (content) cachedCsrfToken = content;
  return cachedCsrfToken;
}

export function setCsrfToken(tok: string): void {
  if (tok) cachedCsrfToken = tok;
}

export function getBootAppVersion(): string {
  return document.querySelector('meta[name="companion-version"]')?.getAttribute('content') || '';
}

export function getBootAssetHash(): string {
  return document.querySelector('meta[name="companion-asset-hash"]')?.getAttribute('content') || '';
}

export function emitAuthRequired(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('companion:auth-required'));
  }
}

export function formatChatErrorBanner(res: {
  status_code?: number;
  error_code?: string;
  auth_required?: boolean;
  network_error?: boolean;
  error?: string;
}): { title: string; detail: string; authRequired: boolean } {
  const code = Number(res.status_code || 0);
  const ecode = String(res.error_code || '').toLowerCase();
  const rawErr = String(res.error || '').trim();
  if (code === 401 || res.auth_required || ecode === 'auth_required') {
    return {
      title: '401 Auth Required',
      detail: rawErr || 'LAN session token is missing or expired. Pair your COMPANION_WEB_TOKEN, then click Retry.',
      authRequired: true,
    };
  }
  if (code === 403 && (ecode === 'csrf_mismatch' || rawErr === 'csrf_mismatch')) {
    return {
      title: '403 Session / CSRF Expired',
      detail: 'Your browser security token expired across a service restart or cookie reset. Click Retry to refresh and resend.',
      authRequired: false,
    };
  }
  if (code === 403) {
    return {
      title: '403 Request Rejected',
      detail: rawErr || 'Browser Origin/Host security guard rejected this request.',
      authRequired: false,
    };
  }
  if (code === 409 || ecode === 'turn_busy') {
    return {
      title: '409 Turn Busy',
      detail: rawErr || 'Companion is currently finishing an active turn. Wait a moment or click Retry.',
      authRequired: false,
    };
  }
  if (code === 413) {
    return {
      title: '413 Payload Too Large',
      detail: rawErr || 'Message or attachment exceeds the server request limit.',
      authRequired: false,
    };
  }
  if (ecode === 'admission_timeout') {
    return {
      title: 'Submission Timed Out',
      detail: rawErr || 'Companion did not acknowledge the message within 25s. Your message is preserved — click Retry.',
      authRequired: false,
    };
  }
  if (res.network_error) {
    return {
      title: 'Connection Unreachable',
      detail: rawErr
        ? `Could not reach Companion server (${rawErr}). Your message is preserved — click Retry.`
        : 'Could not reach Companion server. Your message is preserved — click Retry.',
      authRequired: false,
    };
  }
  if (ecode === 'llm_unreachable' || /could not reach|connection refused|timed out|offline/i.test(rawErr)) {
    return {
      title: 'LLM Endpoint Unreachable',
      detail: rawErr || 'The configured LLM provider could not be reached.',
      authRequired: false,
    };
  }
  if (ecode === 'interrupted') {
    return {
      title: 'Turn Interrupted',
      detail: rawErr || 'The active turn was interrupted before completion (service restarted or stream disconnected).',
      authRequired: false,
    };
  }
  return {
    title: code >= 400 ? `${code} Turn Error` : 'Turn Error',
    detail: rawErr || 'Turn failed unexpectedly — click Retry to resend.',
    authRequired: false,
  };
}

async function parseJsonSafe(res: Response): Promise<Record<string, unknown>> {
  const raw = await res.text();
  if (!raw) {
    return res.ok ? {} : { ok: false, error: `HTTP ${res.status}`, status_code: res.status };
  }
  try {
    const parsed = JSON.parse(raw);
    const out: Record<string, unknown> =
      parsed && typeof parsed === 'object'
        ? (parsed as Record<string, unknown>)
        : { ok: res.ok, value: parsed };
    if (!res.ok) out.status_code = res.status;
    return out;
  } catch {
    return {
      ok: false,
      status_code: res.status,
      error: raw.trim().slice(0, 240) || `HTTP ${res.status}`,
    };
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  try {
    const res = await fetch(path, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
      cache: 'no-store',
    });
    const data = await parseJsonSafe(res);
    if (res.status === 401) {
      data.ok = false;
      data.auth_required = true;
    }
    if (typeof data.csrf_token === 'string' && data.csrf_token) {
      setCsrfToken(data.csrf_token);
    }
    return data as T;
  } catch (err) {
    return {
      ok: false,
      network_error: true,
      error: err instanceof Error ? err.message : 'Network connection unavailable.',
    } as unknown as T;
  }
}

export async function apiPost<T>(
  path: string,
  body: Record<string, unknown> = {},
  retried = false,
  timeoutMs = 0
): Promise<T> {
  const csrf = getCsrfToken();
  const effTimeout = timeoutMs > 0 ? timeoutMs : path === '/api/chat' && body.async === true ? 25000 : 0;
  const ctrl = effTimeout > 0 && typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer =
    ctrl && effTimeout > 0
      ? window.setTimeout(() => {
          ctrl.abort();
        }, effTimeout)
      : 0;
  try {
    const res = await fetch(path, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'X-Companion-CSRF': csrf,
      },
      credentials: 'same-origin',
      body: JSON.stringify(body),
      ...(ctrl ? { signal: ctrl.signal } : {}),
    });
    const data = await parseJsonSafe(res);
    if (res.status === 401) {
      data.ok = false;
      data.auth_required = true;
      emitAuthRequired();
    }
    if (!retried && res.status === 403 && data && data.error === 'csrf_mismatch') {
      const h = await apiGet<{ ok?: boolean; csrf_token?: string; auth_required?: boolean }>('/api/health');
      if (h && h.csrf_token) {
        return apiPost<T>(path, body, true, timeoutMs);
      }
      if (h && h.auth_required) {
        data.auth_required = true;
        emitAuthRequired();
      }
    }
    return data as T;
  } catch (err) {
    const isAbort = err instanceof Error && err.name === 'AbortError';
    return {
      ok: false,
      network_error: true,
      ...(isAbort ? { error_code: 'admission_timeout' } : {}),
      error: isAbort
        ? 'Request timed out waiting for server acknowledgment.'
        : err instanceof Error
          ? err.message
          : 'Network connection unavailable.',
    } as unknown as T;
  } finally {
    if (timer) window.clearTimeout(timer);
  }
}

export async function pairLanToken(token: string): Promise<{
  ok: boolean;
  csrf_token?: string;
  auth_required?: boolean;
  error?: string;
}> {
  return apiGet(`/api/health?token=${encodeURIComponent(token.trim())}`);
}

export async function cancelActiveJob(jobId = 'active'): Promise<{
  ok?: boolean;
  cancelled?: boolean;
  status?: string;
  error?: string;
}> {
  const clean = (jobId || 'active').trim();
  return apiPost(`/api/jobs/${encodeURIComponent(clean)}/cancel`, { job_id: clean });
}

export async function reloadCompanionService(): Promise<{
  ok: boolean;
  summary?: string;
  error?: string;
}> {
  const res = await apiPost<{ ok?: boolean; summary?: string; error?: string }>('/api/reload', {});
  if (!res.ok) {
    return { ok: false, error: res.error || 'Service reload could not be started.' };
  }
  await new Promise((r) => window.setTimeout(r, 500));
  for (let attempt = 0; attempt < 25; attempt += 1) {
    await new Promise((r) => window.setTimeout(r, 400));
    const payload = await apiGet<{ ok?: boolean; csrf_token?: string; network_error?: boolean }>(
      '/api/health'
    );
    if (payload.ok && !payload.network_error && payload.csrf_token) {
      setCsrfToken(payload.csrf_token);
      return { ok: true, summary: 'Companion service reloaded with latest code.' };
    }
  }
  return { ok: true, summary: res.summary || 'Reload triggered — refreshing Studio...' };
}

export async function callAction<T = Record<string, unknown>>(
  actionName: string,
  params: Record<string, unknown> = {}
): Promise<T & { ok?: boolean; error?: string; summary?: string; network_error?: boolean }> {
  return apiPost(`/api/actions/${encodeURIComponent(actionName)}`, params);
}

export async function fetchStatus(): Promise<WebStatus & { auth_required?: boolean; network_error?: boolean }> {
  return apiGet<WebStatus & { auth_required?: boolean; network_error?: boolean }>('/api/status');
}

export async function fetchHistory(
  limit = 80,
  beforeId = 0
): Promise<{ ok: boolean; has_more?: boolean; messages: ChatMessage[] }> {
  const qs = beforeId > 0 ? `?limit=${limit}&before_id=${beforeId}` : `?limit=${limit}`;
  return apiGet<{ ok: boolean; has_more?: boolean; messages: ChatMessage[] }>(`/api/history${qs}`);
}

export interface JobPollResponse {
  ok?: boolean;
  network_error?: boolean;
  status_code?: number;
  job?: {
    job_id?: string;
    status?: string;
    response?: string;
    error?: string;
    error_code?: string;
    stream_text?: string;
    stream_thinking?: string;
    active_phase?: string;
    active_tool?: string;
    tool_steps?: ToolStepMeta[];
  };
}

export async function fetchJobSnapshot(jobId: string): Promise<JobPollResponse> {
  return apiGet<JobPollResponse>(`/api/jobs/${encodeURIComponent(jobId)}`);
}

export function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
    reader.onerror = () => reject(reader.error || new Error('Failed to read file'));
    reader.readAsDataURL(file);
  });
}

export async function uploadAttachment(
  file: File
): Promise<{ ok: boolean; attachment?: ChatAttachment; error?: string }> {
  const dataUrl = await fileToDataUrl(file);
  return apiPost<{ ok: boolean; attachment?: ChatAttachment; error?: string }>('/api/attachments', {
    filename: file.name || `upload-${Date.now()}.bin`,
    mime_type: file.type || 'application/octet-stream',
    data_base64: dataUrl,
  });
}

export async function fetchTelemetry(traceId = ''): Promise<TelemetryPayload> {
  const q = traceId ? `&trace_id=${encodeURIComponent(traceId)}` : '';
  return apiGet<TelemetryPayload>(`/api/telemetry?limit=16${q}`);
}

export interface StreamCallbacks {
  onTextDelta?: (delta: string) => void;
  onThinkingDelta?: (delta: string) => void;
  onOrphanThinkClosed?: (thinking: string) => void;
  onPhase?: (phase: string) => void;
  onTool?: (toolLabel: string, active: boolean, stepMeta?: ToolStepMeta) => void;
  onSnapshot?: (snap: {
    text: string;
    thinking: string;
    phase: string;
    activeTool: string;
    toolSteps: ToolStepMeta[];
  }) => void;
  onDone?: (payload: {
    status: string;
    response: string;
    error: string;
    errorCode?: string;
    cancelled?: boolean;
  }) => void;
  onError?: (err: string) => void;
}

export function subscribeJobStream(
  jobId: string,
  cb: StreamCallbacks,
  cursor = 0,
  watchdogMs = 20000
): () => void {
  const qs = cursor > 0 ? `?cursor=${cursor}` : '';
  let closed = false;
  let unsubscribed = false;
  let watchdogTimer = 0;

  const clearWatchdog = () => {
    if (watchdogTimer) {
      window.clearTimeout(watchdogTimer);
      watchdogTimer = 0;
    }
  };

  const pollFallback = async () => {
    clearWatchdog();
    let consecutiveNetErrors = 0;
    while (!unsubscribed && consecutiveNetErrors < 40) {
      const res = await fetchJobSnapshot(jobId);
      if (res.network_error) {
        consecutiveNetErrors += 1;
      } else if (res.status_code === 404) {
        if (!unsubscribed) {
          cb.onDone?.({
            status: 'interrupted',
            response: '',
            error: 'Turn interrupted before completion (service restarted or job expired).',
            errorCode: 'interrupted',
          });
        }
        return;
      } else {
        consecutiveNetErrors = 0;
        const job = res.job;
        if (job) {
          cb.onSnapshot?.({
            text: String(job.stream_text || ''),
            thinking: String(job.stream_thinking || ''),
            phase: String(job.active_phase || ''),
            activeTool: String(job.active_tool || ''),
            toolSteps: Array.isArray(job.tool_steps) ? job.tool_steps : [],
          });
          if (job.status && job.status !== 'running') {
            if (!unsubscribed) {
              cb.onDone?.({
                status: String(job.status),
                response: String(job.response || ''),
                error: String(job.error || ''),
                errorCode: String(job.error_code || ''),
                cancelled: job.status === 'cancelled',
              });
            }
            return;
          }
        }
      }
      await new Promise((r) => window.setTimeout(r, 900));
    }
    if (!unsubscribed) {
      cb.onError?.('Stream connection interrupted — could not reach server to finish turn.');
    }
  };

  if (typeof EventSource === 'undefined') {
    closed = true;
    void pollFallback();
    return () => {
      unsubscribed = true;
      clearWatchdog();
    };
  }

  const es = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/stream${qs}`);

  const armWatchdog = () => {
    clearWatchdog();
    if (closed || unsubscribed || watchdogMs <= 0) return;
    watchdogTimer = window.setTimeout(() => {
      if (!closed && !unsubscribed) {
        closed = true;
        es.close();
        void pollFallback();
      }
    }, watchdogMs);
  };

  armWatchdog();

  es.onmessage = (ev) => {
    armWatchdog();
    try {
      const data = JSON.parse(ev.data);
      const t = String(data.type || '');
      if (t === 'heartbeat') return;
      if (t === 'text_delta' && data.delta) cb.onTextDelta?.(String(data.delta));
      else if (t === 'thinking_delta' && data.delta) cb.onThinkingDelta?.(String(data.delta));
      else if (t === 'orphan_think_closed') cb.onOrphanThinkClosed?.(String(data.thinking || ''));
      else if (t === 'phase') cb.onPhase?.(String(data.phase || ''));
      else if (t === 'tool_start') {
        cb.onTool?.(String(data.summary || data.tool || ''), true);
      } else if (t === 'tool_end') {
        const label = String(data.summary || data.tool || '');
        cb.onTool?.(label, false, {
          tool: String(data.tool || ''),
          summary: label,
          ok: data.ok !== false,
          duration_ms: Number(data.duration_ms || 0),
        });
      } else if (t === 'done') {
        closed = true;
        clearWatchdog();
        es.close();
        cb.onDone?.({
          status: String(data.status || 'done'),
          response: String(data.response || ''),
          error: String(data.error || ''),
          errorCode: String(data.error_code || ''),
          cancelled: Boolean(data.cancelled) || data.status === 'cancelled',
        });
      }
    } catch {
      // Ignore malformed SSE frame
    }
  };

  es.onerror = () => {
    if (!closed) {
      closed = true;
      clearWatchdog();
      es.close();
      void pollFallback();
    }
  };

  return () => {
    unsubscribed = true;
    closed = true;
    clearWatchdog();
    es.close();
  };
}

export interface ApprovalResolvedDetail {
  approvalId: string;
  decision: 'approve' | 'deny';
  category?: string;
  resumedBackgroundJob?: boolean;
  source: 'chat' | 'popover' | 'inspector';
}

export function emitApprovalResolved(detail: ApprovalResolvedDetail): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent<ApprovalResolvedDetail>('companion:approval-resolved', { detail }));
  }
}

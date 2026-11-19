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
  retried = false
): Promise<T> {
  const csrf = getCsrfToken();
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
    });
    const data = await parseJsonSafe(res);
    if (res.status === 401) {
      data.ok = false;
      data.auth_required = true;
    }
    if (!retried && res.status === 403 && data && data.error === 'csrf_mismatch') {
      const h = await apiGet<{ ok?: boolean; csrf_token?: string }>('/api/health');
      if (h && h.csrf_token) {
        return apiPost<T>(path, body, true);
      }
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
  onDone?: (payload: { status: string; response: string; error: string; cancelled?: boolean }) => void;
  onError?: (err: string) => void;
}

export function subscribeJobStream(jobId: string, cb: StreamCallbacks, cursor = 0): () => void {
  const qs = cursor > 0 ? `?cursor=${cursor}` : '';
  const es = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/stream${qs}`);
  let closed = false;
  let unsubscribed = false;

  const pollFallback = async () => {
    let consecutiveNetErrors = 0;
    while (!unsubscribed && consecutiveNetErrors < 40) {
      const res = await apiGet<{
        ok?: boolean;
        network_error?: boolean;
        status_code?: number;
        job?: {
          status?: string;
          response?: string;
          error?: string;
          stream_text?: string;
          stream_thinking?: string;
          active_phase?: string;
          active_tool?: string;
          tool_steps?: ToolStepMeta[];
        };
      }>(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (res.network_error) {
        consecutiveNetErrors += 1;
      } else if (res.status_code === 404) {
        if (!unsubscribed) {
          cb.onDone?.({ status: 'completed', response: '', error: '' });
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
      cb.onError?.('Stream connection interrupted — refreshing conversation history.');
    }
  };

  es.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      const t = String(data.type || '');
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
        es.close();
        cb.onDone?.({
          status: String(data.status || 'done'),
          response: String(data.response || ''),
          error: String(data.error || ''),
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
      es.close();
      void pollFallback();
    }
  };

  return () => {
    unsubscribed = true;
    closed = true;
    es.close();
  };
}

import type { ChatAttachment, ChatMessage, TelemetryPayload, WebStatus } from './types';

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
    return res.ok ? {} : { ok: false, error: `HTTP ${res.status}` };
  }
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object'
      ? (parsed as Record<string, unknown>)
      : { ok: res.ok, value: parsed };
  } catch {
    return {
      ok: false,
      error: raw.trim().slice(0, 240) || `HTTP ${res.status}`,
    };
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  });
  const data = await parseJsonSafe(res);
  if (typeof data.csrf_token === 'string' && data.csrf_token) {
    setCsrfToken(data.csrf_token);
  }
  return data as T;
}

export async function apiPost<T>(
  path: string,
  body: Record<string, unknown> = {},
  retried = false
): Promise<T> {
  const csrf = getCsrfToken();
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
  if (!retried && res.status === 403 && data && data.error === 'csrf_mismatch') {
    try {
      await apiGet<{ ok?: boolean; csrf_token?: string }>('/api/health');
      return apiPost<T>(path, body, true);
    } catch {
      // Fall through with original response if health probe fails
    }
  }
  return data as T;
}

export async function reloadCompanionService(): Promise<{
  ok: boolean;
  summary?: string;
  error?: string;
}> {
  const prevCsrf = getCsrfToken();
  const res = await apiPost<{ ok?: boolean; summary?: string; error?: string }>('/api/reload', {});
  if (!res.ok) {
    return { ok: false, error: res.error || 'Service reload could not be started.' };
  }
  for (let attempt = 0; attempt < 25; attempt += 1) {
    await new Promise((r) => window.setTimeout(r, 450));
    try {
      const h = await fetch('/api/health', {
        method: 'GET',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (h.ok) {
        const payload = (await h.json()) as { ok?: boolean; csrf_token?: string };
        if (payload.ok && payload.csrf_token && (!prevCsrf || payload.csrf_token !== prevCsrf)) {
          setCsrfToken(payload.csrf_token);
          return { ok: true, summary: 'Companion service reloaded with latest code.' };
        }
      }
    } catch {
      // Expected briefly while old socket closes and replacement process binds
    }
  }
  return { ok: true, summary: res.summary || 'Reload triggered — refreshing Studio...' };
}

export async function callAction<T = Record<string, unknown>>(
  actionName: string,
  params: Record<string, unknown> = {}
): Promise<T & { ok?: boolean; error?: string; summary?: string }> {
  return apiPost(`/api/actions/${encodeURIComponent(actionName)}`, params);
}

export async function fetchStatus(): Promise<WebStatus> {
  return apiGet<WebStatus>('/api/status');
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
  onPhase?: (phase: string) => void;
  onTool?: (tool: string, active: boolean) => void;
  onDone?: (payload: { status: string; response: string; error: string }) => void;
  onError?: (err: string) => void;
}

export function subscribeJobStream(jobId: string, cb: StreamCallbacks): () => void {
  const es = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/stream`);
  let closed = false;
  let unsubscribed = false;

  const pollFallback = async () => {
    for (let attempt = 0; attempt < 15 && !unsubscribed; attempt += 1) {
      try {
        const res = await apiGet<{
          ok?: boolean;
          job?: { status?: string; response?: string; error?: string };
        }>(`/api/jobs/${encodeURIComponent(jobId)}`);
        const job = res.job;
        if (job && job.status && job.status !== 'running') {
          if (!unsubscribed) {
            cb.onDone?.({
              status: String(job.status),
              response: String(job.response || ''),
              error: String(job.error || ''),
            });
          }
          return;
        }
      } catch {
        // Ignore transient network blip while polling job status
      }
      await new Promise((r) => window.setTimeout(r, 900));
    }
    if (!unsubscribed) {
      cb.onError?.('Stream connection interrupted — polling final job status.');
    }
  };

  es.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      const t = String(data.type || '');
      if (t === 'text_delta' && data.delta) cb.onTextDelta?.(String(data.delta));
      else if (t === 'thinking_delta' && data.delta) cb.onThinkingDelta?.(String(data.delta));
      else if (t === 'phase') cb.onPhase?.(String(data.phase || ''));
      else if (t === 'tool_start') cb.onTool?.(String(data.tool || ''), true);
      else if (t === 'tool_end') cb.onTool?.(String(data.tool || ''), false);
      else if (t === 'done') {
        closed = true;
        es.close();
        cb.onDone?.({
          status: String(data.status || 'done'),
          response: String(data.response || ''),
          error: String(data.error || ''),
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

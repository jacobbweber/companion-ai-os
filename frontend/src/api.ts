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

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  });
  const data = await res.json();
  if (data && typeof data.csrf_token === 'string' && data.csrf_token) {
    setCsrfToken(data.csrf_token);
  }
  return data as T;
}

export async function apiPost<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
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
  const data = await res.json();
  return data as T;
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

export async function fetchHistory(limit = 100): Promise<{ ok: boolean; messages: ChatMessage[] }> {
  return apiGet<{ ok: boolean; messages: ChatMessage[] }>(`/api/history?limit=${limit}`);
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
      cb.onError?.('Stream connection interrupted — polling final job status.');
    }
  };

  return () => {
    closed = true;
    es.close();
  };
}

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import * as path from 'node:path';
import * as readline from 'node:readline';
import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { setCsrfToken } from '../src/api';
import { App } from '../src/App';

const PYTHON_FRONTIER_SERVER = `
import json, os, shutil, sys, tempfile
from pathlib import Path

ipc_out = sys.stdout
sys.stdout = sys.stderr

repo_root = Path(sys.argv[1])
tmp_dir = Path(tempfile.mkdtemp(prefix="companion_frontier_vitest_"))
os.environ["COMPANION_DATA_DIR"] = str(tmp_dir / "data")
os.environ["COMPANION_SECRETS_FILE"] = str(tmp_dir / "secrets.env")
sys.path.insert(0, str(repo_root))

import companion.memory_store as ms
import companion.model_registry as mr
from companion.approvals import get_approval_queue
from companion.core_actions import execute_core_action
from companion.workflows import DAGNode, DAGSpec, execute_dag_spec
from companion.projects import get_project_graph
from surfaces.web_assets import compute_static_asset_hash
from surfaces.web_server import create_web_server, dispatch_raw_http

store = ms.MemoryStore(db_path=tmp_dir / "data" / "companion.db")
ms._DEFAULT_STORE = store

# Seed project, wiki note, persisted trace-pill assistant turn, broken wikilink message, DAG run, and R3 approval
pg = get_project_graph(store=store)
pg.create_project(title="Homelab Ops", goal_summary="Local service reliability", kind="project")

execute_core_action(
    "wiki",
    {
        "subaction": "save",
        "path": "02_knowledge/homelab/ubuntu-rdp-gdm.md",
        "title": "Ubuntu RDP & GDM Restart",
        "content": "Restart display manager with \`sudo systemctl restart gdm\`. #linux",
        "tags": ["linux"],
    },
    actor="user",
    store=store,
)

store.log_message(
    "user",
    "Saved \`report.md\` with **bold** notes and link [[nonexistent-stub-note]].",
    session_tag="web_ui",
)
store.log_message(
    "assistant",
    "Saved Ubuntu RDP troubleshooting note to [[02_knowledge/homelab/ubuntu-rdp-gdm.md]].",
    session_tag="web_ui",
    meta={
        "thinking": "User asked to save Ubuntu RDP notes directly to Wiki.",
        "tool_steps": [
            {
                "tool": "wiki_manage",
                "summary": "wiki_manage: save -> 02_knowledge/homelab/ubuntu-rdp-gdm.md",
                "ok": True,
                "duration_ms": 18,
            }
        ],
        "duration_ms": 2100,
        "model": "qwen3-fast",
    },
)

# Execute a fast 1-node DAGSpec so Customizations has a completed run with node checkpoints
execute_dag_spec(
    DAGSpec(
        name="frontier-check",
        description="Fast 1-node checkpoint check",
        nodes=[DAGNode(node_id="verify_step", fn=lambda ctx: {"verified": True})],
    ),
    store=store,
    run_id="dagrun_frontier1",
)

# Seed a waiting R3 shell approval with full command preview
get_approval_queue(store=store).check_or_enqueue(
    category="r3_tool",
    action_name="run_shell",
    target="systemctl status ssh",
    summary="Approve R3 tool: run_shell",
    payload={"tool_name": "run_shell", "arguments": {"command": "systemctl restart gdm", "timeout": 30}},
)

# Avoid live network probe delay during discover_models test
mr.discover_provider_models = lambda *a, **kw: {
    "ok": True,
    "models": [{"id": "qwen3.5-9b-nvfp4"}, {"id": "qwen3.5-35b-a3b"}],
    "latency_ms": 3.5,
    "status_detail": "200 OK (2 models)",
}

def fake_chat(*args, **kwargs):
    from companion.streaming import emit_stream_event
    tools = kwargs.get("tools") or (args[1] if len(args) > 1 else None)
    model_name = str(kwargs.get("model") or "")
    if tools and any((t.get("function") or {}).get("name") == "calibration_ping" for t in tools):
        if "no-tools" in model_name:
            emit_stream_event({"type": "text_delta", "delta": "Plain text without tool call."})
            return {"choices": [{"message": {"role": "assistant", "content": "Plain text without tool call."}}]}
        emit_stream_event({"type": "thinking_delta", "delta": "Calling calibration_ping..."})
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "Calling calibration_ping...",
                    "tool_calls": [{"id": "call_cal_1", "type": "function", "function": {"name": "calibration_ping", "arguments": '{"echo": "ok"}'}}],
                }
            }]
        }
    emit_stream_event({"type": "thinking_delta", "delta": "Checking request..."})
    emit_stream_event({"type": "text_delta", "delta": "Frontier reply verified."})
    return {"choices": [{"message": {"role": "assistant", "content": "Frontier reply verified."}}]}

mr.chat = fake_chat

srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=fake_chat)
ipc_out.write(json.dumps({"ready": True, "csrf_token": sess.csrf_token, "asset_hash": compute_static_asset_hash()}) + "\\n")
ipc_out.flush()

for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    req = json.loads(line)
    if req.get("cmd") == "quit":
        break
    if req.get("cmd") == "seed_running_job":
        with sess._jobs_lock:
            sess._jobs["wjob_reattach1"] = {
                "job_id": "wjob_reattach1",
                "status": "running",
                "mode": "chat",
                "message": "Long running architectural analysis",
                "response": "",
                "error": "",
                "stream_text": "Streaming partial answer...",
                "stream_thinking": "Analyzing invariants...",
                "tool_steps": [{"tool": "file_glob", "summary": "file_glob: companion/*.py", "ok": True, "duration_ms": 9}],
                "active_phase": "EXECUTING",
                "active_tool": "read_file",
                "events": [],
                "created_at": "2026-11-18T12:00:00Z",
            }
        sess.active_job_id = "wjob_reattach1"
        sess.busy = True
        sess.active_action = "chat turn"
        ipc_out.write(json.dumps({"id": req.get("id"), "status": 200, "body": json.dumps({"ok": True})}) + "\\n")
        ipc_out.flush()
        continue
    if req.get("cmd") == "clear_running_job":
        with sess._jobs_lock:
            if "wjob_reattach1" in sess._jobs:
                sess._jobs["wjob_reattach1"]["status"] = "cancelled"
        sess.active_job_id = ""
        sess.busy = False
        sess.active_action = ""
        ipc_out.write(json.dumps({"id": req.get("id"), "status": 200, "body": json.dumps({"ok": True})}) + "\\n")
        ipc_out.flush()
        continue
    body_str = req.get("body") or ""
    raw_b64 = req.get("body_b64") or ""
    if raw_b64:
        import base64
        body_bytes = base64.b64decode(raw_b64)
    else:
        body_bytes = body_str.encode("utf-8") if body_str else None
    code, out = dispatch_raw_http(
        srv,
        method=req.get("method", "GET"),
        path=req.get("path", "/"),
        body=body_bytes,
        headers=req.get("headers") or {},
    )
    ipc_out.write(json.dumps({"id": req.get("id"), "status": code, "body": out}) + "\\n")
    ipc_out.flush()

srv.server_close()
store.close()
shutil.rmtree(tmp_dir, ignore_errors=True)
`;

class PyFrontierBridge {
  private proc!: ChildProcessWithoutNullStreams;
  private rl!: readline.Interface;
  private pending = new Map<number, (msg: { status: number; body: string }) => void>();
  private bootResolve: ((line: string) => void) | null = null;
  private nextId = 1;
  public csrfToken = '';
  public assetHash = '';

  async start(): Promise<void> {
    const repoRoot = path.resolve(__dirname, '..', '..');
    this.proc = spawn('python', ['-B', '-c', PYTHON_FRONTIER_SERVER, repoRoot], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.rl = readline.createInterface({ input: this.proc.stdout });
    this.rl.on('line', (line) => {
      if (this.bootResolve) {
        const fn = this.bootResolve;
        this.bootResolve = null;
        fn(line);
        return;
      }
      const msg = JSON.parse(line);
      const waiter = this.pending.get(msg.id);
      if (waiter) {
        this.pending.delete(msg.id);
        waiter({ status: msg.status, body: msg.body });
      }
    });
    let errBuf = '';
    this.proc.stderr.on('data', (chunk) => {
      errBuf += String(chunk);
    });
    const bootLine = await new Promise<string>((resolve, reject) => {
      this.bootResolve = resolve;
      this.proc.once('exit', (code) => {
        if (this.bootResolve) {
          this.bootResolve = null;
          reject(new Error(`Python bridge exited (${code}): ${errBuf}`));
        }
      });
    });
    const boot = JSON.parse(bootLine);
    this.csrfToken = boot.csrf_token;
    this.assetHash = boot.asset_hash || '';
  }

  async cmd(command: string): Promise<void> {
    const id = this.nextId++;
    return new Promise((resolve) => {
      this.pending.set(id, () => resolve());
      this.proc.stdin.write(JSON.stringify({ id, cmd: command }) + '\n');
    });
  }

  async request(
    method: string,
    urlPath: string,
    body?: string,
    headers?: Record<string, string>,
    bodyB64?: string
  ): Promise<{ status: number; body: string }> {
    const id = this.nextId++;
    const payload = JSON.stringify({
      id,
      method,
      path: urlPath,
      body: body || '',
      body_b64: bodyB64 || '',
      headers: { Origin: 'http://127.0.0.1', ...(headers || {}) },
    });
    return new Promise((resolve) => {
      this.pending.set(id, resolve);
      this.proc.stdin.write(payload + '\n');
    });
  }

  async stop(): Promise<void> {
    try {
      this.proc.stdin.write(JSON.stringify({ cmd: 'quit' }) + '\n');
      this.rl.close();
      this.proc.kill();
    } catch {
      // ignore cleanup
    }
  }
}

async function flushUi(ms = 75): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, ms));
  });
}

function setInputValue(
  el: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement,
  val: string
): void {
  const proto =
    el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
  setter?.call(el, val);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

describe('Frontier Harness Interactive Regressions (WEB-1..6)', () => {
  const bridge = new PyFrontierBridge();
  let container: HTMLDivElement;
  let root: Root;
  let simulateAuth401 = false;
  let simulateChatNetworkDrop = false;
  let simulateChatHttpStatus = 0;
  let simulateChatHttpBody: Record<string, unknown> = {};
  let simulateServerVersion = '';
  let cancelCalls: string[] = [];

  beforeAll(async () => {
    (globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    await bridge.start();
    document.head.innerHTML = `<meta name="companion-csrf" content="${bridge.csrfToken}"><meta name="companion-version" content="5.1.1"><meta name="companion-asset-hash" content="${bridge.assetHash}">`;

    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const urlStr = typeof input === 'string' ? input : input.toString();
      const parsed = new URL(urlStr, 'http://127.0.0.1');

      if (simulateAuth401 && parsed.pathname === '/api/status') {
        return new Response(JSON.stringify({ ok: false, error: 'Unauthorized', auth_required: true }), {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (parsed.pathname === '/api/health' && parsed.searchParams.get('token')) {
        simulateAuth401 = false;
        return new Response(
          JSON.stringify({ ok: true, service: 'companion-web-ui', csrf_token: bridge.csrfToken }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (simulateChatNetworkDrop && parsed.pathname === '/api/chat') {
        throw new TypeError('Failed to fetch');
      }
      if (simulateChatHttpStatus > 0 && parsed.pathname === '/api/chat') {
        return new Response(JSON.stringify(simulateChatHttpBody), {
          status: simulateChatHttpStatus,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (parsed.pathname.endsWith('/cancel') && parsed.pathname.startsWith('/api/jobs/')) {
        cancelCalls.push(parsed.pathname);
        await bridge.cmd('clear_running_job');
        return new Response(JSON.stringify({ ok: true, cancelled: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }

      const headersObj: Record<string, string> = {};
      if (init?.headers) {
        const h = new Headers(init.headers);
        h.forEach((v, k) => {
          headersObj[k] = v;
        });
      }
      let bodyStr: string | undefined;
      let bodyB64: string | undefined;
      if (typeof init?.body === 'string') {
        bodyStr = init.body;
      } else if (init?.body instanceof Uint8Array) {
        bodyB64 = Buffer.from(init.body).toString('base64');
      } else if (init?.body instanceof File || init?.body instanceof Blob) {
        const ab = await init.body.arrayBuffer();
        bodyB64 = Buffer.from(ab).toString('base64');
      }

      const res = await bridge.request(
        init?.method || 'GET',
        parsed.pathname + parsed.search,
        bodyStr,
        headersObj,
        bodyB64
      );
      let outBody = res.body;
      if (simulateServerVersion && parsed.pathname === '/api/status' && res.status === 200) {
        const parsedSt = JSON.parse(res.body);
        parsedSt.app_version = simulateServerVersion;
        parsedSt.asset_hash = 'new_hash_9999';
        outBody = JSON.stringify(parsedSt);
      }
      return new Response(outBody, {
        status: res.status,
        headers: { 'Content-Type': 'application/json' },
      });
    }) as typeof fetch;

    class IdleEventSource {
      onmessage: ((ev: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      close(): void {}
    }
    (globalThis as unknown as Record<string, unknown>).EventSource = IdleEventSource;

    Object.defineProperty(window, 'innerWidth', { value: 1366, writable: true });
    container = document.createElement('div');
    document.body.appendChild(container);
    await act(async () => {
      root = createRoot(container);
      root.render(<App />);
    });
    await flushUi(140);
  });

  afterAll(async () => {
    await act(async () => {
      root?.unmount();
    });
    container?.remove();
    await bridge.stop();
  });

  it('hydrates persisted meta_json tool/thinking pills, renders user markdown, and creates missing wikilink notes (WEB-2, WEB-3, WEB-5)', async () => {
    // Persisted trace pill from SQLite `conversations.meta_json`
    const tracePill = container.querySelector('[data-testid="msg-trace-pill"]') as HTMLButtonElement | null;
    expect(tracePill).not.toBeNull();
    expect(tracePill!.textContent).toContain('Used 1 tool');
    expect(tracePill!.textContent).toContain('Thought for 2s');

    await act(async () => {
      tracePill!.click();
    });
    await flushUi(50);
    expect(container.textContent).toContain('wiki_manage: save -> 02_knowledge/homelab/ubuntu-rdp-gdm.md');
    expect(container.textContent).toContain('User asked to save Ubuntu RDP notes directly to Wiki.');

    // User bubble rendered via <MarkdownView> (bold + inline code + clickable wikilink)
    const userBubble = container.querySelector('.msg-user-bubble') as HTMLElement | null;
    expect(userBubble).not.toBeNull();
    expect(userBubble!.querySelector('strong')?.textContent).toBe('bold');
    expect(userBubble!.querySelector('code')?.textContent).toBe('report.md');

    // Click broken `[[nonexistent-stub-note]]` -> opens missing note card -> creates stub in 01_inbox/
    const wikiLinks = Array.from(userBubble!.querySelectorAll('button, a')).filter((el) =>
      (el.textContent || '').includes('nonexistent-stub-note')
    );
    expect(wikiLinks.length).toBeGreaterThan(0);
    await act(async () => {
      (wikiLinks[0] as HTMLElement).click();
    });
    await flushUi(100);

    const createBtn = container.querySelector('[data-testid="wiki-create-missing-btn"]') as HTMLButtonElement | null;
    expect(createBtn).not.toBeNull();
    await act(async () => {
      createBtn!.click();
    });
    await flushUi(120);
    expect(container.querySelector('[data-testid="knowledge-surface"]')).not.toBeNull();

    // Return to Chat surface
    const chatNav = container.querySelector('[data-testid="nav-chat"]') as HTMLButtonElement;
    await act(async () => {
      chatNav.click();
    });
    await flushUi(90);
  });

  it('handles 401 LAN token pairing, preserves draft & attachment on network error, and supports mid-turn re-attach + Stop (WEB-1, WEB-2, WEB-4)', async () => {
    // 1. Trigger 401 -> LAN token pairing modal
    simulateAuth401 = true;
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    await flushUi(90);
    const tokenModal = container.querySelector('[data-testid="lan-token-modal"]');
    expect(tokenModal).not.toBeNull();

    const tokenInput = container.querySelector('[data-testid="lan-token-input"]') as HTMLInputElement;
    await act(async () => {
      setInputValue(tokenInput, 'test-lan-secret-123');
    });
    const pairBtn = tokenModal!.querySelector('button[type="submit"]') as HTMLButtonElement;
    await act(async () => {
      pairBtn.click();
    });
    await flushUi(100);
    expect(container.querySelector('[data-testid="lan-token-modal"]')).toBeNull();

    // 2. Paste file attachment + type draft + simulate network drop -> draft & attachment preserved, zero ghost bubble
    const textarea = container.querySelector('[data-testid="chat-composer-input"]') as HTMLTextAreaElement;
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const testFile = new File(['alpha beta gamma'], 'notes.txt', { type: 'text/plain' });
    Object.defineProperty(fileInput, 'files', { value: [testFile], configurable: true });
    await act(async () => {
      fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await flushUi(90);
    expect(container.textContent).toContain('notes.txt');

    await act(async () => {
      setInputValue(textarea, 'Preserve this carefully drafted message');
    });
    await flushUi(40);

    const beforeMsgCount = container.querySelectorAll('.msg-assistant-block').length;
    simulateChatNetworkDrop = true;
    const sendBtn = container.querySelector('[data-testid="composer-send-btn"]') as HTMLButtonElement;
    await act(async () => {
      sendBtn.click();
    });
    await flushUi(90);
    simulateChatNetworkDrop = false;

    expect(textarea.value).toBe('Preserve this carefully drafted message');
    expect(container.textContent).toContain('notes.txt');
    expect(container.querySelectorAll('.msg-assistant-block').length).toBe(beforeMsgCount);
    const netErrCard = container.querySelector('[data-testid="chat-delivery-error-card"]');
    expect(netErrCard).not.toBeNull();
    expect(netErrCard!.textContent).toContain('Connection Unreachable');
    const dismissErrBtn = container.querySelector('[data-testid="chat-error-dismiss-btn"]') as HTMLButtonElement;
    await act(async () => {
      dismissErrBtn.click();
    });
    await flushUi(30);
    expect(container.querySelector('[data-testid="chat-delivery-error-card"]')).toBeNull();

    // Clear draft + `/plan` slash command mode pill check
    await act(async () => {
      setInputValue(textarea, '/plan');
    });
    await flushUi(40);
    const planSlashRow = Array.from(container.querySelectorAll('.popover-row')).find((el) =>
      (el.textContent || '').includes('/plan')
    ) as HTMLButtonElement | undefined;
    expect(planSlashRow).toBeDefined();
    await act(async () => {
      planSlashRow!.click();
    });
    await flushUi(40);
    expect(container.querySelector('[data-testid="composer-mode-pill-plan"]')).not.toBeNull();

    // 3. Seed an active running job (simulating F5 reload mid-turn) -> auto re-attach + `■ Stop`
    await bridge.cmd('seed_running_job');
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    await flushUi(120);

    expect(container.textContent).toContain('Streaming partial answer...');
    const stopBtn = container.querySelector('[data-testid="composer-stop-btn"]') as HTMLButtonElement | null;
    expect(stopBtn).not.toBeNull();

    await act(async () => {
      stopBtn!.click();
    });
    await flushUi(100);
    expect(cancelCalls.some((p) => p.includes('/api/jobs/wjob_reattach1/cancel'))).toBe(true);
  });

  it('renders Approval command previews, saves Models & Tiers settings, inspects DAG Run node checkpoints, and searches Wiki via Ctrl+K (WEB-4, WEB-5, WEB-6)', async () => {
    // 1. Approval command preview in AuxSplitPane
    const viewApprovalsBtn = container.querySelector('[data-testid="chat-approvals-view-all"]') as HTMLButtonElement | null;
    expect(viewApprovalsBtn).not.toBeNull();
    await act(async () => {
      viewApprovalsBtn!.click();
    });
    await flushUi(100);

    const previewBtn = container.querySelector('[data-testid^="preview-approval-"]') as HTMLButtonElement | null;
    expect(previewBtn).not.toBeNull();
    await act(async () => {
      previewBtn!.click();
    });
    await flushUi(80);
    expect(container.textContent).toContain('systemctl restart gdm');

    // 2. Settings -> Models & Tiers: probe provider, update tier reasoning effort (`save_tier`), verify 75% compaction
    const openSettingsBtn = container.querySelector('[data-testid="open-settings-btn"]') as HTMLButtonElement;
    await act(async () => {
      openSettingsBtn.click();
    });
    await flushUi(80);

    const modelsTab = container.querySelector('[data-testid="settings-tab-models"]') as HTMLButtonElement;
    await act(async () => {
      modelsTab.click();
    });
    await flushUi(100);

    const probeBtn = container.querySelector('[data-testid="probe-provider-local-default"]') as HTMLButtonElement;
    expect(probeBtn).not.toBeNull();
    await act(async () => {
      probeBtn.click();
    });
    await flushUi(90);

    const calFastBtn = container.querySelector('[data-testid="calibrate-tier-btn-fast"]') as HTMLButtonElement;
    expect(calFastBtn).not.toBeNull();
    await act(async () => {
      calFastBtn.click();
    });
    await flushUi(110);
    const calFastRes = container.querySelector('[data-testid="calibrate-tier-result-fast"]') as HTMLElement | null;
    expect(calFastRes).not.toBeNull();
    expect(calFastRes!.textContent).toContain('✓ Stream · ✓ Tool Call · ✓ Reasoning');

    const cfgAudio = container.querySelector('[data-testid="configure-tier-audio"]') as HTMLButtonElement;
    await act(async () => {
      cfgAudio.click();
    });
    await flushUi(60);
    const audioModelInput = container.querySelector('[data-testid="tier-model-audio"]') as HTMLInputElement;
    await act(async () => {
      audioModelInput.focus();
      setInputValue(audioModelInput, 'no-tools-local');
      audioModelInput.blur();
    });
    await flushUi(110);
    const calAudioBtn = container.querySelector('[data-testid="calibrate-tier-btn-audio"]') as HTMLButtonElement;
    await act(async () => {
      calAudioBtn.click();
    });
    await flushUi(110);
    const calAudioRes = container.querySelector('[data-testid="calibrate-tier-result-audio"]') as HTMLElement | null;
    expect(calAudioRes).not.toBeNull();
    expect(calAudioRes!.textContent).toContain('Tool-Call Template Warning');

    const cfgFast = container.querySelector('[data-testid="configure-tier-fast"]') as HTMLButtonElement;
    await act(async () => {
      cfgFast.click();
    });
    await flushUi(60);

    const effortSelect = container.querySelector('[data-testid="tier-effort-fast"]') as HTMLSelectElement;
    expect(effortSelect).not.toBeNull();
    await act(async () => {
      setInputValue(effortSelect, 'high');
    });
    await flushUi(100);
    expect(effortSelect.value).toBe('high');

    const compAdvBtn = container.querySelector('[data-testid="toggle-compaction-adv"]') as HTMLButtonElement;
    await act(async () => {
      compAdvBtn.click();
    });
    await flushUi(50);
    const keepRecentInput = container.querySelector('[data-testid="compaction-keep-recent"]') as HTMLInputElement;
    expect(keepRecentInput).not.toBeNull();
    expect(keepRecentInput.value).toBe('16');
    await act(async () => {
      setInputValue(keepRecentInput, '20');
    });
    const saveCompBtn = container.querySelector('[data-testid="save-compaction-btn"]') as HTMLButtonElement;
    await act(async () => {
      saveCompBtn.click();
    });
    await flushUi(90);
    expect(keepRecentInput.value).toBe('20');

    // Close Settings modal
    const backdrop = container.querySelector('[data-testid="settings-modal-backdrop"]') as HTMLElement;
    await act(async () => {
      backdrop.click();
    });
    await flushUi(60);

    // 3. Customizations -> Python DAGs -> click completed DAG run -> DAG Run Inspector shows node checkpoints (no misleading Resume button)
    const custNav = container.querySelector('[data-testid="nav-customizations"]') as HTMLButtonElement;
    await act(async () => {
      custNav.click();
    });
    await flushUi(100);

    const dagsTabBtn = Array.from(container.querySelectorAll('.segmented-pill')).find((el) =>
      (el.textContent || '').includes('Workflows')
    ) as HTMLButtonElement;
    await act(async () => {
      dagsTabBtn.click();
    });
    await flushUi(80);

    const runRow = Array.from(container.querySelectorAll('.card-row')).find((el) =>
      (el.textContent || '').includes('dagrun_')
    ) as HTMLElement | undefined;
    expect(runRow).toBeDefined();
    await act(async () => {
      runRow!.click();
    });
    await flushUi(100);

    const dagInspector = container.querySelector('[data-testid="dag-run-inspector"]') as HTMLElement | null;
    expect(dagInspector).not.toBeNull();
    expect(dagInspector!.textContent).toContain('Step Checkpoints');
    expect(dagInspector!.textContent).not.toContain('Approve & Resume DAG');

    // 4. Ctrl+K Command Palette -> Wiki search + Escape dismiss
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true }));
    });
    await flushUi(60);
    const paletteInput = container.querySelector('[data-testid="command-palette-input"]') as HTMLInputElement | null;
    expect(paletteInput).not.toBeNull();
    await act(async () => {
      setInputValue(paletteInput!, 'ubuntu');
    });
    await flushUi(220);
    expect(container.textContent).toContain('Knowledge Wiki Matches');
    expect(container.textContent).toContain('Ubuntu RDP & GDM Restart');

    await act(async () => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    await flushUi(50);
    expect(container.querySelector('[data-testid="command-palette-input"]')).toBeNull();
  });

  it('enforces Turn Delivery invariants: 401/403/409/500 inline cards + 1-click Retry, stale CSRF auto-heal, wedged-SSE busy self-healing, and version-drift banner', async () => {
    const chatNav = container.querySelector('[data-testid="nav-chat"]') as HTMLButtonElement;
    await act(async () => {
      chatNav.click();
    });
    await flushUi(80);

    const textarea = container.querySelector('[data-testid="chat-composer-input"]') as HTMLTextAreaElement;

    // 1. Simulate HTTP 409 Turn Busy -> inline error card + preserved draft
    await act(async () => {
      setInputValue(textarea, 'continue');
    });
    await flushUi(30);
    simulateChatHttpStatus = 409;
    simulateChatHttpBody = {
      ok: false,
      error_code: 'turn_busy',
      error: 'Companion is currently busy processing an active job.',
    };
    const sendBtn1 = container.querySelector('[data-testid="composer-send-btn"]') as HTMLButtonElement;
    await act(async () => {
      sendBtn1.click();
    });
    await flushUi(80);
    let errCard = container.querySelector('[data-testid="chat-delivery-error-card"]');
    expect(errCard).not.toBeNull();
    expect(errCard!.textContent).toContain('409 Turn Busy');
    expect(textarea.value).toBe('continue');

    // 2. Simulate HTTP 403 Origin rejection -> updates inline error card
    simulateChatHttpStatus = 403;
    simulateChatHttpBody = { ok: false, error: 'Origin mismatch (tailscale != lan)' };
    await act(async () => {
      (container.querySelector('[data-testid="chat-error-retry-btn"]') as HTMLButtonElement).click();
    });
    await flushUi(80);
    errCard = container.querySelector('[data-testid="chat-delivery-error-card"]');
    expect(errCard!.textContent).toContain('403 Request Rejected');

    // 3. Simulate HTTP 500 Turn Error -> updates inline error card
    simulateChatHttpStatus = 500;
    simulateChatHttpBody = { ok: false, error: 'Turn error: RuntimeError: upstream crashed' };
    await act(async () => {
      (container.querySelector('[data-testid="chat-error-retry-btn"]') as HTMLButtonElement).click();
    });
    await flushUi(80);
    errCard = container.querySelector('[data-testid="chat-delivery-error-card"]');
    expect(errCard!.textContent).toContain('500 Turn Error');

    // 4. Simulate HTTP 401 on POST /api/chat -> opens LAN token modal immediately + 401 card
    simulateChatHttpStatus = 401;
    simulateChatHttpBody = { ok: false, auth_required: true, error: 'LAN token expired' };
    await act(async () => {
      (container.querySelector('[data-testid="chat-error-retry-btn"]') as HTMLButtonElement).click();
    });
    await flushUi(80);
    expect(container.querySelector('[data-testid="lan-token-modal"]')).not.toBeNull();
    errCard = container.querySelector('[data-testid="chat-delivery-error-card"]');
    expect(errCard!.textContent).toContain('401 Auth Required');

    // Pair token to dismiss modal, corrupt cached CSRF to verify automatic 403 csrf_mismatch self-heal, and click 1-click Retry!
    simulateChatHttpStatus = 0;
    const tokenInput = container.querySelector('[data-testid="lan-token-input"]') as HTMLInputElement;
    await act(async () => {
      setInputValue(tokenInput, 'valid-lan-token');
    });
    const pairSubmit = container.querySelector('[data-testid="lan-token-modal"] button[type="submit"]') as HTMLButtonElement;
    await act(async () => {
      pairSubmit.click();
    });
    await flushUi(80);
    expect(container.querySelector('[data-testid="lan-token-modal"]')).toBeNull();

    setCsrfToken('expired_csrf_after_restart');
    const retryBtn = container.querySelector('[data-testid="chat-error-retry-btn"]') as HTMLButtonElement;
    await act(async () => {
      retryBtn.click();
    });
    await flushUi(150);

    // `IdleEventSource` never emits `done`, simulating a wedged mobile/Tailscale SSE socket while Python finished the turn.
    // Reconciling `/api/status` (`online` event) must self-heal `sending`, render the reply, and clear the preserved draft!
    expect(container.querySelector('[data-testid="version-drift-banner"]')).toBeNull();
    expect(container.querySelector('[data-testid="sidebar-version-label"]')?.textContent).toBe('v5.1.1');
    for (let poll = 0; poll < 12; poll += 1) {
      await flushUi(120);
      await act(async () => {
        window.dispatchEvent(new Event('online'));
      });
      await flushUi(120);
      if ((container.textContent || '').includes('Frontier reply verified.')) break;
    }

    expect(container.querySelector('[data-testid="chat-delivery-error-card"]')).toBeNull();
    expect(textarea.value).toBe('');
    expect(container.textContent).toContain('Frontier reply verified.');
    expect(container.querySelector('[data-testid="composer-send-btn"]')).not.toBeNull();

    // 5. Simulate server upgrade (`v5.9.0`) -> polite version-drift reload banner appears
    simulateServerVersion = '5.9.0';
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    await flushUi(100);
    const driftBanner = container.querySelector('[data-testid="version-drift-banner"]');
    expect(driftBanner).not.toBeNull();
    expect(driftBanner!.textContent).toContain('v5.9.0');
    simulateServerVersion = '';
  });
});

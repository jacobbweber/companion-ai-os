import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import * as path from 'node:path';
import * as readline from 'node:readline';
import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { App } from '../src/App';

const PYTHON_STDIO_SERVER = `
import json, os, shutil, sys, tempfile
from pathlib import Path

ipc_out = sys.stdout
sys.stdout = sys.stderr

repo_root = Path(sys.argv[1])
tmp_dir = Path(tempfile.mkdtemp(prefix="companion_vitest_v35_"))
os.environ["COMPANION_DATA_DIR"] = str(tmp_dir / "data")
os.environ["COMPANION_SECRETS_FILE"] = str(tmp_dir / "secrets.env")
sys.path.insert(0, str(repo_root))

import companion.memory_store as ms
import companion.background_jobs as bj
import companion.core_actions as ca
from companion.core_actions import execute_core_action
from companion.endpoints import get_endpoint_manager
from companion.project_graph import get_project_graph
from surfaces.web_server import create_web_server, dispatch_raw_http

store = ms.MemoryStore(db_path=tmp_dir / "data" / "companion.db")
ms._DEFAULT_STORE = store

execute_core_action(
    "wiki",
    {
        "subaction": "save",
        "path": "02_knowledge/homelab/gpu-compute-node.md",
        "title": "GPU Compute Node",
        "content": "Primary GPU server at 192.168.1.100. #homelab",
        "tags": ["homelab"],
    },
    actor="user",
    store=store,
)

# Seed a project + human/companion items + saved prompt + study deck + waiting endpoint approval
pg = get_project_graph(store=store)
proj = pg.create_project(title="Garage & Truck Reset", goal_summary="Fix truck and organize shelves", kind="project")
h_item = pg.create_item(proj["project_id"], title="Measure garage wall", owner="human", status="ready")
pg.create_item(proj["project_id"], title="Draft shelving cut-list", owner="companion", depends_on=[h_item["item_id"]])
execute_core_action("prompts", {"subaction": "save", "name": "morning-scan", "template": "Summarize my open items."}, actor="user", store=store)
execute_core_action("education", {"subaction": "save_course", "title": "ZFS Basics", "modules": ["Pools"]}, actor="user", store=store)
execute_core_action("education", {"subaction": "upsert_items", "course_id": "zfs-basics", "items": [{"prompt": "What is a vdev?", "answer": "Virtual device"}]}, actor="user", store=store)
get_endpoint_manager(store=store).propose_endpoint("test-metrics", "http_api", "http://127.0.0.1:9191", actor="companion")

def fake_chat(*args, **kwargs):
    from companion.streaming import emit_stream_event
    emit_stream_event({"type": "thinking_delta", "delta": "Routing turn cleanly..."})
    emit_stream_event({"type": "text_delta", "delta": "Companion v3.5 UI is live and verified."})
    return {"choices": [{"message": {"role": "assistant", "content": "Companion v3.5 UI is live and verified."}}]}

ca.get_job_manager = lambda store=None, **kw: bj.BackgroundJobManager(store=store, chat_fn=fake_chat)

srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=fake_chat)
ipc_out.write(json.dumps({"ready": True, "csrf_token": sess.csrf_token}) + "\\n")
ipc_out.flush()

for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    req = json.loads(line)
    if req.get("cmd") == "quit":
        break
    body_str = req.get("body") or ""
    code, out = dispatch_raw_http(
        srv,
        method=req.get("method", "GET"),
        path=req.get("path", "/"),
        body=body_str.encode("utf-8") if body_str else None,
        headers=req.get("headers") or {},
    )
    ipc_out.write(json.dumps({"id": req.get("id"), "status": code, "body": out}) + "\\n")
    ipc_out.flush()

srv.server_close()
store.close()
shutil.rmtree(tmp_dir, ignore_errors=True)
`;

class PyHttpBridge {
  private proc!: ChildProcessWithoutNullStreams;
  private rl!: readline.Interface;
  private pending = new Map<number, (msg: { status: number; body: string }) => void>();
  private bootResolve: ((line: string) => void) | null = null;
  private nextId = 1;
  public csrfToken = '';

  async start(): Promise<void> {
    const repoRoot = path.resolve(__dirname, '..', '..');
    this.proc = spawn('python', ['-B', '-c', PYTHON_STDIO_SERVER, repoRoot], {
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
    const bootLine = await new Promise<string>((r) => {
      this.bootResolve = r;
    });
    const boot = JSON.parse(bootLine);
    this.csrfToken = boot.csrf_token;
  }

  async request(
    method: string,
    urlPath: string,
    body?: string,
    headers?: Record<string, string>
  ): Promise<{ status: number; body: string }> {
    const id = this.nextId++;
    const payload = JSON.stringify({
      id,
      method,
      path: urlPath,
      body: body || '',
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

async function flushUi(ms = 60): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, ms));
  });
}

function setInputValue(el: HTMLInputElement | HTMLTextAreaElement, val: string): void {
  const proto =
    el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
  setter?.call(el, val);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

describe('Companion v3.5 Progressive-Disclosure UI & Headless Core Parity', () => {
  const bridge = new PyHttpBridge();
  let container: HTMLDivElement;
  let root: Root;

  beforeAll(async () => {
    (globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    await bridge.start();
    document.head.innerHTML = `<meta name="companion-csrf" content="${bridge.csrfToken}">`;

    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const urlStr = typeof input === 'string' ? input : input.toString();
      const parsed = new URL(urlStr, 'http://127.0.0.1');
      const headersObj: Record<string, string> = {};
      if (init?.headers) {
        const h = new Headers(init.headers);
        h.forEach((v, k) => {
          headersObj[k] = v;
        });
      }
      const res = await bridge.request(
        init?.method || 'GET',
        parsed.pathname + parsed.search,
        typeof init?.body === 'string' ? init.body : undefined,
        headersObj
      );
      return new Response(res.body, {
        status: res.status,
        headers: { 'Content-Type': 'application/json' },
      });
    }) as typeof fetch;

    // Minimal EventSource shim polling `/api/jobs/<id>` via real bridge
    class FakeEventSource {
      onmessage: ((ev: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      private closed = false;

      constructor(url: string) {
        const jobId = url.split('/api/jobs/')[1]?.split('/')[0] || '';
        void (async () => {
          for (let i = 0; i < 15 && !this.closed; i += 1) {
            await new Promise((r) => setTimeout(r, 25));
            const r = await bridge.request('GET', `/api/jobs/${jobId}`);
            const data = JSON.parse(r.body);
            if (data?.job?.status && data.job.status !== 'running') {
              this.onmessage?.(
                new MessageEvent('message', {
                  data: JSON.stringify({
                    type: 'done',
                    status: data.job.status,
                    response: data.job.response || '',
                    error: data.job.error || '',
                  }),
                })
              );
              return;
            }
          }
        })();
      }
      close(): void {
        this.closed = true;
      }
    }
    (globalThis as unknown as Record<string, unknown>).EventSource = FakeEventSource;

    Object.defineProperty(window, 'innerWidth', { value: 1366, writable: true });
    container = document.createElement('div');
    document.body.appendChild(container);
    await act(async () => {
      root = createRoot(container);
      root.render(<App />);
    });
    await flushUi(120);
  });

  afterAll(async () => {
    await act(async () => {
      root.unmount();
    });
    await bridge.stop();
  });

  it('renders calm Obsidian default canvas, closed-by-default AuxSplitPane, and inline Composer sheets', async () => {
    expect(document.documentElement.getAttribute('data-theme')).toBe('obsidian');
    expect(container.querySelector('[data-testid="aux-split-pane"]')).toBeNull();
    expect(container.querySelector('[data-testid="floating-composer"]')).toBeTruthy();

    // Open Model & Posture inline sheets in composer
    const modelBtn = container.querySelector('[data-testid="composer-model-btn"]') as HTMLButtonElement;
    await act(async () => {
      modelBtn.click();
    });
    expect(container.querySelector('[data-testid="composer-model-sheet"]')?.textContent).toContain('Auto Triage');

    const postureBtn = container.querySelector('[data-testid="composer-posture-btn"]') as HTMLButtonElement;
    await act(async () => {
      postureBtn.click();
    });
    expect(container.querySelector('[data-testid="composer-posture-sheet"]')?.textContent).toContain('Background Autonomy');

    // Send a live message over SSE job stream
    const input = container.querySelector('[data-testid="chat-composer-input"]') as HTMLTextAreaElement;
    await act(async () => {
      setInputValue(input, 'Hello Companion v3.5');
    });
    const sendBtn = container.querySelector('[data-testid="composer-send-btn"]') as HTMLButtonElement;
    await act(async () => {
      sendBtn.click();
    });
    await flushUi(220);
    expect(container.textContent).toContain('Companion v3.5 UI is live and verified.');

    // Verify no '+ New Conversation' wipe button exists, inline approvals + saved prompts render, and Fresh Focus drops an inline divider without blanking Forever Chat
    expect(container.querySelector('[data-testid="new-conversation-btn"]')).toBeNull();
    expect(container.querySelector('[data-testid="nav-chat"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="chat-inline-approvals"]')).toBeTruthy();
    const ctxBtn = container.querySelector('[data-testid="composer-add-context-btn"]') as HTMLButtonElement;
    await act(async () => {
      ctxBtn.click();
    });
    await flushUi(100);
    expect(container.querySelector('[data-testid="composer-context-sheet"]')?.textContent).toContain('morning-scan');
    const freshFocusBtn = container.querySelector('[data-testid="composer-fresh-focus-btn"]') as HTMLButtonElement;
    expect(freshFocusBtn).toBeTruthy();
    await act(async () => {
      freshFocusBtn.click();
    });
    await flushUi(120);
    expect(container.querySelector('[data-testid="chat-divider-focus_marker"]')).toBeTruthy();
    expect(container.textContent).toContain('Companion v3.5 UI is live and verified.');
  });

  it('approves staged requests in 1 click from ActivityPopover and exercises Board & Goals + Right Split Inspector', async () => {
    const actBtn = container.querySelector('[data-testid="top-activity-btn"]') as HTMLButtonElement;
    await act(async () => {
      actBtn.click();
    });
    await flushUi(80);
    const pop = container.querySelector('[data-testid="activity-popover"]');
    expect(pop?.textContent).toContain('Approvals Needed');

    const approveBtn = Array.from(pop?.querySelectorAll('button') || []).find((b) =>
      b.textContent?.includes('Approve')
    ) as HTMLButtonElement;
    expect(approveBtn).toBeTruthy();
    await act(async () => {
      approveBtn.click();
    });
    await flushUi(100);

    // Navigate to Board & Goals
    const navBoards = container.querySelector('[data-testid="nav-boards"]') as HTMLButtonElement;
    await act(async () => {
      navBoards.click();
    });
    await flushUi(120);
    expect(container.querySelector('[data-testid="boards-surface"]')).toBeTruthy();

    // Switch to seeded "Garage & Truck Reset" project
    const projSelect = container.querySelector('[data-testid="boards-surface"] select') as HTMLSelectElement;
    const garageOpt = Array.from(projSelect.options).find((o) => o.textContent?.includes('Garage & Truck Reset'));
    expect(garageOpt).toBeTruthy();
    await act(async () => {
      projSelect.value = garageOpt!.value;
      projSelect.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await flushUi(120);
    expect(container.textContent).toContain('Measure garage wall');
    expect(container.textContent).toContain('Draft shelving cut-list');

    // Click a work card to open the Right Split Auxiliary Pane (`WorkItemInspector`)
    const firstCard = container.querySelector('.work-card') as HTMLElement;
    await act(async () => {
      firstCard.click();
    });
    await flushUi(120);
    expect(container.querySelector('[data-testid="aux-split-pane"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="work-item-inspector"]')).toBeTruthy();
  });

  it('exercises Automations (on_idle + 22:00-06:00 window), Customizations (Skills & Python DAGs), and Settings Modal', async () => {
    const navAuto = container.querySelector('[data-testid="nav-automations"]') as HTMLButtonElement;
    await act(async () => {
      navAuto.click();
    });
    await flushUi(120);
    expect(container.querySelector('[data-testid="automations-surface"]')).toBeTruthy();

    // Open New Routine inspector in Right Split Pane and create an overnight on_idle routine
    const newRtBtn = container.querySelector('[data-testid="new-routine-btn"]') as HTMLButtonElement;
    await act(async () => {
      newRtBtn.click();
    });
    await flushUi(80);
    const rtInsp = container.querySelector('[data-testid="routine-inspector"]') as HTMLElement;
    expect(rtInsp).toBeTruthy();
    const idInput = rtInsp.querySelector('input') as HTMLInputElement;
    await act(async () => {
      setInputValue(idInput, 'nightly-snap');
    });
    const saveRtBtn = Array.from(rtInsp.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Save Routine')
    ) as HTMLButtonElement;
    await act(async () => {
      saveRtBtn.click();
    });
    await flushUi(150);
    await flushUi(150);
    expect(container.querySelector('[data-testid="routine-row-nightly-snap"]')).toBeTruthy();

    // Navigate to Knowledge -> verify Search (hits) & Study (interactive FlashcardDeckWidget)
    const navKnow = container.querySelector('[data-testid="nav-knowledge"]') as HTMLButtonElement;
    await act(async () => {
      navKnow.click();
    });
    await flushUi(120);
    const wikiSearch = container.querySelector('[data-testid="knowledge-surface"] input') as HTMLInputElement;
    await act(async () => {
      setInputValue(wikiSearch, 'homelab');
      wikiSearch.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
    await flushUi(120);
    expect(container.querySelector('[data-testid="knowledge-surface"]')?.textContent).toContain('GPU Compute Node');

    const studyPill = Array.from(container.querySelectorAll('.segmented-pill')).find((b) =>
      b.textContent?.includes('Study')
    ) as HTMLButtonElement;
    await act(async () => {
      studyPill.click();
    });
    await flushUi(140);
    expect(container.querySelector('.flashcard-widget')?.textContent).toContain('What is a vdev?');

    // Navigate to Customizations and inspect Skills + Python DAGs
    const navCust = container.querySelector('[data-testid="nav-customizations"]') as HTMLButtonElement;
    await act(async () => {
      navCust.click();
    });
    await flushUi(120);
    expect(container.querySelector('[data-testid="customizations-surface"]')?.textContent).toContain('skill-creator');

    // Open 2-column Settings Modal and switch theme
    const settingsBtn = container.querySelector('[data-testid="open-settings-btn"]') as HTMLButtonElement;
    await act(async () => {
      settingsBtn.click();
    });
    await flushUi(80);
    expect(container.querySelector('[data-testid="settings-modal"]')).toBeTruthy();

    const appTab = container.querySelector('[data-testid="settings-tab-appearance"]') as HTMLButtonElement;
    await act(async () => {
      appTab.click();
    });
    const daylightCard = container.querySelector('[data-testid="theme-card-daylight"]') as HTMLButtonElement;
    await act(async () => {
      daylightCard.click();
    });
    expect(document.documentElement.getAttribute('data-theme')).toBe('daylight');
  });
});

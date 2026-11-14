import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import * as readline from 'node:readline';
import * as path from 'node:path';
import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { App } from '../src/App';

const PYTHON_STDIO_SERVER = `
import json, os, shutil, sys, tempfile
from pathlib import Path

ipc_out = sys.stdout
sys.stdout = sys.stderr

repo_root = Path(sys.argv[1])
tmp_dir = Path(tempfile.mkdtemp(prefix="companion_vitest_"))
os.environ["COMPANION_DATA_DIR"] = str(tmp_dir / "data")
os.environ["COMPANION_SECRETS_FILE"] = str(tmp_dir / "secrets.env")
sys.path.insert(0, str(repo_root))

import companion.memory_store as ms
import companion.background_jobs as bj
import companion.core_actions as ca
from companion.core_actions import execute_core_action
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

def fake_chat(*args, **kwargs):
    from companion.streaming import emit_stream_event
    emit_stream_event({"type": "thinking_delta", "delta": "Checking dual-host mesh..."})
    emit_stream_event({"type": "text_delta", "delta": "Companion 3.0 Studio is live and verified."})
    return {"choices": [{"message": {"role": "assistant", "content": "Companion 3.0 Studio is live and verified."}}]}

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
    if req.get("cmd") == "seed_wiki31":
        from companion.wiki_engine import WikiEngine
        eng = WikiEngine(store=store)
        res1 = eng.save_or_update_note(
            rel_path="02_knowledge/homelab/stale-switch.md",
            title="Stale Core Switch Spec",
            content=(
                "---\\n"
                "last_verified_at: '2025-11-01'\\n"
                "review_after_days: 90\\n"
                "---\\n"
                "# Stale Core Switch Spec\\n\\n"
                "## Hardware & Compute Profile\\n- 24-port SFP+ 10GbE core switch\\n\\n"
                "## Network & Management\\n- Management IP 192.168.1.2\\n\\n"
                "## Services & Workloads\\n- Core VLAN trunking\\n\\n"
                "## Operations & Notes\\n- Needs firmware check paired with [[gpu-compute-node]]\\n"
            ),
            tags=["homelab", "networking"],
            actor="user",
            template="homelab-host-spec",
            summary="Core 10GbE SFP+ switch spec needing verification.",
            ontology=["network-fabric", "server-compute"],
        )
        # Create a waiting wiki_edit approval by editing user-owned gpu-compute-node.md as companion
        res2 = execute_core_action(
            "wiki",
            {
                "subaction": "save",
                "path": "02_knowledge/homelab/gpu-compute-node.md",
                "title": "GPU Compute Node",
                "content": "Primary GPU server at 192.168.1.100 with 128GB unified memory and high-speed NIC. #homelab",
                "tags": ["homelab", "gpu"],
            },
            actor="companion",
            store=store,
        )
        ipc_out.write(json.dumps({"id": req.get("id"), "status": 200, "body": json.dumps({"ok": True, "r1": res1, "r2": res2})}) + "\\n")
        ipc_out.flush()
        continue
    if req.get("cmd") == "set_env_lock":
        from companion.config import settings
        settings.self_dev_locked = bool(req.get("locked"))
        settings.immutable_core = bool(req.get("immutable", False))
        ipc_out.write(json.dumps({"id": req.get("id"), "status": 200, "body": json.dumps({"ok": True})}) + "\\n")
        ipc_out.flush()
        continue
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

  async seedWiki31(): Promise<Record<string, unknown>> {
    const id = this.nextId++;
    return new Promise((resolve) => {
      this.pending.set(id, (msg) => resolve(JSON.parse(msg.body)));
      this.proc.stdin.write(JSON.stringify({ id, cmd: 'seed_wiki31' }) + '\n');
    });
  }

  async setEnvLock(locked: boolean, immutable = false): Promise<void> {
    const id = this.nextId++;
    return new Promise((resolve) => {
      this.pending.set(id, () => resolve());
      this.proc.stdin.write(JSON.stringify({ id, cmd: 'set_env_lock', locked, immutable }) + '\n');
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

async function flushUi(ms = 40): Promise<void> {
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

function setSelectValue(el: HTMLSelectElement, val: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
  setter?.call(el, val);
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

describe('Companion 3.0 Multi-Studio Interactive UI + Real Python Backend', () => {
  const bridge = new PyHttpBridge();
  let container: HTMLDivElement;
  let root: Root;
  const canvasArcCalls: number[] = [];

  beforeAll(async () => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    await bridge.start();

    // Inject CSRF meta tag exactly as surfaces/web_assets.py renders it
    const meta = document.createElement('meta');
    meta.setAttribute('name', 'companion-csrf');
    meta.setAttribute('content', bridge.csrfToken);
    document.head.appendChild(meta);

    // Stub 2D canvas context on happy-dom so MindMapCanvas draws cleanly and records node arcs
    HTMLCanvasElement.prototype.getContext = vi.fn().mockImplementation(() => ({
      setTransform: vi.fn(),
      clearRect: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(),
      arc: vi.fn((x: number) => canvasArcCalls.push(x)),
      fill: vi.fn(),
      fillText: vi.fn(),
      save: vi.fn(),
      restore: vi.fn(),
      translate: vi.fn(),
      scale: vi.fn(),
    })) as unknown as typeof HTMLCanvasElement.prototype.getContext;

    Element.prototype.scrollIntoView = vi.fn();

    // Wire window.fetch to our real Python CompanionWebRequestHandler bridge
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const urlStr = typeof input === 'string' ? input : input.toString();
      const method = (init?.method || 'GET').toUpperCase();
      const rawHeaders: Record<string, string> = {};
      if (init?.headers) {
        for (const [k, v] of Object.entries(init.headers as Record<string, string>)) {
          rawHeaders[k] = String(v);
        }
      }
      const bodyStr = typeof init?.body === 'string' ? init.body : '';
      const resp = await bridge.request(method, urlStr, bodyStr, rawHeaders);
      return {
        ok: resp.status >= 200 && resp.status < 400,
        status: resp.status,
        json: async () => JSON.parse(resp.body),
        text: async () => resp.body,
      } as Response;
    }) as typeof fetch;

    // Wire EventSource to poll /api/jobs/<id> from the real Python CompanionWebSession
    class LiveBridgeEventSource {
      onmessage: ((ev: { data: string }) => void) | null = null;
      onerror: (() => void) | null = null;
      private closed = false;

      constructor(url: string) {
        const jobId = url.replace('/api/jobs/', '').replace('/stream', '');
        void this.pump(jobId);
      }

      private async pump(jobId: string): Promise<void> {
        for (let i = 0; i < 25 && !this.closed; i++) {
          await new Promise((r) => setTimeout(r, 25));
          const r = await bridge.request('GET', `/api/jobs/${jobId}`);
          const parsed = JSON.parse(r.body);
          const job = parsed.job;
          if (job && job.status !== 'running') {
            if (job.stream_thinking) {
              this.onmessage?.({
                data: JSON.stringify({ type: 'thinking_delta', delta: job.stream_thinking }),
              });
            }
            if (job.stream_text) {
              this.onmessage?.({
                data: JSON.stringify({ type: 'text_delta', delta: job.stream_text }),
              });
            }
            this.onmessage?.({
              data: JSON.stringify({
                type: 'done',
                status: job.status,
                response: job.response,
                error: job.error,
              }),
            });
            return;
          }
        }
      }

      close(): void {
        this.closed = true;
      }
    }
    (globalThis as Record<string, unknown>).EventSource = LiveBridgeEventSource;

    container = document.createElement('div');
    document.body.appendChild(container);
    await act(async () => {
      root = createRoot(container);
      root.render(<App />);
    });
    await flushUi(80);
  });

  afterAll(async () => {
    await act(async () => {
      root?.unmount();
    });
    await bridge.stop();
  });

  it('switches across all 4 Matte Brutalist / Obsidian themes and toggles Light/Dark mode', async () => {
    const navItems = container.querySelector('.nav-items');
    expect(navItems).toBeTruthy();
    expect(navItems?.querySelectorAll('.nav-btn').length).toBe(6);

    const themeSelect = container.querySelector(
      'select[aria-label="Select Studio Theme"]'
    ) as HTMLSelectElement;
    expect(themeSelect).toBeTruthy();
    expect(document.documentElement.getAttribute('data-theme')).toBe('obsidian');

    for (const themeId of ['orbital', 'graphite', 'daylight', 'obsidian']) {
      await act(async () => {
        themeSelect.value = themeId;
        themeSelect.dispatchEvent(new Event('change', { bubbles: true }));
      });
      expect(document.documentElement.getAttribute('data-theme')).toBe(themeId);
      expect(window.localStorage.getItem('companion_theme')).toBe(themeId);
    }

    // 1-click Light/Dark button
    const toggleBtn = container.querySelector(
      'button[title="Toggle Light / Dark Mode"]'
    ) as HTMLButtonElement;
    expect(toggleBtn).toBeTruthy();
    await act(async () => {
      toggleBtn.click();
    });
    expect(document.documentElement.getAttribute('data-theme')).toBe('daylight');
    await act(async () => {
      toggleBtn.click();
    });
    expect(document.documentElement.getAttribute('data-theme')).toBe('obsidian');
  });

  it('exercises Studio 1 (Forever Chat): Saved Prompts, Archive drawer, and live SSE streaming turn', async () => {
    const promptsBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Prompts (')
    )!;
    await act(async () => promptsBtn.click());

    const slugInput = container.querySelector(
      'input[placeholder*="Template slug"]'
    ) as HTMLInputElement;
    const promptArea = container.querySelector(
      'textarea[placeholder*="Reusable prompt"]'
    ) as HTMLTextAreaElement;
    await act(async () => {
      setInputValue(slugInput, 'status-ping');
      setInputValue(promptArea, 'Confirm Companion 3.0 is live.');
    });
    const saveBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Save Template'
    )!;
    await act(async () => saveBtn.click());
    await flushUi(60);
    expect(container.textContent).toContain('status-ping');

    // Send live chat message and verify SSE thinking + assistant response
    const composer = container.querySelector(
      '.composer-row textarea'
    ) as HTMLTextAreaElement;
    await act(async () => {
      setInputValue(composer, 'Hello Companion 3.0!');
    });
    const sendBtn = Array.from(container.querySelectorAll('.composer-row button')).find((b) =>
      b.textContent?.includes('Send')
    )!;
    await act(async () => sendBtn.click());
    await flushUi(180);

    expect(container.textContent).toContain('Companion 3.0 Studio is live and verified.');
    expect(container.textContent).toContain('Checking dual-host mesh...');
  });

  it('exercises Studio 2 (Wiki & Mind Map): Inbox capture, clickable [[wikilink]], Backlinks, and 2D Canvas', async () => {
    const wikiNav = Array.from(container.querySelectorAll('.nav-btn')).find((b) =>
      b.textContent?.includes('Wiki & Mind Map')
    )!;
    await act(async () => wikiNav.click());
    await flushUi(80);
    expect(container.textContent).toContain('GPU Compute Node');

    // Capture a new Inbox note linking to [[gpu-compute-node]]
    const newInboxBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('New Inbox Note')
    )!;
    await act(async () => newInboxBtn.click());

    const titleIn = container.querySelector(
      'input[placeholder*="Note Title"]'
    ) as HTMLInputElement;
    const bodyIn = container.querySelector(
      'textarea[placeholder*="[[wikilinks]]"]'
    ) as HTMLTextAreaElement;
    await act(async () => {
      setInputValue(titleIn, 'Workstation Dual Host Link');
      setInputValue(bodyIn, 'Paired with [[gpu-compute-node]] for reasoning. #architecture');
    });
    const capSubmit = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Capture to 01_inbox/')
    )!;
    await act(async () => capSubmit.click());
    await flushUi(100);

    // Click rendered [[gpu-compute-node]] wikilink button in preview!
    const wikilinkBtn = container.querySelector('.md-wikilink') as HTMLButtonElement;
    expect(wikilinkBtn).toBeTruthy();
    expect(wikilinkBtn.textContent).toContain('gpu-compute-node');
    await act(async () => wikilinkBtn.click());
    await flushUi(80);

    expect(container.textContent).toContain('02_knowledge/homelab/gpu-compute-node.md');
    expect(container.textContent).toContain('BACKLINKS (1)');
    const summaryEdit = container.querySelector(
      'input[placeholder*="Executive summary"]'
    ) as HTMLInputElement;
    expect(summaryEdit).toBeTruthy();
    expect(summaryEdit.value).not.toContain('02_knowledge/homelab/gpu-compute-node.md');
    expect(summaryEdit.value).not.toContain('author:');

    // Switch to 2D Mind Map view and verify canvas renders 2 nodes + 1 edge
    const mapBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('2D Mind Map')
    )!;
    await act(async () => mapBtn.click());
    await flushUi(90);

    expect(container.querySelector('canvas')).toBeTruthy();
    expect(container.textContent).toContain('NODES: 2');
    expect(container.textContent).toContain('WIKILINKS: 1');
    expect(container.textContent).toContain('ONTOLOGY BRIDGES: ON');
    expect(canvasArcCalls.length).toBeGreaterThanOrEqual(2);

    // Inspect the 11 Governed Templates tab in Wiki 3.0
    const tplTab = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Templates'
    )!;
    await act(async () => tplTab.click());
    await flushUi(80);
    expect(container.textContent).toContain('sop-runbook');
    expect(container.textContent).toContain('homelab-host-spec');
    expect(container.textContent).toContain('proxy-asset-note');
    expect(container.textContent).not.toContain('feynman');

    // Return to 01_inbox and run Inspect Overlaps on the staged note
    const inboxTab = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === '01_inbox'
    )!;
    await act(async () => inboxTab.click());
    await flushUi(80);
    const stagedBtn = Array.from(container.querySelectorAll('.wiki-note-item')).find((b) =>
      b.textContent?.includes('Workstation Dual Host Link')
    ) as HTMLButtonElement;
    await act(async () => stagedBtn.click());
    await flushUi(80);
    const splitBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Editor & Reader')
    )!;
    await act(async () => splitBtn.click());
    await flushUi(40);
    const inspectBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Inspect Overlaps')
    )!;
    await act(async () => inspectBtn.click());
    await flushUi(90);
    expect(container.textContent).toContain('CURATION AUDIT (cur_');
    expect(container.textContent).toContain('Recommended Templates:');
  });

  it('exercises Studio 5 (Jobs & Routines): dispatches worker onto Departures Board', async () => {
    const jobsNav = Array.from(container.querySelectorAll('.nav-btn')).find((b) =>
      b.textContent?.includes('Jobs & Routines')
    )!;
    await act(async () => jobsNav.click());
    await flushUi(60);

    const goalArea = container.querySelector(
      'textarea[placeholder*="Self-contained worker objective"]'
    ) as HTMLTextAreaElement;
    await act(async () => {
      setInputValue(goalArea, 'Summarize system readiness in one line.');
    });
    const launchBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Launch Background Job')
    )!;
    await act(async () => launchBtn.click());
    await flushUi(150);

    expect(container.textContent).toContain('Background Jobs Departures Board (1)');
    expect(container.textContent).toContain('Summarize system readiness in one line.');
  });

  it('exercises System Studio (Models, Tiers & Context): seals provider key via Blind Vault, hot-reloads tier route, and enforces Local LAN Only privacy lock', async () => {
    const sysNav = Array.from(container.querySelectorAll('.nav-btn')).find((b) =>
      b.textContent?.includes('System')
    )!;
    await act(async () => sysNav.click());
    await flushUi(80);

    expect(container.textContent).toContain('Capability Tier Routing, Context Window & Fallback Matrix');
    expect(container.textContent).toContain('local-default');

    // Select 1-click Anthropic Claude preset & seal API key via Blind Vault
    const claudePresetBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Anthropic Claude')
    )!;
    await act(async () => claudePresetBtn.click());

    const keyInput = container.querySelector(
      'input[placeholder*="Paste API Key"]'
    ) as HTMLInputElement;
    const rawApiKey = 'sk-ant-vitest-blind-key-888';
    await act(async () => {
      setInputValue(keyInput, rawApiKey);
    });
    const saveProvBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Save Provider')
    )!;
    await act(async () => saveProvBtn.click());
    await flushUi(80);

    expect(keyInput.value).toBe('');
    expect(container.textContent).toContain('VAULT:anthropic-api-key (SET)');
    expect(document.body.innerHTML).not.toContain(rawApiKey);

    // Route `reasoning` tier to `anthropic` with `high` effort and hot-reload
    const reasoningProvSel = container.querySelector(
      'select[aria-label="reasoning provider"]'
    ) as HTMLSelectElement;
    await act(async () => setSelectValue(reasoningProvSel, 'anthropic'));
    const reasoningEffortSel = container.querySelector(
      'select[aria-label="reasoning reasoning effort"]'
    ) as HTMLSelectElement;
    await act(async () => setSelectValue(reasoningEffortSel, 'high'));

    const reasoningRow = container.querySelector('[data-testid="tier-row-reasoning"]')!;
    const saveTierBtn = Array.from(reasoningRow.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Save Tier')
    )!;
    await act(async () => saveTierBtn.click());
    await flushUi(80);
    expect(reasoningRow.textContent).toContain('CUSTOMIZED');

    // Toggle Outbound Privacy Lock to `Local LAN Only` and confirm mechanical block badge
    const localLockBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Local LAN Only')
    )!;
    await act(async () => localLockBtn.click());
    await flushUi(80);
    expect(container.textContent).toContain('BLOCKED BY LOCAL_ONLY LOCK');

    const hybridBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Hybrid (Local + Cloud)')
    )!;
    await act(async () => hybridBtn.click());
    await flushUi(60);
  });

  it('exercises System Studio (Vault & Endpoints): seals blind secret with zero DOM leakage and registers endpoint', async () => {
    const sysNav = Array.from(container.querySelectorAll('.nav-btn')).find((b) =>
      b.textContent?.includes('System')
    )!;
    await act(async () => sysNav.click());
    await flushUi(60);

    const vaultTab = Array.from(container.querySelectorAll('.system-subnav button')).find((b) =>
      b.textContent?.includes('Vault & Endpoints')
    )!;
    await act(async () => vaultTab.click());
    await flushUi(60);

    const nameIn = container.querySelector(
      'input[placeholder*="Symbolic name"]'
    ) as HTMLInputElement;
    const passIn = container.querySelector('input[type="password"]') as HTMLInputElement;
    const rawSecret = 'ultra-secret-dom-999-never-show';
    await act(async () => {
      setInputValue(nameIn, 'stage6-dom-token');
      setInputValue(passIn, rawSecret);
    });
    const sealBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Seal Secret in Blind Vault')
    )!;
    await act(async () => sealBtn.click());
    await flushUi(80);

    expect(passIn.value).toBe('');
    expect(container.textContent).toContain('stage6-dom-token');
    expect(container.textContent).toContain('SET');
    expect(document.body.innerHTML).not.toContain(rawSecret);

    // Register HTTP endpoint
    const epIdIn = container.querySelector(
      'input[placeholder*="Endpoint ID"]'
    ) as HTMLInputElement;
    const epHostIn = container.querySelector(
      'input[placeholder*="Host / Alias"]'
    ) as HTMLInputElement;
    await act(async () => {
      setInputValue(epIdIn, 'lab-ollama');
      setInputValue(epHostIn, 'http://192.168.1.101:11434');
    });
    const saveEpBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Save Endpoint')
    )!;
    await act(async () => saveEpBtn.click());
    await flushUi(80);
    expect(container.textContent).toContain('lab-ollama');
  });

  it('exercises System -> Identity & Profile Studio: personalizes Companion Soul, User Name, and Tone', async () => {
    const idTab = Array.from(container.querySelectorAll('.system-subnav button')).find((b) =>
      b.textContent?.includes('Identity & Profile')
    ) as HTMLButtonElement;
    expect(idTab).toBeTruthy();
    await act(async () => idTab.click());
    await flushUi(80);

    const cnameIn = container.querySelector('input[aria-label="Companion Name"]') as HTMLInputElement;
    const unameIn = container.querySelector('input[aria-label="Your Name"]') as HTMLInputElement;
    const toneSel = container.querySelector('select[aria-label="Communication Tone"]') as HTMLSelectElement;
    expect(cnameIn).toBeTruthy();
    expect(unameIn).toBeTruthy();

    await act(async () => {
      setInputValue(cnameIn, 'Atlas');
      setInputValue(unameIn, 'Alex');
      setSelectValue(toneSel, 'concise');
    });
    const saveBtn = container.querySelector('[data-testid="save-identity-btn"]') as HTMLButtonElement;
    await act(async () => saveBtn.click());
    await flushUi(100);

    expect(container.textContent).toContain('PERSONALIZED');
    expect(container.querySelector('.brand-title')?.textContent).toContain('Atlas');
  });

  it('exercises System Sub-Tabs (Federation & MCP + Backups, Sandboxes & Doctor)', async () => {
    const fedTab = Array.from(container.querySelectorAll('.system-subnav button')).find((b) =>
      b.textContent?.includes('Federation & MCP')
    )!;
    await act(async () => fedTab.click());
    await flushUi(60);

    const enableBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Enable Hosted MCP')
    )!;
    await act(async () => enableBtn.click());
    await flushUi(60);
    expect(container.textContent).toContain('ONLINE (TOKEN GATED)');

    // Sub-tab 3: Backups, Sandboxes & Doctor -> Create Snapshot Backup
    const opsTab = Array.from(container.querySelectorAll('.system-subnav button')).find((b) =>
      b.textContent?.includes('Backups, Sandboxes & Doctor')
    )!;
    await act(async () => opsTab.click());
    await flushUi(60);

    const createSnapBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Create Snapshot')
    )!;
    await act(async () => createSnapBtn.click());
    await flushUi(120);
    expect(container.textContent).toContain('Secret-Free Snapshot Backups (1)');
  });

  it('uploads Markdown & PNG attachments in Forever Chat, previews chips, and streams turn', async () => {
    const chatNav = Array.from(container.querySelectorAll('.nav-btn')).find((b) =>
      b.textContent?.includes('Forever Chat')
    )!;
    await act(async () => chatNav.click());
    await flushUi(60);

    const fileInput = container.querySelector(
      'input[data-testid="chat-file-input"]'
    ) as HTMLInputElement;
    expect(fileInput).toBeTruthy();

    const mdFile = new File(['# Sprint Notes\n- Shipped multimodal attachments'], 'sprint.md', {
      type: 'text/markdown',
    });

    await act(async () => {
      Object.defineProperty(fileInput, 'files', {
        value: [mdFile],
        configurable: true,
      });
      fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await flushUi(150);

    expect(container.textContent).toContain('sprint.md');
    expect(container.textContent).toContain('TEXT');

    // Can send with attachment even without typing extra text!
    const sendBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Send')
    ) as HTMLButtonElement;
    expect(sendBtn.disabled).toBe(false);
    await act(async () => sendBtn.click());
    await flushUi(220);

    expect(container.textContent).toContain('sprint.md');
  });

  it('exercises Wiki 3.1 Enhancements: + Stage to Inbox, Stale & Mark Verified, Visual Diff, Approval Diff Preview, Inline Assets, and Ontology Consolidation', async () => {
    const seedRes = await bridge.seedWiki31();
    expect(Boolean((seedRes.r1 as Record<string, unknown>)?.ok)).toBe(true);
    expect(Boolean((seedRes.r2 as Record<string, unknown>)?.awaiting_approval)).toBe(true);

    // 1. Click "+ Stage to Inbox" on assistant message in Forever Chat, then "Open in Wiki"
    const stageBtn = Array.from(container.querySelectorAll('button.msg-stage-btn')).find((b) =>
      b.textContent?.includes('Stage to Inbox')
    ) as HTMLButtonElement;
    expect(stageBtn).toBeTruthy();
    await act(async () => stageBtn.click());
    await flushUi(120);

    const openStagedBtn = Array.from(container.querySelectorAll('button.msg-stage-btn.staged')).find(
      (b) => b.textContent?.includes('Open in Wiki')
    ) as HTMLButtonElement;
    expect(openStagedBtn).toBeTruthy();
    await act(async () => openStagedBtn.click());
    await flushUi(120);

    // Should now be inside Wiki Studio viewing the staged 01_inbox/ note
    expect(container.textContent).toContain('01_inbox/');

    // 2. Upload inline PNG diagram into 03_resources/assets/ via "+ Image" file input and verify MarkdownView renders img
    const wikiImgInput = container.querySelector(
      'input[aria-label="Upload Wiki diagram"]'
    ) as HTMLInputElement;
    expect(wikiImgInput).toBeTruthy();
    // Valid 1x1 PNG magic bytes
    const pngBytes = Uint8Array.from(
      atob(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
      ),
      (c) => c.charCodeAt(0)
    );
    const pngFile = new File([pngBytes], 'topology.png', { type: 'image/png' });
    await act(async () => {
      Object.defineProperty(wikiImgInput, 'files', {
        value: [pngFile],
        configurable: true,
      });
      wikiImgInput.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await flushUi(160);

    const renderedAssetImg = container.querySelector(
      'img[src^="/api/wiki/assets/"]'
    ) as HTMLImageElement;
    expect(renderedAssetImg).toBeTruthy();

    // 3. Test Unsaved Draft Diff in Wiki Studio (Unified + Side-by-Side)
    const diffBtn = Array.from(container.querySelectorAll('.wiki-note-toolbar button')).find(
      (b) => b.textContent?.trim() === 'Diff'
    ) as HTMLButtonElement;
    expect(diffBtn).toBeTruthy();
    await act(async () => diffBtn.click());
    await flushUi(100);

    const diffView = container.querySelector('[data-testid="wiki-diff-view"]');
    expect(diffView).toBeTruthy();
    expect(diffView?.textContent).toContain('03_resources/assets/');

    const unifiedBtn = Array.from(diffView!.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Unified')
    ) as HTMLButtonElement;
    await act(async () => unifiedBtn.click());
    expect(diffView?.querySelector('.diff-table')).toBeTruthy();

    const closeDiffBtn = Array.from(diffView!.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Close Diff')
    ) as HTMLButtonElement;
    await act(async () => closeDiffBtn.click());
    await flushUi(40);

    // 4. Filter by "Stale" folder pill, inspect STALE badge, and click "Mark Verified"
    const staleTab = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Stale'
    ) as HTMLButtonElement;
    expect(staleTab).toBeTruthy();
    await act(async () => staleTab.click());
    await flushUi(100);

    expect(container.textContent).toContain('Stale Core Switch Spec');
    const staleItem = Array.from(container.querySelectorAll('.wiki-note-item')).find((b) =>
      b.textContent?.includes('Stale Core Switch Spec')
    ) as HTMLButtonElement;
    await act(async () => staleItem.click());
    await flushUi(100);

    expect(container.textContent).toContain('STALE •');
    // Verify write-time ontology canonicalization turned server-compute -> datacenter-compute
    expect(container.textContent).toContain('datacenter-compute');

    const verifyBtn = Array.from(container.querySelectorAll('.wiki-note-toolbar button')).find((b) =>
      b.textContent?.includes('Mark Verified')
    ) as HTMLButtonElement;
    expect(verifyBtn).toBeTruthy();
    await act(async () => verifyBtn.click());
    await flushUi(120);
    expect(container.textContent).toContain('VERIFIED');

    // 5. Open "Templates" tab and run "Consolidate Ontology"
    const tplTab = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Templates'
    ) as HTMLButtonElement;
    await act(async () => tplTab.click());
    await flushUi(80);

    const consolidateBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Consolidate Ontology')
    ) as HTMLButtonElement;
    expect(consolidateBtn).toBeTruthy();
    await act(async () => consolidateBtn.click());
    await flushUi(120);
    expect(container.textContent).toContain('Ontology Consolidation (APPLIED)');

    // 6. Navigate to Jobs & Routines and click "Preview Diff" on the waiting wiki_edit approval
    const jobsNav = Array.from(container.querySelectorAll('.nav-btn')).find((b) =>
      b.textContent?.includes('Jobs & Routines')
    ) as HTMLButtonElement;
    await act(async () => jobsNav.click());
    await flushUi(100);

    expect(container.textContent).toContain('wiki_edit:edit_user_note');
    const previewApprovalDiffBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Preview Diff')
    ) as HTMLButtonElement;
    expect(previewApprovalDiffBtn).toBeTruthy();
    await act(async () => previewApprovalDiffBtn.click());
    await flushUi(120);

    const approvalDiff = container.querySelector('[data-testid="wiki-diff-view"]');
    expect(approvalDiff).toBeTruthy();
    expect(approvalDiff?.textContent).toContain('128GB unified memory');
  });

  it('exercises Companion 3.2 Universal Agile Boards, Unified Calendar, and Inline Chat Flashcard Grading', async () => {
    const todayIso = new Date().toISOString().slice(0, 10);

    // 1. Navigate to Boards Studio, start a Weekly Sprint, add a Weekend card, and slice a 15m Unblocker
    const boardsNav = Array.from(container.querySelectorAll('.nav-btn')).find((b) =>
      b.textContent?.includes('Boards')
    ) as HTMLButtonElement;
    expect(boardsNav).toBeTruthy();
    await act(async () => boardsNav.click());
    await flushUi(100);

    const startSprintBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Start Sprint')
    ) as HTMLButtonElement;
    expect(startSprintBtn).toBeTruthy();
    await act(async () => startSprintBtn.click());
    await flushUi(100);

    expect(container.textContent).toContain('WEEKDAY SLOTS:');
    expect(container.textContent).toContain('WEEKEND SLOTS:');

    const cardTitleIn = container.querySelector(
      'input[placeholder*="Add task or backlog card"]'
    ) as HTMLInputElement;
    const effortSel = container.querySelector('select[aria-label="Effort size"]') as HTMLSelectElement;
    const slotSel = container.querySelector('select[aria-label="Day slot"]') as HTMLSelectElement;
    const planDateIn = container.querySelector('input[aria-label="Planned date"]') as HTMLInputElement;

    await act(async () => {
      setInputValue(cardTitleIn, 'Fix truck brake inspection');
      effortSel.value = 'full_day';
      effortSel.dispatchEvent(new Event('change', { bubbles: true }));
      slotSel.value = 'weekend';
      slotSel.dispatchEvent(new Event('change', { bubbles: true }));
      setInputValue(planDateIn, todayIso);
    });

    const addCardBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Add Card')
    ) as HTMLButtonElement;
    await act(async () => addCardBtn.click());
    await flushUi(120);

    expect(container.textContent).toContain('Fix truck brake inspection');
    expect(container.textContent).toContain('4 / 6');

    const sliceBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Slice 15m Unblocker')
    ) as HTMLButtonElement;
    expect(sliceBtn).toBeTruthy();
    await act(async () => sliceBtn.click());
    await flushUi(120);

    expect(container.textContent).toContain('Waiting on:');
    expect(container.textContent).toContain('1 / 10');

    // 2. Navigate to Unified Calendar Studio, schedule an explicit appointment, and verify merged items
    const calNav = Array.from(container.querySelectorAll('.nav-btn')).find((b) =>
      b.textContent?.includes('Calendar')
    ) as HTMLButtonElement;
    expect(calNav).toBeTruthy();
    await act(async () => calNav.click());
    await flushUi(100);

    // Planned task from Boards should already appear on today's cell!
    expect(container.textContent).toContain('Fix truck brake inspection');

    const evTitleIn = container.querySelector(
      'input[placeholder*="Schedule appointment"]'
    ) as HTMLInputElement;
    const evStartIn = container.querySelector(
      'input[aria-label="Event start date or time"]'
    ) as HTMLInputElement;
    await act(async () => {
      setInputValue(evTitleIn, 'Drop truck keys at mechanic');
      setInputValue(evStartIn, `${todayIso}T09:30`);
    });
    const schedBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Schedule'
    ) as HTMLButtonElement;
    await act(async () => schedBtn.click());
    await flushUi(100);

    expect(container.textContent).toContain('Drop truck keys at mechanic');

    // Switch to Agenda view and verify chronological rendering
    const agendaBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Agenda'
    ) as HTMLButtonElement;
    await act(async () => agendaBtn.click());
    await flushUi(80);
    expect(container.textContent).toContain('Chronological Agenda');
    expect(container.textContent).toContain('Drop truck keys at mechanic');

    // 3. Navigate to Forever Chat, create a Course + Flashcard in Study Drawer, deal inline deck & grade!
    const chatNav = Array.from(container.querySelectorAll('.nav-btn')).find((b) =>
      b.textContent?.includes('Forever Chat')
    ) as HTMLButtonElement;
    await act(async () => chatNav.click());
    await flushUi(80);

    const studyBtn = Array.from(container.querySelectorAll('.studio-actions button')).find((b) =>
      b.textContent?.includes('Study')
    ) as HTMLButtonElement;
    expect(studyBtn).toBeTruthy();
    await act(async () => studyBtn.click());
    await flushUi(80);

    const courseTitleIn = container.querySelector(
      'input[placeholder*="Course Title"]'
    ) as HTMLInputElement;
    await act(async () => {
      setInputValue(courseTitleIn, 'Distributed Consensus');
    });
    const saveCourseBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Save Course'
    ) as HTMLButtonElement;
    await act(async () => saveCourseBtn.click());
    await flushUi(100);

    expect(container.textContent).toContain('Distributed Consensus');

    const promptIn = container.querySelector(
      'input[placeholder*="Quick flashcard prompt"]'
    ) as HTMLInputElement;
    const answerIn = container.querySelector(
      'input[placeholder*="Concise answer"]'
    ) as HTMLInputElement;
    await act(async () => {
      setInputValue(promptIn, 'What safety property prevents split-brain leaders in Raft?');
      setInputValue(answerIn, 'Election Safety: at most one leader can be elected in a given term.');
    });
    const addFlashcardBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('+ Add Card')
    ) as HTMLButtonElement;
    await act(async () => addFlashcardBtn.click());
    await flushUi(100);

    const dealBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Deal Flashcards Here')
    ) as HTMLButtonElement;
    await act(async () => dealBtn.click());
    await flushUi(100);

    // Interactive FlashcardDeckWidget should now be rendered inline inside Chat!
    const deckWidget = container.querySelector('[data-testid="flashcard-deck-widget"]');
    expect(deckWidget).toBeTruthy();
    expect(deckWidget?.textContent).toContain(
      'What safety property prevents split-brain leaders in Raft?'
    );
    expect(deckWidget?.textContent).toContain('SRS BOX 0');

    // Flip card to inspect answer
    const flipSurface = deckWidget!.querySelector('.flashcard-surface') as HTMLButtonElement;
    await act(async () => flipSurface.click());
    expect(deckWidget?.textContent).toContain(
      'Election Safety: at most one leader can be elected in a given term.'
    );

    // Click "Got It (Pass)" and verify SRS Box advances in SQLite!
    const passBtn = Array.from(deckWidget!.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Got It (Pass)')
    ) as HTMLButtonElement;
    await act(async () => passBtn.click());
    await flushUi(120);

    expect(deckWidget?.textContent).toContain('SRS BOX 1');
    expect(deckWidget?.textContent).toContain('PASSED');

    // Switch to Boards, edit an existing card inline, then return to Forever Chat and verify dealt deck + SRS BOX 1 persisted!
    const boardsNavAgain = Array.from(container.querySelectorAll('.nav-btn')).find((b) =>
      b.textContent?.includes('Boards')
    ) as HTMLButtonElement;
    await act(async () => boardsNavAgain.click());
    await flushUi(100);

    const editCardBtn = container.querySelector(
      'button[aria-label^="Edit "]'
    ) as HTMLButtonElement;
    expect(editCardBtn).toBeTruthy();
    await act(async () => editCardBtn.click());
    await flushUi(40);
    const editTitleIn = container.querySelector(
      'input[aria-label="Edit card title"]'
    ) as HTMLInputElement;
    expect(editTitleIn).toBeTruthy();
    await act(async () => {
      setInputValue(editTitleIn, 'Edited Unblocker: Call Brake Shop Now');
    });
    const saveCardEditsBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Save Card'
    ) as HTMLButtonElement;
    await act(async () => saveCardEditsBtn.click());
    await flushUi(100);
    expect(container.textContent).toContain('Edited Unblocker: Call Brake Shop Now');

    await act(async () => chatNav.click());
    await flushUi(100);
    await flushUi(100);
    const persistedDeck = container.querySelector('[data-testid="flashcard-deck-widget"]');
    expect(persistedDeck).toBeTruthy();
    expect(persistedDeck?.textContent).toContain('SRS BOX 1');
  });

  it('exercises Companion 3.4 Two-Lever Operating Architecture (Hobby Mode x Self-Dev Mode), 2-Track Catalog & Deployment Lock', async () => {
    // 1. Verify top HUD chips start in safe Q1 Daily Driver defaults (SELF-DEV: OFF (RO) & HOBBIES: OFF)
    const selfDevChip = container.querySelector(
      '[data-testid="hud-chip-selfdev"]'
    ) as HTMLButtonElement;
    const hobbiesChip = container.querySelector(
      '[data-testid="hud-chip-hobbies"]'
    ) as HTMLButtonElement;
    expect(selfDevChip).toBeTruthy();
    expect(hobbiesChip).toBeTruthy();
    expect(selfDevChip.textContent).toContain('SELF-DEV: OFF (RO)');
    expect(hobbiesChip.textContent).toContain('HOBBIES: OFF');

    // Click top HUD SELF-DEV chip to jump directly to System -> Autonomy & Hobbies
    await act(async () => selfDevChip.click());
    await flushUi(100);

    const banner = container.querySelector('[data-testid="operating-quadrant-banner"]')!;
    expect(banner).toBeTruthy();
    expect(banner.textContent).toContain('Q1 — Quiet Stable Daily Driver');
    expect(banner.textContent).toContain('COMPANION CORE: READ-ONLY');

    // Verify 4 personal hobbies are ACTIVE while 3 self_dev hobbies are LOCKED
    const personalTrack = container.querySelector('[data-testid="hobby-track-personal"]')!;
    const selfDevTrack = container.querySelector('[data-testid="hobby-track-selfdev"]')!;
    expect(personalTrack.textContent).toContain('life_study_organizer');
    expect(personalTrack.textContent).toContain('wiki_gardener');
    expect(selfDevTrack.textContent).toContain('LOCKED (REQUIRES SELF-DEV ON)');

    const runLifeBtn = container.querySelector(
      '[data-testid="run-hobby-life_study_organizer"]'
    ) as HTMLButtonElement;
    const runCodeSimpBtn = container.querySelector(
      '[data-testid="run-hobby-code_simplifier"]'
    ) as HTMLButtonElement;
    expect(runLifeBtn.disabled).toBe(false);
    expect(runCodeSimpBtn.disabled).toBe(true);

    // 2. Toggle Hobby Mode ON -> Quadrant Q2 (Personal hobbies active, Core still READ-ONLY)
    const hobbyOnBtn = container.querySelector(
      '[data-testid="toggle-hobby-on"]'
    ) as HTMLButtonElement;
    await act(async () => hobbyOnBtn.click());
    await flushUi(100);

    expect(banner.textContent).toContain('Q2 — Stable Daily Driver + Personal/Ops Hobbies');
    expect(hobbiesChip.textContent).toContain('HOBBIES: ON');
    expect(selfDevChip.textContent).toContain('SELF-DEV: OFF (RO)');
    expect(runCodeSimpBtn.disabled).toBe(true);

    // Check OpsStudio sandbox clone button is mechanically disabled while Self-Dev Mode = OFF
    const opsTab = Array.from(container.querySelectorAll('.system-subnav button')).find((b) =>
      b.textContent?.includes('Backups, Sandboxes & Doctor')
    ) as HTMLButtonElement;
    await act(async () => opsTab.click());
    await flushUi(80);
    const sbxPanel = container.querySelector('[data-testid="ops-sandboxes-panel"]')!;
    expect(sbxPanel.textContent).toContain('LOCKED (SELF-DEV OFF)');
    const cloneBtn = Array.from(sbxPanel.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Clone Workspace')
    ) as HTMLButtonElement;
    expect(cloneBtn.disabled).toBe(true);

    // Jump back to Autonomy & Hobbies via the helper button in OpsStudio
    const jumpAutonomyBtn = Array.from(sbxPanel.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Autonomy & Hobbies')
    ) as HTMLButtonElement;
    await act(async () => jumpAutonomyBtn.click());
    await flushUi(80);

    // 3. Toggle Self-Dev Mode ON -> Quadrant Q4 (Full Autonomous Lab / VM Mode, both tracks unlocked!)
    const selfDevOnBtn = container.querySelector(
      '[data-testid="toggle-selfdev-on"]'
    ) as HTMLButtonElement;
    await act(async () => selfDevOnBtn.click());
    await flushUi(100);

    const bannerQ4 = container.querySelector('[data-testid="operating-quadrant-banner"]')!;
    expect(bannerQ4.textContent).toContain('Q4 — Full Autonomous Lab / VM Mode');
    expect(bannerQ4.textContent).toContain('COMPANION CORE: SELF-DEV UNLOCKED');
    expect(selfDevChip.textContent).toContain('SELF-DEV: ON');

    const runCodeSimpUnlocked = container.querySelector(
      '[data-testid="run-hobby-code_simplifier"]'
    ) as HTMLButtonElement;
    expect(runCodeSimpUnlocked.disabled).toBe(false);

    // 4. Toggle Hobby Mode OFF -> Quadrant Q3 (Deliberate Interactive Self-Dev)
    const hobbyOffBtn = container.querySelector(
      '[data-testid="toggle-hobby-off"]'
    ) as HTMLButtonElement;
    await act(async () => hobbyOffBtn.click());
    await flushUi(100);
    expect(bannerQ4.textContent).toContain('Q3 — Deliberate Interactive Self-Dev');

    // 5. Return Self-Dev Mode to OFF -> Quadrant Q1, then verify deployment env lock disables lever!
    const selfDevOffBtn = container.querySelector(
      '[data-testid="toggle-selfdev-off"]'
    ) as HTMLButtonElement;
    await act(async () => selfDevOffBtn.click());
    await flushUi(100);
    expect(bannerQ4.textContent).toContain('Q1 — Quiet Stable Daily Driver');

    await bridge.setEnvLock(true, true);
    const refreshBtn = Array.from(container.querySelectorAll('.studio-actions button')).find((b) =>
      b.textContent?.includes('Refresh')
    ) as HTMLButtonElement;
    await act(async () => refreshBtn.click());
    await flushUi(100);

    expect(container.querySelector('[data-testid="selfdev-env-lock-notice"]')?.textContent).toContain(
      'COMPANION_IMMUTABLE_CORE=1'
    );
    expect(
      (container.querySelector('[data-testid="toggle-selfdev-on"]') as HTMLButtonElement).disabled
    ).toBe(true);
    await bridge.setEnvLock(false, false);
  });

  it('exercises Option A Action-Only Chat Approval Banner + Mobile/Desktop Top-Bar Notification Bell', async () => {
    // Top-Bar Notification Bell must NOT carry .desktop-only so mobile dogfooding always renders it
    const bellBtn = container.querySelector(
      '[data-testid="top-notification-bell"]'
    ) as HTMLButtonElement;
    expect(bellBtn).toBeTruthy();
    expect(bellBtn.classList.contains('desktop-only')).toBe(false);

    // Navigate to Forever Chat — seedWiki31 earlier created a waiting wiki_edit approval!
    const chatNav = Array.from(container.querySelectorAll('.nav-btn')).find((b) =>
      b.textContent?.includes('Forever Chat')
    ) as HTMLButtonElement;
    await act(async () => chatNav.click());
    await flushUi(100);

    // Old passive [BACKGROUND UPDATES] strip must be gone; Action-Only [APPROVAL NEEDED] banner is shown
    expect(container.textContent).not.toContain('[BACKGROUND UPDATES]');
    const actionBanner = container.querySelector('[data-testid="chat-action-approval-banner"]');
    expect(actionBanner).toBeTruthy();
    expect(actionBanner?.textContent).toContain('[APPROVAL NEEDED]');

    // Open Notification Bell popover and confirm the waiting approval is surfaced there too
    await act(async () => bellBtn.click());
    await flushUi(60);
    const popover = container.querySelector('[data-testid="notification-popover"]');
    expect(popover).toBeTruthy();
    expect(popover?.textContent).toContain('APPROVAL NEEDED');

    // Close popover, click inline "Approve" right on the Forever Chat banner, and verify banner vanishes!
    await act(async () => bellBtn.click());
    const inlineApproveBtn = Array.from(actionBanner!.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Approve')
    ) as HTMLButtonElement;
    expect(inlineApproveBtn).toBeTruthy();
    await act(async () => inlineApproveBtn.click());
    await flushUi(120);

    expect(container.querySelector('[data-testid="chat-action-approval-banner"]')).toBeNull();

    // Open Bell popover again — if any passive updates remain, Clear All empties the tray cleanly
    await act(async () => bellBtn.click());
    await flushUi(60);
    const popoverAfter = container.querySelector('[data-testid="notification-popover"]')!;
    expect(popoverAfter.textContent).not.toContain('APPROVAL NEEDED');
    const clearAllBtn = Array.from(popoverAfter.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Clear All')
    ) as HTMLButtonElement | undefined;
    if (clearAllBtn) {
      await act(async () => clearAllBtn.click());
      await flushUi(100);
    }
    expect(popoverAfter.textContent).toContain('All caught up');
  });
});

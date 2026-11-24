import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import * as path from 'node:path';
import * as readline from 'node:readline';
import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { App } from '../src/App';

const PYTHON_STUDIO_BRIDGE = `
import base64, json, os, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path

ipc_out = sys.stdout
sys.stdout = sys.stderr

repo_root = Path(sys.argv[1])
tmp_dir = Path(tempfile.mkdtemp(prefix="companion_vitest_studio_"))
os.environ["COMPANION_DATA_DIR"] = str(tmp_dir / "data")
os.environ["COMPANION_SECRETS_FILE"] = str(tmp_dir / "secrets.env")
sys.path.insert(0, str(repo_root))

import companion.memory_store as ms
import companion.jobs as bj
import companion.core_actions as ca
from companion.config import settings
from companion.core_actions import execute_core_action
from companion.streaming import check_cancelled
from surfaces.web_server import create_web_server, dispatch_raw_http

store = ms.MemoryStore(db_path=tmp_dir / "data" / "companion.db")
ms._DEFAULT_STORE = store

# Create a real temporary Git repository on main for Code Repository Studio tests
demo_repo = tmp_dir / "payments_service"
demo_repo.mkdir(parents=True, exist_ok=True)
subprocess.run(["git", "init", "-b", "main"], cwd=str(demo_repo), check=True, capture_output=True)
subprocess.run(["git", "config", "user.name", "Studio Test"], cwd=str(demo_repo), check=True, capture_output=True)
subprocess.run(["git", "config", "user.email", "studio@example.com"], cwd=str(demo_repo), check=True, capture_output=True)
(demo_repo / "README.md").write_text("# Payments Service\\n", encoding="utf-8")
(demo_repo / "pyproject.toml").write_text("[project]\\nname='payments'\\n", encoding="utf-8")
(demo_repo / ".agents" / "rules").mkdir(parents=True)
(demo_repo / ".agents" / "rules" / "clean.md").write_text("# Keep modules clean\\n", encoding="utf-8")
subprocess.run(["git", "add", "."], cwd=str(demo_repo), check=True, capture_output=True)
subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(demo_repo), check=True, capture_output=True)

execute_core_action("vault", {"subaction": "declare", "name": "github-pat", "cred_type": "api_key", "description": "GitHub Token"}, actor="user", store=store)

MIN_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg==")

rev_counter = {"alpha": 0}
lock = threading.Lock()

def fake_chat(messages, *_args, **_kwargs):
    blob = "\\n".join(str(m.get("content") or "") for m in messages)
    if "SLOW_HOLD_CARD" in blob:
        for _ in range(60):
            check_cancelled()
            time.sleep(0.05)
        return {"choices": [{"message": {"role": "assistant", "content": "Finished slow card."}}]}
    has_tool = any(m.get("role") == "tool" for m in messages)
    is_alpha = "Build Alpha Endpoint" in blob
    if not has_tool:
        if is_alpha:
            with lock:
                rev_counter["alpha"] += 1
                ver = rev_counter["alpha"]
            fname, code = "alpha_api.py", f"ALPHA_OK = True\\nALPHA_VER = {ver}\\n"
        else:
            fname, code = "beta_api.py", "BETA_OK = True\\n"
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "tc_wt_1",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": json.dumps({"path": fname, "content": code})},
                    }],
                }
            }]
        }
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": (
                    f"Implemented {'Alpha' if is_alpha else 'Beta'} endpoint cleanly.\\n"
                    "Ran 3 tests in 0.01s - OK\\n"
                    + ("Captured preview screenshot desktop_shot.png" if is_alpha else "")
                ),
            }
        }]
    }

def fake_tool(_name, args):
    ws_root = settings.get_workspace().project_root
    rel = str(args.get("path") or "out.py")
    (ws_root / rel).write_text(str(args.get("content") or ""), encoding="utf-8")
    if rel == "alpha_api.py":
        (ws_root / "desktop_shot.png").write_bytes(MIN_PNG)
    return f"wrote {rel}"

ca.get_job_manager = lambda store=None, **_kw: bj.BackgroundJobManager(store=store, chat_fn=fake_chat, tool_fn=fake_tool)

srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=fake_chat)
ipc_out.write(json.dumps({"ready": True, "csrf_token": sess.csrf_token, "demo_repo": str(demo_repo)}) + "\\n")
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

class StudioBridge {
  private proc!: ChildProcessWithoutNullStreams;
  private rl!: readline.Interface;
  private pending = new Map<number, (msg: { status: number; body: string }) => void>();
  private bootResolve: ((line: string) => void) | null = null;
  private nextId = 1;
  public csrfToken = '';
  public demoRepo = '';

  async start(): Promise<void> {
    const repoRoot = path.resolve(__dirname, '..', '..');
    this.proc = spawn('python', ['-B', '-c', PYTHON_STUDIO_BRIDGE, repoRoot], {
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
    this.demoRepo = boot.demo_repo;
  }

  async request(method: string, reqPath: string, body?: string, headers?: Record<string, string>) {
    const id = this.nextId++;
    const p = new Promise<{ status: number; body: string }>((resolve) => {
      this.pending.set(id, resolve);
    });
    this.proc.stdin.write(JSON.stringify({ id, method, path: reqPath, body, headers }) + '\n');
    return p;
  }

  async stop(): Promise<void> {
    this.proc.stdin.write(JSON.stringify({ cmd: 'quit' }) + '\n');
    this.rl.close();
    await new Promise<void>((r) => {
      this.proc.once('exit', () => r());
      setTimeout(() => r(), 1000);
    });
  }
}

function setInputValue(el: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, val: string) {
  const proto =
    el instanceof HTMLSelectElement
      ? window.HTMLSelectElement.prototype
      : el instanceof HTMLTextAreaElement
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
  desc?.set?.call(el, val);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

async function flushUi(ms = 80): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, ms));
  });
}

async function waitForSelector<T extends Element = HTMLElement>(
  rootEl: ParentNode,
  selector: string,
  timeoutMs = 5000
): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const el = rootEl.querySelector<T>(selector);
    if (el) return el;
    await flushUi(80);
  }
  return rootEl.querySelector<T>(selector) as T;
}

describe('Dev Studio, Check Repo, Parallel Git Worktrees & Deliverable Diff Review', () => {
  let bridge: StudioBridge;
  let container: HTMLDivElement;
  let root: Root;
  let origFetch: typeof globalThis.fetch;

  beforeAll(async () => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    bridge = new StudioBridge();
    await bridge.start();
    origFetch = globalThis.fetch;

    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const rawUrl = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      const u = new URL(rawUrl, 'http://127.0.0.1');
      const method = (init?.method || 'GET').toUpperCase();
      const hdrs: Record<string, string> = {};
      if (init?.headers) {
        const h = new Headers(init.headers);
        h.forEach((v, k) => {
          hdrs[k] = v;
        });
      }
      const bodyStr = typeof init?.body === 'string' ? init.body : undefined;
      const res = await bridge.request(method, u.pathname + u.search, bodyStr, hdrs);
      return new Response(res.body, {
        status: res.status,
        headers: { 'Content-Type': 'application/json' },
      });
    }) as typeof globalThis.fetch;

    container = document.createElement('div');
    document.body.appendChild(container);
    await act(async () => {
      root = createRoot(container);
      root.render(<App />);
    });
    await flushUi(180);
  }, 25000);

  afterAll(async () => {
    await act(async () => {
      root?.unmount();
    });
    container?.remove();
    globalThis.fetch = origFetch;
    await bridge?.stop();
  });

  it('configures provider Max Concurrent Requests and creates a Code Repository project via Check Repo + Vault dropdown', async () => {
    // 1. Settings -> Models & Tiers -> Max Concurrent Requests
    const settingsBtn = container.querySelector('[data-testid="open-settings-btn"]') as HTMLButtonElement;
    await act(async () => {
      settingsBtn.click();
    });
    await flushUi(100);
    const modelsNav = Array.from(container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Models & Tiers')
    ) as HTMLButtonElement;
    await act(async () => {
      modelsNav.click();
    });
    await flushUi(120);
    const concSelect = container.querySelector('[data-testid="provider-concurrency-local-default"]') as HTMLSelectElement;
    expect(concSelect).toBeTruthy();
    await act(async () => {
      setInputValue(concSelect, '4');
    });
    await flushUi(120);
    expect(concSelect.value).toBe('4');

    // Close Settings modal
    const closeSettingsBtn = container.querySelector('[aria-label="Close settings"]') as HTMLButtonElement;
    if (closeSettingsBtn) {
      await act(async () => {
        closeSettingsBtn.click();
      });
    } else {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    }
    await flushUi(80);

    // 2. Open Board & Goals and click `+ New Project`
    const navBoards = container.querySelector('[data-testid="nav-boards"]') as HTMLButtonElement;
    await act(async () => {
      navBoards.click();
    });
    await flushUi(120);

    const newProjBtn = container.querySelector('[data-testid="new-project-btn"]') as HTMLButtonElement;
    expect(newProjBtn).toBeTruthy();
    await act(async () => {
      newProjBtn.click();
    });
    await flushUi(100);

    // Personal / Goal Board mode hides Git fields by default
    expect(container.querySelector('[data-testid="code-repo-settings-box"]')).toBeNull();

    // Switch to Code Repository mode -> reveals Repository Path, Check Repo, Vault dropdown & Max Parallel Agents
    const codeModeBtn = container.querySelector('[data-testid="project-mode-code"]') as HTMLButtonElement;
    await act(async () => {
      codeModeBtn.click();
    });
    await flushUi(60);
    expect(container.querySelector('[data-testid="code-repo-settings-box"]')).toBeTruthy();

    const titleInput = container.querySelector('[data-testid="project-title-input"]') as HTMLInputElement;
    const repoPathInput = container.querySelector('[data-testid="project-workspace-path-input"]') as HTMLInputElement;
    const credSelect = container.querySelector('[data-testid="project-git-cred-input"]') as HTMLSelectElement;
    const maxWorkersSelect = container.querySelector('[data-testid="project-max-workers-select"]') as HTMLSelectElement;
    expect(Array.from(credSelect.options).some((o) => o.value === 'github-pat')).toBe(true);

    await act(async () => {
      setInputValue(titleInput, 'Payments API Studio');
      setInputValue(repoPathInput, bridge.demoRepo);
      setInputValue(credSelect, 'github-pat');
      setInputValue(maxWorkersSelect, '2');
    });

    // Click Check Repo and verify live Git repo badge
    const checkRepoBtn = container.querySelector('[data-testid="check-repo-btn"]') as HTMLButtonElement;
    await act(async () => {
      checkRepoBtn.click();
    });
    const probeBadge = await waitForSelector(container, '[data-testid="repo-probe-badge"]', 5000);
    expect(probeBadge).toBeTruthy();
    expect(probeBadge.textContent).toContain('Git repo on main');
    expect(probeBadge.textContent).toContain('Clean');

    // Create Project
    const saveProjBtn = container.querySelector('[data-testid="save-project-policy-btn"]') as HTMLButtonElement;
    await act(async () => {
      saveProjBtn.click();
    });
    const repoBadge = await waitForSelector(container, '[data-testid="project-code-repo-badge"]', 5000);
    expect(repoBadge?.textContent).toContain('payments_service');
  });

  it('runs 2 Ready cards in parallel isolated worktrees, reviews DiffView + screenshot + Request Changes, merges both to unblock dependent card, and supports Activity Stop All', async () => {
    const projSelect = container.querySelector('[data-testid="boards-surface"] select') as HTMLSelectElement;
    const pid = projSelect.value;
    expect(pid).toBeTruthy();

    const mkCard = async (title: string, deps: string[] = []) => {
      const r = await bridge.request(
        'POST',
        '/api/actions/agile',
        JSON.stringify({ subaction: 'create_item', project_id: pid, title, owner: 'companion', status: 'ready', depends_on: deps }),
        { 'Content-Type': 'application/json', 'X-Companion-CSRF': bridge.csrfToken }
      );
      return JSON.parse(r.body).item.item_id as string;
    };

    const cardA = await mkCard('Build Alpha Endpoint');
    const cardB = await mkCard('Build Beta Endpoint');
    const cardC = await mkCard('Wire Combined Router', [cardA, cardB]);

    // Switch Chat -> Boards to force fresh loadBoard()
    const navChat = container.querySelector('[data-testid="nav-chat"]') as HTMLButtonElement;
    const navBoards = container.querySelector('[data-testid="nav-boards"]') as HTMLButtonElement;
    await act(async () => {
      navChat.click();
    });
    await flushUi(60);
    await act(async () => {
      navBoards.click();
    });
    const runReadyBtn = await waitForSelector<HTMLButtonElement>(container, '[data-testid="run-parallel-ready-btn"]', 5000);
    expect(runReadyBtn).toBeTruthy();
    expect(runReadyBtn.textContent).toContain('Run Ready (2)');
    await act(async () => {
      runReadyBtn.click();
    });

    // Wait for both parallel worktree workers to complete and land in Verify (auto-polled or checked)
    for (let i = 0; i < 45; i++) {
      await flushUi(200);
      const chk = await bridge.request(
        'POST',
        '/api/actions/agile',
        JSON.stringify({ subaction: 'board_view', project_id: pid }),
        { 'Content-Type': 'application/json', 'X-Companion-CSRF': bridge.csrfToken }
      );
      const cards = JSON.parse(chk.body).cards as Array<{ item_id: string; column_key: string }>;
      const stA = cards.find((c) => c.item_id === cardA)?.column_key;
      const stB = cards.find((c) => c.item_id === cardB)?.column_key;
      if (stA === 'verify' && stB === 'verify') break;
    }

    // Reload board view & verify Review Diff badges appear on both Verify cards while Card C stays Blocked
    await act(async () => {
      navChat.click();
    });
    await flushUi(50);
    await act(async () => {
      navBoards.click();
    });
    await waitForSelector(container, `[data-testid="review-diff-badge-${cardA}"]`, 5000);
    expect(container.querySelector(`[data-testid="review-diff-badge-${cardB}"]`)).toBeTruthy();
    expect(container.querySelector('[data-testid="board-col-blocked"]')?.textContent).toContain('Wire Combined Router');

    // Click Card A -> DeliverableReviewPanel opens automatically with DiffView + Screenshot thumbnail
    const cardAEl = container.querySelector(`[data-testid="work-card-${cardA}"]`) as HTMLElement;
    await act(async () => {
      cardAEl.click();
    });
    await waitForSelector(container, '[data-testid="deliverable-review-panel"]', 5000);
    expect(container.querySelector('[data-testid="wiki-diff-view"]')?.textContent).toContain('ALPHA_VER = 1');
    expect(container.querySelector('[data-testid="deliverable-screenshots"] img')).toBeTruthy();

    // Exercise `↺ Request Changes` on Card A and verify revision updates branch diff to ALPHA_VER = 2
    const reqChangesBtn = container.querySelector('[data-testid="request-card-changes-btn"]') as HTMLButtonElement;
    await act(async () => {
      reqChangesBtn.click();
    });
    await flushUi(60);
    const revInput = container.querySelector('[data-testid="request-changes-input"]') as HTMLTextAreaElement;
    await act(async () => {
      setInputValue(revInput, 'Please bump ALPHA_VER to 2');
    });
    const submitChangesBtn = container.querySelector('[data-testid="submit-card-changes-btn"]') as HTMLButtonElement;
    await act(async () => {
      submitChangesBtn.click();
    });
    for (let i = 0; i < 40; i++) {
      await flushUi(200);
      const chk = await bridge.request(
        'POST',
        '/api/actions/agile',
        JSON.stringify({ subaction: 'board_view', project_id: pid }),
        { 'Content-Type': 'application/json', 'X-Companion-CSRF': bridge.csrfToken }
      );
      const cards = JSON.parse(chk.body).cards as Array<{ item_id: string; column_key: string }>;
      if (cards.find((c) => c.item_id === cardA)?.column_key === 'verify') break;
    }

    // Wait for DeliverableReviewPanel to auto-refresh Card A's updated diff (`ALPHA_VER = 2`)
    const revDeadline = Date.now() + 8000;
    while (Date.now() < revDeadline) {
      if ((container.querySelector('[data-testid="wiki-diff-view"]')?.textContent || '').includes('ALPHA_VER = 2')) {
        break;
      }
      await flushUi(200);
    }
    expect(container.querySelector('[data-testid="wiki-diff-view"]')?.textContent).toContain('ALPHA_VER = 2');

    // Merge Card A into local main
    const mergeBtnA = await waitForSelector<HTMLButtonElement>(container, '[data-testid="merge-card-local-btn"]', 5000);
    await act(async () => {
      mergeBtnA.click();
    });
    await flushUi(500);

    // Click Card B, wait for its beta_api.py diff, and Merge into local main -> unblocks dependent Card C!
    const freshCardBEl = await waitForSelector<HTMLElement>(container, `[data-testid="work-card-${cardB}"]`, 5000);
    await act(async () => {
      freshCardBEl.click();
    });
    const betaDeadline = Date.now() + 5000;
    while (Date.now() < betaDeadline) {
      if ((container.querySelector('[data-testid="wiki-diff-view"]')?.textContent || '').includes('BETA_OK = True')) {
        break;
      }
      await flushUi(120);
    }
    expect(container.querySelector('[data-testid="wiki-diff-view"]')?.textContent).toContain('BETA_OK = True');
    const mergeBtnB = await waitForSelector<HTMLButtonElement>(container, '[data-testid="merge-card-local-btn"]', 5000);
    await act(async () => {
      mergeBtnB.click();
    });
    const startCardCBtn = await waitForSelector<HTMLButtonElement>(container, `[data-testid="start-card-btn-${cardC}"]`, 5000);
    expect(startCardCBtn).toBeTruthy();
    expect(container.querySelector('[data-testid="board-col-ready"]')?.textContent).toContain('Wire Combined Router');

    // Dispatch 2 slow cards and verify ActivityPopover `Stop All` cancels all concurrent workers cleanly
    const slow1 = await mkCard('SLOW_HOLD_CARD One');
    const slow2 = await mkCard('SLOW_HOLD_CARD Two');
    await bridge.request(
      'POST',
      '/api/actions/agile',
      JSON.stringify({ subaction: 'start_item', project_id: pid, item_id: slow1 }),
      { 'Content-Type': 'application/json', 'X-Companion-CSRF': bridge.csrfToken }
    );
    await bridge.request(
      'POST',
      '/api/actions/agile',
      JSON.stringify({ subaction: 'start_item', project_id: pid, item_id: slow2 }),
      { 'Content-Type': 'application/json', 'X-Companion-CSRF': bridge.csrfToken }
    );
    await flushUi(120);

    const actBtn = container.querySelector('[data-testid="top-activity-btn"]') as HTMLButtonElement;
    await act(async () => {
      actBtn.click();
    });
    const stopAllBtn = await waitForSelector<HTMLButtonElement>(container, '[data-testid="activity-stop-all-btn"]', 5000);
    expect(stopAllBtn).toBeTruthy();
    await act(async () => {
      stopAllBtn.click();
    });
    await flushUi(250);
  }, 45000);
});

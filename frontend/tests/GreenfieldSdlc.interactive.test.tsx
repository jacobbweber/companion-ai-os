import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as readline from 'node:readline';
import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { App } from '../src/App';

const PYTHON_GREENFIELD_BRIDGE = `
import json, os, shutil, sys, tempfile
from pathlib import Path

ipc_out = sys.stdout
sys.stdout = sys.stderr

repo_root = Path(sys.argv[1]).resolve()
tmp_dir = Path(tempfile.mkdtemp(prefix="companion_vitest_greenfield_")).resolve()
projects_dir = (tmp_dir / "projects").resolve()
projects_dir.mkdir(parents=True, exist_ok=True)
os.environ["COMPANION_DATA_DIR"] = str(tmp_dir / "data")
os.environ["COMPANION_PROJECTS_DIR"] = str(projects_dir)
os.environ["COMPANION_SECRETS_FILE"] = str(tmp_dir / "secrets.env")
sys.path.insert(0, str(repo_root))

import companion.memory_store as ms
from companion.config import settings
from surfaces.web_server import create_web_server, dispatch_raw_http

store = ms.MemoryStore(db_path=tmp_dir / "data" / "companion.db")
ms._DEFAULT_STORE = store
store.set_user_profile("onboarded", "true")

def fake_chat(messages, *_args, **_kwargs):
    blob = "\\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")
    if "/sdlc-next" in blob or "[SDLC ROUTER DIRECTIVE" in blob:
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": (
                        "### Stage 1: Product Specification (sdlc-product) — Guided Co-Design\\n\\n"
                        "**Stop 1 — Problem, Vision & Stakeholders:** Let us align on Invoice Reconciler personas before writing product.md."
                    ),
                }
            }]
        }
    return {"choices": [{"message": {"role": "assistant", "content": "Ready."}}]}

srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=fake_chat)
ipc_out.write(json.dumps({
    "ready": True,
    "csrf_token": sess.csrf_token,
    "projects_dir": str(projects_dir),
    "app_root": str(settings.root.resolve()),
}) + "\\n")
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
    ipc_out.write(json.dumps({"id": req["id"], "status": code, "body": out}) + "\\n")
    ipc_out.flush()

srv.server_close()
store.close()
shutil.rmtree(tmp_dir, ignore_errors=True)
`;

interface BridgeRes {
  status: number;
  body: string;
}

class GreenfieldBridge {
  private proc!: ChildProcessWithoutNullStreams;
  private rl!: readline.Interface;
  private pending = new Map<number, (r: BridgeRes) => void>();
  private seq = 1;
  public csrfToken = '';
  public projectsDir = '';
  public appRoot = '';

  async start(): Promise<void> {
    const repoRoot = path.resolve(__dirname, '..', '..');
    const pyExe = process.platform === 'win32' ? 'python' : 'python3';
    this.proc = spawn(pyExe, ['-B', '-c', PYTHON_GREENFIELD_BRIDGE, repoRoot], {
      cwd: repoRoot,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stderrBuf = '';
    this.proc.stderr.on('data', (chunk) => {
      stderrBuf += String(chunk);
    });
    this.rl = readline.createInterface({ input: this.proc.stdout });
    await new Promise<void>((resolve, reject) => {
      const onFirst = (line: string) => {
        try {
          const msg = JSON.parse(line);
          if (msg.ready) {
            this.csrfToken = msg.csrf_token;
            this.projectsDir = msg.projects_dir;
            this.appRoot = msg.app_root;
            this.rl.off('line', onFirst);
            this.rl.on('line', (l) => this.onLine(l));
            resolve();
          }
        } catch (err) {
          reject(err);
        }
      };
      this.rl.on('line', onFirst);
      this.proc.once('error', reject);
      this.proc.once('exit', (code) => {
        if (!this.csrfToken) {
          reject(new Error(`Python bridge exited early (${code}): ${stderrBuf}`));
        }
      });
    });
  }

  private onLine(line: string): void {
    try {
      const msg = JSON.parse(line);
      const cb = this.pending.get(msg.id);
      if (cb) {
        this.pending.delete(msg.id);
        cb({ status: msg.status, body: msg.body });
      }
    } catch {
      // ignore non-json
    }
  }

  request(method: string, reqPath: string, body?: string, headers?: Record<string, string>): Promise<BridgeRes> {
    const id = this.seq++;
    return new Promise((resolve) => {
      this.pending.set(id, resolve);
      this.proc.stdin.write(JSON.stringify({ id, method, path: reqPath, body, headers }) + '\n');
    });
  }

  async stop(): Promise<void> {
    if (!this.proc) return;
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

describe('Greenfield Git Repo Bootstrap & /sdlc-* Co-Design Slash Router', () => {
  let bridge: GreenfieldBridge;
  let container: HTMLDivElement;
  let root: Root;
  let origFetch: typeof globalThis.fetch;

  beforeAll(async () => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    bridge = new GreenfieldBridge();
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

  it('auto-populates projects_dir/<slug> in Create New Repo mode, rejects app/ paths & missing clones, and bootstraps a Git repo on main', async () => {
    const navBoards = container.querySelector('[data-testid="nav-boards"]') as HTMLButtonElement;
    await act(async () => {
      navBoards.click();
    });
    await flushUi(120);

    const newProjBtn = container.querySelector('[data-testid="new-project-btn"]') as HTMLButtonElement;
    await act(async () => {
      newProjBtn.click();
    });
    await flushUi(120);

    const codeModeBtn = container.querySelector('[data-testid="project-mode-code"]') as HTMLButtonElement;
    await act(async () => {
      codeModeBtn.click();
    });
    await flushUi(80);

    const createRepoBtn = container.querySelector('[data-testid="repo-mode-create"]') as HTMLButtonElement;
    const attachRepoBtn = container.querySelector('[data-testid="repo-mode-attach"]') as HTMLButtonElement;
    expect(createRepoBtn.classList.contains('active')).toBe(true);
    expect(attachRepoBtn).toBeTruthy();

    const titleInput = container.querySelector('[data-testid="project-title-input"]') as HTMLInputElement;
    const repoPathInput = container.querySelector('[data-testid="project-workspace-path-input"]') as HTMLInputElement;
    await act(async () => {
      setInputValue(titleInput, 'Invoice Reconciler');
    });
    await flushUi(80);

    const expectedPath = path.join(bridge.projectsDir, 'invoice-reconciler');
    expect(path.normalize(repoPathInput.value)).toBe(path.normalize(expectedPath));

    // Check Repo on the not-yet-created greenfield path -> valid & ready to initialize
    const checkBtn = container.querySelector('[data-testid="check-repo-btn"]') as HTMLButtonElement;
    await act(async () => {
      checkBtn.click();
    });
    const probeBadge = await waitForSelector(container, '[data-testid="repo-probe-badge"]', 5000);
    expect(probeBadge.textContent).toContain('Path ready');

    // Probing a path inside Companion's own app/ directory must be mechanically rejected
    await act(async () => {
      setInputValue(repoPathInput, path.join(bridge.appRoot, 'nested-illegal-repo'));
      checkBtn.click();
    });
    await flushUi(120);
    expect(container.querySelector('[data-testid="repo-probe-badge"]')?.textContent).toContain('SAFETY GUARD REJECTED');

    // Attach Existing Clone mode must warn on Check Repo AND reject a non-existent folder on Save
    await act(async () => {
      attachRepoBtn.click();
    });
    await flushUi(60);
    await act(async () => {
      setInputValue(repoPathInput, path.join(bridge.projectsDir, 'missing-clone'));
      checkBtn.click();
    });
    await flushUi(120);
    const attachWarnBadge = container.querySelector('[data-testid="repo-probe-badge"] .badge');
    expect(attachWarnBadge?.classList.contains('warn')).toBe(true);
    expect(attachWarnBadge?.textContent).toContain('does not exist');

    const saveBtn = container.querySelector('[data-testid="save-project-policy-btn"]') as HTMLButtonElement;
    await act(async () => {
      saveBtn.click();
    });
    await flushUi(120);
    // Inspector remains open because attach failed closed
    expect(container.querySelector('[data-testid="project-inspector"]')).toBeTruthy();

    // Switch back to Create New Repo, set clean greenfield path, and save
    await act(async () => {
      createRepoBtn.click();
    });
    await flushUi(60);
    await act(async () => {
      setInputValue(repoPathInput, expectedPath);
    });
    await act(async () => {
      saveBtn.click();
    });
    const repoBadge = await waitForSelector(container, '[data-testid="project-code-repo-badge"]', 5000);
    expect(repoBadge?.textContent).toContain('invoice-reconciler');

    // Verify real Git repo initialized on disk with .gitignore + README.md + active workspace focus
    expect(fs.existsSync(path.join(expectedPath, '.git'))).toBe(true);
    expect(fs.existsSync(path.join(expectedPath, '.gitignore'))).toBe(true);
    expect(fs.existsSync(path.join(expectedPath, 'README.md'))).toBe(true);

    const statusRes = await bridge.request('GET', '/api/status');
    const statusJson = JSON.parse(statusRes.body);
    expect(path.normalize(statusJson.workspace.project_root)).toBe(path.normalize(expectedPath));
    expect(statusJson.workspace.is_self_project).toBe(false);
  });

  it('surfaces /sdlc-next and /sdlc-1..7 + /sdlc-change in Composer autocomplete (including >14 chars) and runs interactively in chat mode', async () => {
    const navChat = container.querySelector('[data-testid="nav-chat"]') as HTMLButtonElement;
    await act(async () => {
      navChat.click();
    });
    await flushUi(120);

    const composer = container.querySelector('[data-testid="chat-composer-input"]') as HTMLTextAreaElement;
    expect(composer).toBeTruthy();

    // Verify >14-char slash prefix (/sdlc-2-architec = 16 chars) still shows autocomplete!
    await act(async () => {
      setInputValue(composer, '/sdlc-2-architec');
    });
    await flushUi(60);
    const menuLong = container.querySelector('[data-testid="slash-autocomplete-menu"]');
    expect(menuLong).toBeTruthy();
    expect(menuLong?.textContent).toContain('/sdlc-2-architecture');

    // Type `/sdlc` -> verify `/sdlc-next`, `/sdlc-1-product`, `/sdlc-7-operate`, and `/sdlc-change` all appear
    await act(async () => {
      setInputValue(composer, '/sdlc');
    });
    await flushUi(60);
    const menuAll = container.querySelector('[data-testid="slash-autocomplete-menu"]') as HTMLElement;
    expect(menuAll).toBeTruthy();
    expect(menuAll.textContent).toContain('/sdlc-next');
    expect(menuAll.textContent).toContain('/sdlc-1-product');
    expect(menuAll.textContent).toContain('/sdlc-7-operate');
    expect(menuAll.textContent).toContain('/sdlc-change');

    // Click `/sdlc-next` -> populates `/sdlc-next ` in chat mode (NOT plan/goal mode!)
    const nextRow = Array.from(menuAll.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('/sdlc-next')
    ) as HTMLButtonElement;
    await act(async () => {
      nextRow.click();
    });
    await flushUi(60);
    expect(composer.value).toBe('/sdlc-next ');
    expect(container.querySelector('[data-testid="composer-mode-pill-plan"]')).toBeNull();
    expect(container.querySelector('[data-testid="composer-mode-pill-goal"]')).toBeNull();

    // Press Enter -> dispatches `/sdlc-next` via SSE 202 and renders Stage 1 Guided Co-Design Stop 1
    await act(async () => {
      composer.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
    const deadline = Date.now() + 6000;
    while (Date.now() < deadline) {
      if ((container.textContent || '').includes('Stop 1 — Problem, Vision & Stakeholders')) {
        break;
      }
      await flushUi(120);
    }
    expect(container.textContent).toContain('Stage 1: Product Specification (sdlc-product)');
    expect(container.textContent).toContain('Stop 1 — Problem, Vision & Stakeholders');

    // Split-brain regression: switch to a Personal Goal Board (`life-home`), verify `/` puts everyday actions first,
    // then run `/project init chat-bootstrapped-app` in Chat and verify Turn 2 preserves the new repo focus!
    const personalRailBtn = Array.from(container.querySelectorAll('aside button')).find((b) =>
      b.textContent?.includes('Home & Life')
    ) as HTMLButtonElement | undefined;
    if (personalRailBtn) {
      await act(async () => {
        personalRailBtn.click();
      });
      await flushUi(120);
      await act(async () => {
        setInputValue(composer, '/');
      });
      await flushUi(60);
      const personalMenu = container.querySelector('[data-testid="slash-autocomplete-menu"]');
      const firstCmd = personalMenu?.querySelector('button strong')?.textContent?.trim();
      expect(firstCmd).toBe('/morning');
    }

    await act(async () => {
      setInputValue(composer, '/project init chat-bootstrapped-app');
    });
    await flushUi(60);
    await act(async () => {
      composer.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
    const initDeadline = Date.now() + 5000;
    while (Date.now() < initDeadline) {
      if ((container.textContent || '').includes('Initialized & activated project')) {
        break;
      }
      await flushUi(100);
    }
    await flushUi(150);
    expect(container.textContent).toContain('Initialized & activated project');

    // Send Turn 2 (`Hello`) without clicking the sidebar -> must NOT clobber `chat-bootstrapped-app` back to `life-home`!
    await act(async () => {
      setInputValue(composer, 'Ready for co-design');
    });
    await flushUi(60);
    await act(async () => {
      composer.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
    await flushUi(350);
    const statusAfter = JSON.parse((await bridge.request('GET', '/api/status')).body);
    expect(path.normalize(statusAfter.workspace.project_root)).toBe(
      path.normalize(path.join(bridge.projectsDir, 'chat-bootstrapped-app'))
    );
    expect(statusAfter.workspace.is_self_project).toBe(false);
  });

  it('pins custom Projects Root in Settings & inline Change Root, creates nested subfolder (+ New Folder -> experiments -> my-app), strips leading /, and selects clones in 1 click', async () => {
    const customRoot = path.join(path.dirname(bridge.projectsDir), 'pinned_spark_projects');

    // 1. Open Settings -> General & Autonomy and pin custom Projects Root (rejecting app/ first)
    const openSettingsBtn = container.querySelector('[data-testid="open-settings-btn"]') as HTMLButtonElement;
    await act(async () => {
      openSettingsBtn.click();
    });
    await flushUi(100);
    const settingsRootInput = container.querySelector('[data-testid="settings-projects-root-input"]') as HTMLInputElement;
    const settingsSaveRootBtn = container.querySelector('[data-testid="settings-save-projects-root-btn"]') as HTMLButtonElement;
    expect(settingsRootInput).toBeTruthy();

    await act(async () => {
      setInputValue(settingsRootInput, bridge.appRoot);
      settingsSaveRootBtn.click();
    });
    await flushUi(120);
    expect(container.querySelector('[data-testid="settings-projects-root-badge"]')?.textContent).toBe('Default');

    await act(async () => {
      setInputValue(settingsRootInput, customRoot);
      settingsSaveRootBtn.click();
    });
    await flushUi(140);
    expect(container.querySelector('[data-testid="settings-projects-root-badge"]')?.textContent).toBe('Pinned');
    expect(fs.existsSync(customRoot)).toBe(true);

    const closeSettingsBtn = container.querySelector('[data-testid="settings-modal"] button[aria-label="Close settings"]') as HTMLButtonElement;
    await act(async () => {
      closeSettingsBtn.click();
    });
    await flushUi(80);

    // 2. Open Boards -> New Project -> Code Repository -> verify Pinned Root Header & inline + New Folder -> experiments
    const navBoards = container.querySelector('[data-testid="nav-boards"]') as HTMLButtonElement;
    await act(async () => {
      navBoards.click();
    });
    await flushUi(100);
    const newProjBtn = container.querySelector('[data-testid="new-project-btn"]') as HTMLButtonElement;
    await act(async () => {
      newProjBtn.click();
    });
    await flushUi(120);
    const codeModeBtn = container.querySelector('[data-testid="project-mode-code"]') as HTMLButtonElement;
    await act(async () => {
      codeModeBtn.click();
    });
    await flushUi(120);

    const rootDisplay = container.querySelector('[data-testid="projects-root-display"]');
    expect(path.normalize(rootDisplay?.textContent || '')).toBe(path.normalize(customRoot));

    const upBtn = container.querySelector('[data-testid="explorer-up-btn"]') as HTMLButtonElement;
    expect(upBtn.disabled).toBe(true);

    const newFolderToggle = container.querySelector('[data-testid="explorer-new-folder-toggle"]') as HTMLButtonElement;
    await act(async () => {
      newFolderToggle.click();
    });
    await flushUi(60);
    const newFolderInput = container.querySelector('[data-testid="explorer-new-folder-input"]') as HTMLInputElement;
    const newFolderSubmit = container.querySelector('[data-testid="explorer-new-folder-submit"]') as HTMLButtonElement;
    await act(async () => {
      setInputValue(newFolderInput, '/experiments');
    });
    await flushUi(40);
    expect(newFolderInput.value).toBe('experiments');
    await act(async () => {
      newFolderSubmit.click();
    });
    await flushUi(150);

    // Explorer automatically drilled into `experiments` (`↑ Up` enabled, plain folder created without .git)
    expect(upBtn.disabled).toBe(false);
    expect(fs.existsSync(path.join(customRoot, 'experiments'))).toBe(true);
    expect(fs.existsSync(path.join(customRoot, 'experiments', '.git'))).toBe(false);

    // 3. Type title + accidental leading `/` in New Repo Folder Name -> strips `/` and previews `<customRoot>/experiments/my-app`
    const titleInput = container.querySelector('[data-testid="project-title-input"]') as HTMLInputElement;
    const folderNameInput = container.querySelector('[data-testid="repo-folder-name-input"]') as HTMLInputElement;
    await act(async () => {
      setInputValue(titleInput, 'Nested Experiment Service');
    });
    await flushUi(60);
    await act(async () => {
      setInputValue(folderNameInput, '/my-app');
    });
    await flushUi(60);
    expect(folderNameInput.value).toBe('my-app');

    const expectedNestedRepo = path.join(customRoot, 'experiments', 'my-app');
    const previewBanner = container.querySelector('[data-testid="create-repo-live-preview"]');
    expect(path.normalize(previewBanner?.textContent || '')).toContain(path.normalize(expectedNestedRepo));

    // Clearing New Repo Folder Name re-enables title-based derivation, then set `my-app` and create!
    await act(async () => {
      setInputValue(folderNameInput, '');
    });
    await flushUi(40);
    await act(async () => {
      setInputValue(titleInput, 'Agent Sandbox');
    });
    await flushUi(40);
    expect(folderNameInput.value).toBe('agent-sandbox');
    await act(async () => {
      setInputValue(folderNameInput, '/my-app');
    });
    await flushUi(40);

    const saveBtn = container.querySelector('[data-testid="save-project-policy-btn"]') as HTMLButtonElement;
    await act(async () => {
      saveBtn.click();
    });
    const saveDeadline = Date.now() + 6000;
    while (Date.now() < saveDeadline) {
      if (!container.querySelector('[data-testid="project-inspector"]') && fs.existsSync(path.join(expectedNestedRepo, '.git'))) {
        break;
      }
      await flushUi(100);
    }
    expect(fs.existsSync(path.join(expectedNestedRepo, '.git'))).toBe(true);

    // 4. Open New Project -> Attach Existing Clone -> drill into `[Folder] experiments` -> 1-click select `[Git Repo · main] my-app`
    const freshNewProjBtn = await waitForSelector<HTMLButtonElement>(container, '[data-testid="new-project-btn"]', 4000);
    await act(async () => {
      freshNewProjBtn.click();
    });
    const freshCodeBtn = await waitForSelector<HTMLButtonElement>(container, '[data-testid="project-mode-code"]', 4000);
    await act(async () => {
      freshCodeBtn.click();
    });
    const freshAttachBtn = await waitForSelector<HTMLButtonElement>(container, '[data-testid="repo-mode-attach"]', 4000);
    await act(async () => {
      freshAttachBtn.click();
    });
    await flushUi(120);

    const expRow = await waitForSelector(container, '[data-testid="explorer-entry-experiments"]', 4000);
    expect(container.querySelector('[data-testid="explorer-badge-folder-experiments"]')?.textContent).toBe('Folder');
    await act(async () => {
      expRow.click();
    });
    await flushUi(140);

    const myAppRow = await waitForSelector(container, '[data-testid="explorer-entry-my-app"]', 4000);
    expect(container.querySelector('[data-testid="explorer-badge-git-my-app"]')?.textContent).toContain('Git Repo · main');
    expect(container.querySelector('[data-testid="explorer-badge-linked-my-app"]')?.textContent).toContain('Linked Project');

    await act(async () => {
      myAppRow.click();
    });
    const probeBadge = await waitForSelector(container, '[data-testid="repo-probe-badge"] .badge', 4000);
    expect(probeBadge.classList.contains('ok')).toBe(true);
    expect(probeBadge.textContent).toContain('Git repo on main');
    const wsInput = container.querySelector('[data-testid="project-workspace-path-input"]') as HTMLInputElement;
    expect(path.normalize(wsInput.value)).toBe(path.normalize(expectedNestedRepo));
  }, 15000);
});

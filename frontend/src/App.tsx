import React, { useEffect, useRef, useState } from 'react';
import { Inbox, KeyRound, PanelLeftOpen, PanelRight } from 'lucide-react';
import { apiPost, callAction, fetchStatus, pairLanToken } from './api';
import { SettingsModal } from './settings/SettingsModal';
import { ActivityPopover } from './shell/ActivityPopover';
import { AuxSplitPane } from './shell/AuxSplitPane';
import { CommandPaletteModal } from './shell/CommandPaletteModal';
import { LeftSidebar } from './shell/LeftSidebar';
import { AutomationsSurface } from './surfaces/AutomationsSurface';
import { BoardGoalsSurface } from './surfaces/BoardGoalsSurface';
import { CalendarSurface } from './surfaces/CalendarSurface';
import { ChatSurface } from './surfaces/ChatSurface';
import { CustomizationsSurface } from './surfaces/CustomizationsSurface';
import { KnowledgeSurface } from './surfaces/KnowledgeSurface';
import type {
  AuxInspectorTarget,
  ProjectGraphEntry,
  SettingsSectionId,
  SurfaceId,
  ThemeId,
  WebStatus,
} from './types';

const SURFACE_LABELS: Record<SurfaceId, string> = {
  chat: 'Chat',
  boards: 'Board & Goals',
  calendar: 'Calendar',
  knowledge: 'Knowledge',
  automations: 'Automations',
  customizations: 'Customizations',
};

export const App: React.FC = () => {
  const [surface, setSurface] = useState<SurfaceId>('chat');
  const [railCollapsed, setRailCollapsed] = useState<boolean>(() =>
    typeof window !== 'undefined' ? window.innerWidth <= 768 : false
  );
  const [theme, setTheme] = useState<ThemeId>(() => {
    const saved = window.localStorage.getItem('companion_theme') as ThemeId | null;
    return saved || 'obsidian';
  });
  const [status, setStatus] = useState<WebStatus | null>(null);
  const [projects, setProjects] = useState<ProjectGraphEntry[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>('');
  const [auxTarget, setAuxTarget] = useState<AuxInspectorTarget>(null);
  const [showActivity, setShowActivity] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSectionId>('general');
  const [searchOpen, setSearchOpen] = useState(false);
  const [wikiInitialPath, setWikiInitialPath] = useState('');
  const [wikiOpenNonce, setWikiOpenNonce] = useState(0);
  const [knowledgeInitialMode, setKnowledgeInitialMode] = useState<'notes' | 'mindmap' | 'study'>('notes');
  const [dataRefreshNonce, setDataRefreshNonce] = useState(0);
  const [authRequired, setAuthRequired] = useState(false);
  const [lanTokenInput, setLanTokenInput] = useState('');
  const [toast, setToast] = useState('');
  const activityAnchorRef = useRef<HTMLDivElement | null>(null);

  const showToast = (msg: string) => {
    setToast(msg);
    window.setTimeout(() => {
      setToast((cur) => (cur === msg ? '' : cur));
    }, 4000);
  };

  const applyTheme = (next: ThemeId) => {
    setTheme(next);
    document.documentElement.setAttribute('data-theme', next);
    window.localStorage.setItem('companion_theme', next);
  };

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    let prevW = window.innerWidth;
    const onResize = () => {
      const nextW = window.innerWidth;
      if (prevW > 768 && nextW <= 768) setRailCollapsed(true);
      prevW = nextW;
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const refreshStatusAndProjects = async () => {
    try {
      const st = await fetchStatus();
      if (st.auth_required) {
        setAuthRequired(true);
        return;
      }
      if (st.ok) {
        setAuthRequired(false);
        setStatus(st);
        const initialFocus = st.focus_project_id || st.workspace?.project_id || '';
        if (!selectedProjectId && initialFocus) {
          setSelectedProjectId(initialFocus);
        }
      }
      const bRes = await callAction<{ boards?: ProjectGraphEntry[] }>('agile', { subaction: 'list_boards' });
      if (bRes.boards) {
        setProjects(bRes.boards);
        if (!selectedProjectId && bRes.boards.length > 0) {
          setSelectedProjectId(bRes.boards[0].project_id);
        }
      }
    } catch {
      // ignore transient poll error
    }
  };

  useEffect(() => {
    void refreshStatusAndProjects();
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'hidden') {
        void fetchStatus().then((s) => {
          if (s.auth_required) setAuthRequired(true);
          else if (s.ok) {
            setAuthRequired(false);
            setStatus(s);
          }
        });
      }
    }, 5000);
    const onWakeOrOnline = () => {
      if (document.visibilityState !== 'hidden') {
        void refreshStatusAndProjects();
      }
    };
    document.addEventListener('visibilitychange', onWakeOrOnline);
    window.addEventListener('online', onWakeOrOnline);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onWakeOrOnline);
      window.removeEventListener('online', onWakeOrOnline);
    };
  }, []);

  useEffect(() => {
    const onKeyDown = (ev: KeyboardEvent) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'k') {
        ev.preventDefault();
        setSearchOpen((v) => !v);
      } else if (ev.key === 'Escape') {
        setShowActivity(false);
        setSearchOpen(false);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  useEffect(() => {
    if (!showActivity) return;
    const onPointerDown = (ev: MouseEvent) => {
      if (activityAnchorRef.current && !activityAnchorRef.current.contains(ev.target as Node)) {
        setShowActivity(false);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [showActivity]);

  const handleSelectSurface = (next: SurfaceId) => {
    setShowActivity(false);
    setSurface(next);
    if (typeof window !== 'undefined' && window.innerWidth <= 768) {
      setRailCollapsed(true);
    }
  };

  const syncProjectFocus = (projectId: string) => {
    setSelectedProjectId(projectId);
    void apiPost('/api/settings', { focus_project_id: projectId }).catch(() => {});
  };

  const handleSelectProject = (projectId: string, openBoard = false) => {
    setShowActivity(false);
    syncProjectFocus(projectId);
    if (openBoard || (surface !== 'chat' && surface !== 'boards')) {
      handleSelectSurface('boards');
    } else if (typeof window !== 'undefined' && window.innerWidth <= 768) {
      setRailCollapsed(true);
    }
  };

  const openNewProjectInspector = () => {
    setShowActivity(false);
    handleSelectSurface('boards');
    setAuxTarget({ kind: 'project', projectId: '', createNew: true });
  };

  const activeProjTitle =
    projects.find((p) => p.project_id === selectedProjectId || p.slug === selectedProjectId)?.title ||
    status?.workspace?.project_id ||
    'Companion';

  const pendingApprovalsCount = status?.pending_approvals_count ?? (status?.pending_approvals?.length || 0);
  const passiveNudgeCount = (status?.session_nudges || []).filter((n) => n.category !== 'approval').length;
  const unseenAutonomy = status?.unseen_autonomy_count || 0;
  const activityBadgeCount = pendingApprovalsCount + passiveNudgeCount + unseenAutonomy;

  return (
    <div className="agy-shell">
      <LeftSidebar
        collapsed={railCollapsed}
        activeSurface={surface}
        status={status}
        projects={projects}
        selectedProjectId={selectedProjectId}
        onToggleCollapse={() => setRailCollapsed((c) => !c)}
        onSelectSurface={handleSelectSurface}
        onSelectProject={handleSelectProject}
        onOpenNewProject={openNewProjectInspector}
        onOpenSearch={() => setSearchOpen(true)}
        onOpenSettings={() => {
          setShowActivity(false);
          setSettingsSection('general');
          setSettingsOpen(true);
        }}
        onRefresh={() => void refreshStatusAndProjects()}
      />

      <div className="agy-workspace-frame">
        <main className="agy-center-col">
          {/* Calm Minimal Breadcrumb Top Bar */}
          <header className="canvas-topbar">
            <div className="breadcrumb-row">
              {railCollapsed && (
                <button
                  type="button"
                  className="icon-btn"
                  data-testid="expand-sidebar-btn"
                  onClick={() => setRailCollapsed(false)}
                  title="Open navigation"
                  aria-label="Open navigation"
                >
                  <PanelLeftOpen size={15} />
                </button>
              )}
              <button
                type="button"
                className="breadcrumb-project"
                style={{
                  background: 'none',
                  border: 'none',
                  padding: '2px 5px',
                  borderRadius: 'var(--radius-xs)',
                  cursor: 'pointer',
                }}
                title={surface === 'boards' ? 'Jump to Chat for this project' : 'Open project Board & Goals'}
                onClick={() => handleSelectSurface(surface === 'boards' ? 'chat' : 'boards')}
              >
                {activeProjTitle}
              </button>
              <span style={{ color: 'var(--text-muted)' }}>/</span>
              <span className="breadcrumb-title">{SURFACE_LABELS[surface]}</span>
            </div>

            <div className="topbar-right" ref={activityAnchorRef}>
              {status?.busy && (
                <span className="status-pill-quiet">
                  <span className="status-dot busy" />
                  <span>{status.active_action || 'Working...'}</span>
                </span>
              )}

              <button
                type="button"
                className={`icon-btn ${showActivity ? 'active' : ''}`}
                data-testid="top-activity-btn"
                onClick={() => setShowActivity((v) => !v)}
                title="Approvals, Background Tasks & Activity"
                aria-label="Workspace Activity"
              >
                <Inbox size={15} />
                {activityBadgeCount > 0 && (
                  <span className={`icon-badge-dot ${pendingApprovalsCount > 0 ? 'urgent' : ''}`}>
                    {activityBadgeCount}
                  </span>
                )}
              </button>

              {showActivity && (
                <ActivityPopover
                  status={status}
                  onClose={() => setShowActivity(false)}
                  onRefreshStatus={refreshStatusAndProjects}
                  onToast={showToast}
                  onSelectSurface={handleSelectSurface}
                  onInspect={(t) => setAuxTarget(t)}
                />
              )}

              <button
                type="button"
                className={`icon-btn ${auxTarget ? 'active' : ''}`}
                data-testid="toggle-aux-pane-btn"
                onClick={() =>
                  setAuxTarget((cur) =>
                    cur
                      ? null
                      : surface === 'boards' && selectedProjectId
                        ? { kind: 'project', projectId: selectedProjectId }
                        : { kind: 'approvals' }
                  )
                }
                title="Toggle Auxiliary Inspector Pane"
                aria-label="Toggle Auxiliary Pane"
              >
                <PanelRight size={15} />
              </button>
            </div>
          </header>

          {/* Active Workspace Surface */}
          {surface === 'chat' && (
            <ChatSurface
              status={status}
              projects={projects}
              selectedProjectId={selectedProjectId}
              onSelectProject={(pid) => syncProjectFocus(pid)}
              onInspect={(t) => setAuxTarget(t)}
              onRefreshStatus={refreshStatusAndProjects}
              onToast={showToast}
            />
          )}

          {surface === 'boards' && (
            <BoardGoalsSurface
              selectedProjectId={selectedProjectId}
              refreshNonce={dataRefreshNonce}
              selectedItemId={auxTarget?.kind === 'work_item' ? auxTarget.itemId : undefined}
              onSelectProject={(pid) => syncProjectFocus(pid)}
              onInspect={(t) => setAuxTarget(t)}
              onRefreshStatus={refreshStatusAndProjects}
              onToast={showToast}
            />
          )}

          {surface === 'calendar' && (
            <CalendarSurface
              onInspect={(t) => setAuxTarget(t)}
              onOpenStudy={() => {
                setKnowledgeInitialMode('study');
                handleSelectSurface('knowledge');
              }}
              onToast={showToast}
            />
          )}

          {surface === 'knowledge' && (
            <KnowledgeSurface
              initialNotePath={wikiInitialPath}
              openNonce={wikiOpenNonce}
              initialMode={knowledgeInitialMode}
              onRefreshStatus={refreshStatusAndProjects}
              onToast={showToast}
            />
          )}

          {surface === 'automations' && (
            <AutomationsSurface
              status={status}
              refreshNonce={dataRefreshNonce}
              onInspect={(t) => setAuxTarget(t)}
              onRefreshStatus={refreshStatusAndProjects}
              onToast={showToast}
            />
          )}

          {surface === 'customizations' && (
            <CustomizationsSurface
              refreshNonce={dataRefreshNonce}
              onInspect={(t) => setAuxTarget(t)}
              onToast={showToast}
            />
          )}
        </main>

        {/* Closed-by-default Right Split Auxiliary Inspector Pane (Mobile Bottom Sheet) */}
        <AuxSplitPane
          target={auxTarget}
          onClose={() => setAuxTarget(null)}
          onChanged={() => {
            setDataRefreshNonce((n) => n + 1);
            void refreshStatusAndProjects();
          }}
          onSelectProject={(pid) => syncProjectFocus(pid)}
          onToast={showToast}
          onJumpToWiki={(notePath) => {
            setWikiInitialPath(notePath);
            setWikiOpenNonce((n) => n + 1);
            setAuxTarget(null);
            handleSelectSurface('knowledge');
          }}
          onSelectSurface={handleSelectSurface}
        />
      </div>

      {authRequired && (
        <div className="modal-backdrop" data-testid="lan-token-modal">
          <div className="cmd-palette-modal" style={{ maxWidth: 420, padding: 20 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <KeyRound size={16} style={{ color: 'var(--accent-primary)' }} />
              <strong style={{ fontSize: 15 }}>LAN Access Token Required</strong>
            </div>
            <p style={{ margin: '0 0 12px', fontSize: 12.5, color: 'var(--text-secondary)' }}>
              Enter your <code className="mono">COMPANION_WEB_TOKEN</code> to pair this browser for 30 days.
            </p>
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                if (!lanTokenInput.trim()) return;
                const r = await pairLanToken(lanTokenInput);
                if (r.ok && !r.auth_required && r.csrf_token) {
                  setLanTokenInput('');
                  setAuthRequired(false);
                  await refreshStatusAndProjects();
                  showToast('LAN session paired.');
                } else {
                  showToast('Invalid COMPANION_WEB_TOKEN.');
                }
              }}
              style={{ display: 'flex', gap: 8 }}
            >
              <input
                type="password"
                className="input mono"
                data-testid="lan-token-input"
                placeholder="Paste COMPANION_WEB_TOKEN..."
                value={lanTokenInput}
                onChange={(e) => setLanTokenInput(e.target.value)}
                autoFocus
              />
              <button type="submit" className="btn btn-primary">
                Pair
              </button>
            </form>
          </div>
        </div>
      )}

      {/* Centered Two-Column Settings Modal */}
      {settingsOpen && (
        <SettingsModal
          initialSection={settingsSection}
          status={status}
          theme={theme}
          projects={projects}
          onApplyTheme={applyTheme}
          onSelectProject={(pid) => {
            syncProjectFocus(pid);
            setSettingsOpen(false);
            handleSelectSurface('boards');
            setAuxTarget({ kind: 'project', projectId: pid });
          }}
          onClose={() => setSettingsOpen(false)}
          onRefreshStatus={refreshStatusAndProjects}
          onToast={showToast}
        />
      )}

      {/* Command Palette (`Ctrl+K`) */}
      {searchOpen && (
        <CommandPaletteModal
          projects={projects}
          onClose={() => setSearchOpen(false)}
          onSelectSurface={handleSelectSurface}
          onSelectProject={handleSelectProject}
          onJumpToWiki={(notePath) => {
            setWikiInitialPath(notePath);
            setWikiOpenNonce((n) => n + 1);
            handleSelectSurface('knowledge');
          }}
          onOpenSettings={(sec) => {
            if (sec) setSettingsSection(sec);
            setSettingsOpen(true);
          }}
        />
      )}

      {toast && (
        <div className="toast-banner" role="status">
          {toast}
        </div>
      )}
    </div>
  );
};

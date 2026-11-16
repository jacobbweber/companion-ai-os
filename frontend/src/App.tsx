import React, { useEffect, useRef, useState } from 'react';
import { Inbox, PanelLeftOpen, PanelRight } from 'lucide-react';
import { callAction, fetchStatus } from './api';
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
  chat: 'Conversation',
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
  const [conversationNonce, setConversationNonce] = useState(0);
  const [dataRefreshNonce, setDataRefreshNonce] = useState(0);
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
  }, []);

  const refreshStatusAndProjects = async () => {
    try {
      const [st, bRes] = await Promise.all([
        fetchStatus(),
        callAction<{ boards?: ProjectGraphEntry[] }>('agile', { subaction: 'list_boards' }),
      ]);
      if (st.ok) {
        setStatus(st);
        if (!selectedProjectId && st.workspace?.project_id) {
          setSelectedProjectId(st.workspace.project_id);
        }
      }
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
          if (s.ok) setStatus(s);
        }).catch(() => {});
      }
    }, 5000);
    return () => window.clearInterval(timer);
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

  const handleSelectProject = (projectId: string, openBoard = false) => {
    setShowActivity(false);
    setSelectedProjectId(projectId);
    if (openBoard) {
      handleSelectSurface('boards');
    } else if (typeof window !== 'undefined' && window.innerWidth <= 768) {
      setRailCollapsed(true);
    }
  };

  const handleCreateProject = async (title: string) => {
    const res = await callAction<{ project?: ProjectGraphEntry }>('agile', {
      subaction: 'create_project',
      title,
      kind: 'project',
    });
    if (res.ok && res.project) {
      await refreshStatusAndProjects();
      setSelectedProjectId(res.project.project_id);
      handleSelectSurface('boards');
      showToast(`Created project "${res.project.title}".`);
    } else {
      showToast(res.error || 'Could not create project.');
    }
  };

  const activeProjTitle =
    projects.find((p) => p.project_id === selectedProjectId || p.slug === selectedProjectId)?.title ||
    status?.workspace?.project_id ||
    'Companion';

  const pendingApprovalsCount = status?.pending_approvals?.length || 0;
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
        onNewConversation={() => {
          setConversationNonce((n) => n + 1);
          handleSelectSurface('chat');
        }}
        onSelectSurface={handleSelectSurface}
        onSelectProject={handleSelectProject}
        onCreateProject={handleCreateProject}
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
              <span className="breadcrumb-project">{activeProjTitle}</span>
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
                  setAuxTarget((cur) => (cur ? null : { kind: 'approvals' }))
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
              conversationNonce={conversationNonce}
              onSelectProject={(pid) => setSelectedProjectId(pid)}
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
              onSelectProject={(pid) => setSelectedProjectId(pid)}
              onInspect={(t) => setAuxTarget(t)}
              onRefreshStatus={refreshStatusAndProjects}
              onToast={showToast}
            />
          )}

          {surface === 'calendar' && <CalendarSurface onToast={showToast} />}

          {surface === 'knowledge' && (
            <KnowledgeSurface
              initialNotePath={wikiInitialPath}
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
          onToast={showToast}
          onJumpToWiki={(notePath) => {
            setWikiInitialPath(notePath);
            setAuxTarget(null);
            handleSelectSurface('knowledge');
          }}
          onSelectSurface={handleSelectSurface}
        />
      </div>

      {/* Centered Two-Column Settings Modal */}
      {settingsOpen && (
        <SettingsModal
          initialSection={settingsSection}
          status={status}
          theme={theme}
          projects={projects}
          onApplyTheme={applyTheme}
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

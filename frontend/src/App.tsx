import React, { useEffect, useRef, useState } from 'react';
import {
  Bell,
  BookOpen,
  Calendar,
  Check,
  FolderKanban,
  MessageSquare,
  Moon,
  Palette,
  Rocket,
  Settings2,
  ShieldAlert,
  Sparkles,
  Sun,
  X,
} from 'lucide-react';
import { callAction, fetchStatus } from './api';
import { BoardsStudio } from './studios/BoardsStudio';
import { CalendarStudio } from './studios/CalendarStudio';
import { ChatStudio } from './studios/ChatStudio';
import { JobsApprovalsStudio } from './studios/JobsApprovalsStudio';
import { SystemStudio, type SystemSubTab } from './studios/SystemStudio';
import { WikiStudio } from './studios/WikiStudio';
import type { StudioId, ThemeId, WebStatus } from './types';

const THEMES: Array<{ id: ThemeId; label: string }> = [
  { id: 'obsidian', label: 'Obsidian Matte' },
  { id: 'orbital', label: 'Orbital Brutalist' },
  { id: 'graphite', label: 'Graphite Minimal' },
  { id: 'daylight', label: 'Daylight Paper' },
];

export const App: React.FC = () => {
  const [studio, setStudio] = useState<StudioId>('chat');
  const [theme, setTheme] = useState<ThemeId>(() => {
    const saved = window.localStorage.getItem('companion_theme') as ThemeId | null;
    return saved && THEMES.some((t) => t.id === saved) ? saved : 'obsidian';
  });
  const [lastDarkTheme, setLastDarkTheme] = useState<ThemeId>('obsidian');
  const [status, setStatus] = useState<WebStatus | null>(null);
  const [wikiTargetNote, setWikiTargetNote] = useState<string>('');
  const [systemTab, setSystemTab] = useState<SystemSubTab>('models');
  const [systemTabNonce, setSystemTabNonce] = useState<number>(0);
  const [toast, setToast] = useState<string>('');
  const [showNotifs, setShowNotifs] = useState<boolean>(false);
  const notifAnchorRef = useRef<HTMLDivElement | null>(null);

  const openSystemTab = (tab: SystemSubTab) => {
    setSystemTab(tab);
    setSystemTabNonce((n) => n + 1);
    setStudio('system');
  };

  const applyTheme = (next: ThemeId) => {
    setTheme(next);
    if (next !== 'daylight') setLastDarkTheme(next);
    document.documentElement.setAttribute('data-theme', next);
    window.localStorage.setItem('companion_theme', next);
  };

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, []);

  const refreshStatus = async () => {
    try {
      const s = await fetchStatus();
      if (s.ok) setStatus(s);
    } catch {
      // ignore transient poll error
    }
  };

  useEffect(() => {
    void refreshStatus();
    const timer = window.setInterval(() => void refreshStatus(), 4000);
    return () => window.clearInterval(timer);
  }, []);

  const showToast = (msg: string) => {
    setToast(msg);
    window.setTimeout(() => {
      setToast((cur) => (cur === msg ? '' : cur));
    }, 4200);
  };

  const openWikiNote = (notePath: string) => {
    setWikiTargetNote(notePath);
    setStudio('wiki');
  };

  const toggleLightDark = () => {
    applyTheme(theme === 'daylight' ? lastDarkTheme : 'daylight');
  };

  const onlinePartners = (status?.connected_partners || []).filter((p) => p.online);
  const dueStudyCount = status?.education_summary?.due_total || 0;
  const openBoardCards = status?.agile_summary?.open_cards || 0;
  const agingBoardCards = status?.agile_summary?.aging_count || 0;
  const pendingApprovals = status?.pending_approvals || [];
  const passiveNudges = (status?.session_nudges || []).filter((n) => n.category !== 'approval');
  const unseenHobbies = status?.unseen_hobbies_count || 0;
  const bellCount = pendingApprovals.length + passiveNudges.length + unseenHobbies;

  useEffect(() => {
    if (!showNotifs) return;
    const onPointerDown = (ev: MouseEvent) => {
      if (notifAnchorRef.current && !notifAnchorRef.current.contains(ev.target as Node)) {
        setShowNotifs(false);
      }
    };
    const onKeyDown = (ev: KeyboardEvent) => {
      if (ev.key === 'Escape') setShowNotifs(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [showNotifs]);

  const dismissOneNudge = async (nudgeId?: number) => {
    if (!nudgeId) return;
    await callAction('approvals', { subaction: 'dismiss_nudges', nudge_id: nudgeId });
    await refreshStatus();
  };

  const clearAllNotifications = async () => {
    const tasks: Array<Promise<unknown>> = [
      callAction('approvals', { subaction: 'dismiss_nudges' }),
    ];
    if (unseenHobbies > 0) {
      tasks.push(callAction('hobbies', { subaction: 'morning', mark_seen: true }));
    }
    await Promise.all(tasks);
    await refreshStatus();
    showToast('Cleared unread notifications.');
  };

  const resolveBellApproval = async (approvalId: string, decision: 'approve' | 'deny') => {
    const res = await callAction('approvals', {
      subaction: decision,
      approval_id: approvalId,
    });
    await refreshStatus();
    showToast(res.summary || res.error || `Marked ${approvalId} as ${decision}.`);
  };

  return (
    <div className="app-shell">
      {/* Top Tactical Telemetry Header */}
      <header className="top-bar">
        <div
          className="brand-cluster"
          role="button"
          tabIndex={0}
          style={{ cursor: 'pointer' }}
          title="Click to customize Companion Name, User Profile & Workspace"
          onClick={() => openSystemTab('identity')}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') openSystemTab('identity');
          }}
        >
          <div className="brand-mark mono">C3</div>
          <div>
            <div className="brand-title">
              {status?.companion_name || 'Companion AI OS'}{' '}
              <span className="mono" style={{ color: 'var(--accent-primary)', fontSize: 11 }}>
                v1.0
              </span>
            </div>
            <div className="brand-sub mono desktop-only">
              PROJECT: {status?.workspace?.project_id || 'companion-self'}
            </div>
          </div>
        </div>

        <div className="hud-strip">
          <span className="hud-chip">
            <span className={`status-dot ${status?.busy ? 'busy' : ''}`} />
            <span className="mono">
              {status?.busy
                ? `ACTIVE: ${status.active_action || 'WORKING'} (${status.busy_seconds}s)`
                : 'READY'}
            </span>
          </span>

          <button
            type="button"
            data-testid="hud-chip-selfdev"
            className={`hud-chip mono desktop-only ${status?.self_dev_enabled ? 'warn' : 'ok'}`}
            style={{ cursor: 'pointer' }}
            onClick={() => openSystemTab('autonomy')}
            title="Open Autonomy, Self-Dev Lock & Two-Track Hobbies"
          >
            {status?.self_dev_enabled ? 'SELF-DEV: ON' : 'SELF-DEV: OFF (RO)'}
          </button>

          <button
            type="button"
            data-testid="hud-chip-hobbies"
            className={`hud-chip mono desktop-only ${status?.hobby_mode === 'on' ? 'accent' : ''}`}
            style={{ cursor: 'pointer' }}
            onClick={() => openSystemTab('autonomy')}
            title="Open Autonomy, Self-Dev Lock & Two-Track Hobbies"
          >
            HOBBIES: {(status?.hobby_mode || 'off').toUpperCase()}
            {(status?.unseen_hobbies_count || 0) > 0
              ? ` • ${status?.unseen_hobbies_count} NEW`
              : ''}
          </button>

          <span className="hud-chip accent mono desktop-only">
            TRUST: {(status?.trust_mode || 'balanced').toUpperCase()}
          </span>

          <button
            type="button"
            className={`hud-chip mono desktop-only ${status?.privacy_mode === 'local_only' ? 'ok' : 'accent'}`}
            style={{ cursor: 'pointer' }}
            onClick={() => openSystemTab('models')}
            title="Open Models, Tiers & Outbound Privacy Lock"
          >
            PRIVACY: {(status?.privacy_mode || 'hybrid').toUpperCase()}
          </button>

          {onlinePartners.length > 0 && (
            <span className="hud-chip ok mono desktop-only">
              PARTNERS: {onlinePartners.length} ONLINE
            </span>
          )}

          {/* Compact Top-Bar Notification Bell (Mobile + Desktop) */}
          <div className="notif-anchor" ref={notifAnchorRef}>
            <button
              type="button"
              data-testid="top-notification-bell"
              aria-label="Notifications"
              className={`btn notif-btn ${
                pendingApprovals.length > 0
                  ? 'has-urgent'
                  : bellCount > 0
                    ? 'has-unread'
                    : ''
              }`}
              onClick={() => setShowNotifs((v) => !v)}
              title="Notifications, Approvals & Background Updates"
            >
              <Bell size={13} />
              {bellCount > 0 && <span className="notif-badge mono">{bellCount}</span>}
            </button>

            {showNotifs && (
              <div className="notif-popover" data-testid="notification-popover">
                <div className="notif-head">
                  <span>Notifications ({bellCount})</span>
                  {(passiveNudges.length > 0 || unseenHobbies > 0) && (
                    <button
                      type="button"
                      className="btn btn-ghost"
                      style={{ padding: '2px 7px', fontSize: 11 }}
                      onClick={() => void clearAllNotifications()}
                    >
                      Clear All
                    </button>
                  )}
                </div>

                {pendingApprovals.map((apr) => (
                  <div key={apr.approval_id} className="notif-item urgent">
                    <div className="notif-item-top">
                      <div>
                        <strong className="mono" style={{ color: 'var(--status-warn)' }}>
                          <ShieldAlert size={11} style={{ marginRight: 4, verticalAlign: '-1px' }} />
                          APPROVAL NEEDED
                        </strong>
                        <div style={{ marginTop: 2 }}>
                          {apr.summary || `${apr.category}:${apr.action_name}`}
                        </div>
                      </div>
                    </div>
                    <div className="notif-item-actions">
                      <button
                        type="button"
                        className="btn btn-primary"
                        style={{ padding: '3px 8px', fontSize: 11 }}
                        onClick={() => void resolveBellApproval(apr.approval_id, 'approve')}
                      >
                        <Check size={11} /> Approve
                      </button>
                      <button
                        type="button"
                        className="btn btn-danger"
                        style={{ padding: '3px 8px', fontSize: 11 }}
                        onClick={() => void resolveBellApproval(apr.approval_id, 'deny')}
                      >
                        <X size={11} /> Deny
                      </button>
                      <button
                        type="button"
                        className="btn"
                        style={{ padding: '3px 8px', fontSize: 11 }}
                        onClick={() => {
                          setStudio('jobs');
                          setShowNotifs(false);
                        }}
                      >
                        Review
                      </button>
                    </div>
                  </div>
                ))}

                {unseenHobbies > 0 && (
                  <div className="notif-item">
                    <div className="notif-item-top">
                      <div>
                        <strong className="mono" style={{ color: 'var(--accent-primary)' }}>
                          <Sparkles size={11} style={{ marginRight: 4, verticalAlign: '-1px' }} />
                          HOBBY JOURNAL
                        </strong>
                        <div style={{ marginTop: 2 }}>
                          {unseenHobbies} unseen hobby session{unseenHobbies === 1 ? '' : 's'} ready
                          in your `/morning` briefing.
                        </div>
                      </div>
                    </div>
                    <div className="notif-item-actions">
                      <button
                        type="button"
                        className="btn"
                        style={{ padding: '3px 8px', fontSize: 11 }}
                        onClick={() => {
                          openSystemTab('autonomy');
                          setShowNotifs(false);
                        }}
                      >
                        Open Briefing
                      </button>
                    </div>
                  </div>
                )}

                {passiveNudges.map((n, idx) => {
                  const rowId = n.id ?? n.nudge_id;
                  return (
                    <div key={rowId ?? idx} className="notif-item">
                      <div className="notif-item-top">
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-muted)' }}>
                            [{(n.category || 'update').toUpperCase()}]
                          </span>{' '}
                          {n.message}
                        </div>
                        {rowId && (
                          <button
                            type="button"
                            aria-label="Dismiss notification"
                            className="btn btn-ghost"
                            style={{ padding: '2px 5px' }}
                            onClick={() => void dismissOneNudge(rowId)}
                          >
                            <X size={11} />
                          </button>
                        )}
                      </div>
                      <div className="notif-item-actions">
                        <button
                          type="button"
                          className="btn btn-ghost"
                          style={{ padding: '2px 7px', fontSize: 11 }}
                          onClick={() => {
                            setStudio('jobs');
                            setShowNotifs(false);
                          }}
                        >
                          Departures
                        </button>
                      </div>
                    </div>
                  );
                })}

                {bellCount === 0 && (
                  <div
                    className="mono"
                    style={{
                      padding: '14px 6px',
                      textAlign: 'center',
                      fontSize: 11.5,
                      color: 'var(--text-muted)',
                    }}
                  >
                    All caught up — no unread updates.
                  </div>
                )}
              </div>
            )}
          </div>

          {/* 1-Click Light / Dark Toggle */}
          <button
            type="button"
            className="btn top-theme-toggle"
            style={{ padding: '4px 9px' }}
            onClick={toggleLightDark}
            title="Toggle Light / Dark Mode"
          >
            {theme === 'daylight' ? <Moon size={13} /> : <Sun size={13} />}
            <span className="desktop-only">{theme === 'daylight' ? 'Dark' : 'Light'}</span>
          </button>

          {/* 4-Preset Theme Changer */}
          <label className="hud-chip top-theme-select" style={{ padding: '2px 7px' }}>
            <Palette size={12} />
            <select
              aria-label="Select Studio Theme"
              className="select"
              style={{ border: 'none', padding: '2px 4px', background: 'transparent' }}
              value={theme}
              onChange={(e) => applyTheme(e.target.value as ThemeId)}
            >
              {THEMES.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </header>

      {/* Left Studio Command Rail (Vertical on Desktop, Single-Row Strip on Mobile) */}
      <aside className="nav-rail" aria-label="Studio Navigation">
        <div className="nav-group">
          <div className="nav-section-label">Command Studios</div>
          <div className="nav-items">
            <button
              type="button"
              className={`nav-btn ${studio === 'chat' ? 'active' : ''}`}
              onClick={() => setStudio('chat')}
            >
              <span className="nav-btn-left">
                <MessageSquare size={15} />
                <span>Forever Chat</span>
              </span>
              {dueStudyCount > 0 && <span className="nav-badge warn">{dueStudyCount}</span>}
            </button>

            <button
              type="button"
              className={`nav-btn ${studio === 'boards' ? 'active' : ''}`}
              onClick={() => setStudio('boards')}
            >
              <span className="nav-btn-left">
                <FolderKanban size={15} />
                <span>Boards</span>
              </span>
              {(agingBoardCards > 0 || openBoardCards > 0) && (
                <span className={`nav-badge ${agingBoardCards > 0 ? 'warn' : ''}`}>
                  {openBoardCards}
                </span>
              )}
            </button>

            <button
              type="button"
              className={`nav-btn ${studio === 'calendar' ? 'active' : ''}`}
              onClick={() => setStudio('calendar')}
            >
              <span className="nav-btn-left">
                <Calendar size={15} />
                <span>Calendar</span>
              </span>
            </button>

            <button
              type="button"
              className={`nav-btn ${studio === 'wiki' ? 'active' : ''}`}
              onClick={() => setStudio('wiki')}
            >
              <span className="nav-btn-left">
                <BookOpen size={15} />
                <span>Wiki &amp; Mind Map</span>
              </span>
            </button>

            <button
              type="button"
              className={`nav-btn ${studio === 'jobs' ? 'active' : ''}`}
              onClick={() => setStudio('jobs')}
            >
              <span className="nav-btn-left">
                <Rocket size={15} />
                <span>Jobs &amp; Routines</span>
              </span>
              {((status?.pending_approvals_count || 0) > 0 ||
                (status?.background_jobs_active || 0) > 0) && (
                <span
                  className={`nav-badge ${(status?.pending_approvals_count || 0) > 0 ? 'warn' : ''}`}
                >
                  {(status?.pending_approvals_count || 0) + (status?.background_jobs_active || 0)}
                </span>
              )}
            </button>

            <button
              type="button"
              className={`nav-btn ${studio === 'system' ? 'active' : ''}`}
              onClick={() => setStudio('system')}
            >
              <span className="nav-btn-left">
                <Settings2 size={15} />
                <span>System</span>
              </span>
              {(status?.connected_partners?.length || 0) > 0 && (
                <span className="nav-badge">{status?.connected_partners?.length}</span>
              )}
            </button>
          </div>
        </div>

        <div className="rail-footer">
          <div className="rail-kv">
            <span>FAST TIER</span>
            <span title={status?.fast_model}>{status?.fast_model || 'qwen2.5:7b'}</span>
          </div>
          <div className="rail-kv">
            <span>REASONING</span>
            <span title={status?.reasoning_model}>{status?.reasoning_model || 'qwen2.5:7b'}</span>
          </div>
          <div className="rail-kv">
            <span>ORDER COUNTER</span>
            <span>{status?.hosted_mcp_enabled ? 'ON' : 'OFF'}</span>
          </div>
          <div className="rail-kv">
            <span>LAN URL</span>
            <span title={status?.lan_url}>{status?.lan_url?.replace('http://', '') || 'local'}</span>
          </div>
        </div>
      </aside>

      {/* Active Studio Viewport */}
      <main className="studio-viewport">
        {studio === 'chat' && (
          <ChatStudio
            status={status}
            onRefreshStatus={() => void refreshStatus()}
            onOpenWikiNote={openWikiNote}
            onNavigateStudio={(target) => setStudio(target)}
            onOpenSystemTab={openSystemTab}
            onToast={showToast}
          />
        )}
        {studio === 'boards' && (
          <BoardsStudio
            onOpenWikiNote={openWikiNote}
            onRefreshStatus={() => void refreshStatus()}
            onToast={showToast}
          />
        )}
        {studio === 'calendar' && (
          <CalendarStudio
            onNavigateStudio={(target) => setStudio(target)}
            onToast={showToast}
          />
        )}
        {studio === 'wiki' && (
          <WikiStudio initialNotePath={wikiTargetNote} onToast={showToast} />
        )}
        {studio === 'jobs' && (
          <JobsApprovalsStudio
            onOpenWikiNote={openWikiNote}
            onRefreshStatus={() => void refreshStatus()}
            onToast={showToast}
          />
        )}
        {studio === 'system' && (
          <SystemStudio
            status={status}
            initialTab={systemTab}
            tabNonce={systemTabNonce}
            onRefreshStatus={() => void refreshStatus()}
            onToast={showToast}
          />
        )}
      </main>

      {toast && <div className="toast-banner mono">{toast}</div>}
    </div>
  );
};

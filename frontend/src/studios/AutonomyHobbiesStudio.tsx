import React, { useEffect, useState } from 'react';
import {
  BookOpenCheck,
  CheckCircle2,
  Compass,
  Lock,
  Play,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Unlock,
  Wrench,
} from 'lucide-react';
import { callAction } from '../api';
import type { HobbyCatalogItem, OperatingModesSnapshot } from '../types';

interface AutonomyHobbiesStudioProps {
  onRefreshStatus: () => void;
  onToast: (msg: string) => void;
}

const renderInlineCode = (text: string): React.ReactNode =>
  text.split(/(`[^`]+`)/g).map((seg, i) =>
    seg.startsWith('`') && seg.endsWith('`') && seg.length >= 2 ? (
      <code key={i} className="mono">
        {seg.slice(1, -1)}
      </code>
    ) : (
      <React.Fragment key={i}>{seg}</React.Fragment>
    )
  );

const QUADRANT_CARDS: Array<{
  code: 'Q1' | 'Q2' | 'Q3' | 'Q4';
  title: string;
  levers: string;
  posture: string;
  hobby: boolean;
  selfDev: boolean;
}> = [
  {
    code: 'Q1',
    title: 'Quiet Stable Daily Driver',
    levers: 'Hobbies OFF • Self-Dev OFF',
    posture: 'Core mechanically READ-ONLY; zero background hobbies; data/ & external projects writable.',
    hobby: false,
    selfDev: false,
  },
  {
    code: 'Q2',
    title: 'Daily Driver + Personal Hobbies',
    levers: 'Hobbies ON • Self-Dev OFF',
    posture: 'Personal/Ops hobbies garden Wiki, Memory, Telemetry & Morning Readiness; Core READ-ONLY.',
    hobby: true,
    selfDev: false,
  },
  {
    code: 'Q3',
    title: 'Deliberate Interactive Self-Dev',
    levers: 'Hobbies OFF • Self-Dev ON',
    posture: 'Companion edits its own engine only when explicitly asked; background idle loop stays OFF.',
    hobby: false,
    selfDev: true,
  },
  {
    code: 'Q4',
    title: 'Full Autonomous Lab / VM Mode',
    levers: 'Hobbies ON • Self-Dev ON',
    posture: 'Both Personal and Self-Dev engineering tracks eligible during idle windows with Doctor rollback.',
    hobby: true,
    selfDev: true,
  },
];

export const AutonomyHobbiesStudio: React.FC<AutonomyHobbiesStudioProps> = ({
  onRefreshStatus,
  onToast,
}) => {
  const [snap, setSnap] = useState<OperatingModesSnapshot | null>(null);
  const [busyLever, setBusyLever] = useState<string>('');
  const [busyHobby, setBusyHobby] = useState<string>('');

  const loadSnapshot = async () => {
    const res = await callAction<OperatingModesSnapshot>('hobbies', { subaction: 'status' });
    if (res.ok) {
      setSnap(res as unknown as OperatingModesSnapshot);
    }
  };

  useEffect(() => {
    void loadSnapshot();
  }, []);

  const setHobbyLever = async (enabled: boolean) => {
    setBusyLever('hobby');
    const res = await callAction<OperatingModesSnapshot>('hobbies', {
      subaction: 'set_hobby',
      enabled,
    });
    setBusyLever('');
    if (res.ok) {
      setSnap(res as unknown as OperatingModesSnapshot);
      onRefreshStatus();
      onToast(res.summary || `Hobby Mode ${enabled ? 'ON' : 'OFF'}.`);
    } else {
      onToast(res.error || 'Could not update Hobby Mode.');
    }
  };

  const setSelfDevLever = async (enabled: boolean) => {
    setBusyLever('self_dev');
    const res = await callAction<OperatingModesSnapshot>('hobbies', {
      subaction: 'set_self_dev',
      enabled,
    });
    setBusyLever('');
    if (res.ok) {
      setSnap(res as unknown as OperatingModesSnapshot);
      onRefreshStatus();
      onToast(res.summary || `Self-Dev Mode ${enabled ? 'ON' : 'OFF'}.`);
    } else {
      if ((res as unknown as OperatingModesSnapshot).quadrant) {
        setSnap(res as unknown as OperatingModesSnapshot);
      }
      onToast(res.error || 'Self-Dev Mode is locked by deployment configuration.');
    }
  };

  const runHobbyOnce = async (hobbyName: string) => {
    setBusyHobby(hobbyName);
    const res = await callAction<OperatingModesSnapshot>('hobbies', {
      subaction: 'run',
      hobby_name: hobbyName,
    });
    setBusyHobby('');
    if ((res as unknown as OperatingModesSnapshot).quadrant) {
      setSnap(res as unknown as OperatingModesSnapshot);
    } else {
      void loadSnapshot();
    }
    onRefreshStatus();
    if (res.ok) {
      onToast(res.summary || `Finished hobby ${hobbyName}.`);
    } else {
      onToast(res.error || res.summary || `Hobby ${hobbyName} did not complete.`);
    }
  };

  const markBriefingSeen = async () => {
    const res = await callAction<OperatingModesSnapshot>('hobbies', {
      subaction: 'morning',
      mark_seen: true,
    });
    if (res.ok) {
      setSnap(res as unknown as OperatingModesSnapshot);
      onRefreshStatus();
      onToast('Morning Briefing refreshed and marked seen.');
    }
  };

  const personalCatalog = (snap?.catalog || []).filter((c) => c.track === 'personal');
  const selfDevCatalog = (snap?.catalog || []).filter((c) => c.track === 'self_dev');
  const activeCode = snap?.quadrant?.code || 'Q1';

  const renderHobbyCard = (h: HobbyCatalogItem) => (
    <div
      key={h.name}
      data-testid={`hobby-card-${h.name}`}
      style={{
        padding: '10px 12px',
        border: `1px solid ${h.is_next ? 'var(--accent-primary)' : 'var(--border-hairline)'}`,
        background: 'var(--bg-elevated)',
        marginBottom: 8,
        opacity: h.eligible ? 1 : 0.72,
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: 8,
          marginBottom: 4,
        }}
      >
        <div>
          <div style={{ fontWeight: 600, fontSize: 12.5 }}>{h.title}</div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {h.name} • {h.discipline}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', alignItems: 'center' }}>
          <span className="badge mono">TIER:{h.preferred_tier.toUpperCase()}</span>
          {h.is_next && <span className="badge accent mono">NEXT UP</span>}
          {h.eligible ? (
            <span className="badge ok mono">ACTIVE</span>
          ) : (
            <span className="badge warn mono">LOCKED</span>
          )}
        </div>
      </div>

      <p style={{ margin: '6px 0 8px', fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.45 }}>
        {renderInlineCode(h.description)}
      </p>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
        <span className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          {h.eligible ? 'Checkpointed + System Doctor gated' : h.locked_reason}
        </span>
        <button
          type="button"
          data-testid={`run-hobby-${h.name}`}
          className="btn"
          disabled={!h.eligible || busyHobby !== ''}
          title={h.eligible ? `Run ${h.name} once now` : h.locked_reason}
          onClick={() => void runHobbyOnce(h.name)}
        >
          {busyHobby === h.name ? (
            <>
              <RefreshCw size={12} /> Running...
            </>
          ) : h.eligible ? (
            <>
              <Play size={12} /> Run Once
            </>
          ) : (
            <>
              <Lock size={12} /> Locked
            </>
          )}
        </button>
      </div>
    </div>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="studio-header">
        <div className="studio-title-group">
          <h1>Autonomy, Self-Dev Lock &amp; Two-Track Hobbies</h1>
          <p>
            Independent two-lever operating architecture (Hobby Mode × Self-Dev Mode) with mechanical
            read-only core lock and checkpointed idle hobbies
          </p>
        </div>
        <div className="studio-actions">
          <button type="button" className="btn" onClick={() => void markBriefingSeen()}>
            <BookOpenCheck size={13} /> Mark Briefing Seen
            {(snap?.unseen_count || 0) > 0 ? ` (${snap?.unseen_count})` : ''}
          </button>
          <button type="button" className="btn" onClick={() => void loadSnapshot()}>
            <RefreshCw size={13} /> Refresh
          </button>
        </div>
      </div>

      <div className="studio-scroll">
        {/* Active Operating Quadrant Matrix */}
        <div className="panel" data-testid="operating-quadrant-banner">
          <div className="panel-head">
            <span className="panel-title">
              <Compass size={13} /> Active Operating Posture:{' '}
              <strong className="mono" style={{ color: 'var(--accent-primary)' }}>
                {snap?.quadrant?.code || 'Q1'} — {snap?.quadrant?.title || 'Quiet Stable Daily Driver'}
              </strong>
            </span>
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              {snap?.quadrant?.core_read_only !== false ? (
                <span className="badge ok mono">
                  <Lock size={11} /> COMPANION CORE: READ-ONLY
                </span>
              ) : (
                <span className="badge warn mono">
                  <Unlock size={11} /> COMPANION CORE: SELF-DEV UNLOCKED
                </span>
              )}
              {snap?.self_dev_locked && (
                <span className="badge warn mono">PINNED BY DEPLOYMENT ENV</span>
              )}
            </div>
          </div>

          <p style={{ margin: '0 0 10px', fontSize: 12.5, color: 'var(--text-secondary)' }}>
            {renderInlineCode(
              snap?.quadrant?.summary ||
                'Hobbies OFF + Self-Dev OFF — Companion core is mechanically locked READ-ONLY; `data/` & external `/project` workspaces remain writable.'
            )}
          </p>

          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))',
              gap: 8,
            }}
          >
            {QUADRANT_CARDS.map((q) => {
              const isActive = activeCode === q.code;
              return (
                <div
                  key={q.code}
                  data-testid={`quadrant-card-${q.code}`}
                  style={{
                    padding: '9px 11px',
                    border: `1px solid ${
                      isActive ? 'var(--accent-primary)' : 'var(--border-hairline)'
                    }`,
                    background: isActive ? 'var(--bg-surface)' : 'var(--bg-elevated)',
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      marginBottom: 3,
                    }}
                  >
                    <strong className="mono" style={{ fontSize: 12 }}>
                      {q.code}: {q.title}
                    </strong>
                    {isActive && <span className="badge accent mono">ACTIVE</span>}
                  </div>
                  <div className="mono" style={{ fontSize: 10.5, color: 'var(--accent-primary)' }}>
                    {q.levers}
                  </div>
                  <div
                    style={{
                      fontSize: 11.5,
                      color: 'var(--text-muted)',
                      marginTop: 4,
                      lineHeight: 1.4,
                    }}
                  >
                    {q.posture}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Two Independent Lever Controls */}
        <div className="grid-2">
          {/* Lever 1: Hobby Mode */}
          <div className="panel" data-testid="lever-card-hobby">
            <div className="panel-head">
              <span className="panel-title">
                <Sparkles size={13} /> Lever 1: Hobby Mode (Background Idle Loop)
              </span>
              <span className={`badge mono ${snap?.hobby_enabled ? 'accent' : ''}`}>
                {snap?.hobby_enabled ? 'HOBBIES: ON' : 'HOBBIES: OFF'}
              </span>
            </div>

            <p style={{ margin: '0 0 10px', fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.45 }}>
              Controls whether Companion runs bounded, checkpointed background hobbies when idle on
              either CLI or the LAN Web UI. Preempts immediately when you send a message.
            </p>

            <div style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
              <button
                type="button"
                data-testid="toggle-hobby-off"
                className={`btn ${!snap?.hobby_enabled ? 'btn-primary' : ''}`}
                disabled={busyLever === 'hobby'}
                onClick={() => void setHobbyLever(false)}
              >
                Hobbies OFF (Quiet)
              </button>
              <button
                type="button"
                data-testid="toggle-hobby-on"
                className={`btn ${snap?.hobby_enabled ? 'btn-primary' : ''}`}
                disabled={busyLever === 'hobby'}
                onClick={() => void setHobbyLever(true)}
              >
                Hobbies ON (Idle Loop Active)
              </button>
            </div>

            <div className="grid-3">
              <div className="hud-chip">
                <span>IDLE TRIGGER:</span>
                <strong className="mono">{snap?.idle_minutes ?? 10}m</strong>
              </div>
              <div className="hud-chip">
                <span>COOLDOWN:</span>
                <strong className="mono">{snap?.cooldown_minutes ?? 30}m</strong>
              </div>
              <div className="hud-chip">
                <span>NEXT UP:</span>
                <strong className="mono">{snap?.next_hobby || 'wiki_gardener'}</strong>
              </div>
            </div>
          </div>

          {/* Lever 2: Self-Dev Mode */}
          <div className="panel" data-testid="lever-card-selfdev">
            <div className="panel-head">
              <span className="panel-title">
                <ShieldCheck size={13} /> Lever 2: Self-Dev Mode (Human-Only Privilege Gate)
              </span>
              <span className={`badge mono ${snap?.self_dev_enabled ? 'warn' : 'ok'}`}>
                {snap?.self_dev_enabled ? 'SELF-DEV: ON' : 'SELF-DEV: OFF (CORE RO)'}
              </span>
            </div>

            <p style={{ margin: '0 0 10px', fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.45 }}>
              When <strong>OFF</strong>, Companion&apos;s own source tree (
              <code className="mono">companion/</code>, <code className="mono">surfaces/</code>,{' '}
              <code className="mono">frontend/</code>, <code className="mono">specs/</code>,{' '}
              <code className="mono">skills/</code>, <code className="mono">tests/</code>) is
              mechanically locked READ-ONLY while <code className="mono">data/</code> and external{' '}
              <code className="mono">/project</code> workspaces remain 100% writable.
            </p>

            <div style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
              <button
                type="button"
                data-testid="toggle-selfdev-off"
                className={`btn ${!snap?.self_dev_enabled ? 'btn-primary' : ''}`}
                disabled={Boolean(snap?.self_dev_locked) || busyLever === 'self_dev'}
                onClick={() => void setSelfDevLever(false)}
              >
                <Lock size={12} /> Self-Dev OFF (Stable Core Lock)
              </button>
              <button
                type="button"
                data-testid="toggle-selfdev-on"
                className={`btn ${snap?.self_dev_enabled ? 'btn-primary' : ''}`}
                disabled={Boolean(snap?.self_dev_locked) || busyLever === 'self_dev'}
                onClick={() => void setSelfDevLever(true)}
              >
                <Unlock size={12} /> Self-Dev ON (Lab / Self-Evolution)
              </button>
            </div>

            {snap?.self_dev_locked ? (
              <div
                data-testid="selfdev-env-lock-notice"
                className="mono"
                style={{
                  padding: '7px 10px',
                  border: '1px solid var(--accent-warn, var(--border-hairline))',
                  background: 'var(--code-bg)',
                  fontSize: 11,
                }}
              >
                <ShieldAlert size={12} style={{ verticalAlign: 'text-bottom', marginRight: 5 }} />
                DEPLOYMENT LOCK ACTIVE: {snap.self_dev_lock_reason}
              </div>
            ) : (
              <div className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                PRIVILEGE BOUNDARY: Only human surfaces (<code className="mono">/selfdev</code>, Web
                UI, <code className="mono">.env</code>) can toggle this lever — the LLM agent can
                never enable it via tools or shell/DB writes.
              </div>
            )}
          </div>
        </div>

        {/* Two-Track Hobby Catalog */}
        <div className="grid-2">
          <div className="panel" data-testid="hobby-track-personal">
            <div className="panel-head">
              <span className="panel-title">
                <CheckCircle2 size={13} /> Personal, Knowledge &amp; Ops Track ({personalCatalog.length})
              </span>
              <span className="badge ok mono">SAFE WHEN SELF-DEV OFF</span>
            </div>
            <p style={{ margin: '0 0 10px', fontSize: 11.5, color: 'var(--text-muted)' }}>
              Operates exclusively on <code className="mono">data/</code> (Knowledge Wiki, Memory,
              Telemetry &amp; Study/Boards/Calendar readiness) — never modifies Companion code.
            </p>
            {personalCatalog.map(renderHobbyCard)}
          </div>

          <div className="panel" data-testid="hobby-track-selfdev">
            <div className="panel-head">
              <span className="panel-title">
                <Wrench size={13} /> Self-Dev Engineering Track ({selfDevCatalog.length})
              </span>
              {snap?.self_dev_enabled ? (
                <span className="badge warn mono">UNLOCKED (SELF-DEV ON)</span>
              ) : (
                <span className="badge mono">LOCKED (REQUIRES SELF-DEV ON)</span>
              )}
            </div>
            <p style={{ margin: '0 0 10px', fontSize: 11.5, color: 'var(--text-muted)' }}>
              Simplifies dead code, implements Engineering Backlog items, and strengthens contract
              unit tests inside checkpointed sandboxes with automatic 7/7 System Doctor rollback.
            </p>
            {selfDevCatalog.map(renderHobbyCard)}
          </div>
        </div>

        {/* Morning Briefing & Recent Hobby Journal */}
        <div className="grid-2">
          <div className="panel">
            <div className="panel-head">
              <span className="panel-title">
                <BookOpenCheck size={13} /> Morning Briefing (`/morning`)
              </span>
              {(snap?.unseen_count || 0) > 0 && (
                <span className="badge accent mono">{snap?.unseen_count} UNSEEN</span>
              )}
            </div>
            <pre
              className="mono"
              style={{
                margin: 0,
                padding: 10,
                background: 'var(--code-bg)',
                border: '1px solid var(--border-hairline)',
                fontSize: 11.5,
                whiteSpace: 'pre-wrap',
                maxHeight: 240,
                overflowY: 'auto',
              }}
            >
              {snap?.morning_briefing || 'Loading Morning Briefing...'}
            </pre>
          </div>

          <div className="panel">
            <div className="panel-head">
              <span className="panel-title">
                <Sparkles size={13} /> Recent Hobby Journal ({snap?.journal?.length || 0})
              </span>
            </div>
            {(snap?.journal || []).length === 0 ? (
              <div style={{ fontSize: 12, color: 'var(--text-muted)', padding: '6px 0' }}>
                No autonomous hobby runs recorded yet. Click <strong>Run Once</strong> on any
                Personal hobby or enable <strong>Hobby Mode</strong> to run during idle windows.
              </div>
            ) : (
              (snap?.journal || []).map((j) => (
                <div
                  key={j.id}
                  style={{
                    padding: '8px 10px',
                    border: '1px solid var(--border-hairline)',
                    background: 'var(--bg-elevated)',
                    marginBottom: 6,
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      gap: 6,
                      marginBottom: 3,
                    }}
                  >
                    <strong className="mono" style={{ fontSize: 11.5 }}>
                      #{j.id} {j.hobby_name}
                    </strong>
                    <div style={{ display: 'flex', gap: 5 }}>
                      <span className={`badge mono ${j.doctor_passed ? 'ok' : 'warn'}`}>
                        {j.status.toUpperCase()}
                      </span>
                      <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-muted)' }}>
                        {j.created_at}
                      </span>
                    </div>
                  </div>
                  <div style={{ fontSize: 11.5, color: 'var(--text-secondary)', lineHeight: 1.4 }}>
                    {j.summary}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

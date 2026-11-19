import React, { useEffect, useState } from 'react';
import {
  ChevronDown,
  ChevronRight,
  Clock,
  Play,
  Plus,
  SlidersHorizontal,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { callAction } from '../api';
import type {
  AutonomyJournalEntry,
  AutonomySnapshot,
  AuxInspectorTarget,
  RoutineItem,
  WebStatus,
} from '../types';

interface AutomationsSurfaceProps {
  status: WebStatus | null;
  refreshNonce: number;
  onInspect: (target: AuxInspectorTarget) => void;
  onRefreshStatus: () => Promise<void>;
  onToast: (msg: string) => void;
}

export const AutomationsSurface: React.FC<AutomationsSurfaceProps> = ({
  status,
  refreshNonce,
  onInspect,
  onRefreshStatus,
  onToast,
}) => {
  const [routines, setRoutines] = useState<RoutineItem[]>([]);
  const [journal, setJournal] = useState<AutonomyJournalEntry[]>([]);
  const [modeFilter, setModeFilter] = useState<'all' | 'on_idle' | 'scheduled' | 'manual'>('all');
  const [showBriefing, setShowBriefing] = useState(true);

  const loadAll = async () => {
    const [aRes, rRes] = await Promise.all([
      callAction<AutonomySnapshot>('autonomy', { subaction: 'status' }),
      callAction<{ routines?: RoutineItem[] }>('routines', { subaction: 'list' }),
    ]);
    if (aRes.journal) setJournal(aRes.journal);
    if (rRes.routines) setRoutines(rRes.routines);
    else if (aRes.routines) setRoutines(aRes.routines);
  };

  useEffect(() => {
    void loadAll();
  }, [refreshNonce]);

  const bgAutonomy = status?.background_autonomy === 'on';

  const toggleBg = async () => {
    const res = await callAction('autonomy', {
      subaction: 'set_autonomy',
      enabled: !bgAutonomy,
    });
    await onRefreshStatus();
    await loadAll();
    onToast(res.summary || `Background Autonomy ${!bgAutonomy ? 'ON' : 'OFF'}.`);
  };

  const toggleRoutineEnabled = async (rt: RoutineItem) => {
    const res = await callAction('routines', {
      subaction: rt.enabled ? 'disable' : 'enable',
      routine_id: rt.routine_id,
    });
    await loadAll();
    onToast(res.summary || `${rt.title} ${rt.enabled ? 'disabled' : 'enabled'}.`);
  };

  const runRoutineNow = async (rt: RoutineItem) => {
    const res = await callAction('routines', {
      subaction: 'run_now',
      routine_id: rt.routine_id,
    });
    await loadAll();
    await onRefreshStatus();
    onToast(res.summary || res.error || `Triggered ${rt.title}.`);
  };

  const deleteRoutine = async (routineId: string) => {
    await callAction('routines', { subaction: 'delete', routine_id: routineId });
    await loadAll();
    onToast(`Removed routine ${routineId}.`);
  };

  const filtered = routines.filter((r) =>
    modeFilter === 'all' ? true : (r.trigger_policy?.mode || 'manual') === modeFilter
  );

  return (
    <div className="surface-scroll" data-testid="automations-surface">
      <div className="surface-container" style={{ maxWidth: 920 }}>
        {/* Header */}
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            gap: 12,
            flexWrap: 'wrap',
            marginBottom: 16,
          }}
        >
          <div>
            <h1 style={{ margin: 0, fontSize: 19, fontWeight: 600 }}>Automations</h1>
            <p style={{ margin: '3px 0 0', fontSize: 12.5, color: 'var(--text-secondary)' }}>
              Unified Manual, Scheduled, and Time-Windowed On-Idle routines.
            </p>
          </div>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="new-routine-btn"
            onClick={() => onInspect({ kind: 'routine', createNew: true })}
          >
            <Plus size={14} /> New Routine
          </button>
        </div>

        {/* Operating Posture Bar */}
        <div className="card-group">
          <div className="card-row">
            <div className="card-row-info">
              <div className="card-row-title">Background Autonomy</div>
              <div className="card-row-desc">
                Allow eligible On-Idle routines and ready project goal slices to run during their configured time slots.
              </div>
            </div>
            <button
              type="button"
              data-testid="automations-bg-switch"
              className={`switch-toggle ${bgAutonomy ? 'on' : ''}`}
              onClick={() => void toggleBg()}
              aria-label="Toggle Background Autonomy"
            />
          </div>
        </div>

        {/* Filter Bar */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
          <div className="segmented-pills">
            {(
              [
                ['all', 'All Routines'],
                ['on_idle', 'On Idle'],
                ['scheduled', 'Scheduled'],
                ['manual', 'Manual'],
              ] as const
            ).map(([m, lbl]) => (
              <button
                key={m}
                type="button"
                className={`segmented-pill ${modeFilter === m ? 'active' : ''}`}
                onClick={() => setModeFilter(m)}
              >
                {lbl}
              </button>
            ))}
          </div>
          <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{filtered.length} routine(s)</span>
        </div>

        {/* Unified Routines List */}
        <div className="card-group">
          {filtered.length === 0 ? (
            <div className="card-row">
              <span style={{ color: 'var(--text-muted)' }}>No routines in this filter.</span>
            </div>
          ) : (
            filtered.map((rt) => {
              const tp = rt.trigger_policy || { mode: 'manual' };
              const modeBadge =
                tp.mode === 'on_idle'
                  ? `On Idle${tp.allowed_window ? ` • ${tp.allowed_window}` : ' • Anytime'}`
                  : tp.mode === 'scheduled'
                    ? `Scheduled • ${tp.schedule_expr || rt.schedule_expr}`
                    : 'Manual';
              return (
                <div key={rt.routine_id} className="card-row" data-testid={`routine-row-${rt.routine_id}`}>
                  <div className="card-row-info">
                    <div className="card-row-title" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span>{rt.title}</span>
                      <span className={`badge ${tp.mode === 'on_idle' ? 'accent' : ''}`}>
                        <Clock size={10} /> {modeBadge}
                      </span>
                    </div>
                    <div className="card-row-desc">
                      Target: <span className="mono">{rt.target_kind}</span>
                      {rt.target_name ? ` (${rt.target_name})` : ''} • Status: {rt.last_status || 'idle'}
                    </div>
                  </div>
                  <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                    <button
                      type="button"
                      className="btn btn-sm"
                      title="Run routine immediately"
                      onClick={() => void runRoutineNow(rt)}
                    >
                      <Play size={12} /> Run
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      title="Configure TriggerPolicy & Window"
                      onClick={() => onInspect({ kind: 'routine', routineId: rt.routine_id })}
                    >
                      <SlidersHorizontal size={13} />
                    </button>
                    <button
                      type="button"
                      className={`switch-toggle ${rt.enabled ? 'on' : ''}`}
                      onClick={() => void toggleRoutineEnabled(rt)}
                      aria-label={`Toggle ${rt.title}`}
                    />
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => void deleteRoutine(rt.routine_id)}
                      title="Delete routine"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Collapsible Morning Autonomy Briefing */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 8 }}>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => setShowBriefing((v) => !v)}
          >
            {showBriefing ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            <Sparkles size={13} /> Morning Autonomy Briefing ({journal.length})
          </button>
          {(status?.unseen_autonomy_count || 0) > 0 && (
            <button
              type="button"
              className="btn btn-sm"
              onClick={async () => {
                await callAction('autonomy', { subaction: 'morning', mark_seen: true });
                await onRefreshStatus();
                await loadAll();
                onToast('Marked Morning Autonomy Briefing as read.');
              }}
            >
              Mark Briefing Read ({status?.unseen_autonomy_count})
            </button>
          )}
        </div>

        {showBriefing && (
          <div className="card-group">
            {journal.length === 0 ? (
              <div className="card-row">
                <span style={{ color: 'var(--text-muted)' }}>
                  No background routine or goal-slice runs recorded yet.
                </span>
              </div>
            ) : (
              journal.map((j) => (
                <div key={j.id} className="card-row">
                  <div className="card-row-info">
                    <div className="card-row-title">
                      {j.entry_name || j.target_id || j.entry_kind}{' '}
                      <span className={`badge ${j.status === 'done' || j.status === 'completed' ? 'ok' : 'warn'}`}>
                        {j.status}
                      </span>
                    </div>
                    <div className="card-row-desc">{j.summary}</div>
                  </div>
                  <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{j.created_at}</span>
                </div>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  );
};

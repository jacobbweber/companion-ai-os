import React, { useEffect, useState } from 'react';
import {
  Activity,
  ArchiveRestore,
  Clock,
  FlaskConical,
  Play,
  RefreshCw,
  RotateCw,
  ShieldCheck,
  Trash2,
} from 'lucide-react';
import { apiPost, callAction, fetchTelemetry, reloadCompanionService } from '../api';
import type {
  RoutineItem,
  SandboxClone,
  SnapshotBackup,
  TelemetryPayload,
  WebStatus,
} from '../types';

interface OpsStudioProps {
  status: WebStatus | null;
  onOpenAutonomy?: () => void;
  onToast: (msg: string) => void;
}

export const OpsStudio: React.FC<OpsStudioProps> = ({ status, onOpenAutonomy, onToast }) => {
  const [routines, setRoutines] = useState<RoutineItem[]>([]);
  const [backups, setBackups] = useState<SnapshotBackup[]>([]);
  const [sandboxes, setSandboxes] = useState<SandboxClone[]>([]);
  const [telemetry, setTelemetry] = useState<TelemetryPayload | null>(null);
  const [doctorReport, setDoctorReport] = useState<string>('');
  const [sandboxOutput, setSandboxOutput] = useState<string>('');
  const [runningDoctor, setRunningDoctor] = useState(false);
  const [reloading, setReloading] = useState(false);

  // Routine form
  const [rtId, setRtId] = useState('');
  const [rtSchedule, setRtSchedule] = useState('every 6h');
  const [rtKind, setRtKind] = useState('snapshot_backup');
  const [snapLabel, setSnapLabel] = useState('manual');
  const [sbxLabel, setSbxLabel] = useState('experiment');

  const loadAll = async () => {
    const [rRes, bRes, sRes, tRes] = await Promise.all([
      callAction<{ routines?: RoutineItem[] }>('routines', { subaction: 'list' }),
      callAction<{ snapshots?: SnapshotBackup[] }>('backup', { subaction: 'list' }),
      callAction<{ sandboxes?: SandboxClone[] }>('sandbox', { subaction: 'list' }),
      fetchTelemetry(),
    ]);
    if (rRes.routines) setRoutines(rRes.routines);
    if (bRes.snapshots) setBackups(bRes.snapshots);
    if (sRes.sandboxes) setSandboxes(sRes.sandboxes);
    if (tRes.ok) setTelemetry(tRes);
  };

  useEffect(() => {
    void loadAll();
  }, []);

  const createRoutine = async () => {
    if (!rtId.trim()) return;
    const res = await callAction('routines', {
      subaction: 'create',
      routine_id: rtId.trim(),
      title: rtId.trim(),
      schedule_expr: rtSchedule.trim(),
      target_kind: rtKind,
      enabled: true,
      catch_up: false,
    });
    if (res.ok) {
      onToast(res.summary || 'Routine saved.');
      setRtId('');
      void loadAll();
    } else {
      onToast(res.error || 'Could not save routine.');
    }
  };

  const runLiveDoctor = async (withTests: boolean) => {
    setRunningDoctor(true);
    const res = await apiPost<{ healthy?: boolean; summary?: string }>('/api/doctor', {
      run_tests: withTests,
    });
    setRunningDoctor(false);
    setDoctorReport(res.summary || JSON.stringify(res, null, 2));
    onToast(res.healthy ? 'System Doctor: HEALTHY (PASS)' : 'System Doctor reported issues.');
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="studio-header">
        <div className="studio-title-group">
          <h1>Operations: Routines, Backups, Sandboxes &amp; Doctor</h1>
          <p>
            Hardware-gated scheduler (zero startup thundering herd), secret-free snapshot backups,
            isolated self-dev clones, and System Doctor
          </p>
        </div>
        <div className="studio-actions">
          <button
            type="button"
            className="btn"
            disabled={reloading}
            title="Restart Companion Python service & reload UI with latest code"
            onClick={async () => {
              setReloading(true);
              onToast('Reloading Companion backend & refreshing Studio...');
              const r = await reloadCompanionService();
              if (!r.ok) {
                setReloading(false);
                onToast(r.error || 'Could not reload service.');
                return;
              }
              window.setTimeout(() => window.location.reload(), 250);
            }}
          >
            <RotateCw size={13} /> {reloading ? 'Reloading...' : 'Reload Service'}
          </button>
          <button
            type="button"
            className="btn"
            disabled={runningDoctor}
            onClick={() => void runLiveDoctor(false)}
          >
            <ShieldCheck size={13} /> Quick Doctor (Stages 1–6)
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={runningDoctor}
            onClick={() => void runLiveDoctor(true)}
          >
            <ShieldCheck size={13} /> {runningDoctor ? 'Running...' : 'Full 7/7 Doctor'}
          </button>
          <button type="button" className="btn" onClick={() => void loadAll()}>
            <RefreshCw size={13} />
          </button>
        </div>
      </div>

      <div className="studio-scroll">
        {doctorReport && (
          <div className="panel">
            <div className="panel-head">
              <span className="panel-title">System Doctor Diagnostic Report</span>
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
              }}
            >
              {doctorReport}
            </pre>
          </div>
        )}

        <div className="grid-2">
          {/* Respectful Hardware-Gated Routines */}
          <div className="panel">
            <div className="panel-head">
              <span className="panel-title">
                <Clock size={13} /> Respectful Hardware-Gated Routines ({routines.length})
              </span>
              <button
                type="button"
                className="btn"
                onClick={async () => {
                  const r = await callAction('routines', { subaction: 'tick' });
                  onToast(r.summary || 'Checked due routines.');
                  void loadAll();
                }}
              >
                <Play size={12} /> Tick Due
              </button>
            </div>
            <div className="grid-3" style={{ marginBottom: 8 }}>
              <input
                className="input"
                placeholder="ID (e.g. nightly-backup)"
                value={rtId}
                onChange={(e) => setRtId(e.target.value)}
              />
              <input
                className="input"
                placeholder="every 6h | daily 09:00"
                value={rtSchedule}
                onChange={(e) => setRtSchedule(e.target.value)}
              />
              <select
                aria-label="Routine target kind"
                className="select"
                value={rtKind}
                onChange={(e) => setRtKind(e.target.value)}
              >
                <option value="snapshot_backup">snapshot_backup</option>
                <option value="wiki_inbox_triage">wiki_inbox_triage</option>
                <option value="wiki_deep_curate">wiki_deep_curate</option>
                <option value="endpoint_pulse">endpoint_pulse</option>
                <option value="chat_archive">chat_archive</option>
              </select>
            </div>
            <button
              type="button"
              className="btn btn-primary"
              style={{ marginBottom: 10 }}
              disabled={!rtId.trim()}
              onClick={() => void createRoutine()}
            >
              Schedule Routine
            </button>

            {routines.map((r) => (
              <div
                key={r.routine_id}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  padding: '8px 10px',
                  border: '1px solid var(--border-hairline)',
                  background: 'var(--bg-elevated)',
                  marginBottom: 6,
                }}
              >
                <div>
                  <strong className="mono">{r.routine_id}</strong>{' '}
                  <span className="badge accent">{r.schedule_expr}</span>{' '}
                  <span className="badge">{r.target_kind}</span>
                  <div className="mono" style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                    Next: {r.next_run_at} • Missed: {r.missed_count}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn btn-danger"
                  onClick={async () => {
                    await callAction('routines', { subaction: 'delete', routine_id: r.routine_id });
                    void loadAll();
                  }}
                >
                  <Trash2 size={12} />
                </button>
              </div>
            ))}
          </div>

          {/* Secret-Free Snapshot Backups */}
          <div className="panel">
            <div className="panel-head">
              <span className="panel-title">
                <ArchiveRestore size={13} /> Secret-Free Snapshot Backups ({backups.length})
              </span>
              <span className="badge ok">SECRETS.ENV EXCLUDED</span>
            </div>
            <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
              <input
                className="input"
                style={{ flex: 1 }}
                placeholder="Snapshot label (e.g. pre_upgrade)"
                value={snapLabel}
                onChange={(e) => setSnapLabel(e.target.value)}
              />
              <button
                type="button"
                className="btn btn-primary"
                onClick={async () => {
                  const r = await callAction('backup', {
                    subaction: 'create',
                    label: snapLabel.trim() || 'manual',
                  });
                  onToast(r.summary || 'Snapshot created.');
                  void loadAll();
                }}
              >
                Create Snapshot
              </button>
            </div>

            {backups.map((b) => (
              <div
                key={b.snapshot_id}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  padding: '8px 10px',
                  border: '1px solid var(--border-hairline)',
                  background: 'var(--bg-elevated)',
                  marginBottom: 6,
                }}
              >
                <div>
                  <code className="mono" style={{ fontSize: 11.5 }}>
                    {b.snapshot_id}
                  </code>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                    Wiki files: {b.wiki_files} • Declared Vault refs:{' '}
                    {b.declared_secrets?.length || 0}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn"
                  onClick={async () => {
                    const r = await callAction('backup', {
                      subaction: 'restore',
                      snapshot_id: b.snapshot_id,
                    });
                    onToast(r.summary || 'Restored snapshot.');
                  }}
                >
                  Restore
                </button>
              </div>
            ))}
          </div>

          {/* Clone-Based Self-Dev Sandboxes */}
          <div className="panel" data-testid="ops-sandboxes-panel">
            <div className="panel-head">
              <span className="panel-title">
                <FlaskConical size={13} /> Isolated Self-Development Clone Sandboxes ({sandboxes.length})
              </span>
              {status?.self_dev_enabled ? (
                <span className="badge warn mono">SELF-DEV UNLOCKED</span>
              ) : (
                <span className="badge mono">LOCKED (SELF-DEV OFF)</span>
              )}
            </div>
            {!status?.self_dev_enabled && (
              <div
                style={{
                  fontSize: 11.5,
                  color: 'var(--text-muted)',
                  marginBottom: 8,
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  gap: 8,
                }}
              >
                <span>
                  Companion core is locked read-only while <strong>Self-Dev Mode</strong> is OFF.
                </span>
                {onOpenAutonomy && (
                  <button type="button" className="btn" onClick={onOpenAutonomy}>
                    Autonomy &amp; Hobbies
                  </button>
                )}
              </div>
            )}
            <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
              <input
                className="input"
                style={{ flex: 1 }}
                placeholder="Clone label (e.g. ui_refactor)"
                value={sbxLabel}
                onChange={(e) => setSbxLabel(e.target.value)}
              />
              <button
                type="button"
                className="btn btn-primary"
                disabled={!status?.self_dev_enabled}
                title={
                  status?.self_dev_enabled
                    ? 'Create isolated self-dev clone sandbox'
                    : 'Enable Self-Dev Mode in Autonomy & Hobbies first'
                }
                onClick={async () => {
                  const r = await callAction('sandbox', {
                    subaction: 'create',
                    label: sbxLabel.trim() || 'experiment',
                  });
                  onToast(r.ok ? r.summary || 'Created sandbox clone.' : r.error || 'Sandbox locked.');
                  void loadAll();
                }}
              >
                Clone Workspace
              </button>
            </div>

            {sandboxes.map((s) => (
              <div
                key={s.sandbox_id}
                style={{
                  padding: '8px 10px',
                  border: '1px solid var(--border-hairline)',
                  background: 'var(--bg-elevated)',
                  marginBottom: 6,
                }}
              >
                <div className="mono" style={{ fontSize: 11.5 }}>
                  {s.sandbox_id} ({s.copied_files} files)
                </div>
                <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                  <button
                    type="button"
                    className="btn"
                    onClick={async () => {
                      const r = await callAction<{ diff?: string; changed_files?: string[] }>(
                        'sandbox',
                        { subaction: 'diff', sandbox_id: s.sandbox_id }
                      );
                      setSandboxOutput(
                        r.diff || `Changed files: ${(r.changed_files || []).join(', ') || 'none'}`
                      );
                    }}
                  >
                    View Diff
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={async () => {
                      const r = await callAction<{ doctor?: { summary?: string } }>('sandbox', {
                        subaction: 'verify',
                        sandbox_id: s.sandbox_id,
                        run_tests: false,
                      });
                      setSandboxOutput(r.doctor?.summary || r.summary || 'Verified.');
                      onToast(r.summary || 'Sandbox Doctor finished.');
                    }}
                  >
                    Verify Doctor
                  </button>
                  <button
                    type="button"
                    className="btn btn-danger"
                    onClick={async () => {
                      await callAction('sandbox', {
                        subaction: 'cleanup',
                        sandbox_id: s.sandbox_id,
                      });
                      void loadAll();
                    }}
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              </div>
            ))}

            {sandboxOutput && (
              <pre
                className="mono"
                style={{
                  marginTop: 8,
                  padding: 8,
                  background: 'var(--code-bg)',
                  border: '1px solid var(--border-hairline)',
                  fontSize: 11,
                  maxHeight: 160,
                  overflowY: 'auto',
                  whiteSpace: 'pre-wrap',
                }}
              >
                {sandboxOutput}
              </pre>
            )}
          </div>

          {/* Live Waterfall Telemetry & Git Savepoints */}
          <div className="panel">
            <div className="panel-head">
              <span className="panel-title">
                <Activity size={13} /> Telemetry KPIs &amp; Git Savepoints
              </span>
            </div>
            <div className="grid-3" style={{ marginBottom: 10 }}>
              <div className="hud-chip">
                <span>TRACES:</span>
                <strong className="mono">{telemetry?.kpis?.total_traces ?? 0}</strong>
              </div>
              <div className="hud-chip">
                <span>LLM CALLS:</span>
                <strong className="mono">{telemetry?.kpis?.llm_calls ?? 0}</strong>
              </div>
              <div className="hud-chip">
                <span>TOOL CALLS:</span>
                <strong className="mono">{telemetry?.kpis?.tool_calls ?? 0}</strong>
              </div>
            </div>
            <div className="mono" style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>
              RECENT GIT SAVEPOINTS
            </div>
            {(status?.recent_savepoints || []).slice(0, 4).map((sp, i) => (
              <div
                key={i}
                className="mono"
                style={{
                  fontSize: 11.5,
                  padding: '4px 0',
                  borderBottom: '1px solid var(--border-hairline)',
                }}
              >
                {typeof sp === 'string' ? sp : `${sp.hash || ''} ${sp.subject || ''}`}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

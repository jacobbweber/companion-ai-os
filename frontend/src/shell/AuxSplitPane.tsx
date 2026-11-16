import React, { useEffect, useState } from 'react';
import { Check, ExternalLink, Play, X } from 'lucide-react';
import { callAction } from '../api';
import { MarkdownView } from '../components/MarkdownView';
import { WorkItemInspector } from '../surfaces/WorkItemInspector';
import type {
  ApprovalRequest,
  AuxInspectorTarget,
  BackgroundJob,
  RoutineItem,
  SurfaceId,
  VerifiedDagSummary,
  WikiNoteDetail,
} from '../types';

interface AuxSplitPaneProps {
  target: AuxInspectorTarget;
  onClose: () => void;
  onChanged: () => void;
  onToast: (msg: string) => void;
  onJumpToWiki: (notePath: string) => void;
  onSelectSurface: (s: SurfaceId) => void;
}

export const AuxSplitPane: React.FC<AuxSplitPaneProps> = ({
  target,
  onClose,
  onChanged,
  onToast,
  onJumpToWiki,
}) => {
  // Routine inspector state
  const [rtId, setRtId] = useState('');
  const [rtTitle, setRtTitle] = useState('');
  const [rtMode, setRtMode] = useState('on_idle');
  const [rtSched, setRtSched] = useState('daily 08:00');
  const [rtWin, setRtWin] = useState('22:00-06:00');
  const [rtIdleSec, setRtIdleSec] = useState(900);
  const [rtCoolSec, setRtCoolSec] = useState(3600);
  const [rtPrecheck, setRtPrecheck] = useState('');
  const [rtSelfDev, setRtSelfDev] = useState(false);
  const [rtKind, setRtKind] = useState('snapshot_backup');
  const [rtTargetName, setRtTargetName] = useState('');

  // Skill / DAG / Wiki / Jobs state
  const [skillFiles, setSkillFiles] = useState<string[]>([]);
  const [activeAsset, setActiveAsset] = useState('SKILL.md');
  const [skillContent, setSkillContent] = useState('');
  const [dagMeta, setDagMeta] = useState<VerifiedDagSummary | null>(null);
  const [dagParamsText, setDagParamsText] = useState('{}');
  const [dagOutput, setDagOutput] = useState('');
  const [wikiDetail, setWikiDetail] = useState<WikiNoteDetail | null>(null);
  const [jobs, setJobs] = useState<BackgroundJob[]>([]);
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);

  useEffect(() => {
    if (!target) return;
    if (target.kind === 'routine') {
      if (target.createNew || !target.routineId) {
        setRtId('');
        setRtTitle('');
        setRtMode('on_idle');
        setRtWin('22:00-06:00');
        setRtIdleSec(900);
        setRtCoolSec(3600);
        setRtPrecheck('');
        setRtSelfDev(false);
        setRtKind('snapshot_backup');
        setRtTargetName('');
      } else {
        void callAction<{ routines?: RoutineItem[] }>('routines', { subaction: 'list' }).then((res) => {
          const found = (res.routines || []).find((r) => r.routine_id === target.routineId);
          if (found) {
            setRtId(found.routine_id);
            setRtTitle(found.title);
            setRtMode(found.trigger_policy?.mode || 'manual');
            setRtSched(found.trigger_policy?.schedule_expr || found.schedule_expr || 'daily 08:00');
            setRtWin(found.trigger_policy?.allowed_window || '');
            setRtIdleSec(found.trigger_policy?.idle_min_seconds ?? 900);
            setRtCoolSec(found.trigger_policy?.cooldown_seconds ?? 3600);
            setRtPrecheck(found.trigger_policy?.precheck_name || '');
            setRtSelfDev(Boolean(found.trigger_policy?.requires_self_dev));
            setRtKind(found.target_kind);
            setRtTargetName(found.target_name || '');
          }
        });
      }
    } else if (target.kind === 'skill') {
      const asset = target.asset || 'SKILL.md';
      setActiveAsset(asset);
      void callAction<{ files?: string[]; content?: string }>('customizations', {
        subaction: 'read_skill',
        skill: target.skillName,
        asset,
      }).then((res) => {
        if (res.files) setSkillFiles(res.files);
        setSkillContent(res.content || '');
      });
    } else if (target.kind === 'dag') {
      setDagOutput('');
      void callAction<{ dags?: VerifiedDagSummary[] }>('customizations', { subaction: 'list' }).then(
        (res) => {
          const found = (res.dags || []).find((d) => d.name === target.dagName) || null;
          setDagMeta(found);
        }
      );
    } else if (target.kind === 'wiki_note') {
      void callAction<WikiNoteDetail>('wiki', { subaction: 'read', path: target.notePath }).then((res) => {
        if (res.ok) setWikiDetail(res as unknown as WikiNoteDetail);
      });
    } else if (target.kind === 'job' || target.kind === 'approvals') {
      void Promise.all([
        callAction<{ jobs?: BackgroundJob[] }>('jobs', { subaction: 'list' }),
        callAction<{ approvals?: ApprovalRequest[] }>('approvals', { subaction: 'list', status: 'waiting' }),
      ]).then(([jRes, aRes]) => {
        if (jRes.jobs) setJobs(jRes.jobs);
        if (aRes.approvals) setApprovals(aRes.approvals);
      });
    }
  }, [target]);

  if (!target) return null;

  const headerTitle =
    target.kind === 'work_item'
      ? 'Work Item Inspector'
      : target.kind === 'project'
        ? 'Goal & Idle Policy'
        : target.kind === 'routine'
          ? target.createNew
            ? 'New Routine'
            : `Routine: ${rtId}`
          : target.kind === 'skill'
            ? `Pack: ${target.skillName}`
            : target.kind === 'dag'
              ? `DAG: ${target.dagName}`
              : target.kind === 'wiki_note'
                ? wikiDetail?.title || target.notePath
                : 'Tasks & Approvals';

  const saveRoutine = async () => {
    const slug = (rtId || rtTitle).trim();
    if (!slug) return;
    const res = await callAction('routines', {
      subaction: 'create',
      routine_id: slug,
      title: rtTitle.trim() || slug,
      mode: rtMode,
      schedule_expr: rtMode === 'scheduled' ? rtSched.trim() : '',
      allowed_window: rtMode === 'on_idle' ? rtWin.trim() : '',
      idle_min_seconds: rtIdleSec,
      cooldown_seconds: rtCoolSec,
      precheck_name: rtPrecheck,
      requires_self_dev: rtSelfDev,
      target_kind: rtKind,
      target_name: rtTargetName.trim(),
      enabled: true,
      confirmed: true,
    });
    if (res.ok) {
      onChanged();
      onClose();
      onToast(res.summary || 'Saved routine.');
    } else {
      onToast(res.error || 'Could not save routine.');
    }
  };

  return (
    <aside className="agy-aux-pane" data-testid="aux-split-pane" aria-label="Inspector Pane">
      <div className="sheet-drag-handle" />
      <div className="aux-pane-header">
        <span className="aux-pane-title">{headerTitle}</span>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close inspector">
          <X size={15} />
        </button>
      </div>

      <div className="aux-pane-body">
        {(target.kind === 'work_item' || target.kind === 'project') && (
          <WorkItemInspector
            mode={target.kind}
            itemId={target.kind === 'work_item' ? target.itemId : undefined}
            projectId={target.projectId}
            onSaved={onChanged}
            onClose={onClose}
            onToast={onToast}
          />
        )}

        {target.kind === 'routine' && (
          <div style={{ display: 'grid', gap: 12 }} data-testid="routine-inspector">
            <div>
              <label className="field-label">Routine ID</label>
              <input className="input mono" placeholder="overnight-backup" value={rtId} onChange={(e) => setRtId(e.target.value)} />
            </div>
            <div>
              <label className="field-label">Display Title</label>
              <input className="input" placeholder="Overnight Snapshot Backup" value={rtTitle} onChange={(e) => setRtTitle(e.target.value)} />
            </div>
            <div>
              <label className="field-label">Trigger Mode</label>
              <select className="select" value={rtMode} onChange={(e) => setRtMode(e.target.value)}>
                <option value="on_idle">On Idle (Respectful Idle + Time Window)</option>
                <option value="scheduled">Scheduled (Interval / Daily / Cron)</option>
                <option value="manual">Manual Only</option>
              </select>
            </div>

            {rtMode === 'scheduled' && (
              <div>
                <label className="field-label">Schedule Expression (e.g. every 6h, daily 08:00, or cron)</label>
                <input className="input mono" value={rtSched} onChange={(e) => setRtSched(e.target.value)} />
              </div>
            )}

            {rtMode === 'on_idle' && (
              <>
                <div>
                  <label className="field-label">Allowed Time Window (e.g. 22:00-06:00 or blank for anytime)</label>
                  <input className="input mono" placeholder="22:00-06:00" value={rtWin} onChange={(e) => setRtWin(e.target.value)} />
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                  <div>
                    <label className="field-label">Min Idle (sec)</label>
                    <input type="number" className="input" value={rtIdleSec} onChange={(e) => setRtIdleSec(Number(e.target.value))} />
                  </div>
                  <div>
                    <label className="field-label">Cooldown (sec)</label>
                    <input type="number" className="input" value={rtCoolSec} onChange={(e) => setRtCoolSec(Number(e.target.value))} />
                  </div>
                </div>
                <div>
                  <label className="field-label">Eligibility Precheck (Optional)</label>
                  <select className="select" value={rtPrecheck} onChange={(e) => setRtPrecheck(e.target.value)}>
                    <option value="">None (Always eligible when idle)</option>
                    <option value="wiki_inbox_or_stale">wiki_inbox_or_stale</option>
                    <option value="recent_unconsolidated_chat">recent_unconsolidated_chat</option>
                    <option value="recent_warn_or_slow_spans">recent_warn_or_slow_spans</option>
                    <option value="workspace_hygiene_or_sdd_drift">workspace_hygiene_or_sdd_drift</option>
                    <option value="active_goal_has_ready_items">active_goal_has_ready_items</option>
                  </select>
                </div>
              </>
            )}

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <div>
                <label className="field-label">Verified Target Kind</label>
                <select className="select" value={rtKind} onChange={(e) => setRtKind(e.target.value)}>
                  <option value="snapshot_backup">snapshot_backup</option>
                  <option value="dag">dag (Python DAGSpec)</option>
                  <option value="wiki_inbox_triage">wiki_inbox_triage</option>
                  <option value="wiki_deep_curate">wiki_deep_curate</option>
                  <option value="maintenance_agent">maintenance_agent</option>
                  <option value="endpoint_pulse">endpoint_pulse</option>
                  <option value="chat_archive">chat_archive</option>
                </select>
              </div>
              <div>
                <label className="field-label">Target Name (DAG slug)</label>
                <input className="input mono" placeholder="e.g. release-check" value={rtTargetName} onChange={(e) => setRtTargetName(e.target.value)} />
              </div>
            </div>

            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5 }}>
              <input type="checkbox" checked={rtSelfDev} onChange={(e) => setRtSelfDev(e.target.checked)} />
              <span>Requires Self-Dev Mode = ON</span>
            </label>

            <button type="button" className="btn btn-primary" onClick={() => void saveRoutine()}>
              Save Routine
            </button>
          </div>
        )}

        {target.kind === 'skill' && (
          <div>
            <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', marginBottom: 12 }}>
              {skillFiles.map((f) => (
                <button
                  key={f}
                  type="button"
                  className={`btn btn-sm ${activeAsset === f ? 'btn-primary' : ''}`}
                  onClick={async () => {
                    setActiveAsset(f);
                    const res = await callAction<{ content?: string }>('customizations', {
                      subaction: 'read_skill',
                      skill: target.skillName,
                      asset: f,
                    });
                    setSkillContent(res.content || '');
                  }}
                >
                  {f}
                </button>
              ))}
            </div>
            {activeAsset.endsWith('.md') ? (
              <div className="md-body">
                <MarkdownView content={skillContent} />
              </div>
            ) : (
              <pre
                className="mono"
                style={{
                  padding: 12,
                  borderRadius: 'var(--radius-sm)',
                  background: 'var(--code-bg)',
                  border: '1px solid var(--border-hairline)',
                  fontSize: 11.5,
                  overflowX: 'auto',
                }}
              >
                {skillContent}
              </pre>
            )}
          </div>
        )}

        {target.kind === 'dag' && (
          <div style={{ display: 'grid', gap: 12 }}>
            <div style={{ fontSize: 12.5, color: 'var(--text-secondary)' }}>
              {dagMeta?.description}
            </div>
            <div>
              <label className="field-label">
                JSON Input Params (`{JSON.stringify(dagMeta?.input_schema || {})}`)
              </label>
              <textarea
                className="textarea mono"
                rows={4}
                value={dagParamsText}
                onChange={(e) => setDagParamsText(e.target.value)}
              />
            </div>
            <button
              type="button"
              className="btn btn-primary"
              onClick={async () => {
                const res = await callAction('customizations', {
                  subaction: 'run_dag',
                  dag: target.dagName,
                  params: dagParamsText,
                });
                setDagOutput(res.summary || res.error || JSON.stringify(res, null, 2));
                onChanged();
              }}
            >
              <Play size={13} /> Execute DAG
            </button>
            {dagOutput && (
              <pre
                className="mono"
                style={{
                  padding: 12,
                  borderRadius: 'var(--radius-sm)',
                  background: 'var(--code-bg)',
                  border: '1px solid var(--border-hairline)',
                  fontSize: 11.5,
                  whiteSpace: 'pre-wrap',
                }}
              >
                {dagOutput}
              </pre>
            )}
          </div>
        )}

        {target.kind === 'wiki_note' && (
          <div>
            <div style={{ marginBottom: 10, display: 'flex', justifyContent: 'flex-end' }}>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => onJumpToWiki(target.notePath)}
              >
                <ExternalLink size={12} /> Open in Knowledge
              </button>
            </div>
            {wikiDetail ? (
              <div className="md-body">
                <MarkdownView
                  content={wikiDetail.body || wikiDetail.raw}
                  onOpenWikiNote={(p) => onJumpToWiki(p)}
                />
              </div>
            ) : (
              <div style={{ color: 'var(--text-muted)' }}>Loading note...</div>
            )}
          </div>
        )}

        {(target.kind === 'job' || target.kind === 'approvals') && (
          <div style={{ display: 'grid', gap: 14 }}>
            <div>
              <div className="field-label">Pending Approvals ({approvals.length})</div>
              <div className="card-group">
                {approvals.length === 0 ? (
                  <div className="card-row"><span style={{ color: 'var(--text-muted)' }}>All caught up.</span></div>
                ) : (
                  approvals.map((apr) => (
                    <div key={apr.approval_id} className="card-row">
                      <div className="card-row-info">
                        <div className="card-row-title">{apr.category}</div>
                        <div className="card-row-desc">{apr.summary}</div>
                      </div>
                      <div style={{ display: 'inline-flex', gap: 6 }}>
                        <button
                          type="button"
                          className="btn btn-primary btn-sm"
                          onClick={async () => {
                            await callAction('approvals', { subaction: 'approve', approval_id: apr.approval_id });
                            setApprovals((prev) => prev.filter((x) => x.approval_id !== apr.approval_id));
                            onChanged();
                          }}
                        >
                          <Check size={11} /> Approve
                        </button>
                        <button
                          type="button"
                          className="btn btn-danger btn-sm"
                          onClick={async () => {
                            await callAction('approvals', { subaction: 'deny', approval_id: apr.approval_id });
                            setApprovals((prev) => prev.filter((x) => x.approval_id !== apr.approval_id));
                            onChanged();
                          }}
                        >
                          Deny
                        </button>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>

            <div>
              <div className="field-label">Departures Board ({jobs.length})</div>
              <div className="card-group">
                {jobs.length === 0 ? (
                  <div className="card-row"><span style={{ color: 'var(--text-muted)' }}>No background jobs yet.</span></div>
                ) : (
                  jobs.map((j) => (
                    <div key={j.job_id} className="card-row">
                      <div className="card-row-info">
                        <div className="card-row-title">{j.title || j.goal}</div>
                        <div className="card-row-desc">{j.envelope_summary || j.status}</div>
                      </div>
                      <span className={`badge ${j.status === 'done' ? 'ok' : ''}`}>{j.status}</span>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </aside>
  );
};

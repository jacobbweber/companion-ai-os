import React, { useEffect, useState } from 'react';
import { Check, ExternalLink, Eye, Play, Plus, RotateCcw, X } from 'lucide-react';
import { callAction } from '../api';
import { DiffView } from '../components/DiffView';
import { MarkdownView } from '../components/MarkdownView';
import { WorkItemInspector } from '../surfaces/WorkItemInspector';
import type {
  ApprovalRequest,
  AuxInspectorTarget,
  BackgroundJob,
  DagNodeRunEntry,
  DagRunSummary,
  RoutineItem,
  SurfaceId,
  VerifiedDagSummary,
  WikiDiffPayload,
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

function formatApprovalPayloadPreview(apr: ApprovalRequest): string {
  try {
    const p = apr.payload || (apr.payload_json ? JSON.parse(apr.payload_json) : null);
    if (!p || typeof p !== 'object') return `${apr.action_name} (${apr.target})`;
    const rec = p as Record<string, unknown>;
    if (typeof rec.command === 'string' && rec.command) {
      return `# Target: ${rec.endpoint_id || rec.tool || apr.target}\n${rec.command}`;
    }
    return JSON.stringify(rec, null, 2);
  } catch {
    return apr.payload_json || `${apr.action_name} (${apr.target})`;
  }
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
  const [rtKind, setRtKind] = useState('snapshot_backup');
  const [rtTargetName, setRtTargetName] = useState('');

  // Skill / DAG / Wiki / Jobs / Approvals state
  const [skillFiles, setSkillFiles] = useState<string[]>([]);
  const [activeAsset, setActiveAsset] = useState('SKILL.md');
  const [skillContent, setSkillContent] = useState('');
  const [dagMeta, setDagMeta] = useState<VerifiedDagSummary | null>(null);
  const [dagParamsText, setDagParamsText] = useState('{}');
  const [dagOutput, setDagOutput] = useState('');
  const [dagRunDetail, setDagRunDetail] = useState<(DagRunSummary & { node_runs?: DagNodeRunEntry[] }) | null>(null);
  const [wikiDetail, setWikiDetail] = useState<WikiNoteDetail | null>(null);
  const [wikiMissing, setWikiMissing] = useState(false);
  const [jobs, setJobs] = useState<BackgroundJob[]>([]);
  const [selectedJob, setSelectedJob] = useState<BackgroundJob | null>(null);
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  const [resolvingAprId, setResolvingAprId] = useState('');
  const [previewAprId, setPreviewAprId] = useState('');
  const [previewWikiDiff, setPreviewWikiDiff] = useState<WikiDiffPayload | null>(null);

  useEffect(() => {
    if (!target) return;
    setPreviewAprId('');
    setPreviewWikiDiff(null);
    if (target.kind === 'routine') {
      if (target.createNew || !target.routineId) {
        setRtId('');
        setRtTitle('');
        setRtMode('on_idle');
        setRtWin('22:00-06:00');
        setRtIdleSec(900);
        setRtCoolSec(3600);
        setRtPrecheck('');
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
    } else if (target.kind === 'dag_run') {
      setDagRunDetail(null);
      void callAction<{
        dag_run?: DagRunSummary & { node_runs?: DagNodeRunEntry[] };
        run?: DagRunSummary & { node_runs?: DagNodeRunEntry[] };
        node_runs?: DagNodeRunEntry[];
      }>('customizations', {
        subaction: 'get_dag_run',
        run_id: target.runId,
      }).then((res) => {
        const rec = res.dag_run || res.run;
        if (rec) {
          setDagRunDetail({ ...rec, node_runs: rec.node_runs || res.node_runs || [] });
        }
      });
    } else if (target.kind === 'wiki_note') {
      setWikiDetail(null);
      setWikiMissing(false);
      void callAction<WikiNoteDetail>('wiki', { subaction: 'read', path: target.notePath }).then((res) => {
        if (res.ok) {
          setWikiDetail(res as unknown as WikiNoteDetail);
          setWikiMissing(false);
        } else {
          setWikiMissing(true);
        }
      });
    } else if (target.kind === 'job' || target.kind === 'approvals') {
      setSelectedJob(null);
      void Promise.all([
        callAction<{ jobs?: BackgroundJob[] }>('jobs', { subaction: 'list' }),
        callAction<{ approvals?: ApprovalRequest[] }>('approvals', { subaction: 'list', status: 'waiting' }),
        target.kind === 'job'
          ? callAction<{ job?: BackgroundJob }>('jobs', { subaction: 'get', job_id: target.jobId })
          : Promise.resolve({ job: undefined }),
      ]).then(([jRes, aRes, oneJob]) => {
        if (jRes.jobs) setJobs(jRes.jobs);
        if (aRes.approvals) setApprovals(aRes.approvals);
        if (oneJob.job) setSelectedJob(oneJob.job);
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
              : target.kind === 'dag_run'
                ? `DAG Run: ${dagRunDetail?.dag_name || target.runId}`
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

  const toggleApprovalPreview = async (apr: ApprovalRequest) => {
    if (previewAprId === apr.approval_id) {
      setPreviewAprId('');
      setPreviewWikiDiff(null);
      return;
    }
    setPreviewAprId(apr.approval_id);
    setPreviewWikiDiff(null);
    if (apr.category === 'wiki_edit') {
      const dRes = await callAction<WikiDiffPayload>('wiki', {
        subaction: 'diff',
        approval_id: apr.approval_id,
      });
      if (dRes.ok) setPreviewWikiDiff(dRes as unknown as WikiDiffPayload);
    }
  };

  const handleResolveApproval = async (apr: ApprovalRequest, decision: 'approve' | 'deny') => {
    if (resolvingAprId) return;
    setResolvingAprId(apr.approval_id);
    try {
      const r = await callAction('approvals', {
        subaction: decision,
        approval_id: apr.approval_id,
      });
      if (r.ok === false) {
        onToast(r.error || `Approval ${decision} failed.`);
      } else {
        setApprovals((prev) => prev.filter((x) => x.approval_id !== apr.approval_id));
        onToast(r.summary || `Approval ${decision}d.`);
      }
      onChanged();
    } finally {
      setResolvingAprId('');
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
                <div className="form-grid-2">
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
                    const res = await callAction<{ content?: string }>('customizations', { subaction: 'read_skill', skill: target.skillName, asset: f });
                    setSkillContent(res.content || '');
                  }}
                >
                  {f}
                </button>
              ))}
            </div>
            {activeAsset.endsWith('.md') ? (
              <div className="md-body"><MarkdownView content={skillContent} /></div>
            ) : (
              <pre className="mono" style={{ padding: 12, borderRadius: 'var(--radius-sm)', background: 'var(--code-bg)', border: '1px solid var(--border-hairline)', fontSize: 11.5, overflowX: 'auto' }}>
                {skillContent}
              </pre>
            )}
          </div>
        )}

        {target.kind === 'dag' && (
          <div style={{ display: 'grid', gap: 12 }}>
            <div style={{ fontSize: 12.5, color: 'var(--text-secondary)' }}>{dagMeta?.description}</div>
            <div>
              <label className="field-label">JSON Input Params ({JSON.stringify(dagMeta?.input_schema || {})})</label>
              <textarea className="textarea mono" rows={4} value={dagParamsText} onChange={(e) => setDagParamsText(e.target.value)} />
            </div>
            <button
              type="button"
              className="btn btn-primary"
              onClick={async () => {
                let parsedParams: Record<string, unknown> = {};
                try {
                  const p = JSON.parse(dagParamsText.trim() || '{}');
                  if (!p || typeof p !== 'object' || Array.isArray(p)) {
                    onToast('DAG params must be a JSON object (e.g. {}).');
                    return;
                  }
                  parsedParams = p as Record<string, unknown>;
                } catch (err) {
                  onToast(`Invalid JSON in DAG params: ${err instanceof Error ? err.message : 'syntax error'}`);
                  return;
                }
                const res = await callAction('customizations', { subaction: 'run_dag', dag: target.dagName, params: parsedParams });
                setDagOutput(res.summary || res.error || JSON.stringify(res, null, 2));
                onChanged();
              }}
            >
              <Play size={13} /> Execute DAG
            </button>
            {dagOutput && (
              <pre className="mono" style={{ padding: 12, borderRadius: 'var(--radius-sm)', background: 'var(--code-bg)', border: '1px solid var(--border-hairline)', fontSize: 11.5, whiteSpace: 'pre-wrap' }}>
                {dagOutput}
              </pre>
            )}
          </div>
        )}

        {target.kind === 'dag_run' && dagRunDetail && (
          <div style={{ display: 'grid', gap: 12 }} data-testid="dag-run-inspector">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span className="mono" style={{ fontSize: 12 }}>{dagRunDetail.run_id}</span>
              <span className={`badge ${dagRunDetail.status === 'completed' ? 'ok' : dagRunDetail.status === 'waiting_approval' ? 'warn' : ''}`}>
                {dagRunDetail.status}
              </span>
            </div>
            {(dagRunDetail.status === 'waiting_approval' || dagRunDetail.status === 'failed') && (
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={async () => {
                  const r = await callAction<{ dag_run?: DagRunSummary & { node_runs?: DagNodeRunEntry[] } }>('customizations', { subaction: 'resume_dag', run_id: dagRunDetail.run_id });
                  onToast(r.summary || r.error || 'Resumed DAG run.');
                  onChanged();
                  const fresh = await callAction<{ dag_run?: DagRunSummary & { node_runs?: DagNodeRunEntry[] }; run?: DagRunSummary & { node_runs?: DagNodeRunEntry[] } }>('customizations', { subaction: 'get_dag_run', run_id: dagRunDetail.run_id });
                  const rec = fresh.dag_run || fresh.run;
                  if (rec) setDagRunDetail(rec);
                }}
              >
                <RotateCcw size={12} /> Approve &amp; Resume DAG
              </button>
            )}
            <div className="field-label">Step Checkpoints</div>
            <div className="card-group">
              {(dagRunDetail.node_runs || []).map((nr) => {
                const outVal = nr.output ?? nr.output_json;
                return (
                  <div key={nr.node_id} className="card-row" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <strong className="mono" style={{ fontSize: 12 }}>{nr.node_id}</strong>
                      <span className={`badge ${nr.status === 'completed' ? 'ok' : ''}`}>{nr.status}</span>
                    </div>
                    {(outVal !== undefined || nr.error) && (
                      <pre className="mono" style={{ margin: 0, fontSize: 11, whiteSpace: 'pre-wrap', color: 'var(--text-secondary)' }}>
                        {nr.error || (typeof outVal === 'string' ? outVal : JSON.stringify(outVal, null, 2))}
                      </pre>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {target.kind === 'wiki_note' && (
          <div>
            <div style={{ marginBottom: 10, display: 'flex', justifyContent: 'flex-end' }}>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => onJumpToWiki(wikiDetail?.path || target.notePath)}
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
            ) : wikiMissing ? (
              <div className="card-group" style={{ padding: 14 }} data-testid="wiki-missing-note-card">
                <div style={{ fontSize: 13, marginBottom: 10, color: 'var(--text-secondary)' }}>
                  Note <code className="mono">{target.notePath}</code> was not found yet.
                </div>
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  data-testid="wiki-create-missing-btn"
                  onClick={async () => {
                    const slug = target.notePath.split('/').pop()?.replace(/\.md$/i, '') || 'new-note';
                    const r = await callAction<{ path?: string }>('wiki', {
                      subaction: 'capture_inbox',
                      title: slug.replace(/-/g, ' '),
                      slug,
                      content: `# ${slug.replace(/-/g, ' ')}\n\nCaptured stub from broken wikilink.`,
                    });
                    if (r.ok && r.path) {
                      onToast(`Created ${r.path} in 01_inbox/.`);
                      onJumpToWiki(r.path);
                    } else {
                      onToast(r.error || 'Could not create note.');
                    }
                  }}
                >
                  <Plus size={12} /> Note not found — Create in 01_inbox/
                </button>
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
                  approvals.map((apr) => {
                    const isPreviewOpen = previewAprId === apr.approval_id;
                    return (
                      <div key={apr.approval_id} className="card-row" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 8 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8, flexWrap: 'wrap' }}>
                          <div className="card-row-info">
                            <div className="card-row-title">{apr.category}</div>
                            <div className="card-row-desc">{(apr.summary || `${apr.action_name} (${apr.target})`).replace(/`/g, '')}</div>
                          </div>
                          <div style={{ display: 'inline-flex', gap: 6, flexWrap: 'wrap' }}>
                            <button
                              type="button"
                              className="btn btn-ghost btn-sm"
                              data-testid={`preview-approval-${apr.approval_id}`}
                              onClick={() => void toggleApprovalPreview(apr)}
                            >
                              <Eye size={11} /> Preview Diff / Command
                            </button>
                            <button
                              type="button"
                              className="btn btn-primary btn-sm"
                              disabled={resolvingAprId === apr.approval_id}
                              onClick={() => void handleResolveApproval(apr, 'approve')}
                            >
                              <Check size={11} /> Approve
                            </button>
                            <button
                              type="button"
                              className="btn btn-danger btn-sm"
                              disabled={resolvingAprId === apr.approval_id}
                              onClick={() => void handleResolveApproval(apr, 'deny')}
                            >
                              Deny
                            </button>
                          </div>
                        </div>

                        {isPreviewOpen && (
                          <div data-testid={`approval-preview-body-${apr.approval_id}`}>
                            {previewWikiDiff ? (
                              <DiffView diff={previewWikiDiff} onClose={() => setPreviewAprId('')} />
                            ) : (
                              <pre
                                className="mono"
                                style={{
                                  margin: 0,
                                  padding: 10,
                                  borderRadius: 'var(--radius-sm)',
                                  background: 'var(--code-bg)',
                                  border: '1px solid var(--border-hairline)',
                                  fontSize: 11.5,
                                  whiteSpace: 'pre-wrap',
                                }}
                              >
                                {formatApprovalPayloadPreview(apr)}
                              </pre>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            </div>

            {selectedJob && (
              <div className="card-group" style={{ padding: 12 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                  <strong>{selectedJob.title || selectedJob.goal}</strong>
                  <div style={{ display: 'inline-flex', gap: 6 }}>
                    {selectedJob.status === 'running' && (
                      <button
                        type="button"
                        className="btn btn-danger btn-sm"
                        onClick={async () => {
                          await callAction('jobs', { subaction: 'cancel', job_id: selectedJob.job_id });
                          onChanged();
                        }}
                      >
                        Cancel
                      </button>
                    )}
                    {selectedJob.status === 'waiting_approval' && (
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        onClick={async () => {
                          await callAction('jobs', { subaction: 'resume', job_id: selectedJob.job_id });
                          onChanged();
                        }}
                      >
                        Resume
                      </button>
                    )}
                  </div>
                </div>
                <div className="md-body" style={{ fontSize: 12.5 }}>
                  <MarkdownView
                    content={
                      selectedJob.envelope_summary ||
                      selectedJob.partial_output ||
                      selectedJob.error ||
                      `Status: ${selectedJob.status}`
                    }
                  />
                </div>
              </div>
            )}

            <div>
              <div className="field-label">Departures Board — Background Tasks ({jobs.length})</div>
              <div className="card-group">
                {jobs.map((j) => (
                  <div
                    key={j.job_id}
                    className="card-row"
                    style={{ cursor: 'pointer' }}
                    onClick={() => setSelectedJob(j)}
                  >
                    <div className="card-row-info">
                      <div className="card-row-title">{j.title || j.goal}</div>
                      <div className="card-row-desc mono">{j.job_id} • {j.tier}</div>
                    </div>
                    <span className={`badge ${j.status === 'done' ? 'ok' : j.status === 'running' ? 'accent' : ''}`}>
                      {j.status}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </aside>
  );
};

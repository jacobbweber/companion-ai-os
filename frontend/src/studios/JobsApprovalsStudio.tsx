import React, { useEffect, useState } from 'react';
import {
  Check,
  Clock,
  ExternalLink,
  FileDiff,
  Play,
  RefreshCw,
  Rocket,
  ShieldAlert,
  Square,
  Trash2,
  X,
} from 'lucide-react';
import { callAction } from '../api';
import { DiffView } from '../components/DiffView';
import { MarkdownView } from '../components/MarkdownView';
import type { ApprovalRequest, BackgroundJob, RoutineItem, WikiDiffPayload } from '../types';

interface JobsApprovalsStudioProps {
  onOpenWikiNote: (path: string) => void;
  onRefreshStatus: () => void;
  onToast: (msg: string) => void;
}

export const JobsApprovalsStudio: React.FC<JobsApprovalsStudioProps> = ({
  onOpenWikiNote,
  onRefreshStatus,
  onToast,
}) => {
  const [jobs, setJobs] = useState<BackgroundJob[]>([]);
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  const [routines, setRoutines] = useState<RoutineItem[]>([]);
  const [approvalFilter, setApprovalFilter] = useState<'waiting' | 'approved' | 'denied'>('waiting');
  const [previewDiffs, setPreviewDiffs] = useState<Record<string, WikiDiffPayload>>({});

  const [title, setTitle] = useState('');
  const [goal, setGoal] = useState('');
  const [tier, setTier] = useState<'fast' | 'reasoning'>('fast');

  const [rtId, setRtId] = useState('');
  const [rtSchedule, setRtSchedule] = useState('daily 09:00');
  const [rtKind, setRtKind] = useState('snapshot_backup');

  const loadAll = async () => {
    try {
      const [jRes, aRes, rRes] = await Promise.all([
        callAction<{ jobs?: BackgroundJob[] }>('jobs', { subaction: 'list', limit: 25 }),
        callAction<{ approvals?: ApprovalRequest[] }>('approvals', {
          subaction: 'list',
          status: approvalFilter,
        }),
        callAction<{ routines?: RoutineItem[] }>('routines', { subaction: 'list' }),
      ]);
      if (Array.isArray(jRes.jobs)) setJobs(jRes.jobs);
      if (Array.isArray(aRes.approvals)) setApprovals(aRes.approvals);
      if (Array.isArray(rRes.routines)) setRoutines(rRes.routines);
    } catch (err) {
      onToast(`Board refresh error: ${String(err)}`);
    }
  };

  useEffect(() => {
    void callAction('approvals', { subaction: 'dismiss_nudges', category: 'job' })
      .then(() => onRefreshStatus())
      .catch(() => {});
  }, []);

  useEffect(() => {
    void loadAll();
    const timer = window.setInterval(() => void loadAll(), 4000);
    return () => window.clearInterval(timer);
  }, [approvalFilter]);

  const dispatchNewJob = async () => {
    if (!goal.trim()) return;
    const res = await callAction<{ job_id?: string }>('jobs', {
      subaction: 'dispatch',
      title: title.trim() || goal.trim().slice(0, 48),
      goal: goal.trim(),
      tier,
      session_id: 'web_ui',
    });
    if (res.ok) {
      onToast(`Dispatched background job ${res.job_id || ''}`);
      setTitle('');
      setGoal('');
      await loadAll();
      onRefreshStatus();
    } else {
      onToast(res.error || 'Dispatch failed.');
    }
  };

  const toggleApprovalDiff = async (approvalId: string) => {
    if (previewDiffs[approvalId]) {
      setPreviewDiffs((prev) => {
        const next = { ...prev };
        delete next[approvalId];
        return next;
      });
      return;
    }
    const res = await callAction<WikiDiffPayload>('wiki', {
      subaction: 'diff',
      approval_id: approvalId,
    });
    if (res.ok && Array.isArray(res.files)) {
      setPreviewDiffs((prev) => ({ ...prev, [approvalId]: res }));
    } else {
      onToast(res.error || 'No diff preview available for this approval.');
    }
  };

  const resolveApproval = async (approvalId: string, decision: 'approve' | 'deny') => {
    const res = await callAction('approvals', {
      subaction: decision,
      approval_id: approvalId,
    });
    onToast(res.summary || res.error || `Marked ${approvalId} as ${decision}`);
    void loadAll();
    onRefreshStatus();
  };

  const createRoutineQuick = async () => {
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
      setRtId('');
      onToast(res.summary || 'Routine scheduled.');
      void loadAll();
    } else {
      onToast(res.error || 'Could not schedule routine.');
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="studio-header">
        <div className="studio-title-group">
          <h1>Departures Board, Routines &amp; Approvals Center</h1>
          <p>
            Zero-identity background workers, hardware-gated scheduled routines (projected onto
            Calendar) &amp; SHA-256 fingerprinted human gates
          </p>
        </div>
        <div className="studio-actions">
          <button type="button" className="btn" onClick={() => void loadAll()}>
            <RefreshCw size={13} /> Refresh Board
          </button>
        </div>
      </div>

      <div className="studio-scroll">
        <div className="grid-2">
          {/* Left Column: Dispatch + Departures Board */}
          <div>
            <div className="panel">
              <div className="panel-head">
                <span className="panel-title">
                  <Rocket size={13} /> Dispatch Background Worker
                </span>
                <span className="badge accent">KEEPS CHAT CLEAN</span>
              </div>
              <div style={{ display: 'flex', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
                <input
                  className="input"
                  style={{ flex: '1 1 180px' }}
                  placeholder="Short job title (e.g. Skills Security Audit)"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                />
                <select
                  aria-label="Worker tier"
                  className="select"
                  style={{ flex: '0 1 auto' }}
                  value={tier}
                  onChange={(e) => setTier(e.target.value as 'fast' | 'reasoning')}
                >
                  <option value="fast">Fast Tier</option>
                  <option value="reasoning">Reasoning Tier</option>
                </select>
              </div>
              <textarea
                className="textarea"
                style={{ width: '100%', marginBottom: 8 }}
                placeholder="Self-contained worker objective... Long outputs automatically route to 01_inbox/."
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
              />
              <button
                type="button"
                className="btn btn-primary"
                disabled={!goal.trim()}
                onClick={() => void dispatchNewJob()}
              >
                <Rocket size={13} /> Launch Background Job
              </button>
            </div>

            <div className="panel">
              <div className="panel-head">
                <span className="panel-title">Background Jobs Departures Board ({jobs.length})</span>
              </div>
              {jobs.length === 0 ? (
                <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                  No background jobs dispatched yet.
                </div>
              ) : (
                jobs.map((j) => {
                  const badgeClass =
                    j.status === 'done'
                      ? 'ok'
                      : j.status === 'running' || j.status === 'queued'
                        ? 'accent'
                        : j.status === 'waiting_approval'
                          ? 'warn'
                          : 'danger';
                  return (
                    <div
                      key={j.job_id}
                      style={{
                        border: '1px solid var(--border-hairline)',
                        background: 'var(--bg-elevated)',
                        padding: '10px 12px',
                        marginBottom: 8,
                      }}
                    >
                      <div
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          alignItems: 'center',
                          gap: 8,
                          flexWrap: 'wrap',
                        }}
                      >
                        <div>
                          <strong>{j.title}</strong>{' '}
                          <code className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                            {j.job_id}
                          </code>
                        </div>
                        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                          <span className="badge">{j.tier}</span>
                          <span className={`badge ${badgeClass}`}>{j.status}</span>
                        </div>
                      </div>

                      {(j.envelope_summary || j.partial_output || j.error) && (
                        <div style={{ marginTop: 8, fontSize: 12.5 }}>
                          <MarkdownView
                            content={j.envelope_summary || j.partial_output || j.error}
                            onOpenWikiNote={onOpenWikiNote}
                          />
                        </div>
                      )}

                      <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
                        {j.overflow_wiki_path && (
                          <button
                            type="button"
                            className="btn"
                            onClick={() => onOpenWikiNote(j.overflow_wiki_path)}
                          >
                            <ExternalLink size={12} /> Open Full Wiki Report ({j.overflow_wiki_path})
                          </button>
                        )}
                        {(j.status === 'running' || j.status === 'queued') && (
                          <button
                            type="button"
                            className="btn btn-danger"
                            onClick={async () => {
                              await callAction('jobs', { subaction: 'cancel', job_id: j.job_id });
                              void loadAll();
                            }}
                          >
                            <Square size={12} /> Cancel
                          </button>
                        )}
                        {(j.status === 'interrupted' || j.status === 'waiting_approval') && (
                          <button
                            type="button"
                            className="btn"
                            onClick={async () => {
                              await callAction('jobs', { subaction: 'resume', job_id: j.job_id });
                              void loadAll();
                            }}
                          >
                            <Play size={12} /> Resume
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          </div>

          {/* Right Column: Human-in-the-Loop Approvals Queue */}
          <div>
            <div className="panel">
              <div className="panel-head">
                <span className="panel-title">
                  <ShieldAlert size={13} /> Human-in-the-Loop Approvals Queue
                </span>
                <div style={{ display: 'flex', gap: 4 }}>
                  {(['waiting', 'approved', 'denied'] as const).map((st) => (
                    <button
                      key={st}
                      type="button"
                      className={`btn ${approvalFilter === st ? 'btn-primary' : ''}`}
                      style={{ padding: '2px 8px', fontSize: 11 }}
                      onClick={() => setApprovalFilter(st)}
                    >
                      {st.toUpperCase()}
                    </button>
                  ))}
                </div>
              </div>

              {approvals.length === 0 ? (
                <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                  No {approvalFilter} approval requests.
                </div>
              ) : (
                approvals.map((a) => (
                  <div
                    key={a.approval_id}
                    style={{
                      border: '1px solid var(--border-strong)',
                      background: 'var(--bg-elevated)',
                      padding: '10px 12px',
                      marginBottom: 8,
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        gap: 8,
                        marginBottom: 4,
                      }}
                    >
                      <span className="badge warn">
                        {a.category}:{a.action_name}
                      </span>
                      <code className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                        {a.approval_id} • fp:{a.fingerprint?.slice(0, 12)}
                      </code>
                    </div>
                    <div style={{ fontWeight: 600, marginBottom: 4 }}>{a.summary}</div>
                    <div className="mono" style={{ fontSize: 11.5, color: 'var(--text-secondary)' }}>
                      Target: {a.target}
                    </div>

                    {previewDiffs[a.approval_id] && (
                      <div style={{ marginTop: 10 }}>
                        <DiffView
                          diff={previewDiffs[a.approval_id]}
                          compact
                          onClose={() => void toggleApprovalDiff(a.approval_id)}
                        />
                      </div>
                    )}

                    <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
                      {a.category === 'wiki_edit' &&
                        ['edit_user_note', 'commit_curation', 'graduate_note'].includes(
                          a.action_name
                        ) && (
                          <button
                            type="button"
                            className="btn"
                            onClick={() => void toggleApprovalDiff(a.approval_id)}
                          >
                            <FileDiff size={13} />{' '}
                            {previewDiffs[a.approval_id] ? 'Hide Diff' : 'Preview Diff'}
                          </button>
                        )}
                      {a.status === 'waiting' && (
                        <>
                          <button
                            type="button"
                            className="btn btn-primary"
                            onClick={() => void resolveApproval(a.approval_id, 'approve')}
                          >
                            <Check size={13} /> Approve &amp; Execute
                          </button>
                          <button
                            type="button"
                            className="btn btn-danger"
                            onClick={() => void resolveApproval(a.approval_id, 'deny')}
                          >
                            <X size={13} /> Deny
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>

            {/* Scheduled Routines Panel */}
            <div className="panel">
              <div className="panel-head">
                <span className="panel-title">
                  <Clock size={13} /> Scheduled Routines ({routines.length})
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
                  placeholder="ID (e.g. morning-triage)"
                  value={rtId}
                  onChange={(e) => setRtId(e.target.value)}
                />
                <input
                  className="input"
                  placeholder="daily 09:00 | every 6h"
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
                style={{ marginBottom: routines.length > 0 ? 10 : 0 }}
                disabled={!rtId.trim()}
                onClick={() => void createRoutineQuick()}
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
                    padding: '7px 10px',
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
                      Next: {r.next_run_at}
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
          </div>
        </div>
      </div>
    </div>
  );
};

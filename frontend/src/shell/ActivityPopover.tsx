import React, { useEffect, useState } from 'react';
import {
  Check,
  Clock,
  Eye,
  GitBranch,
  Layers,
  ShieldAlert,
  Sparkles,
  Square,
  Terminal,
  X,
} from 'lucide-react';
import { callAction, cancelActiveJob, emitApprovalResolved } from '../api';
import type {
  AuxInspectorTarget,
  BackgroundJob,
  ProjectBoardProjection,
  SurfaceId,
  WebStatus,
} from '../types';

interface ActivityPopoverProps {
  status: WebStatus | null;
  onClose: () => void;
  onRefreshStatus: () => Promise<void>;
  onToast: (msg: string) => void;
  onSelectSurface: (surface: SurfaceId) => void;
  onInspect: (target: AuxInspectorTarget) => void;
}

export const ActivityPopover: React.FC<ActivityPopoverProps> = ({
  status,
  onClose,
  onRefreshStatus,
  onToast,
  onSelectSurface,
  onInspect,
}) => {
  const [jobs, setJobs] = useState<BackgroundJob[]>([]);
  const [resolvingId, setResolvingId] = useState('');

  const refreshJobs = async () => {
    const res = await callAction<{ jobs?: BackgroundJob[] }>('jobs', { subaction: 'list' });
    if (res.jobs) setJobs(res.jobs.slice(0, 8));
  };

  useEffect(() => {
    void refreshJobs();
  }, []);

  const approvals = status?.pending_approvals || [];
  const totalApprovals = status?.pending_approvals_count ?? approvals.length;
  const nudges = (status?.session_nudges || []).filter((n) => n.category !== 'approval');
  const unseenAutonomy = status?.unseen_autonomy_count || 0;
  const activeForeground = status?.active_job?.status === 'running' ? status.active_job : null;
  const hasForeground = Boolean(status?.busy || activeForeground);
  const runningBgJobs = jobs.filter((j) => j.status === 'running' || j.status === 'queued');
  const totalActiveCount = runningBgJobs.length + (hasForeground ? 1 : 0);

  const resolveApproval = async (approvalId: string, decision: 'approve' | 'deny') => {
    if (resolvingId) return;
    setResolvingId(approvalId);
    try {
      const res = await callAction<{
        category?: string;
        resumed_background_job?: boolean;
      }>('approvals', {
        subaction: decision,
        approval_id: approvalId,
      });
      await onRefreshStatus();
      onToast(
        res.ok === false
          ? res.error || `Approval ${decision} failed.`
          : res.summary || `Approval ${decision}d.`
      );
      if (res.ok !== false) {
        emitApprovalResolved({
          approvalId,
          decision,
          category: res.category,
          resumedBackgroundJob: Boolean(res.resumed_background_job),
          source: 'popover',
        });
      }
    } finally {
      setResolvingId('');
    }
  };

  const stopActiveTurn = async () => {
    const r = await cancelActiveJob(activeForeground?.job_id || 'active');
    await onRefreshStatus();
    onToast(r.ok === false ? r.error || 'Could not stop turn.' : 'Stopped active turn.');
  };

  const stopOneJob = async (e: React.MouseEvent, jobId: string) => {
    e.stopPropagation();
    const res = await callAction('jobs', { subaction: 'cancel', job_id: jobId });
    await Promise.all([refreshJobs(), onRefreshStatus()]);
    onToast(res.summary || `Stopped job ${jobId}.`);
  };

  const stopAllWorkers = async () => {
    const ops: Array<Promise<unknown>> = runningBgJobs.map((j) =>
      callAction('jobs', { subaction: 'cancel', job_id: j.job_id })
    );
    if (hasForeground) {
      ops.push(cancelActiveJob(activeForeground?.job_id || 'active'));
    }
    await Promise.all(ops);
    await Promise.all([refreshJobs(), onRefreshStatus()]);
    onToast('Stopped all active turns and parallel workers.');
  };

  const openJobOrCard = async (job: BackgroundJob) => {
    if (job.starter === 'worktree' && job.project_id) {
      const bRes = await callAction<ProjectBoardProjection>('agile', {
        subaction: 'board_view',
        project_id: job.project_id,
      });
      if (bRes.ok) {
        const matched = (bRes.cards || []).find(
          (c) => c.active_job_id === job.job_id || c.deliverable?.job_id === job.job_id
        );
        if (matched) {
          onSelectSurface('boards');
          onInspect({ kind: 'work_item', itemId: matched.item_id, projectId: job.project_id });
          onClose();
          return;
        }
      }
    }
    onInspect({ kind: 'job', jobId: job.job_id });
    onClose();
  };

  const clearNotifications = async () => {
    const ops: Array<Promise<unknown>> = [
      callAction('approvals', { subaction: 'dismiss_nudges' }),
    ];
    if (unseenAutonomy > 0) {
      ops.push(callAction('autonomy', { subaction: 'morning', mark_seen: true }));
    }
    await Promise.all(ops);
    await onRefreshStatus();
    onToast('Cleared unread notifications.');
  };

  return (
    <div className="activity-popover" data-testid="activity-popover">
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '4px 8px 8px',
          borderBottom: '1px solid var(--border-hairline)',
          marginBottom: 4,
          gap: 6,
        }}
      >
        <strong style={{ fontSize: 12.5 }}>Workspace Activity</strong>
        <div style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
          {totalActiveCount >= 2 && (
            <button
              type="button"
              className="btn btn-danger btn-sm"
              data-testid="activity-stop-all-btn"
              onClick={() => void stopAllWorkers()}
            >
              <Square size={9} fill="currentColor" /> Stop All ({totalActiveCount})
            </button>
          )}
          {(nudges.length > 0 || unseenAutonomy > 0) && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => void clearNotifications()}>
              Clear Read
            </button>
          )}
        </div>
      </div>

      {hasForeground && (
        <div className="popover-approval-card" style={{ marginBottom: 6 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 12, fontWeight: 600 }}>
              {activeForeground?.active_tool
                ? `Running: ${activeForeground.active_tool}`
                : status?.active_action || 'Active Turn Running'}
            </span>
            <button
              type="button"
              className="btn btn-danger btn-sm"
              data-testid="activity-stop-turn-btn"
              onClick={() => void stopActiveTurn()}
            >
              <Square size={10} fill="currentColor" /> Stop
            </button>
          </div>
        </div>
      )}

      {approvals.length > 0 && (
        <div>
          <div className="popover-group-label">
            <span>Approvals Needed</span>
            <span className="count-pill">{totalApprovals}</span>
          </div>
          {approvals.map((apr) => (
            <div key={apr.approval_id} className="popover-approval-card">
              <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11.5, color: 'var(--status-warn)', fontWeight: 600 }}>
                <ShieldAlert size={12} />
                <span>{apr.category}</span>
              </div>
              <div style={{ fontSize: 12.5, margin: '4px 0 8px', color: 'var(--text-primary)' }}>
                {(apr.summary || `${apr.action_name} (${apr.target})`).replace(/`/g, '')}
              </div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => {
                    onInspect({ kind: 'approvals' });
                    onClose();
                  }}
                >
                  <Eye size={11} /> Preview
                </button>
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  disabled={resolvingId === apr.approval_id}
                  onClick={() => void resolveApproval(apr.approval_id, 'approve')}
                >
                  <Check size={11} /> Approve
                </button>
                <button
                  type="button"
                  className="btn btn-danger btn-sm"
                  disabled={resolvingId === apr.approval_id}
                  onClick={() => void resolveApproval(apr.approval_id, 'deny')}
                >
                  <X size={11} /> Deny
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {unseenAutonomy > 0 && (
        <div>
          <div className="popover-group-label">
            <span>Autonomy Briefing</span>
            <span className="count-pill">{unseenAutonomy}</span>
          </div>
          <button
            type="button"
            className="popover-row"
            onClick={() => {
              onSelectSurface('automations');
              onClose();
            }}
          >
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
              <Sparkles size={13} style={{ color: 'var(--accent-primary)' }} />
              <span>{unseenAutonomy} new overnight / idle run(s)</span>
            </span>
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>View</span>
          </button>
        </div>
      )}

      <div className="popover-group-label">
        <span>Parallel Workers &amp; Tasks</span>
        <span className="count-pill">{jobs.length}</span>
      </div>
      {jobs.length === 0 ? (
        <div style={{ padding: '6px 10px', fontSize: 12, color: 'var(--text-muted)' }}>
          No recent background tasks.
        </div>
      ) : (
        jobs.map((job) => {
          const isJobRunning = job.status === 'running' || job.status === 'queued';
          const brMatch = /^\[([^\]]+)\]\s*(.*)$/.exec(job.title || '');
          const branchTag = job.starter === 'worktree' && brMatch ? brMatch[1] : '';
          const displayTitle = branchTag && brMatch ? brMatch[2] : job.title || job.goal;
          return (
            <div
              key={job.job_id}
              role="button"
              tabIndex={0}
              className="popover-row"
              onClick={() => void openJobOrCard(job)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void openJobOrCard(job);
              }}
            >
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, minWidth: 0, flex: 1 }}>
                {branchTag ? (
                  <GitBranch size={13} style={{ flexShrink: 0, color: 'var(--accent-primary)' }} />
                ) : (
                  <Terminal size={13} style={{ flexShrink: 0, color: 'var(--text-muted)' }} />
                )}
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {displayTitle}
                </span>
                {branchTag && (
                  <span className="badge mono" style={{ fontSize: 10 }}>
                    {branchTag.replace(/^feat\//, '')}
                  </span>
                )}
              </span>
              <span style={{ display: 'inline-flex', gap: 5, alignItems: 'center', flexShrink: 0 }}>
                <span className={`badge ${job.status === 'done' ? 'ok' : isJobRunning ? 'accent' : ''}`}>
                  {job.status}
                </span>
                {isJobRunning && (
                  <button
                    type="button"
                    className="btn btn-danger btn-sm"
                    style={{ padding: '2px 6px', fontSize: 10.5 }}
                    data-testid={`activity-stop-job-${job.job_id}`}
                    onClick={(e) => void stopOneJob(e, job.job_id)}
                  >
                    <Square size={8} fill="currentColor" /> Stop
                  </button>
                )}
              </span>
            </div>
          );
        })
      )}

      {nudges.length > 0 && (
        <div>
          <div className="popover-group-label">
            <span>Updates</span>
          </div>
          {nudges.slice(0, 4).map((n, i) => (
            <div key={n.id ?? i} style={{ padding: '6px 9px', fontSize: 12, color: 'var(--text-secondary)' }}>
              {n.message}
            </div>
          ))}
        </div>
      )}

      <div className="rail-divider" style={{ margin: '6px 2px' }} />
      <button
        type="button"
        className="popover-row"
        onClick={() => {
          onInspect({ kind: 'approvals' });
          onClose();
        }}
      >
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          <Layers size={13} />
          <span>Open Full Departures &amp; Approvals Queue</span>
        </span>
      </button>
      <button
        type="button"
        className="popover-row"
        onClick={() => {
          onSelectSurface('automations');
          onClose();
        }}
      >
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          <Clock size={13} />
          <span>Automations &amp; Morning Briefing</span>
        </span>
      </button>
    </div>
  );
};

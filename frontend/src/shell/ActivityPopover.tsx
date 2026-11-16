import React, { useEffect, useState } from 'react';
import {
  Check,
  Clock,
  Layers,
  ShieldAlert,
  Sparkles,
  Terminal,
  X,
} from 'lucide-react';
import { callAction } from '../api';
import type { AuxInspectorTarget, BackgroundJob, SurfaceId, WebStatus } from '../types';

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

  useEffect(() => {
    void callAction<{ jobs?: BackgroundJob[] }>('jobs', { subaction: 'list' }).then((res) => {
      if (res.jobs) setJobs(res.jobs.slice(0, 6));
    });
  }, []);

  const approvals = status?.pending_approvals || [];
  const nudges = (status?.session_nudges || []).filter((n) => n.category !== 'approval');
  const unseenAutonomy = status?.unseen_autonomy_count || 0;

  const resolveApproval = async (approvalId: string, decision: 'approve' | 'deny') => {
    const res = await callAction('approvals', {
      subaction: decision,
      approval_id: approvalId,
    });
    await onRefreshStatus();
    onToast(res.summary || res.error || `Approval ${decision}d.`);
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
        }}
      >
        <strong style={{ fontSize: 12.5 }}>Workspace Activity</strong>
        {(nudges.length > 0 || unseenAutonomy > 0) && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => void clearNotifications()}>
            Clear Read
          </button>
        )}
      </div>

      {approvals.length > 0 && (
        <div>
          <div className="popover-group-label">
            <span>Approvals Needed</span>
            <span className="count-pill">{approvals.length}</span>
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
              <div style={{ display: 'flex', gap: 6 }}>
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  onClick={() => void resolveApproval(apr.approval_id, 'approve')}
                >
                  <Check size={11} /> Approve
                </button>
                <button
                  type="button"
                  className="btn btn-danger btn-sm"
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
        <span>Background Tasks</span>
        <span className="count-pill">{jobs.length}</span>
      </div>
      {jobs.length === 0 ? (
        <div style={{ padding: '6px 10px', fontSize: 12, color: 'var(--text-muted)' }}>
          No recent background tasks.
        </div>
      ) : (
        jobs.map((job) => (
          <button
            key={job.job_id}
            type="button"
            className="popover-row"
            onClick={() => {
              onInspect({ kind: 'job', jobId: job.job_id });
              onClose();
            }}
          >
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
              <Terminal size={13} style={{ flexShrink: 0, color: 'var(--text-muted)' }} />
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {job.title || job.goal}
              </span>
            </span>
            <span className={`badge ${job.status === 'done' ? 'ok' : job.status === 'running' ? 'accent' : ''}`}>
              {job.status}
            </span>
          </button>
        ))
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

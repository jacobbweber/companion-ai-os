import React, { useEffect, useState } from 'react';
import {
  CheckCircle2,
  ExternalLink,
  GitBranch,
  GitMerge,
  Play,
  RotateCcw,
  Send,
  Square,
  Trash2,
} from 'lucide-react';
import { callAction } from '../api';
import { DiffView } from '../components/DiffView';
import { MarkdownView } from '../components/MarkdownView';
import type { BackgroundJob, ProjectGraphEntry, WorkItem } from '../types';

interface DeliverableReviewPanelProps {
  item: WorkItem;
  project?: ProjectGraphEntry | null;
  onChanged: () => void;
  onToast: (msg: string) => void;
}

export const DeliverableReviewPanel: React.FC<DeliverableReviewPanelProps> = ({
  item,
  project,
  onChanged,
  onToast,
}) => {
  const [liveJob, setLiveJob] = useState<BackgroundJob | null>(null);
  const [busyAction, setBusyAction] = useState('');
  const [showRevisionForm, setShowRevisionForm] = useState(false);
  const [revisionNote, setRevisionNote] = useState('');

  const isRunning = item.column_key === 'in_progress' && Boolean(item.active_job_id);
  const deliv = item.deliverable || {};
  const branch = item.branch_name || deliv.branch_name || '';
  const diffPayload = deliv.diff_payload;
  const shotIds = deliv.screenshot_ids || [];
  const canPushPr = Boolean(
    branch &&
      (deliv.has_remote || (project?.delivery_policy && project.delivery_policy !== 'local_only'))
  );

  useEffect(() => {
    if (!item.active_job_id) {
      setLiveJob(null);
      return;
    }
    const fetchJob = () => {
      void callAction<{ job?: BackgroundJob }>('jobs', {
        subaction: 'get',
        job_id: item.active_job_id,
      }).then((res) => {
        if (res.job) {
          setLiveJob(res.job);
          if (res.job.status !== 'running' && res.job.status !== 'queued') {
            onChanged();
          }
        }
      });
    };
    fetchJob();
    if (!isRunning) return;
    const id = window.setInterval(fetchJob, 2000);
    return () => window.clearInterval(id);
  }, [item.active_job_id, isRunning]);

  const stopWorker = async () => {
    if (!item.active_job_id || busyAction) return;
    setBusyAction('stop');
    try {
      const res = await callAction('jobs', { subaction: 'cancel', job_id: item.active_job_id });
      onChanged();
      onToast(res.summary || 'Stopped card worker.');
    } finally {
      setBusyAction('');
    }
  };

  const startOrRetryWorker = async () => {
    if (busyAction) return;
    setBusyAction('start');
    try {
      const res = await callAction('agile', {
        subaction: 'start_item',
        item_id: item.item_id,
        project_id: item.project_id,
      });
      onChanged();
      onToast(res.ok === false ? res.error || 'Could not start card.' : res.summary || 'Started worker.');
    } finally {
      setBusyAction('');
    }
  };

  const mergeLocal = async () => {
    if (busyAction) return;
    setBusyAction('merge');
    try {
      const res = await callAction('agile', { subaction: 'merge_item', item_id: item.item_id });
      if (res.ok === false) {
        onToast(res.error || 'Merge failed.');
      } else {
        onChanged();
        onToast(res.summary || 'Merged into local main and completed card.');
      }
    } finally {
      setBusyAction('');
    }
  };

  const pushAndPr = async () => {
    if (busyAction) return;
    setBusyAction('push');
    try {
      const res = await callAction('agile', { subaction: 'push_item_pr', item_id: item.item_id });
      if (res.ok === false) {
        onToast(res.error || 'Push / PR failed.');
      } else {
        onChanged();
        onToast(res.summary || 'Pushed branch to remote.');
      }
    } finally {
      setBusyAction('');
    }
  };

  const submitRevision = async () => {
    const clean = revisionNote.trim();
    if (!clean || busyAction) return;
    setBusyAction('revise');
    try {
      const res = await callAction('agile', {
        subaction: 'request_changes',
        item_id: item.item_id,
        feedback: clean,
      });
      if (res.ok === false) {
        onToast(res.error || 'Could not dispatch revision.');
      } else {
        setRevisionNote('');
        setShowRevisionForm(false);
        onChanged();
        onToast(res.summary || 'Sent revision feedback to Companion worktree.');
      }
    } finally {
      setBusyAction('');
    }
  };

  const discardWt = async () => {
    if (busyAction) return;
    setBusyAction('discard');
    try {
      const res = await callAction('agile', { subaction: 'discard_worktree', item_id: item.item_id });
      onChanged();
      onToast(res.summary || 'Discarded worktree.');
    } finally {
      setBusyAction('');
    }
  };

  return (
    <div style={{ display: 'grid', gap: 12 }} data-testid="deliverable-review-panel">
      {/* Branch & Status Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          {branch && (
            <span className="badge accent mono" title={item.worktree_path || branch}>
              <GitBranch size={11} /> {branch}
              {deliv.commit_sha ? ` @ ${deliv.commit_sha}` : ''}
            </span>
          )}
          {deliv.merged && (
            <span className="badge ok">
              <CheckCircle2 size={11} /> Merged ({deliv.merged_commit || deliv.merged_into || 'main'})
            </span>
          )}
          {deliv.pr_url && (
            <a
              href={deliv.pr_url}
              target="_blank"
              rel="noreferrer"
              className="badge accent"
              style={{ textDecoration: 'none' }}
            >
              PR #{deliv.pr_number || ''} <ExternalLink size={10} />
            </a>
          )}
        </div>

        {isRunning ? (
          <button
            type="button"
            className="btn btn-danger btn-sm"
            data-testid="stop-card-worker-btn"
            disabled={busyAction === 'stop'}
            onClick={() => void stopWorker()}
          >
            <Square size={10} fill="currentColor" /> Stop Worker
          </button>
        ) : item.column_key === 'ready' ? (
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => void startOrRetryWorker()}
          >
            <Play size={11} /> Start Worker
          </button>
        ) : null}
      </div>

      {/* Live Worker Stream Box */}
      {isRunning && (
        <div
          style={{
            padding: 10,
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-hairline)',
            background: 'var(--code-bg)',
          }}
          data-testid="live-worker-box"
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
            <span className="status-dot busy" />
            <span>Worker executing in isolated worktree ({liveJob?.status || 'running'})...</span>
          </div>
          <pre
            className="mono"
            style={{ margin: 0, fontSize: 11, whiteSpace: 'pre-wrap', maxHeight: 180, overflowY: 'auto', color: 'var(--text-secondary)' }}
          >
            {liveJob?.partial_output || 'Preparing isolated worktree & inspecting repository rules...'}
          </pre>
        </div>
      )}

      {/* Deliverable Summary */}
      {deliv.summary && (
        <div
          style={{
            padding: 10,
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-hairline)',
            background: 'var(--bg-elevated)',
          }}
        >
          <div className="field-label" style={{ marginBottom: 4 }}>Deliverable Summary</div>
          <div className="md-body" style={{ fontSize: 12.5 }}>
            <MarkdownView content={deliv.summary} />
          </div>
        </div>
      )}

      {/* Test Verification Summary */}
      {deliv.test_summary && (
        <div
          style={{
            padding: '8px 10px',
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-hairline)',
            background: 'var(--code-bg)',
          }}
          data-testid="deliverable-test-summary"
        >
          <div className="field-label" style={{ marginBottom: 4 }}>Automated Verification</div>
          <pre className="mono" style={{ margin: 0, fontSize: 11, whiteSpace: 'pre-wrap' }}>
            {deliv.test_summary}
          </pre>
        </div>
      )}

      {/* Dogfood Screenshots */}
      {shotIds.length > 0 && (
        <div data-testid="deliverable-screenshots">
          <div className="field-label">Preview Screenshots ({shotIds.length})</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(130px, 1fr))', gap: 8 }}>
            {shotIds.map((aid) => (
              <a
                key={aid}
                href={`/api/attachments/${aid}/raw`}
                target="_blank"
                rel="noreferrer"
                style={{
                  display: 'block',
                  borderRadius: 'var(--radius-sm)',
                  overflow: 'hidden',
                  border: '1px solid var(--border-hairline)',
                }}
              >
                <img
                  src={`/api/attachments/${aid}/raw`}
                  alt={`Deliverable preview ${aid}`}
                  style={{ width: '100%', height: 92, objectFit: 'cover', display: 'block' }}
                />
              </a>
            ))}
          </div>
        </div>
      )}

      {/* Interactive Unified & Side-by-Side Git Diff */}
      {diffPayload && diffPayload.files && diffPayload.files.length > 0 && (
        <div data-testid="deliverable-diff-section">
          <DiffView diff={diffPayload} compact />
        </div>
      )}

      {/* 1-Click Review Action Bar */}
      {!isRunning && item.column_key !== 'done' && (
        <div style={{ display: 'grid', gap: 8, paddingTop: 4 }}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              data-testid="merge-card-local-btn"
              disabled={Boolean(busyAction)}
              onClick={() => void mergeLocal()}
            >
              <GitMerge size={12} /> {branch ? 'Merge to Local Main & Complete' : 'Approve & Complete'}
            </button>

            {canPushPr && (
              <button
                type="button"
                className="btn btn-sm"
                data-testid="push-card-pr-btn"
                disabled={Boolean(busyAction)}
                onClick={() => void pushAndPr()}
              >
                <Send size={12} /> Push Branch &amp; Open PR
              </button>
            )}

            <button
              type="button"
              className="btn btn-sm"
              data-testid="request-card-changes-btn"
              disabled={Boolean(busyAction)}
              onClick={() => setShowRevisionForm((v) => !v)}
            >
              <RotateCcw size={12} /> Request Changes
            </button>

            {item.worktree_path && (
              <button
                type="button"
                className="btn btn-danger btn-sm"
                data-testid="discard-card-worktree-btn"
                disabled={Boolean(busyAction)}
                onClick={() => void discardWt()}
              >
                <Trash2 size={12} /> Discard Worktree
              </button>
            )}
          </div>

          {showRevisionForm && (
            <div style={{ display: 'grid', gap: 6 }}>
              <textarea
                className="textarea"
                rows={3}
                data-testid="request-changes-input"
                placeholder="Describe what Companion should adjust on this branch..."
                value={revisionNote}
                onChange={(e) => setRevisionNote(e.target.value)}
              />
              <button
                type="button"
                className="btn btn-primary btn-sm"
                data-testid="submit-card-changes-btn"
                disabled={!revisionNote.trim() || Boolean(busyAction)}
                onClick={() => void submitRevision()}
              >
                Send Back to Companion
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

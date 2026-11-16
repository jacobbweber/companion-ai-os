import React, { useEffect, useState } from 'react';
import { Scissors, Trash2 } from 'lucide-react';
import { callAction } from '../api';
import type {
  ProjectBoardProjection,
  ProjectGraphEntry,
  VerifiedDagSummary,
  WorkItem,
} from '../types';

interface WorkItemInspectorProps {
  mode: 'work_item' | 'project';
  itemId?: string;
  projectId?: string;
  onSaved: () => void;
  onClose: () => void;
  onToast: (msg: string) => void;
}

export const WorkItemInspector: React.FC<WorkItemInspectorProps> = ({
  mode,
  itemId,
  projectId,
  onSaved,
  onClose,
  onToast,
}) => {
  const [board, setBoard] = useState<ProjectBoardProjection | null>(null);
  const [dags, setDags] = useState<VerifiedDagSummary[]>([]);

  // Work-item fields
  const [title, setTitle] = useState('');
  const [desc, setDesc] = useState('');
  const [owner, setOwner] = useState('companion');
  const [status, setStatus] = useState('ready');
  const [stageKind, setStageKind] = useState('DELIVER_AND_VERIFY');
  const [milestoneId, setMilestoneId] = useState('');
  const [priority, setPriority] = useState(3);
  const [dependsOn, setDependsOn] = useState<string[]>([]);
  const [dagName, setDagName] = useState('');
  const [verKind, setVerKind] = useState('assertion');
  const [verTarget, setVerTarget] = useState('');
  const [plannedDate, setPlannedDate] = useState('');
  const [dueDate, setDueDate] = useState('');

  // Project fields
  const [projTitle, setProjTitle] = useState('');
  const [projGoal, setProjGoal] = useState('');
  const [tpMode, setTpMode] = useState('manual');
  const [tpWindow, setTpWindow] = useState('22:00-06:00');
  const [tpIdleSec, setTpIdleSec] = useState(900);
  const [tpCooldownSec, setTpCooldownSec] = useState(3600);
  const [tpReqSelfDev, setTpReqSelfDev] = useState(false);

  useEffect(() => {
    void Promise.all([
      callAction<ProjectBoardProjection>('agile', {
        subaction: 'board_view',
        project_id: projectId || '',
      }),
      callAction<{ dags?: VerifiedDagSummary[] }>('customizations', { subaction: 'list' }),
    ]).then(([bRes, cRes]) => {
      if (bRes.ok) {
        const view = bRes as unknown as ProjectBoardProjection;
        setBoard(view);
        const p: ProjectGraphEntry = view.project;
        if (p) {
          setProjTitle(p.title);
          setProjGoal(p.goal_summary || '');
          setTpMode(p.trigger_policy?.mode || 'manual');
          setTpWindow(p.trigger_policy?.allowed_window || '22:00-06:00');
          setTpIdleSec(p.trigger_policy?.idle_min_seconds ?? 900);
          setTpCooldownSec(p.trigger_policy?.cooldown_seconds ?? 3600);
          setTpReqSelfDev(Boolean(p.trigger_policy?.requires_self_dev));
        }
        if (mode === 'work_item' && itemId) {
          const found = (view.cards || []).find((c) => c.item_id === itemId);
          if (found) {
            setTitle(found.title);
            setDesc(found.description || '');
            setOwner(found.owner || 'companion');
            setStatus(found.status || 'ready');
            setStageKind(found.stage_kind || 'DELIVER_AND_VERIFY');
            setMilestoneId(found.milestone_id || '');
            setPriority(found.priority ?? 3);
            setDependsOn(found.depends_on || []);
            setDagName(found.dag_name || '');
            setVerKind(found.verification?.kind || 'assertion');
            setVerTarget(found.verification?.target || '');
            setPlannedDate(found.planned_date || '');
            setDueDate(found.due_date || '');
          }
        }
      }
      if (cRes.dags) setDags(cRes.dags);
    });
  }, [mode, itemId, projectId]);

  if (mode === 'project') {
    const saveProject = async () => {
      if (!board?.project) return;
      const res = await callAction('agile', {
        subaction: 'update_project',
        project_id: board.project.project_id,
        title: projTitle.trim(),
        goal_summary: projGoal.trim(),
        trigger_policy: {
          mode: tpMode,
          allowed_window: tpMode === 'on_idle' ? tpWindow.trim() : '',
          idle_min_seconds: tpIdleSec,
          cooldown_seconds: tpCooldownSec,
          precheck_name: tpMode === 'on_idle' ? 'active_goal_has_ready_items' : '',
          requires_self_dev: tpReqSelfDev,
        },
      });
      if (res.ok) {
        onSaved();
        onToast('Updated Goal / Project settings.');
      } else {
        onToast(res.error || 'Could not update project.');
      }
    };

    return (
      <div style={{ display: 'grid', gap: 12 }} data-testid="project-inspector">
        <div>
          <label className="field-label">Goal / Project Title</label>
          <input className="input" value={projTitle} onChange={(e) => setProjTitle(e.target.value)} />
        </div>
        <div>
          <label className="field-label">Goal Summary &amp; Outcome</label>
          <textarea className="textarea" rows={3} value={projGoal} onChange={(e) => setProjGoal(e.target.value)} />
        </div>
        <div>
          <label className="field-label">Autonomous Execution Policy</label>
          <select className="select" value={tpMode} onChange={(e) => setTpMode(e.target.value)}>
            <option value="manual">Manual (Only advance when asked)</option>
            <option value="on_idle">On Idle (Advance unblocked Companion slices automatically)</option>
          </select>
        </div>
        {tpMode === 'on_idle' && (
          <>
            <div>
              <label className="field-label">Allowed Time Window (Optional, e.g. 22:00-06:00)</label>
              <input className="input mono" placeholder="22:00-06:00 or leave blank for anytime" value={tpWindow} onChange={(e) => setTpWindow(e.target.value)} />
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <div>
                <label className="field-label">Min Idle Seconds</label>
                <input type="number" className="input" value={tpIdleSec} onChange={(e) => setTpIdleSec(Number(e.target.value))} />
              </div>
              <div>
                <label className="field-label">Cooldown Seconds</label>
                <input type="number" className="input" value={tpCooldownSec} onChange={(e) => setTpCooldownSec(Number(e.target.value))} />
              </div>
            </div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5 }}>
              <input type="checkbox" checked={tpReqSelfDev} onChange={(e) => setTpReqSelfDev(e.target.checked)} />
              <span>Requires Self-Dev Mode = ON (mutates Companion core)</span>
            </label>
          </>
        )}
        <button type="button" className="btn btn-primary" onClick={() => void saveProject()}>
          Save Goal Policy
        </button>
      </div>
    );
  }

  const siblingItems: WorkItem[] = (board?.cards || []).filter((c) => c.item_id !== itemId);

  const toggleDep = (depId: string) => {
    setDependsOn((prev) =>
      prev.includes(depId) ? prev.filter((x) => x !== depId) : [...prev, depId]
    );
  };

  const saveWorkItem = async () => {
    if (!itemId) return;
    const res = await callAction('agile', {
      subaction: 'update_item',
      item_id: itemId,
      title: title.trim(),
      description: desc,
      owner,
      status,
      stage_kind: stageKind,
      milestone_id: milestoneId,
      priority,
      depends_on: dependsOn,
      dag_name: dagName,
      verification: { kind: verKind, target: verTarget.trim() },
      planned_date: plannedDate,
      due_date: dueDate,
    });
    if (res.ok) {
      onSaved();
      onToast('Saved work item.');
    } else {
      onToast(res.error || 'Could not update work item.');
    }
  };

  const slice15MinStep = async () => {
    if (!board?.project || !itemId) return;
    const stepRes = await callAction<{ item?: WorkItem }>('agile', {
      subaction: 'create_item',
      project_id: board.project.project_id,
      title: `15-min first step: ${title.slice(0, 48)}`,
      owner: 'human',
      status: 'ready',
      estimate_minutes: 15,
    });
    if (stepRes.ok && stepRes.item) {
      const nextDeps = [...new Set([...dependsOn, stepRes.item.item_id])];
      await callAction('agile', {
        subaction: 'update_item',
        item_id: itemId,
        depends_on: nextDeps,
      });
      setDependsOn(nextDeps);
      onSaved();
      onToast('Created 15-minute human unblocker and linked dependency.');
    }
  };

  const removeItem = async () => {
    if (!itemId) return;
    await callAction('agile', { subaction: 'delete_item', item_id: itemId });
    onSaved();
    onClose();
    onToast('Deleted work item.');
  };

  return (
    <div style={{ display: 'grid', gap: 12 }} data-testid="work-item-inspector">
      <div>
        <label className="field-label">Title</label>
        <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
      </div>

      <div className="form-grid-2">
        <div>
          <label className="field-label">Owner</label>
          <select className="select" value={owner} onChange={(e) => setOwner(e.target.value)}>
            <option value="companion">Companion (Autonomous)</option>
            <option value="human">You (Human Action)</option>
            <option value="collaborative">Shared / Collaborative</option>
          </select>
        </div>
        <div>
          <label className="field-label">Status</label>
          <select className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="backlog">Backlog</option>
            <option value="ready">Ready</option>
            <option value="in_progress">In Progress</option>
            <option value="blocked">Blocked</option>
            <option value="verify">Verify</option>
            <option value="done">Done</option>
          </select>
        </div>
      </div>

      {(board?.milestones || []).length > 0 && (
        <div>
          <label className="field-label">Milestone</label>
          <select className="select" value={milestoneId} onChange={(e) => setMilestoneId(e.target.value)}>
            <option value="">Unassigned</option>
            {(board?.milestones || []).map((ms) => (
              <option key={ms.milestone_id} value={ms.milestone_id}>
                {ms.title}
              </option>
            ))}
          </select>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 90px', gap: 10 }}>
        <div>
          <label className="field-label">DIKW Prerequisite Stage</label>
          <select className="select" value={stageKind} onChange={(e) => setStageKind(e.target.value)}>
            <option value="OBSERVE_DATA">1. Observe Data</option>
            <option value="CURATE_DIKW">2. Curate Knowledge</option>
            <option value="FORGE_CAPABILITY">3. Forge Capability / DAG</option>
            <option value="DELIVER_AND_VERIFY">4. Deliver &amp; Verify</option>
          </select>
        </div>
        <div>
          <label className="field-label">Priority</label>
          <select className="select" value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
            {[1, 2, 3, 4, 5].map((p) => (
              <option key={p} value={p}>P{p}</option>
            ))}
          </select>
        </div>
      </div>

      <div>
        <label className="field-label">Notes &amp; Acceptance Criteria</label>
        <textarea className="textarea" rows={3} value={desc} onChange={(e) => setDesc(e.target.value)} />
      </div>

      <div>
        <label className="field-label">Bound Deterministic Python DAG (Optional)</label>
        <select className="select" value={dagName} onChange={(e) => setDagName(e.target.value)}>
          <option value="">None (Agentic phase slice)</option>
          {dags.map((d) => (
            <option key={d.name} value={d.name}>
              {d.name} ({d.skill})
            </option>
          ))}
        </select>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        <div>
          <label className="field-label">Verification Gate</label>
          <select className="select" value={verKind} onChange={(e) => setVerKind(e.target.value)}>
            <option value="assertion">Assertion</option>
            <option value="evidence_file">Evidence File</option>
            <option value="check_command">Check Command</option>
            <option value="domain_validator">Domain Validator</option>
            <option value="human_gate">Human Gate</option>
          </select>
        </div>
        <div>
          <label className="field-label">Gate Target</label>
          <input className="input mono" placeholder="path or validator" value={verTarget} onChange={(e) => setVerTarget(e.target.value)} />
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        <div>
          <label className="field-label">Planned Date</label>
          <input type="date" className="input" value={plannedDate} onChange={(e) => setPlannedDate(e.target.value)} />
        </div>
        <div>
          <label className="field-label">Due Date</label>
          <input type="date" className="input" value={dueDate} onChange={(e) => setDueDate(e.target.value)} />
        </div>
      </div>

      {siblingItems.length > 0 && (
        <div>
          <label className="field-label">Upstream Blockers</label>
          <div
            style={{
              maxHeight: 130,
              overflowY: 'auto',
              padding: 8,
              borderRadius: 'var(--radius-sm)',
              border: '1px solid var(--border-hairline)',
              background: 'var(--bg-input)',
              display: 'grid',
              gap: 5,
            }}
          >
            {siblingItems.map((sib) => (
              <label key={sib.item_id} style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12 }}>
                <input
                  type="checkbox"
                  checked={dependsOn.includes(sib.item_id)}
                  onChange={() => toggleDep(sib.item_id)}
                />
                <span>
                  [{sib.owner === 'human' ? 'You' : 'AI'}] {sib.title}
                </span>
              </label>
            ))}
          </div>
        </div>
      )}

      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, paddingTop: 4, flexWrap: 'wrap' }}>
        <button type="button" className="btn btn-sm" onClick={() => void slice15MinStep()}>
          <Scissors size={12} /> 15m First Step
        </button>
        <div style={{ display: 'inline-flex', gap: 6 }}>
          <button type="button" className="btn btn-danger btn-sm" onClick={() => void removeItem()}>
            <Trash2 size={12} />
          </button>
          <button type="button" className="btn btn-primary btn-sm" onClick={() => void saveWorkItem()}>
            Save Changes
          </button>
        </div>
      </div>
    </div>
  );
};

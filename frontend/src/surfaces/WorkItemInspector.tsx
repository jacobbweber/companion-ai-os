import React, { useEffect, useState } from 'react';
import { Scissors, Trash2 } from 'lucide-react';
import { callAction } from '../api';
import { DeliverableReviewPanel } from './DeliverableReviewPanel';
import { ProjectConfigPanel } from './ProjectConfigPanel';
import type {
  ProjectBoardProjection,
  VerifiedDagSummary,
  WorkItem,
} from '../types';

interface WorkItemInspectorProps {
  mode: 'work_item' | 'project';
  itemId?: string;
  projectId?: string;
  createNewProject?: boolean;
  onSaved: () => void;
  onCreatedProject?: (newProjectId: string) => void;
  onClose: () => void;
  onToast: (msg: string) => void;
}

export const WorkItemInspector: React.FC<WorkItemInspectorProps> = ({
  mode,
  itemId,
  projectId,
  createNewProject = false,
  onSaved,
  onCreatedProject,
  onClose,
  onToast,
}) => {
  const [board, setBoard] = useState<ProjectBoardProjection | null>(null);
  const [dags, setDags] = useState<VerifiedDagSummary[]>([]);
  const [loadedItem, setLoadedItem] = useState<WorkItem | null>(null);
  const [activeTab, setActiveTab] = useState<'deliverable' | 'edit'>('edit');

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
  const [origVerExtra, setOrigVerExtra] = useState<Record<string, unknown>>({});
  const [plannedDate, setPlannedDate] = useState('');
  const [dueDate, setDueDate] = useState('');

  const reloadWorkItem = async (resetDefaultTab = false) => {
    if (mode === 'project') return;
    const [bRes, cRes] = await Promise.all([
      callAction<ProjectBoardProjection>('agile', {
        subaction: 'board_view',
        project_id: projectId || '',
      }),
      callAction<{ dags?: VerifiedDagSummary[] }>('customizations', { subaction: 'list' }),
    ]);
    if (bRes.ok) {
      const view = bRes as unknown as ProjectBoardProjection;
      setBoard(view);
      if (itemId) {
        const found = (view.cards || []).find((c) => c.item_id === itemId) || null;
        setLoadedItem(found);
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
          setOrigVerExtra({ ...(found.verification || {}) } as Record<string, unknown>);
          setPlannedDate(found.planned_date || '');
          setDueDate(found.due_date || '');
          const showDeliv =
            found.column_key === 'in_progress' ||
            found.column_key === 'verify' ||
            Boolean(
              found.deliverable?.summary ||
                found.deliverable?.unified_diff ||
                found.deliverable?.changed_files?.length
            );
          if (resetDefaultTab) {
            setActiveTab(showDeliv ? 'deliverable' : 'edit');
          }
        }
      }
    }
    if (cRes.dags) setDags(cRes.dags);
  };

  useEffect(() => {
    void reloadWorkItem(true);
  }, [mode, itemId, projectId]);

  if (mode === 'project') {
    return (
      <ProjectConfigPanel
        projectId={projectId}
        createNew={createNewProject}
        onSaved={onSaved}
        onCreatedProject={onCreatedProject}
        onClose={onClose}
        onToast={onToast}
      />
    );
  }

  const hasDeliverable = Boolean(
    loadedItem &&
      (loadedItem.column_key === 'in_progress' ||
        loadedItem.column_key === 'verify' ||
        loadedItem.deliverable?.summary ||
        loadedItem.deliverable?.unified_diff ||
        loadedItem.deliverable?.changed_files?.length ||
        loadedItem.branch_name)
  );

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
      verification: { ...origVerExtra, kind: verKind, target: verTarget.trim() },
      planned_date: plannedDate,
      due_date: dueDate,
    });
    if (res.ok) {
      await reloadWorkItem(false);
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
      await reloadWorkItem(false);
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
      {hasDeliverable && (
        <div className="segmented-pills" style={{ width: '100%', display: 'grid', gridTemplateColumns: '1fr 1fr' }}>
          <button
            type="button"
            data-testid="inspector-tab-deliverable"
            className={`segmented-pill ${activeTab === 'deliverable' ? 'active' : ''}`}
            onClick={() => setActiveTab('deliverable')}
          >
            Deliverable
          </button>
          <button
            type="button"
            data-testid="inspector-tab-edit"
            className={`segmented-pill ${activeTab === 'edit' ? 'active' : ''}`}
            onClick={() => setActiveTab('edit')}
          >
            Edit Card
          </button>
        </div>
      )}

      {hasDeliverable && activeTab === 'deliverable' && loadedItem ? (
        <DeliverableReviewPanel
          item={loadedItem}
          project={board?.project}
          onChanged={() => {
            void reloadWorkItem(false);
            onSaved();
          }}
          onToast={onToast}
        />
      ) : (
        <>
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

          <div className="form-grid-2">
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

          <div className="form-grid-2">
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
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      [{sib.status}] {sib.title}
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}

          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', paddingTop: 4 }}>
            <button type="button" className="btn btn-primary" onClick={() => void saveWorkItem()}>
              Save Changes
            </button>
            <button type="button" className="btn" onClick={() => void slice15MinStep()} title="Create a 15-minute first step">
              <Scissors size={13} /> Slice 15m Unblocker
            </button>
            <button type="button" className="btn btn-danger" onClick={() => void removeItem()}>
              <Trash2 size={13} /> Delete
            </button>
          </div>
        </>
      )}
    </div>
  );
};

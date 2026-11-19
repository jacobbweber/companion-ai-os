import React, { useEffect, useState } from 'react';
import {
  CheckCircle2,
  Circle,
  GitBranch,
  Lock,
  Plus,
  SlidersHorizontal,
  Sparkles,
  User,
} from 'lucide-react';
import { callAction } from '../api';
import type {
  AuxInspectorTarget,
  MilestoneEntry,
  ProjectBoardProjection,
  WorkItem,
} from '../types';

interface BoardGoalsSurfaceProps {
  selectedProjectId: string;
  refreshNonce: number;
  selectedItemId?: string;
  onSelectProject: (projectId: string) => void;
  onInspect: (target: AuxInspectorTarget) => void;
  onRefreshStatus: () => Promise<void>;
  onToast: (msg: string) => void;
}

const OWNER_BADGE: Record<string, { label: string; cls: string }> = {
  human: { label: 'You', cls: 'warn' },
  companion: { label: 'Companion', cls: 'accent' },
  collaborative: { label: 'Shared', cls: 'ok' },
};

export const BoardGoalsSurface: React.FC<BoardGoalsSurfaceProps> = ({
  selectedProjectId,
  refreshNonce,
  selectedItemId,
  onSelectProject,
  onInspect,
  onRefreshStatus,
  onToast,
}) => {
  const [viewMode, setViewMode] = useState<'board' | 'list' | 'roadmap'>('board');
  const [ownerFilter, setOwnerFilter] = useState<'all' | 'human' | 'companion' | 'blocked'>('all');
  const [board, setBoard] = useState<ProjectBoardProjection | null>(null);
  const [quickDrafts, setQuickDrafts] = useState<Record<string, string>>({});
  const [quickOwner, setQuickOwner] = useState<'companion' | 'human'>('companion');
  const [newMilestoneTitle, setNewMilestoneTitle] = useState('');

  const loadBoard = async () => {
    const res = await callAction<ProjectBoardProjection>('agile', {
      subaction: 'board_view',
      project_id: selectedProjectId,
    });
    if (res.ok) {
      setBoard(res as unknown as ProjectBoardProjection);
    }
  };

  useEffect(() => {
    void loadBoard();
  }, [selectedProjectId, refreshNonce]);

  const addQuickItem = async (colKey: string) => {
    const title = (quickDrafts[colKey] || '').trim();
    if (!title || !board?.project) return;
    setQuickDrafts((p) => ({ ...p, [colKey]: '' }));
    const res = await callAction<{ item?: WorkItem }>('agile', {
      subaction: 'create_item',
      project_id: board.project.project_id,
      title,
      owner: quickOwner,
      status: colKey === 'backlog' ? 'backlog' : 'ready',
    });
    if (res.ok) {
      await loadBoard();
      await onRefreshStatus();
      if (res.item) {
        onInspect({ kind: 'work_item', itemId: res.item.item_id, projectId: board.project.project_id });
      }
    } else {
      onToast(res.error || 'Could not create item.');
    }
  };

  const toggleItemDone = async (e: React.MouseEvent, item: WorkItem) => {
    e.stopPropagation();
    const nextStatus = item.column_key === 'done' ? 'ready' : 'done';
    const res = await callAction('agile', {
      subaction: 'update_item',
      item_id: item.item_id,
      status: nextStatus,
    });
    if (res.ok) {
      await loadBoard();
      await onRefreshStatus();
      onToast(
        nextStatus === 'done'
          ? `Completed "${item.title}" (dependent items auto-recomputed).`
          : `Reopened "${item.title}".`
      );
    }
  };

  const seedDikwMilestones = async () => {
    if (!board?.project) return;
    const res = await callAction('agile', {
      subaction: 'seed_dikw',
      project_id: board.project.project_id,
    });
    await loadBoard();
    onToast(res.summary || 'Seeded 4 DIKW milestones.');
  };

  const createMilestone = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newMilestoneTitle.trim() || !board?.project) return;
    const res = await callAction('agile', {
      subaction: 'create_milestone',
      project_id: board.project.project_id,
      title: newMilestoneTitle.trim(),
    });
    setNewMilestoneTitle('');
    await loadBoard();
    onToast(res.summary || 'Created milestone.');
  };

  const matchesFilter = (it: WorkItem): boolean => {
    if (ownerFilter === 'all') return true;
    if (ownerFilter === 'blocked') return it.column_key === 'blocked';
    if (ownerFilter === 'human') return it.owner === 'human';
    return it.owner === 'companion' || it.owner === 'collaborative';
  };

  const proj = board?.project;
  const counts = board?.counts;
  const allItems = (board?.cards || []).filter(matchesFilter);
  const tp = proj?.trigger_policy;

  return (
    <div className="surface-scroll" data-testid="boards-surface">
      <div className="surface-container">
        {/* Top Clean Header & View Controls */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 12,
            flexWrap: 'wrap',
            marginBottom: 16,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <select
              className="select"
              style={{ width: 'auto', minWidth: 190, fontWeight: 600 }}
              value={proj?.project_id || selectedProjectId}
              onChange={(e) => onSelectProject(e.target.value)}
            >
              {(board?.boards || []).map((b) => (
                <option key={b.project_id} value={b.project_id}>
                  {b.title}
                </option>
              ))}
            </select>

            {proj && (
              <>
                <button
                  type="button"
                  className="btn btn-sm"
                  data-testid="configure-project-trigger-btn"
                  onClick={() => onInspect({ kind: 'project', projectId: proj.project_id })}
                  title="Configure Repository Workspace, Delivery Policy & Idle Window"
                >
                  <SlidersHorizontal size={12} />
                  <span>
                    Trigger: {tp?.mode === 'on_idle' ? `On Idle (${tp.allowed_window || 'anytime'})` : 'Manual'}
                  </span>
                </button>
                {(proj.workspace_path || (proj.delivery_policy && proj.delivery_policy !== 'local_only')) && (
                  <span className="badge" data-testid="project-delivery-badge" title={proj.workspace_path || 'OS Mode'}>
                    {proj.delivery_policy === 'remote_branch_pr'
                      ? 'Branch + PR'
                      : proj.delivery_policy === 'ask_me'
                        ? 'Ask Before Push'
                        : 'Local Only'}
                  </span>
                )}
              </>
            )}

            {counts && (
              <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                {counts.human_pending} on you • {counts.companion_ready} Companion ready •{' '}
                {counts.blocked} blocked
              </span>
            )}
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <div className="segmented-pills">
              {(
                [
                  ['all', 'All'],
                  ['human', 'On Me'],
                  ['companion', 'Companion'],
                  ['blocked', 'Blocked'],
                ] as const
              ).map(([id, lbl]) => (
                <button
                  key={id}
                  type="button"
                  className={`segmented-pill ${ownerFilter === id ? 'active' : ''}`}
                  onClick={() => setOwnerFilter(id)}
                >
                  {lbl}
                </button>
              ))}
            </div>

            <div className="segmented-pills">
              {(
                [
                  ['board', 'Board'],
                  ['list', 'List'],
                  ['roadmap', 'Milestones'],
                ] as const
              ).map(([id, lbl]) => (
                <button
                  key={id}
                  type="button"
                  className={`segmented-pill ${viewMode === id ? 'active' : ''}`}
                  onClick={() => setViewMode(id)}
                >
                  {lbl}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* View 1: Kanban Board Columns */}
        {viewMode === 'board' && (
          <div className="board-columns-grid">
            {(board?.columns || []).map((col) => {
              const colCards = col.cards.filter(matchesFilter);
              return (
                <div key={col.col_key} className="board-col">
                  <div className="board-col-head">
                    <span>{col.title}</span>
                    <span className="count-pill">{colCards.length}</span>
                  </div>

                  {colCards.map((it) => {
                    const ob = OWNER_BADGE[it.owner] || OWNER_BADGE.companion;
                    const isDone = it.column_key === 'done';
                    return (
                      <div
                        key={it.item_id}
                        role="button"
                        tabIndex={0}
                        data-testid={`work-card-${it.item_id}`}
                        className={`work-card ${selectedItemId === it.item_id ? 'selected' : ''}`}
                        onClick={() =>
                          onInspect({
                            kind: 'work_item',
                            itemId: it.item_id,
                            projectId: it.project_id,
                          })
                        }
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            onInspect({
                              kind: 'work_item',
                              itemId: it.item_id,
                              projectId: it.project_id,
                            });
                          }
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 7 }}>
                          <button
                            type="button"
                            style={{
                              background: 'none',
                              border: 'none',
                              padding: '1px 0 0',
                              cursor: 'pointer',
                              color: isDone ? 'var(--status-ok)' : 'var(--text-muted)',
                            }}
                            title={isDone ? 'Reopen item' : 'Mark item done'}
                            onClick={(e) => void toggleItemDone(e, it)}
                          >
                            {isDone ? <CheckCircle2 size={14} /> : <Circle size={14} />}
                          </button>
                          <div
                            className="work-card-title"
                            style={{
                              textDecoration: isDone ? 'line-through' : 'none',
                              opacity: isDone ? 0.68 : 1,
                              margin: 0,
                            }}
                          >
                            {it.title}
                          </div>
                        </div>

                        <div className="work-card-meta" style={{ marginTop: 8 }}>
                          <span className={`badge ${ob.cls}`}>
                            {it.owner === 'human' ? <User size={10} /> : <Sparkles size={10} />}
                            {ob.label}
                          </span>
                          {it.depends_on?.length > 0 && it.column_key !== 'done' && (
                            <span className="badge warn" title={`Depends on: ${it.depends_on.join(', ')}`}>
                              <Lock size={10} /> Deps ({it.depends_on.length})
                            </span>
                          )}
                          {it.dag_name && (
                            <span className="badge">
                              <GitBranch size={10} /> {it.dag_name}
                            </span>
                          )}
                        </div>
                      </div>
                    );
                  })}

                  {(col.col_key === 'ready' || col.col_key === 'backlog') && (
                    <div style={{ marginTop: 4, display: 'flex', flexDirection: 'column', gap: 5 }}>
                      <div style={{ display: 'flex', gap: 5 }}>
                        <input
                          type="text"
                          className="input"
                          style={{ fontSize: 12, padding: '5px 8px' }}
                          placeholder={`+ Add ${col.title.toLowerCase()} item...`}
                          value={quickDrafts[col.col_key] || ''}
                          onChange={(e) =>
                            setQuickDrafts((p) => ({ ...p, [col.col_key]: e.target.value }))
                          }
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') void addQuickItem(col.col_key);
                          }}
                        />
                        <button
                          type="button"
                          className="btn btn-sm"
                          title={`Owner: ${quickOwner} (click to switch)`}
                          onClick={() =>
                            setQuickOwner((o) => (o === 'companion' ? 'human' : 'companion'))
                          }
                        >
                          {quickOwner === 'human' ? 'You' : 'AI'}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {/* View 2: Clean Linear List View */}
        {viewMode === 'list' && (
          <div className="card-group">
            {allItems.length === 0 ? (
              <div className="card-row">
                <span style={{ color: 'var(--text-muted)' }}>No work items match the current filter.</span>
              </div>
            ) : (
              allItems.map((it) => {
                const ob = OWNER_BADGE[it.owner] || OWNER_BADGE.companion;
                return (
                  <div
                    key={it.item_id}
                    className="card-row"
                    style={{ cursor: 'pointer' }}
                    onClick={() =>
                      onInspect({ kind: 'work_item', itemId: it.item_id, projectId: it.project_id })
                    }
                  >
                    <div className="card-row-info" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                      <button
                        type="button"
                        style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
                        onClick={(e) => void toggleItemDone(e, it)}
                      >
                        {it.column_key === 'done' ? (
                          <CheckCircle2 size={15} style={{ color: 'var(--status-ok)' }} />
                        ) : (
                          <Circle size={15} style={{ color: 'var(--text-muted)' }} />
                        )}
                      </button>
                      <div>
                        <div className="card-row-title">{it.title}</div>
                        <div className="card-row-desc">
                          {it.stage_kind}
                          {it.depends_on?.length > 0 ? ` • Depends on ${it.depends_on.join(', ')}` : ''}
                        </div>
                      </div>
                    </div>
                    <div style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
                      <span className={`badge ${ob.cls}`}>{ob.label}</span>
                      <span className="badge">{it.status}</span>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        )}

        {/* View 3: Milestones & DIKW Prerequisite Stages */}
        {viewMode === 'roadmap' && (
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
              <form onSubmit={(e) => void createMilestone(e)} style={{ display: 'flex', gap: 8, flex: 1, maxWidth: 420 }}>
                <input
                  className="input"
                  placeholder="New milestone title..."
                  value={newMilestoneTitle}
                  onChange={(e) => setNewMilestoneTitle(e.target.value)}
                />
                <button type="button" className="btn btn-primary btn-sm" onClick={(e) => void createMilestone(e)}>
                  <Plus size={12} /> Add
                </button>
              </form>
              <button type="button" className="btn btn-sm" onClick={() => void seedDikwMilestones()}>
                <Sparkles size={12} /> Seed 4 DIKW Stages
              </button>
            </div>

            <div className="card-group">
              {(board?.milestones || []).length === 0 ? (
                <div className="card-row">
                  <span style={{ color: 'var(--text-muted)' }}>
                    No milestones yet — add one or click &ldquo;Seed 4 DIKW Stages&rdquo; (Observe &rarr; Curate &rarr; Forge &rarr; Deliver).
                  </span>
                </div>
              ) : (
                (board?.milestones || []).map((ms: MilestoneEntry) => {
                  const msCards = (board?.cards || []).filter((c) => c.milestone_id === ms.milestone_id);
                  const doneCnt = msCards.filter((c) => c.column_key === 'done').length;
                  return (
                    <div key={ms.milestone_id} className="card-row">
                      <div className="card-row-info">
                        <div className="card-row-title">{ms.title}</div>
                        <div className="card-row-desc">
                          Stage: {ms.stage_kind}
                          {ms.target_date ? ` • Target: ${ms.target_date}` : ''}
                        </div>
                      </div>
                      <span className="badge accent">
                        {doneCnt}/{msCards.length} done
                      </span>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

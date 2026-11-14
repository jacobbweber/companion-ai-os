import React, { useEffect, useState } from 'react';
import {
  AlertTriangle,
  Calendar,
  Compass,
  FolderKanban,
  Pencil,
  Plus,
  RefreshCw,
  Scissors,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { callAction } from '../api';
import type { AgileBoardView, AgileCard } from '../types';

interface BoardsStudioProps {
  onOpenWikiNote: (path: string) => void;
  onRefreshStatus: () => void;
  onToast: (msg: string) => void;
}

const COLUMN_META: Array<{ id: string; label: string }> = [
  { id: 'backlog', label: 'Backlog' },
  { id: 'sprint_todo', label: 'Sprint To-Do' },
  { id: 'in_progress', label: 'In Progress' },
  { id: 'blocked', label: 'Blocked' },
  { id: 'verify', label: 'Verify' },
  { id: 'done', label: 'Done' },
];

const HORIZON_META: Array<{ id: string; label: string; sub: string }> = [
  { id: 'now', label: 'NOW', sub: 'Active / This Month' },
  { id: 'next', label: 'NEXT', sub: '1–3 Months' },
  { id: 'later', label: 'LATER', sub: 'Quarter / Year' },
  { id: 'someday', label: 'SOMEDAY', sub: 'Ideas & Renovations' },
];

export const BoardsStudio: React.FC<BoardsStudioProps> = ({
  onOpenWikiNote,
  onRefreshStatus,
  onToast,
}) => {
  const [selectedBoard, setSelectedBoard] = useState('all-active-sprint');
  const [mode, setMode] = useState<'kanban' | 'horizons' | 'aging'>('kanban');
  const [view, setView] = useState<AgileBoardView | null>(null);
  const [showNewBoard, setShowNewBoard] = useState(false);

  // New board form
  const [newBoardTitle, setNewBoardTitle] = useState('');
  const [newBoardKind, setNewBoardKind] = useState('project');

  // Sprint form
  const [sprintTitle, setSprintTitle] = useState('');
  const [weekdayCap, setWeekdayCap] = useState(10);
  const [weekendCap, setWeekendCap] = useState(6);

  // Quick Add Card form
  const [cardTitle, setCardTitle] = useState('');
  const [cardBoard, setCardBoard] = useState('life-home');
  const [cardStatus, setCardStatus] = useState('sprint_todo');
  const [cardEffort, setCardEffort] = useState('quick');
  const [cardDaySlot, setCardDaySlot] = useState('weekday');
  const [cardHorizon, setCardHorizon] = useState('now');
  const [cardPlannedDate, setCardPlannedDate] = useState('');
  const [cardDueDate, setCardDueDate] = useState('');

  // Epic form
  const [epicTitle, setEpicTitle] = useState('');
  const [epicHorizon, setEpicHorizon] = useState('later');
  const [epicTargetDate, setEpicTargetDate] = useState('');

  // Custom unblocker step titles per stalled card
  const [unblockerSteps, setUnblockerSteps] = useState<Record<string, string>>({});

  // Inline card editor state
  const [editingCardId, setEditingCardId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState<{
    title: string;
    effort_size: string;
    day_slot: string;
    horizon: string;
    planned_date: string;
    due_date: string;
    blocked_reason: string;
  }>({
    title: '',
    effort_size: 'quick',
    day_slot: 'weekday',
    horizon: 'now',
    planned_date: '',
    due_date: '',
    blocked_reason: '',
  });

  const loadBoard = async (boardId = selectedBoard) => {
    const res = await callAction<AgileBoardView>('agile', {
      subaction: 'board_view',
      board_id: boardId,
    });
    if (res.ok) {
      setView(res);
    }
  };

  useEffect(() => {
    void loadBoard(selectedBoard);
    if (selectedBoard !== 'all-active-sprint') {
      setCardBoard(selectedBoard);
    }
  }, [selectedBoard]);

  const createBoard = async () => {
    if (!newBoardTitle.trim()) return;
    const res = await callAction<{ board_id?: string }>('agile', {
      subaction: 'save_board',
      title: newBoardTitle.trim(),
      kind: newBoardKind,
    });
    if (res.ok && res.board_id) {
      setNewBoardTitle('');
      setShowNewBoard(false);
      setSelectedBoard(res.board_id);
      await loadBoard(res.board_id);
      onRefreshStatus();
      onToast(res.summary || 'Created board.');
    } else {
      onToast(res.error || 'Could not create board.');
    }
  };

  const startSprint = async () => {
    const res = await callAction('agile', {
      subaction: 'start_sprint',
      title: sprintTitle.trim(),
      weekday_capacity_slots: Number(weekdayCap) || 10,
      weekend_capacity_slots: Number(weekendCap) || 6,
    });
    if (res.ok) {
      setSprintTitle('');
      await loadBoard();
      onRefreshStatus();
      onToast(res.summary || 'Started active sprint.');
    } else {
      onToast(res.error || 'Could not start sprint.');
    }
  };

  const rolloverActiveSprint = async () => {
    const res = await callAction('agile', {
      subaction: 'rollover_sprint',
      start_next: true,
    });
    if (res.ok) {
      await loadBoard();
      onRefreshStatus();
      onToast(res.summary || 'Rolled over sprint.');
    } else {
      onToast(res.error || 'Could not rollover sprint.');
    }
  };

  const addCard = async () => {
    if (!cardTitle.trim()) return;
    const targetBoard =
      selectedBoard === 'all-active-sprint' ? cardBoard || 'life-home' : selectedBoard;
    const res = await callAction('agile', {
      subaction: 'save_card',
      title: cardTitle.trim(),
      board_id: targetBoard,
      status: cardStatus,
      effort_size: cardEffort,
      day_slot: cardDaySlot,
      horizon: cardHorizon,
      planned_date: cardPlannedDate.trim(),
      due_date: cardDueDate.trim(),
    });
    if (res.ok) {
      setCardTitle('');
      setCardPlannedDate('');
      setCardDueDate('');
      await loadBoard();
      onRefreshStatus();
      onToast(res.summary || 'Added card.');
    } else {
      onToast(res.error || 'Could not add card.');
    }
  };

  const startEditCard = (card: AgileCard) => {
    if (editingCardId === card.card_id) {
      setEditingCardId(null);
      return;
    }
    setEditingCardId(card.card_id);
    setEditDraft({
      title: card.title,
      effort_size: card.effort_size,
      day_slot: card.day_slot,
      horizon: card.horizon,
      planned_date: card.planned_date || '',
      due_date: card.due_date || '',
      blocked_reason: card.blocked_reason || '',
    });
  };

  const saveCardEdits = async (card: AgileCard) => {
    if (!editDraft.title.trim()) return;
    const res = await callAction('agile', {
      subaction: 'update_card',
      card_id: card.card_id,
      board_id: card.board_id,
      title: editDraft.title.trim(),
      status: card.status,
      effort_size: editDraft.effort_size,
      day_slot: editDraft.day_slot,
      horizon: editDraft.horizon,
      planned_date: editDraft.planned_date.trim(),
      due_date: editDraft.due_date.trim(),
      blocked_reason: editDraft.blocked_reason.trim(),
    });
    if (res.ok) {
      setEditingCardId(null);
      await loadBoard();
      onRefreshStatus();
      onToast(res.summary || 'Updated card.');
    } else {
      onToast(res.error || 'Could not update card.');
    }
  };

  const moveCard = async (card: AgileCard, nextStatus: string) => {
    const res = await callAction('agile', {
      subaction: 'move_card',
      card_id: card.card_id,
      status: nextStatus,
    });
    if (res.ok) {
      await loadBoard();
      onRefreshStatus();
      if (nextStatus === 'blocked' && !card.blocked_reason) {
        startEditCard({ ...card, status: 'blocked' });
      }
    } else {
      onToast(res.error || 'Cannot move card.');
    }
  };

  const removeCard = async (cardId: string) => {
    const res = await callAction('agile', {
      subaction: 'delete_card',
      card_id: cardId,
    });
    if (res.ok) {
      await loadBoard();
      onRefreshStatus();
    }
  };

  const sliceUnblocker = async (card: AgileCard) => {
    const stepText = (unblockerSteps[card.card_id] || '').trim();
    const res = await callAction('agile', {
      subaction: 'slice_unblocker',
      card_id: card.card_id,
      first_step_title: stepText,
      day_slot: 'weekday',
    });
    if (res.ok) {
      setUnblockerSteps((prev) => ({ ...prev, [card.card_id]: '' }));
      await loadBoard();
      onRefreshStatus();
      onToast(res.summary || 'Sliced 15-minute unblocker card into active sprint!');
    } else {
      onToast(res.error || 'Could not slice unblocker card.');
    }
  };

  const saveEpic = async () => {
    if (!epicTitle.trim()) return;
    const targetBoard =
      selectedBoard === 'all-active-sprint' ? cardBoard || 'life-home' : selectedBoard;
    const res = await callAction('agile', {
      subaction: 'save_epic',
      title: epicTitle.trim(),
      board_id: targetBoard,
      horizon: epicHorizon,
      target_date: epicTargetDate.trim(),
    });
    if (res.ok) {
      setEpicTitle('');
      setEpicTargetDate('');
      await loadBoard();
      onToast(res.summary || 'Saved epic.');
    } else {
      onToast(res.error || 'Could not save epic.');
    }
  };

  const boards = view?.boards || [];
  const cap = view?.capacity;
  const agingCards = view?.aging_cards || [];

  const renderCardTile = (c: AgileCard) => (
    <div
      key={c.card_id}
      className={`kanban-card ${c.is_aging ? 'aging' : ''}`}
      data-testid={`agile-card-${c.card_id}`}
    >
      <div className="kanban-card-top">
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', alignItems: 'center' }}>
          {selectedBoard === 'all-active-sprint' && (
            <span className="badge mono">{c.board_title || c.board_id}</span>
          )}
          <span className="badge accent mono">
            {c.effort_size}/{c.day_slot} ({c.slot_points}s)
          </span>
          {c.is_aging && (
            <span className="badge warn mono" title="Untouched >=14 days or >=2 sprint rollovers">
              AGING {c.untouched_days}D
            </span>
          )}
          {c.workflow_id != null && <span className="badge ok mono">WF #{c.workflow_id}</span>}
        </div>
        <div style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
          <button
            type="button"
            className="attachment-remove-btn"
            title="Edit card details & dates"
            aria-label={`Edit ${c.title}`}
            onClick={() => startEditCard(c)}
          >
            <Pencil size={12} />
          </button>
          <button
            type="button"
            className="attachment-remove-btn"
            title="Delete card"
            aria-label={`Delete ${c.title}`}
            onClick={() => void removeCard(c.card_id)}
          >
            <Trash2 size={12} />
          </button>
        </div>
      </div>

      <div className="kanban-card-title">{c.title}</div>

      {c.description && <div className="kanban-card-desc">{c.description}</div>}

      {(c.planned_date || c.due_date) && (
        <div className="kanban-card-dates mono">
          {c.planned_date && (
            <span>
              <Calendar size={10} /> Plan: {c.planned_date}
            </span>
          )}
          {c.due_date && <span className="warn-text">Due: {c.due_date}</span>}
        </div>
      )}

      {c.blocked_reason && (
        <div className="kanban-card-alert mono">Blocked: {c.blocked_reason}</div>
      )}

      {c.unresolved_deps && c.unresolved_deps.length > 0 && (
        <div className="kanban-card-alert mono">
          Waiting on: {c.unresolved_deps.map((d) => d.title).join(', ')}
        </div>
      )}

      <div className="kanban-card-actions">
        <select
          aria-label={`Status for ${c.title}`}
          className="select"
          style={{ padding: '3px 6px', fontSize: 11 }}
          value={c.status}
          onChange={(e) => void moveCard(c, e.target.value)}
        >
          {COLUMN_META.map((col) => (
            <option key={col.id} value={col.id}>
              {col.label}
            </option>
          ))}
        </select>

        {c.status !== 'done' && (
          <button
            type="button"
            className="btn"
            style={{ padding: '3px 7px', fontSize: 11 }}
            title="Break procrastination: create a quick 15-minute first-step card in the active sprint"
            onClick={() => void sliceUnblocker(c)}
          >
            <Scissors size={11} /> Slice 15m Unblocker
          </button>
        )}
      </div>

      {editingCardId === c.card_id && (
        <div
          style={{
            marginTop: 8,
            paddingTop: 8,
            borderTop: '1px solid var(--border-hairline)',
            display: 'grid',
            gap: 6,
          }}
          data-testid={`edit-card-panel-${c.card_id}`}
        >
          <input
            className="input"
            style={{ padding: '4px 7px', fontSize: 11.5 }}
            aria-label="Edit card title"
            value={editDraft.title}
            onChange={(e) => setEditDraft((d) => ({ ...d, title: e.target.value }))}
          />
          <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
            <select
              aria-label="Edit card effort"
              className="select"
              style={{ padding: '3px 6px', fontSize: 11 }}
              value={editDraft.effort_size}
              onChange={(e) => setEditDraft((d) => ({ ...d, effort_size: e.target.value }))}
            >
              <option value="quick">Quick (&lt;30m • 1s)</option>
              <option value="half_day">Half-Day (2s)</option>
              <option value="full_day">Full-Day (4s)</option>
              <option value="multi_day">Multi-Day (8s)</option>
            </select>
            <select
              aria-label="Edit card day slot"
              className="select"
              style={{ padding: '3px 6px', fontSize: 11 }}
              value={editDraft.day_slot}
              onChange={(e) => setEditDraft((d) => ({ ...d, day_slot: e.target.value }))}
            >
              <option value="weekday">Weekday</option>
              <option value="weekend">Weekend</option>
              <option value="any">Any Day</option>
            </select>
            <select
              aria-label="Edit card horizon"
              className="select"
              style={{ padding: '3px 6px', fontSize: 11 }}
              value={editDraft.horizon}
              onChange={(e) => setEditDraft((d) => ({ ...d, horizon: e.target.value }))}
            >
              <option value="now">Now</option>
              <option value="next">Next</option>
              <option value="later">Later</option>
              <option value="someday">Someday</option>
            </select>
          </div>
          <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
            <input
              className="input"
              style={{ flex: '1 1 110px', padding: '4px 7px', fontSize: 11 }}
              placeholder="Plan YYYY-MM-DD"
              aria-label="Edit planned date"
              value={editDraft.planned_date}
              onChange={(e) => setEditDraft((d) => ({ ...d, planned_date: e.target.value }))}
            />
            <input
              className="input"
              style={{ flex: '1 1 110px', padding: '4px 7px', fontSize: 11 }}
              placeholder="Due YYYY-MM-DD"
              aria-label="Edit due date"
              value={editDraft.due_date}
              onChange={(e) => setEditDraft((d) => ({ ...d, due_date: e.target.value }))}
            />
          </div>
          {c.status === 'blocked' && (
            <input
              className="input"
              style={{ padding: '4px 7px', fontSize: 11 }}
              placeholder="Blocked reason (e.g. Waiting on brake pads delivery)"
              aria-label="Edit blocked reason"
              value={editDraft.blocked_reason}
              onChange={(e) => setEditDraft((d) => ({ ...d, blocked_reason: e.target.value }))}
            />
          )}
          <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
            <button
              type="button"
              className="btn"
              style={{ padding: '3px 8px', fontSize: 11 }}
              onClick={() => setEditingCardId(null)}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              style={{ padding: '3px 8px', fontSize: 11 }}
              onClick={() => void saveCardEdits(c)}
            >
              Save Card
            </button>
          </div>
        </div>
      )}
    </div>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="studio-header">
        <div className="studio-title-group">
          <h1>Universal Agile &amp; Kanban Boards</h1>
          <p>
            Life, Work &amp; Software boards unified under realistic Weekday vs. Weekend slot
            budgets &amp; anti-procrastination slicing
          </p>
        </div>
        <div className="studio-actions">
          <button
            type="button"
            className={`btn ${mode === 'kanban' ? 'btn-primary' : ''}`}
            onClick={() => setMode('kanban')}
          >
            <FolderKanban size={13} /> Kanban Columns
          </button>
          <button
            type="button"
            className={`btn ${mode === 'horizons' ? 'btn-primary' : ''}`}
            onClick={() => setMode('horizons')}
          >
            <Compass size={13} /> Horizon Roadmap
          </button>
          <button
            type="button"
            className={`btn ${mode === 'aging' ? 'btn-primary' : ''}`}
            onClick={() => setMode('aging')}
          >
            <AlertTriangle size={13} /> Aging Radar ({agingCards.length})
          </button>
          <button
            type="button"
            className={`btn ${showNewBoard ? 'btn-primary' : ''}`}
            onClick={() => setShowNewBoard((v) => !v)}
          >
            <Plus size={13} /> New Board
          </button>
          <button type="button" className="btn" onClick={() => void loadBoard()}>
            <RefreshCw size={13} />
          </button>
        </div>
      </div>

      <div className="studio-scroll">
        {/* Board Switcher Pills */}
        <div className="board-switcher-bar">
          <button
            type="button"
            className={`btn ${selectedBoard === 'all-active-sprint' ? 'btn-primary' : ''}`}
            onClick={() => setSelectedBoard('all-active-sprint')}
          >
            <Sparkles size={12} /> All Active Sprint
          </button>
          {boards.map((b) => (
            <button
              key={b.board_id}
              type="button"
              className={`btn ${selectedBoard === b.board_id ? 'btn-primary' : ''}`}
              onClick={() => setSelectedBoard(b.board_id)}
            >
              {b.title} ({b.open_cards})
            </button>
          ))}
        </div>

        {showNewBoard && (
          <div className="panel" style={{ marginTop: 10 }}>
            <div className="panel-head">
              <span className="panel-title">Create Reusable Kanban Board</span>
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <input
                className="input"
                style={{ flex: '1 1 220px' }}
                placeholder="Board Title (e.g. Basement Renovation, Homelab Upgrade)"
                value={newBoardTitle}
                onChange={(e) => setNewBoardTitle(e.target.value)}
              />
              <select
                aria-label="Board kind"
                className="select"
                value={newBoardKind}
                onChange={(e) => setNewBoardKind(e.target.value)}
              >
                <option value="project">Project</option>
                <option value="life">Life &amp; Home</option>
                <option value="work">Work &amp; Career</option>
                <option value="software">Software Engineering</option>
              </select>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!newBoardTitle.trim()}
                onClick={() => void createBoard()}
              >
                Create Board
              </button>
            </div>
          </div>
        )}

        {/* Active Sprint & Capacity Bar */}
        <div className="panel" style={{ marginTop: 10 }}>
          {cap?.has_active_sprint && cap.sprint ? (
            <div>
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  gap: 10,
                  flexWrap: 'wrap',
                }}
              >
                <div>
                  <strong style={{ fontSize: 13.5 }}>{cap.sprint.title}</strong>{' '}
                  <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-muted)' }}>
                    ({cap.sprint.start_date} &rarr; {cap.sprint.end_date})
                  </span>{' '}
                  <span className="badge ok mono">
                    DONE {cap.done_count}/{cap.total_count}
                  </span>
                </div>

                <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                  <div className="slot-meter mono">
                    <span>WEEKDAY SLOTS:</span>
                    <strong
                      style={{
                        color:
                          cap.weekday_used > cap.weekday_capacity
                            ? 'var(--status-danger)'
                            : 'var(--accent-primary)',
                      }}
                    >
                      {cap.weekday_used} / {cap.weekday_capacity}
                    </strong>
                  </div>
                  <div className="slot-meter mono">
                    <span>WEEKEND SLOTS:</span>
                    <strong
                      style={{
                        color:
                          cap.weekend_used > cap.weekend_capacity
                            ? 'var(--status-danger)'
                            : 'var(--accent-primary)',
                      }}
                    >
                      {cap.weekend_used} / {cap.weekend_capacity}
                    </strong>
                  </div>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => void rolloverActiveSprint()}
                  >
                    Complete &amp; Rollover Sprint
                  </button>
                </div>
              </div>

              {cap.warnings.length > 0 && (
                <div className="sprint-warning-strip mono">
                  <AlertTriangle size={13} /> {cap.warnings.join(' • ')}
                </div>
              )}
            </div>
          ) : (
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                gap: 8,
                flexWrap: 'wrap',
              }}
            >
              <div style={{ fontSize: 12.5, color: 'var(--text-secondary)' }}>
                <strong>No Active Weekly Sprint</strong> — Start a sprint to track realistic Weekday
                evenings vs. Weekend capacity slots across all boards.
              </div>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                <input
                  className="input"
                  placeholder="Sprint Title (optional, e.g. Week 46 Focus)"
                  value={sprintTitle}
                  onChange={(e) => setSprintTitle(e.target.value)}
                />
                <label className="hud-chip mono">
                  <span>WD:</span>
                  <input
                    type="number"
                    aria-label="Weekday capacity slots"
                    className="input"
                    style={{ width: 54, padding: '3px 6px' }}
                    value={weekdayCap}
                    onChange={(e) => setWeekdayCap(Number(e.target.value))}
                  />
                </label>
                <label className="hud-chip mono">
                  <span>WE:</span>
                  <input
                    type="number"
                    aria-label="Weekend capacity slots"
                    className="input"
                    style={{ width: 54, padding: '3px 6px' }}
                    value={weekendCap}
                    onChange={(e) => setWeekendCap(Number(e.target.value))}
                  />
                </label>
                <button type="button" className="btn btn-primary" onClick={() => void startSprint()}>
                  Start Sprint
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Quick Add Card Composer */}
        <div className="panel">
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <input
              className="input"
              style={{ flex: '2 1 240px' }}
              placeholder="Add task or backlog card (e.g. Take truck in for brake check, Clean gutters)..."
              value={cardTitle}
              onChange={(e) => setCardTitle(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void addCard()}
            />
            {selectedBoard === 'all-active-sprint' && (
              <select
                aria-label="Target board"
                className="select"
                value={cardBoard}
                onChange={(e) => setCardBoard(e.target.value)}
              >
                {boards.map((b) => (
                  <option key={b.board_id} value={b.board_id}>
                    {b.title}
                  </option>
                ))}
              </select>
            )}
            <select
              aria-label="Initial column"
              className="select"
              value={cardStatus}
              onChange={(e) => setCardStatus(e.target.value)}
            >
              <option value="sprint_todo">Sprint To-Do</option>
              <option value="backlog">Backlog</option>
              <option value="in_progress">In Progress</option>
            </select>
            <select
              aria-label="Effort size"
              className="select"
              value={cardEffort}
              onChange={(e) => setCardEffort(e.target.value)}
            >
              <option value="quick">Quick &lt;30m (1s)</option>
              <option value="half_day">Half-Day (2s)</option>
              <option value="full_day">Full-Day (4s)</option>
              <option value="multi_day">Multi-Day (8s)</option>
            </select>
            <select
              aria-label="Day slot"
              className="select"
              value={cardDaySlot}
              onChange={(e) => setCardDaySlot(e.target.value)}
            >
              <option value="weekday">Weekday</option>
              <option value="weekend">Weekend</option>
              <option value="any">Any Day</option>
            </select>
            <select
              aria-label="Planning horizon"
              className="select"
              value={cardHorizon}
              onChange={(e) => setCardHorizon(e.target.value)}
            >
              <option value="now">Horizon: NOW</option>
              <option value="next">Horizon: NEXT</option>
              <option value="later">Horizon: LATER</option>
              <option value="someday">Horizon: SOMEDAY</option>
            </select>
            <input
              className="input"
              style={{ width: 132 }}
              placeholder="Plan YYYY-MM-DD"
              aria-label="Planned date"
              value={cardPlannedDate}
              onChange={(e) => setCardPlannedDate(e.target.value)}
            />
            <input
              className="input"
              style={{ width: 132 }}
              placeholder="Due YYYY-MM-DD"
              aria-label="Due date"
              value={cardDueDate}
              onChange={(e) => setCardDueDate(e.target.value)}
            />
            <button
              type="button"
              className="btn btn-primary"
              disabled={!cardTitle.trim()}
              onClick={() => void addCard()}
            >
              <Plus size={13} /> Add Card
            </button>
          </div>
        </div>

        {/* Mode 1: Kanban Columns */}
        {mode === 'kanban' && (
          <div className="kanban-grid">
            {COLUMN_META.map((col) => {
              const colCards = view?.columns?.[col.id] || [];
              return (
                <div key={col.id} className="kanban-col" data-testid={`kanban-col-${col.id}`}>
                  <div className="kanban-col-head">
                    <span>{col.label}</span>
                    <span className="badge mono">{colCards.length}</span>
                  </div>
                  <div className="kanban-col-body">
                    {colCards.length === 0 ? (
                      <div className="kanban-empty mono">Empty</div>
                    ) : (
                      colCards.map((c) => renderCardTile(c))
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Mode 2: Horizon Roadmap */}
        {mode === 'horizons' && (
          <div>
            <div className="panel">
              <div className="panel-head">
                <span className="panel-title">Long-Range Projects &amp; Epics ({(view?.epics || []).length})</span>
              </div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
                <input
                  className="input"
                  style={{ flex: '2 1 240px' }}
                  placeholder="Epic / Big Goal (e.g. Clean & Renovate Basement)"
                  value={epicTitle}
                  onChange={(e) => setEpicTitle(e.target.value)}
                />
                <select
                  aria-label="Epic horizon"
                  className="select"
                  value={epicHorizon}
                  onChange={(e) => setEpicHorizon(e.target.value)}
                >
                  <option value="now">NOW</option>
                  <option value="next">NEXT (1–3 mo)</option>
                  <option value="later">LATER (3–12 mo)</option>
                  <option value="someday">SOMEDAY</option>
                </select>
                <input
                  className="input"
                  style={{ width: 150 }}
                  placeholder="Target YYYY-MM-DD"
                  value={epicTargetDate}
                  onChange={(e) => setEpicTargetDate(e.target.value)}
                />
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={!epicTitle.trim()}
                  onClick={() => void saveEpic()}
                >
                  Save Epic
                </button>
              </div>
              {(view?.epics || []).length > 0 && (
                <div className="grid-3">
                  {(view?.epics || []).map((ep) => (
                    <div
                      key={ep.epic_id}
                      style={{
                        padding: '8px 10px',
                        border: '1px solid var(--border-hairline)',
                        background: 'var(--bg-elevated)',
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 6 }}>
                        <strong>{ep.title}</strong>
                        <span className="badge accent mono">{ep.horizon}</span>
                      </div>
                      <div className="mono" style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
                        Board: {ep.board_id} {ep.target_date ? `• Target: ${ep.target_date}` : ''}
                      </div>
                      {ep.wiki_path && (
                        <button
                          type="button"
                          className="md-wikilink"
                          style={{ marginTop: 6 }}
                          onClick={() => onOpenWikiNote(ep.wiki_path)}
                        >
                          Open Epic Wiki Spec
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="horizon-grid">
              {HORIZON_META.map((hz) => {
                const hzCards = view?.horizons?.[hz.id] || [];
                return (
                  <div key={hz.id} className="kanban-col">
                    <div className="kanban-col-head">
                      <div>
                        <div>{hz.label}</div>
                        <div style={{ fontSize: 10, color: 'var(--text-muted)', fontWeight: 400 }}>
                          {hz.sub}
                        </div>
                      </div>
                      <span className="badge mono">{hzCards.length}</span>
                    </div>
                    <div className="kanban-col-body">
                      {hzCards.length === 0 ? (
                        <div className="kanban-empty mono">No items</div>
                      ) : (
                        hzCards.map((c) => renderCardTile(c))
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Mode 3: Aging & Procrastination Radar */}
        {mode === 'aging' && (
          <div className="panel">
            <div className="panel-head">
              <span className="panel-title">
                <AlertTriangle size={13} /> Zero-Guilt Procrastination &amp; Aging Radar
              </span>
              <span className="badge warn mono">{agingCards.length} STALLED</span>
            </div>
            <p style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginTop: 0 }}>
              Cards untouched for &ge;14 days or rolled over &ge;2 sprints appear here. Instead of
              carrying guilt, slice a tiny 15-minute unblocker step into this week&apos;s sprint.
            </p>
            {agingCards.length === 0 ? (
              <div style={{ color: 'var(--text-muted)', fontSize: 12.5 }}>
                Zero procrastinated or stalled cards right now!
              </div>
            ) : (
              agingCards.map((c) => (
                <div
                  key={c.card_id}
                  style={{
                    padding: '10px 12px',
                    border: '1px solid var(--status-warn)',
                    background: 'var(--bg-elevated)',
                    marginBottom: 8,
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    gap: 10,
                    flexWrap: 'wrap',
                  }}
                >
                  <div>
                    <strong>{c.title}</strong>{' '}
                    <span className="badge warn mono">{c.untouched_days}d untouched</span>{' '}
                    {c.rollover_count > 0 && (
                      <span className="badge danger mono">{c.rollover_count}x rolled over</span>
                    )}
                    <div className="mono" style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
                      Board: {c.board_title || c.board_id} • Effort: {c.effort_size}/{c.day_slot}
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                    <input
                      className="input"
                      style={{ width: 260 }}
                      placeholder={`15m step (e.g. Call shop for ${c.title.slice(0, 20)})`}
                      value={unblockerSteps[c.card_id] || ''}
                      onChange={(e) =>
                        setUnblockerSteps((prev) => ({ ...prev, [c.card_id]: e.target.value }))
                      }
                    />
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={() => void sliceUnblocker(c)}
                    >
                      <Scissors size={12} /> Slice 15m Unblocker
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  );
};

import React, { useEffect, useState } from 'react';
import {
  AlertTriangle,
  ArrowRight,
  Calendar,
  CheckSquare,
  ChevronDown,
  ChevronRight,
  Compass,
  FolderKanban,
  Inbox,
  Layers,
  Pencil,
  Plus,
  RefreshCw,
  Scissors,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import { callAction } from '../api';
import type { AgileBoardView, AgileCard, AgileChecklistItem, AgileEpic } from '../types';

interface BoardsStudioProps {
  onOpenWikiNote: (path: string) => void;
  onRefreshStatus: () => void;
  onToast: (msg: string) => void;
}

const ACTIVE_COLUMNS: Array<{ id: string; label: string }> = [
  { id: 'sprint_todo', label: 'To-Do' },
  { id: 'in_progress', label: 'In Progress' },
  { id: 'blocked', label: 'Blocked' },
  { id: 'verify', label: 'Verify' },
  { id: 'done', label: 'Done' },
];

const ALL_STATUS_OPTIONS: Array<{ id: string; label: string }> = [
  { id: 'backlog', label: 'Backlog' },
  ...ACTIVE_COLUMNS,
];

const HORIZON_META: Array<{ id: string; label: string; sub: string; idx: number }> = [
  { id: 'now', label: 'NOW', sub: 'This Month', idx: 0 },
  { id: 'next', label: 'NEXT', sub: '1–3 Months', idx: 1 },
  { id: 'later', label: 'LATER', sub: '3–12 Months', idx: 2 },
  { id: 'someday', label: 'SOMEDAY', sub: 'Long-Term Ideas', idx: 3 },
];

const SLOT_SHORT: Record<string, string> = {
  weekday: 'WD',
  weekend: 'WE',
  any: 'ANY',
};

interface CardModalDraft {
  card_id: string;
  board_id: string;
  title: string;
  description: string;
  status: string;
  epic_id: string;
  horizon: string;
  effort_size: string;
  day_slot: string;
  planned_date: string;
  due_date: string;
  blocked_reason: string;
  checklist: AgileChecklistItem[];
}

export const BoardsStudio: React.FC<BoardsStudioProps> = ({
  onOpenWikiNote,
  onRefreshStatus,
  onToast,
}) => {
  const [selectedBoard, setSelectedBoard] = useState('all-active-sprint');
  const [mode, setMode] = useState<'board' | 'backlog' | 'epics'>('board');
  const [view, setView] = useState<AgileBoardView | null>(null);

  // Compact Sprint setup popover toggle
  const [showSprintConfig, setShowSprintConfig] = useState(false);
  const [sprintTitle, setSprintTitle] = useState('');
  const [weekdayCap, setWeekdayCap] = useState(10);
  const [weekendCap, setWeekendCap] = useState(6);

  // Quick inline task inputs
  const [quickColId, setQuickColId] = useState('sprint_todo');
  const [quickTaskTitle, setQuickTaskTitle] = useState('');
  const [quickBacklogTitle, setQuickBacklogTitle] = useState('');
  const [epicTaskDrafts, setEpicTaskDrafts] = useState<Record<string, string>>({});

  // Quick inline Epic composer in Epics & Roadmap tab
  const [quickEpicTitle, setQuickEpicTitle] = useState('');
  const [quickEpicHorizon, setQuickEpicHorizon] = useState('next');
  const [quickEpicStart, setQuickEpicStart] = useState('');
  const [quickEpicTarget, setQuickEpicTarget] = useState('');

  // Backlog filters
  const [backlogFilter, setBacklogFilter] = useState<'all' | 'now' | 'next' | 'later' | 'someday' | 'aging'>('all');
  const [backlogEpicFilter, setBacklogEpicFilter] = useState<string>('all');

  // Epics expand/collapse & unlinked helper tray
  const [collapsedEpics, setCollapsedEpics] = useState<Record<string, boolean>>({});
  const [showUnlinkedTray, setShowUnlinkedTray] = useState(false);

  // Centered "+ New" Create Modal (Task | Epic | Board)
  const [createModalTab, setCreateModalTab] = useState<'card' | 'epic' | 'board' | null>(null);
  const [newCardTitle, setNewCardTitle] = useState('');
  const [newCardBoard, setNewCardBoard] = useState('life-home');
  const [newCardStatus, setNewCardStatus] = useState('sprint_todo');
  const [newCardEpicId, setNewCardEpicId] = useState('');
  const [newCardEffort, setNewCardEffort] = useState('quick');
  const [newCardSlot, setNewCardSlot] = useState('weekday');
  const [newCardHorizon, setNewCardHorizon] = useState('now');
  const [newCardPlan, setNewCardPlan] = useState('');
  const [newCardDue, setNewCardDue] = useState('');
  const [newCardDesc, setNewCardDesc] = useState('');
  const [newCardChecklistText, setNewCardChecklistText] = useState('');

  // Epic create / edit state (inside Create/Edit Epic modal)
  const [editingEpicId, setEditingEpicId] = useState('');
  const [epicFormTitle, setEpicFormTitle] = useState('');
  const [epicFormBoard, setEpicFormBoard] = useState('life-home');
  const [epicFormHorizon, setEpicFormHorizon] = useState('next');
  const [epicFormStart, setEpicFormStart] = useState('');
  const [epicFormTarget, setEpicFormTarget] = useState('');
  const [epicFormSummary, setEpicFormSummary] = useState('');

  // New board form
  const [newBoardTitle, setNewBoardTitle] = useState('');
  const [newBoardKind, setNewBoardKind] = useState('project');

  // Centered Planner Card Detail Modal
  const [activeCardModal, setActiveCardModal] = useState<AgileCard | null>(null);
  const [cardDraft, setCardDraft] = useState<CardModalDraft | null>(null);
  const [newCheckItemText, setNewCheckItemText] = useState('');
  const [modalUnblockerStep, setModalUnblockerStep] = useState('');

  const defaultTargetBoard =
    selectedBoard === 'all-active-sprint' ? newCardBoard || 'life-home' : selectedBoard;

  const loadBoard = async (boardId = selectedBoard): Promise<AgileBoardView | null> => {
    const res = await callAction<AgileBoardView>('agile', {
      subaction: 'board_view',
      board_id: boardId,
    });
    if (res.ok) {
      setView(res);
      return res;
    }
    return null;
  };

  useEffect(() => {
    void loadBoard(selectedBoard);
    if (selectedBoard !== 'all-active-sprint') {
      setNewCardBoard(selectedBoard);
      setEpicFormBoard(selectedBoard);
    }
  }, [selectedBoard]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (activeCardModal) {
          setActiveCardModal(null);
          setCardDraft(null);
        } else if (createModalTab) {
          setCreateModalTab(null);
        }
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [activeCardModal, createModalTab]);

  const openCardModal = (card: AgileCard) => {
    setActiveCardModal(card);
    setCardDraft({
      card_id: card.card_id,
      board_id: card.board_id,
      title: card.title,
      description: card.description || '',
      status: card.status,
      epic_id: card.epic_id || '',
      horizon: card.horizon || 'now',
      effort_size: card.effort_size || 'quick',
      day_slot: card.day_slot || 'weekday',
      planned_date: card.planned_date || '',
      due_date: card.due_date || '',
      blocked_reason: card.blocked_reason || '',
      checklist: Array.isArray(card.checklist) ? card.checklist.map((it) => ({ ...it })) : [],
    });
    setNewCheckItemText('');
    setModalUnblockerStep('');
  };

  const openCreateModal = (tab: 'card' | 'epic' | 'board', prefillTitle = '') => {
    if (tab === 'card') {
      setNewCardTitle(prefillTitle);
      setNewCardStatus(mode === 'backlog' ? 'backlog' : 'sprint_todo');
      setNewCardBoard(selectedBoard === 'all-active-sprint' ? 'life-home' : selectedBoard);
    } else if (tab === 'epic') {
      setEditingEpicId('');
      setEpicFormTitle(prefillTitle);
      setEpicFormBoard(selectedBoard === 'all-active-sprint' ? 'life-home' : selectedBoard);
      setEpicFormHorizon('next');
      setEpicFormStart('');
      setEpicFormTarget('');
      setEpicFormSummary('');
    }
    setCreateModalTab(tab);
  };

  const openEditEpicModal = (ep: AgileEpic) => {
    setEditingEpicId(ep.epic_id);
    setEpicFormTitle(ep.title);
    setEpicFormBoard(ep.board_id || 'life-home');
    setEpicFormHorizon(ep.horizon || 'next');
    setEpicFormStart(ep.start_date || '');
    setEpicFormTarget(ep.target_date || '');
    setEpicFormSummary(ep.summary || '');
    setCreateModalTab('epic');
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
      setShowSprintConfig(false);
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

  const createQuickCard = async (opts: {
    title: string;
    status: string;
    epic_id?: string;
    board_id?: string;
    horizon?: string;
    onSuccess?: () => void;
  }) => {
    const clean = opts.title.trim();
    if (!clean) return;
    const res = await callAction('agile', {
      subaction: 'save_card',
      title: clean,
      board_id: opts.board_id || defaultTargetBoard,
      status: opts.status,
      epic_id: opts.epic_id || '',
      effort_size: 'quick',
      day_slot: 'weekday',
      horizon: opts.horizon || 'now',
    });
    if (res.ok) {
      opts.onSuccess?.();
      await loadBoard();
      onRefreshStatus();
      onToast(res.summary || 'Added card.');
    } else {
      onToast(res.error || 'Could not add card.');
    }
  };

  const submitFullNewCard = async () => {
    if (!newCardTitle.trim()) return;
    const checklistLines = newCardChecklistText
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean);
    const res = await callAction('agile', {
      subaction: 'save_card',
      title: newCardTitle.trim(),
      board_id: newCardBoard || defaultTargetBoard,
      status: newCardStatus,
      epic_id: newCardEpicId,
      effort_size: newCardEffort,
      day_slot: newCardSlot,
      horizon: newCardHorizon,
      planned_date: newCardPlan.trim(),
      due_date: newCardDue.trim(),
      description: newCardDesc.trim(),
      checklist: checklistLines,
    });
    if (res.ok) {
      setNewCardTitle('');
      setNewCardPlan('');
      setNewCardDue('');
      setNewCardDesc('');
      setNewCardChecklistText('');
      setCreateModalTab(null);
      await loadBoard();
      onRefreshStatus();
      onToast(res.summary || 'Added card.');
    } else {
      onToast(res.error || 'Could not add card.');
    }
  };

  const persistChecklistImmediately = async (cardId: string, nextList: AgileChecklistItem[]) => {
    const res = await callAction<{ card?: AgileCard }>('agile', {
      subaction: 'update_card',
      card_id: cardId,
      checklist: nextList,
    });
    if (res.ok) {
      const updated = await loadBoard();
      if (updated) {
        const fresh = (updated.all_cards || updated.cards || []).find((c) => c.card_id === cardId);
        if (fresh) setActiveCardModal(fresh);
      }
    }
  };

  const toggleChecklistItem = (itemId: string) => {
    if (!cardDraft) return;
    const nextList = cardDraft.checklist.map((it) =>
      it.id === itemId ? { ...it, done: !it.done } : it
    );
    setCardDraft({ ...cardDraft, checklist: nextList });
    void persistChecklistImmediately(cardDraft.card_id, nextList);
  };

  const addChecklistItem = () => {
    if (!cardDraft || !newCheckItemText.trim()) return;
    const nextItem: AgileChecklistItem = {
      id: `chk_${Date.now()}_${cardDraft.checklist.length + 1}`,
      text: newCheckItemText.trim(),
      done: false,
    };
    const nextList = [...cardDraft.checklist, nextItem];
    setCardDraft({ ...cardDraft, checklist: nextList });
    setNewCheckItemText('');
    void persistChecklistImmediately(cardDraft.card_id, nextList);
  };

  const removeChecklistItem = (itemId: string) => {
    if (!cardDraft) return;
    const nextList = cardDraft.checklist.filter((it) => it.id !== itemId);
    setCardDraft({ ...cardDraft, checklist: nextList });
    void persistChecklistImmediately(cardDraft.card_id, nextList);
  };

  const saveCardModal = async () => {
    if (!cardDraft || !cardDraft.title.trim()) return;
    const res = await callAction('agile', {
      subaction: 'update_card',
      card_id: cardDraft.card_id,
      board_id: cardDraft.board_id,
      title: cardDraft.title.trim(),
      description: cardDraft.description,
      status: cardDraft.status,
      epic_id: cardDraft.epic_id,
      horizon: cardDraft.horizon,
      effort_size: cardDraft.effort_size,
      day_slot: cardDraft.day_slot,
      planned_date: cardDraft.planned_date.trim(),
      due_date: cardDraft.due_date.trim(),
      blocked_reason: cardDraft.blocked_reason.trim(),
      checklist: cardDraft.checklist,
    });
    if (res.ok) {
      setActiveCardModal(null);
      setCardDraft(null);
      await loadBoard();
      onRefreshStatus();
      onToast(res.summary || 'Saved card.');
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
      const updated = await loadBoard();
      onRefreshStatus();
      if (nextStatus === 'blocked' && !card.blocked_reason) {
        const fresh =
          (updated?.all_cards || updated?.cards || []).find((c) => c.card_id === card.card_id) || {
            ...card,
            status: 'blocked',
          };
        openCardModal(fresh);
      }
    } else {
      onToast(res.error || 'Cannot move card.');
    }
  };

  const assignCardToEpic = async (card: AgileCard, epicId: string) => {
    const res = await callAction('agile', {
      subaction: 'update_card',
      card_id: card.card_id,
      epic_id: epicId,
    });
    if (res.ok) {
      await loadBoard();
      onToast(epicId ? 'Linked task to epic.' : 'Unlinked task from epic.');
    }
  };

  const removeCard = async (cardId: string) => {
    const res = await callAction('agile', {
      subaction: 'delete_card',
      card_id: cardId,
    });
    if (res.ok) {
      if (activeCardModal?.card_id === cardId) {
        setActiveCardModal(null);
        setCardDraft(null);
      }
      await loadBoard();
      onRefreshStatus();
      onToast('Deleted card.');
    }
  };

  const sliceUnblocker = async (card: AgileCard, stepOverride = '') => {
    const res = await callAction('agile', {
      subaction: 'slice_unblocker',
      card_id: card.card_id,
      first_step_title: stepOverride.trim(),
      day_slot: 'weekday',
    });
    if (res.ok) {
      setModalUnblockerStep('');
      const updated = await loadBoard();
      onRefreshStatus();
      if (activeCardModal?.card_id === card.card_id && updated) {
        const fresh = (updated.all_cards || updated.cards || []).find(
          (c) => c.card_id === card.card_id
        );
        if (fresh) openCardModal(fresh);
      }
      onToast(res.summary || 'Sliced 15-minute unblocker into To-Do!');
    } else {
      onToast(res.error || 'Could not slice unblocker card.');
    }
  };

  const saveQuickEpic = async () => {
    if (!quickEpicTitle.trim()) return;
    const res = await callAction('agile', {
      subaction: 'save_epic',
      title: quickEpicTitle.trim(),
      board_id: defaultTargetBoard,
      horizon: quickEpicHorizon,
      start_date: quickEpicStart.trim(),
      target_date: quickEpicTarget.trim(),
    });
    if (res.ok) {
      setQuickEpicTitle('');
      setQuickEpicStart('');
      setQuickEpicTarget('');
      await loadBoard();
      onToast(res.summary || 'Saved epic.');
    } else {
      onToast(res.error || 'Could not save epic.');
    }
  };

  const submitFullEpicModal = async () => {
    if (!epicFormTitle.trim()) return;
    const res = await callAction('agile', {
      subaction: 'save_epic',
      epic_id: editingEpicId,
      title: epicFormTitle.trim(),
      board_id: epicFormBoard || defaultTargetBoard,
      horizon: epicFormHorizon,
      start_date: epicFormStart.trim(),
      target_date: epicFormTarget.trim(),
      summary: epicFormSummary.trim(),
    });
    if (res.ok) {
      setCreateModalTab(null);
      setEditingEpicId('');
      await loadBoard();
      onToast(res.summary || 'Saved epic.');
    } else {
      onToast(res.error || 'Could not save epic.');
    }
  };

  const removeEpic = async (ep: AgileEpic) => {
    const res = await callAction('agile', {
      subaction: 'delete_epic',
      epic_id: ep.epic_id,
    });
    if (res.ok) {
      await loadBoard();
      onToast(`Removed epic "${ep.title}" (child tasks kept safely).`);
    } else {
      onToast(res.error || 'Could not delete epic.');
    }
  };

  const createBoard = async () => {
    if (!newBoardTitle.trim()) return;
    const res = await callAction<{ board_id?: string }>('agile', {
      subaction: 'save_board',
      title: newBoardTitle.trim(),
      kind: newBoardKind,
    });
    if (res.ok && res.board_id) {
      setNewBoardTitle('');
      setCreateModalTab(null);
      setSelectedBoard(res.board_id);
      await loadBoard(res.board_id);
      onRefreshStatus();
      onToast(res.summary || 'Created board.');
    } else {
      onToast(res.error || 'Could not create board.');
    }
  };

  const boards = view?.boards || [];
  const epics = view?.epics || [];
  const cap = view?.capacity;
  const agingCards = view?.aging_cards || [];

  // Merge cards from all view buckets so Backlog & Epics work even before a backend restart
  const mergedCardMap = new Map<string, AgileCard>();
  for (const bucket of [
    ...Object.values(view?.horizons || {}),
    ...Object.values(view?.columns || {}),
    view?.cards || [],
    view?.backlog_cards || [],
    view?.all_cards || [],
  ]) {
    for (const c of bucket) {
      if (c && c.card_id) {
        mergedCardMap.set(c.card_id, c);
      }
    }
  }
  const allCards = Array.from(mergedCardMap.values());

  // Active cards for the Board tab (strictly excluding backlog)
  const activeCount = ACTIVE_COLUMNS.reduce(
    (acc, col) => acc + (view?.columns?.[col.id]?.length || 0),
    0
  );

  // Backlog cards derived from all known cards with status === 'backlog'
  const rawBacklogCards = allCards.filter(
    (c) =>
      c.status === 'backlog' &&
      (selectedBoard === 'all-active-sprint' ? true : c.board_id === selectedBoard)
  );
  const filteredBacklogCards = (backlogFilter === 'aging' ? agingCards : rawBacklogCards).filter(
    (c) => {
      const hz = (c.horizon || 'later').toLowerCase();
      if (backlogFilter !== 'all' && backlogFilter !== 'aging' && hz !== backlogFilter) {
        return false;
      }
      if (backlogEpicFilter === '__unlinked__') return !c.epic_id;
      if (backlogEpicFilter !== 'all') return c.epic_id === backlogEpicFilter;
      return true;
    }
  );

  // Cards scoped for Epics & Roadmap view
  const scopedAllCards = allCards.filter((c) =>
    selectedBoard === 'all-active-sprint' ? true : c.board_id === selectedBoard
  );
  const unlinkedOpenCards = scopedAllCards.filter((c) => !c.epic_id && c.status !== 'done');

  // Render minimalist Microsoft Planner card tile
  const renderPlannerTile = (c: AgileCard) => {
    const hasChecklist = (c.checklist_total || 0) > 0;
    const checkPct = hasChecklist
      ? Math.round(((c.checklist_done || 0) / c.checklist_total) * 100)
      : 0;
    return (
      <div
        key={c.card_id}
        role="button"
        tabIndex={0}
        className={`planner-card ${c.is_aging ? 'aging' : ''} ${c.status === 'done' ? 'is-done' : ''}`}
        data-testid={`agile-card-${c.card_id}`}
        onClick={() => openCardModal(c)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            openCardModal(c);
          }
        }}
      >
        {/* Top subtle pills (only shown when informative) + Edit trigger */}
        <div className="planner-card-top">
          <div className="planner-card-tags">
            {c.epic_title && (
              <span className="epic-tag-pill" title={`Epic: ${c.epic_title}`}>
                <Layers size={10} /> {c.epic_title}
              </span>
            )}
            {selectedBoard === 'all-active-sprint' && (
              <span className="badge mono">{c.board_title || c.board_id}</span>
            )}
            {c.is_aging && (
              <span className="badge warn mono" title={`Untouched ${c.untouched_days}d`}>
                {c.untouched_days}d stalled
              </span>
            )}
            {c.workflow_id != null && <span className="badge ok mono">WF #{c.workflow_id}</span>}
          </div>
          <button
            type="button"
            className="planner-card-edit-btn"
            title="Open card details & checklist"
            aria-label={`Edit ${c.title}`}
            onClick={(e) => {
              e.stopPropagation();
              openCardModal(c);
            }}
          >
            <Pencil size={12} />
          </button>
        </div>

        {/* Clean Title */}
        <div className="planner-card-title">{c.title}</div>

        {/* Interactive Checklist Summary Bar (Planner style) */}
        {hasChecklist && (
          <div className="planner-card-checklist-chip mono">
            <CheckSquare size={11} />
            <span>
              {c.checklist_done}/{c.checklist_total}
            </span>
            <div className="mini-progress-track">
              <div className="mini-progress-fill" style={{ width: `${checkPct}%` }} />
            </div>
          </div>
        )}

        {/* Blocked or Dependency Alert */}
        {c.blocked_reason && (
          <div className="kanban-card-alert mono">Blocked: {c.blocked_reason}</div>
        )}
        {c.unresolved_deps && c.unresolved_deps.length > 0 && (
          <div className="kanban-card-alert mono">
            Waiting on: {c.unresolved_deps.map((d) => d.title).join(', ')}
          </div>
        )}

        {/* Minimal Footer: Date / Slot Meta + Quick Column Select */}
        <div className="planner-card-footer" onClick={(e) => e.stopPropagation()}>
          <div className="planner-card-meta mono">
            <span title={`Effort: ${c.effort_size} (${c.slot_points} slot pts)`}>
              {SLOT_SHORT[c.day_slot] || c.day_slot}·{c.slot_points}s
            </span>
            {c.due_date ? (
              <span className="warn-text">
                <Calendar size={10} /> {c.due_date.slice(5)}
              </span>
            ) : c.planned_date ? (
              <span>
                <Calendar size={10} /> {c.planned_date.slice(5)}
              </span>
            ) : null}
          </div>

          <div style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
            {c.status !== 'done' && c.is_aging && (
              <button
                type="button"
                className="btn"
                style={{ padding: '2px 6px', fontSize: 10.5 }}
                title="Slice a quick 15-minute unblocker card"
                onClick={() => void sliceUnblocker(c)}
              >
                <Scissors size={10} /> 15m
              </button>
            )}
            <select
              aria-label={`Status for ${c.title}`}
              className="select planner-status-select"
              value={c.status}
              onChange={(e) => void moveCard(c, e.target.value)}
            >
              {ALL_STATUS_OPTIONS.map((col) => (
                <option key={col.id} value={col.id}>
                  {col.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Clean Header: 3 Clear Views + "+ New" */}
      <div className="studio-header">
        <div className="studio-title-group">
          <h1>Boards &amp; Epics</h1>
          <p>
            Planner-style active columns, scannable backlog triage, and Jira-style spanning epics
          </p>
        </div>
        <div className="studio-actions">
          <button
            type="button"
            data-testid="boards-tab-board"
            className={`btn ${mode === 'board' ? 'btn-primary' : ''}`}
            onClick={() => setMode('board')}
          >
            <FolderKanban size={13} /> Board ({activeCount})
          </button>
          <button
            type="button"
            data-testid="boards-tab-backlog"
            className={`btn ${mode === 'backlog' ? 'btn-primary' : ''}`}
            onClick={() => setMode('backlog')}
          >
            <Inbox size={13} /> Backlog ({rawBacklogCards.length})
            {agingCards.length > 0 && (
              <span className="badge warn mono" style={{ marginLeft: 4 }}>
                {agingCards.length} aging
              </span>
            )}
          </button>
          <button
            type="button"
            data-testid="boards-tab-epics"
            className={`btn ${mode === 'epics' ? 'btn-primary' : ''}`}
            onClick={() => setMode('epics')}
          >
            <Compass size={13} /> Epics &amp; Roadmap ({epics.length})
          </button>
          <button
            type="button"
            data-testid="boards-new-btn"
            className="btn"
            onClick={() => openCreateModal(mode === 'epics' ? 'epic' : 'card')}
          >
            <Plus size={13} /> New
          </button>
          <button
            type="button"
            className="btn"
            title="Refresh boards"
            onClick={() => void loadBoard()}
          >
            <RefreshCw size={13} />
          </button>
        </div>
      </div>

      <div className="studio-scroll">
        {/* Slim Unified Context Bar: Board Switcher Pills (Left) + Compact Sprint Capsule (Right) */}
        <div className="board-context-bar">
          <div className="board-switcher-pills">
            <button
              type="button"
              className={`btn ${selectedBoard === 'all-active-sprint' ? 'btn-primary' : ''}`}
              onClick={() => setSelectedBoard('all-active-sprint')}
            >
              <Sparkles size={12} /> All Boards
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

          <div className="sprint-capsule">
            {cap?.has_active_sprint && cap.sprint ? (
              <>
                <span className="badge ok mono">
                  {cap.sprint.title} • DONE {cap.done_count}/{cap.total_count}
                </span>
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
                  style={{ padding: '4px 9px', fontSize: 11.5 }}
                  onClick={() => void rolloverActiveSprint()}
                >
                  Rollover Sprint
                </button>
              </>
            ) : (
              <>
                <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-muted)' }}>
                  No active sprint
                </span>
                <button
                  type="button"
                  className="btn"
                  style={{ padding: '4px 8px', fontSize: 11.5 }}
                  title="Configure Weekday & Weekend slot caps"
                  onClick={() => setShowSprintConfig((v) => !v)}
                >
                  <SlidersHorizontal size={12} /> {weekdayCap} WD / {weekendCap} WE
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  style={{ padding: '4px 10px', fontSize: 11.5 }}
                  onClick={() => void startSprint()}
                >
                  Start Sprint
                </button>
              </>
            )}
          </div>
        </div>

        {/* Optional collapsible Sprint Settings Drawer when adjusting caps before starting */}
        {showSprintConfig && !cap?.has_active_sprint && (
          <div className="panel" style={{ marginBottom: 10, padding: '9px 12px' }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <input
                className="input"
                style={{ flex: '1 1 200px' }}
                placeholder="Optional Sprint Title (e.g. Week 46 Focus)"
                value={sprintTitle}
                onChange={(e) => setSprintTitle(e.target.value)}
              />
              <label className="hud-chip mono">
                <span>Weekday Slots:</span>
                <input
                  type="number"
                  aria-label="Weekday capacity slots"
                  className="input"
                  style={{ width: 56, padding: '3px 6px' }}
                  value={weekdayCap}
                  onChange={(e) => setWeekdayCap(Number(e.target.value))}
                />
              </label>
              <label className="hud-chip mono">
                <span>Weekend Slots:</span>
                <input
                  type="number"
                  aria-label="Weekend capacity slots"
                  className="input"
                  style={{ width: 56, padding: '3px 6px' }}
                  value={weekendCap}
                  onChange={(e) => setWeekendCap(Number(e.target.value))}
                />
              </label>
            </div>
          </div>
        )}

        {cap?.has_active_sprint && cap.warnings.length > 0 && (
          <div className="sprint-warning-strip mono" style={{ marginBottom: 10, marginTop: 0 }}>
            <AlertTriangle size={13} /> {cap.warnings.join(' • ')}
          </div>
        )}

        {/* ===================================================================
            VIEW 1: BOARD (Active Columns Only — Microsoft Planner Style)
            =================================================================== */}
        {mode === 'board' && (
          <div className="kanban-grid active-only">
            {ACTIVE_COLUMNS.map((col) => {
              const colCards = view?.columns?.[col.id] || [];
              const isQuickTarget = quickColId === col.id;
              return (
                <div key={col.id} className="kanban-col" data-testid={`kanban-col-${col.id}`}>
                  <div className="kanban-col-head">
                    <span>{col.label}</span>
                    <div style={{ display: 'inline-flex', gap: 5, alignItems: 'center' }}>
                      <span className="badge mono">{colCards.length}</span>
                      <button
                        type="button"
                        className="attachment-remove-btn"
                        title={`Quick add card to ${col.label}`}
                        aria-label={`Quick add to ${col.label}`}
                        onClick={() => setQuickColId(col.id)}
                      >
                        <Plus size={12} />
                      </button>
                    </div>
                  </div>

                  {/* Lightweight Planner "+ Add task" bar */}
                  {isQuickTarget && (
                    <div className="planner-quick-add">
                      <input
                        className="input"
                        aria-label="Quick add task"
                        placeholder={`+ Add task to ${col.label}...`}
                        value={quickTaskTitle}
                        onChange={(e) => setQuickTaskTitle(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            void createQuickCard({
                              title: quickTaskTitle,
                              status: col.id,
                              onSuccess: () => setQuickTaskTitle(''),
                            });
                          }
                        }}
                      />
                      <button
                        type="button"
                        className="btn"
                        style={{ padding: '4px 7px' }}
                        title="Open full card dialog with dates, slot & checklist"
                        aria-label="More options for new task"
                        onClick={() => {
                          setNewCardStatus(col.id);
                          openCreateModal('card', quickTaskTitle);
                          setQuickTaskTitle('');
                        }}
                      >
                        <SlidersHorizontal size={11} />
                      </button>
                    </div>
                  )}

                  <div className="kanban-col-body">
                    {colCards.length === 0 ? (
                      <div className="kanban-empty mono">No active cards</div>
                    ) : (
                      colCards.map((c) => renderPlannerTile(c))
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* ===================================================================
            VIEW 2: BACKLOG (Scannable Triage + Horizon / Epic / Aging Filter)
            =================================================================== */}
        {mode === 'backlog' && (
          <div className="panel">
            <div className="backlog-toolbar">
              <div style={{ display: 'flex', gap: 6, flex: '1 1 280px' }}>
                <input
                  className="input"
                  style={{ flex: 1 }}
                  aria-label="Quick add backlog item"
                  placeholder="+ Add idea or task to Backlog (press Enter)..."
                  value={quickBacklogTitle}
                  onChange={(e) => setQuickBacklogTitle(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      void createQuickCard({
                        title: quickBacklogTitle,
                        status: 'backlog',
                        epic_id:
                          backlogEpicFilter !== 'all' && backlogEpicFilter !== '__unlinked__'
                            ? backlogEpicFilter
                            : '',
                        horizon:
                          backlogFilter !== 'all' && backlogFilter !== 'aging'
                            ? backlogFilter
                            : 'later',
                        onSuccess: () => setQuickBacklogTitle(''),
                      });
                    }
                  }}
                />
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={!quickBacklogTitle.trim()}
                  onClick={() =>
                    void createQuickCard({
                      title: quickBacklogTitle,
                      status: 'backlog',
                      epic_id:
                        backlogEpicFilter !== 'all' && backlogEpicFilter !== '__unlinked__'
                          ? backlogEpicFilter
                          : '',
                      horizon:
                        backlogFilter !== 'all' && backlogFilter !== 'aging'
                          ? backlogFilter
                          : 'later',
                      onSuccess: () => setQuickBacklogTitle(''),
                    })
                  }
                >
                  <Plus size={13} /> Add
                </button>
              </div>

              <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', alignItems: 'center' }}>
                {(['all', 'now', 'next', 'later', 'someday'] as const).map((hz) => (
                  <button
                    key={hz}
                    type="button"
                    className={`btn ${backlogFilter === hz ? 'btn-primary' : ''}`}
                    style={{ padding: '4px 8px', fontSize: 11.5 }}
                    onClick={() => setBacklogFilter(hz)}
                  >
                    {hz.toUpperCase()}
                  </button>
                ))}
                <button
                  type="button"
                  className={`btn ${backlogFilter === 'aging' ? 'btn-primary' : ''}`}
                  style={{ padding: '4px 8px', fontSize: 11.5 }}
                  onClick={() => setBacklogFilter('aging')}
                >
                  <AlertTriangle size={11} /> Aging ({agingCards.length})
                </button>

                <select
                  aria-label="Filter backlog by epic"
                  className="select"
                  style={{ padding: '4px 8px', fontSize: 11.5 }}
                  value={backlogEpicFilter}
                  onChange={(e) => setBacklogEpicFilter(e.target.value)}
                >
                  <option value="all">All Epics</option>
                  <option value="__unlinked__">No Epic (Standalone)</option>
                  {epics.map((ep) => (
                    <option key={ep.epic_id} value={ep.epic_id}>
                      Epic: {ep.title}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {backlogFilter === 'aging' && (
              <div className="sprint-warning-strip mono" style={{ marginBottom: 10 }}>
                <AlertTriangle size={13} /> Zero-guilt procrastination radar: cards untouched ≥14
                days or rolled over ≥2 sprints. Slice a 15-minute unblocker into To-Do!
              </div>
            )}

            {filteredBacklogCards.length === 0 ? (
              <div className="kanban-empty mono" style={{ padding: 24 }}>
                {backlogFilter === 'aging' ? (
                  'Zero procrastinated or aging tasks right now!'
                ) : rawBacklogCards.length > 0 ? (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8, alignItems: 'center' }}>
                    <span>
                      No backlog items match this filter ({rawBacklogCards.length} total in
                      Backlog).
                    </span>
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={() => {
                        setBacklogFilter('all');
                        setBacklogEpicFilter('all');
                      }}
                    >
                      Show All Backlog Items ({rawBacklogCards.length})
                    </button>
                  </div>
                ) : (
                  'Backlog is clear for this filter.'
                )}
              </div>
            ) : (
              <div className="backlog-list">
                {filteredBacklogCards.map((c) => (
                  <div
                    key={c.card_id}
                    role="button"
                    tabIndex={0}
                    className={`backlog-row ${c.is_aging ? 'aging' : ''}`}
                    data-testid={`backlog-row-${c.card_id}`}
                    onClick={() => openCardModal(c)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        openCardModal(c);
                      }
                    }}
                  >
                    <div className="backlog-row-main">
                      <span className="backlog-row-title">{c.title}</span>
                      {c.epic_title && (
                        <span className="epic-tag-pill">
                          <Layers size={10} /> {c.epic_title}
                        </span>
                      )}
                      {selectedBoard === 'all-active-sprint' && (
                        <span className="badge mono">{c.board_title || c.board_id}</span>
                      )}
                      <span className="badge mono">{(c.horizon || 'later').toUpperCase()}</span>
                      {c.checklist_total > 0 && (
                        <span className="badge mono">
                          ☑ {c.checklist_done}/{c.checklist_total}
                        </span>
                      )}
                      {c.unresolved_deps && c.unresolved_deps.length > 0 && (
                        <span className="badge warn mono">
                          Waiting on: {c.unresolved_deps.map((d) => d.title).join(', ')}
                        </span>
                      )}
                      {c.is_aging && (
                        <span className="badge warn mono">{c.untouched_days}d untouched</span>
                      )}
                    </div>

                    <div
                      className="backlog-row-actions"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {(c.is_aging || backlogFilter === 'aging') && (
                        <button
                          type="button"
                          className="btn"
                          style={{ padding: '3px 8px', fontSize: 11 }}
                          onClick={() => void sliceUnblocker(c)}
                        >
                          <Scissors size={11} /> Slice 15m Unblocker
                        </button>
                      )}
                      {c.status === 'backlog' && (
                        <button
                          type="button"
                          className="btn btn-primary"
                          style={{ padding: '3px 8px', fontSize: 11 }}
                          onClick={() => void moveCard(c, 'sprint_todo')}
                        >
                          Move to Board <ArrowRight size={11} />
                        </button>
                      )}
                      <button
                        type="button"
                        className="attachment-remove-btn"
                        aria-label={`Edit ${c.title}`}
                        onClick={() => openCardModal(c)}
                      >
                        <Pencil size={12} />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* ===================================================================
            VIEW 3: EPICS & ROADMAP (Jira Spanning Epics + Ordered Child Tasks)
            =================================================================== */}
        {mode === 'epics' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {/* Quick Inline Add Epic / Personal Goal Bar */}
            <div className="panel" style={{ marginBottom: 0 }}>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
                <input
                  className="input"
                  style={{ flex: '2 1 240px' }}
                  aria-label="Quick epic title"
                  placeholder="+ Add Epic or Personal Long-Running Goal (e.g. Barn Cleaning Winter Project)..."
                  value={quickEpicTitle}
                  onChange={(e) => setQuickEpicTitle(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && void saveQuickEpic()}
                />
                <select
                  aria-label="Epic horizon"
                  className="select"
                  value={quickEpicHorizon}
                  onChange={(e) => setQuickEpicHorizon(e.target.value)}
                >
                  <option value="now">NOW (This Month)</option>
                  <option value="next">NEXT (1–3 Mo)</option>
                  <option value="later">LATER (3–12 Mo)</option>
                  <option value="someday">SOMEDAY</option>
                </select>
                <input
                  type="date"
                  className="input"
                  style={{ width: 146 }}
                  aria-label="Epic start date"
                  title="Start date (optional)"
                  value={quickEpicStart}
                  onChange={(e) => setQuickEpicStart(e.target.value)}
                />
                <input
                  type="date"
                  className="input"
                  style={{ width: 146 }}
                  aria-label="Epic target date"
                  title="Target completion date (surfaces on Calendar!)"
                  value={quickEpicTarget}
                  onChange={(e) => setQuickEpicTarget(e.target.value)}
                />
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={!quickEpicTitle.trim()}
                  onClick={() => void saveQuickEpic()}
                >
                  <Plus size={13} /> Save Epic
                </button>
              </div>

              {/* Jira-style 4-Horizon Visual Timeline Header */}
              <div className="epic-horizon-scale mono">
                {HORIZON_META.map((hz) => (
                  <div key={hz.id} className="epic-horizon-scale-col">
                    <strong>{hz.label}</strong>
                    <span>{hz.sub}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Spanning Epic Containers with Ordered Nested Tasks */}
            {epics.length === 0 ? (
              <div className="panel kanban-empty mono" style={{ padding: 28 }}>
                No epics yet — add a long-running goal above to group and track related tasks over
                time.
              </div>
            ) : (
              epics.map((ep) => {
                const isCollapsed = !!collapsedEpics[ep.epic_id];
                const childCards = allCards
                  .filter((c) => c.epic_id === ep.epic_id)
                  .sort((a, b) => {
                    const aDone = a.status === 'done' ? 1 : 0;
                    const bDone = b.status === 'done' ? 1 : 0;
                    if (aDone !== bDone) return aDone - bDone;
                    const aDate = a.planned_date || a.due_date || '9999-99-99';
                    const bDate = b.planned_date || b.due_date || '9999-99-99';
                    if (aDate !== bDate) return aDate.localeCompare(bDate);
                    return a.priority - b.priority;
                  });
                const doneCount = childCards.filter((c) => c.status === 'done').length;
                const totalCount = childCards.length;
                const pct = totalCount > 0 ? Math.round((doneCount / totalCount) * 100) : 0;
                const hzIdx = Math.max(
                  0,
                  HORIZON_META.findIndex((h) => h.id === ep.horizon)
                );
                // Span visually from NOW (0) through the epic's target horizon (Jira roadmap bar feel)
                const spanStartCol = ep.horizon === 'someday' ? 2 : 0;
                const spanWidthCols = Math.max(1, hzIdx - spanStartCol + 1);

                return (
                  <div
                    key={ep.epic_id}
                    className="epic-roadmap-card"
                    data-testid={`epic-card-${ep.epic_id}`}
                  >
                    <div className="epic-roadmap-head">
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                        <button
                          type="button"
                          className="attachment-remove-btn"
                          aria-label={`Toggle tasks for ${ep.title}`}
                          onClick={() =>
                            setCollapsedEpics((prev) => ({
                              ...prev,
                              [ep.epic_id]: !prev[ep.epic_id],
                            }))
                          }
                        >
                          {isCollapsed ? <ChevronRight size={15} /> : <ChevronDown size={15} />}
                        </button>
                        <Layers size={14} style={{ color: 'var(--accent-primary)', flexShrink: 0 }} />
                        <strong style={{ fontSize: 14 }}>{ep.title}</strong>
                        <span className="badge mono">{ep.board_title || ep.board_id}</span>
                        <span className="badge accent mono">{ep.horizon.toUpperCase()}</span>
                        {(ep.start_date || ep.target_date) && (
                          <span className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                            <Calendar size={11} /> {ep.start_date || 'now'} &rarr;{' '}
                            {ep.target_date || 'open'}
                          </span>
                        )}
                      </div>

                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                        <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-secondary)' }}>
                          {doneCount}/{totalCount} tasks ({pct}%)
                        </span>
                        {ep.wiki_path && (
                          <button
                            type="button"
                            className="md-wikilink"
                            onClick={() => onOpenWikiNote(ep.wiki_path)}
                          >
                            Wiki Spec
                          </button>
                        )}
                        <button
                          type="button"
                          className="attachment-remove-btn"
                          title="Edit Epic dates & summary"
                          aria-label={`Edit epic ${ep.title}`}
                          onClick={() => openEditEpicModal(ep)}
                        >
                          <Pencil size={12} />
                        </button>
                        <button
                          type="button"
                          className="attachment-remove-btn"
                          title="Delete Epic (keeps child tasks)"
                          aria-label={`Delete epic ${ep.title}`}
                          onClick={() => void removeEpic(ep)}
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    </div>

                    {ep.summary && <div className="epic-roadmap-summary">{ep.summary}</div>}

                    {/* Jira-style Spanning Bar across NOW -> NEXT -> LATER -> SOMEDAY */}
                    <div className="epic-span-track">
                      <div
                        className="epic-span-bar"
                        style={{
                          gridColumn: `${spanStartCol + 1} / span ${spanWidthCols}`,
                        }}
                      >
                        <div className="epic-span-fill" style={{ width: `${pct}%` }} />
                        <span className="epic-span-label mono">
                          {ep.title} • {pct}% complete
                          {ep.target_date ? ` • Target ${ep.target_date}` : ''}
                        </span>
                      </div>
                    </div>

                    {/* Ordered Child Tasks nested directly underneath the Epic */}
                    {!isCollapsed && (
                      <div className="epic-children-list">
                        {childCards.length === 0 ? (
                          <div className="mono" style={{ fontSize: 11.5, color: 'var(--text-muted)', padding: '4px 2px' }}>
                            No tasks linked yet — add the ordered steps below.
                          </div>
                        ) : (
                          childCards.map((c, idx) => (
                            <div
                              key={c.card_id}
                              role="button"
                              tabIndex={0}
                              className={`epic-child-row ${c.status === 'done' ? 'is-done' : ''}`}
                              onClick={() => openCardModal(c)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter' || e.key === ' ') {
                                  e.preventDefault();
                                  openCardModal(c);
                                }
                              }}
                            >
                              <div className="epic-child-left">
                                <span className="epic-step-num mono">{idx + 1}</span>
                                <span className="epic-child-title">{c.title}</span>
                                {c.checklist_total > 0 && (
                                  <span className="badge mono">
                                    ☑ {c.checklist_done}/{c.checklist_total}
                                  </span>
                                )}
                                {(c.planned_date || c.due_date) && (
                                  <span className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                                    {c.due_date ? `Due ${c.due_date}` : `Plan ${c.planned_date}`}
                                  </span>
                                )}
                              </div>

                              <div
                                style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}
                                onClick={(e) => e.stopPropagation()}
                              >
                                <select
                                  aria-label={`Status for ${c.title}`}
                                  className="select planner-status-select"
                                  value={c.status}
                                  onChange={(e) => void moveCard(c, e.target.value)}
                                >
                                  {ALL_STATUS_OPTIONS.map((col) => (
                                    <option key={col.id} value={col.id}>
                                      {col.label}
                                    </option>
                                  ))}
                                </select>
                                <button
                                  type="button"
                                  className="attachment-remove-btn"
                                  aria-label={`Edit ${c.title}`}
                                  onClick={() => openCardModal(c)}
                                >
                                  <Pencil size={12} />
                                </button>
                              </div>
                            </div>
                          ))
                        )}

                        {/* Inline "+ Add task under Epic" */}
                        <div className="epic-inline-add">
                          <input
                            className="input"
                            aria-label={`Add task under ${ep.title}`}
                            placeholder={`+ Add ordered task under "${ep.title}" (Enter for Board To-Do)...`}
                            value={epicTaskDrafts[ep.epic_id] || ''}
                            onChange={(e) =>
                              setEpicTaskDrafts((prev) => ({
                                ...prev,
                                [ep.epic_id]: e.target.value,
                              }))
                            }
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') {
                                void createQuickCard({
                                  title: epicTaskDrafts[ep.epic_id] || '',
                                  status: 'sprint_todo',
                                  epic_id: ep.epic_id,
                                  board_id: ep.board_id,
                                  horizon: ep.horizon === 'now' ? 'now' : 'next',
                                  onSuccess: () =>
                                    setEpicTaskDrafts((prev) => ({ ...prev, [ep.epic_id]: '' })),
                                });
                              }
                            }}
                          />
                          <button
                            type="button"
                            className="btn"
                            disabled={!(epicTaskDrafts[ep.epic_id] || '').trim()}
                            onClick={() =>
                              void createQuickCard({
                                title: epicTaskDrafts[ep.epic_id] || '',
                                status: 'backlog',
                                epic_id: ep.epic_id,
                                board_id: ep.board_id,
                                horizon: ep.horizon,
                                onSuccess: () =>
                                  setEpicTaskDrafts((prev) => ({ ...prev, [ep.epic_id]: '' })),
                              })
                            }
                          >
                            + Backlog
                          </button>
                          <button
                            type="button"
                            className="btn btn-primary"
                            disabled={!(epicTaskDrafts[ep.epic_id] || '').trim()}
                            onClick={() =>
                              void createQuickCard({
                                title: epicTaskDrafts[ep.epic_id] || '',
                                status: 'sprint_todo',
                                epic_id: ep.epic_id,
                                board_id: ep.board_id,
                                horizon: 'now',
                                onSuccess: () =>
                                  setEpicTaskDrafts((prev) => ({ ...prev, [ep.epic_id]: '' })),
                              })
                            }
                          >
                            + To-Do
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })
            )}

            {/* Helper Tray to link existing standalone tasks to an Epic in 1 click */}
            {epics.length > 0 && unlinkedOpenCards.length > 0 && (
              <div className="panel" style={{ marginBottom: 0 }}>
                <button
                  type="button"
                  className="btn"
                  style={{ width: '100%', justifyContent: 'space-between' }}
                  onClick={() => setShowUnlinkedTray((v) => !v)}
                >
                  <span>
                    Unlinked Standalone Tasks ({unlinkedOpenCards.length}) — click to assign tasks
                    into an Epic
                  </span>
                  {showUnlinkedTray ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                </button>
                {showUnlinkedTray && (
                  <div className="backlog-list" style={{ marginTop: 10 }}>
                    {unlinkedOpenCards.map((c) => (
                      <div key={c.card_id} className="backlog-row">
                        <div className="backlog-row-main">
                          <span className="backlog-row-title">{c.title}</span>
                          <span className="badge mono">{c.status}</span>
                        </div>
                        <select
                          aria-label={`Assign ${c.title} to epic`}
                          className="select"
                          style={{ padding: '3px 8px', fontSize: 11.5 }}
                          value=""
                          onChange={(e) => {
                            if (e.target.value) void assignCardToEpic(c, e.target.value);
                          }}
                        >
                          <option value="">Assign to Epic...</option>
                          {epics.map((ep) => (
                            <option key={ep.epic_id} value={ep.epic_id}>
                              {ep.title}
                            </option>
                          ))}
                        </select>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>

      {/* =====================================================================
          CENTERED MICROSOFT PLANNER / TRELLO CARD DETAIL MODAL
          ===================================================================== */}
      {activeCardModal && cardDraft && (
        <div
          className="modal-backdrop"
          data-testid="planner-card-modal"
          onClick={() => {
            setActiveCardModal(null);
            setCardDraft(null);
          }}
        >
          <div
            className="planner-modal"
            role="dialog"
            aria-modal="true"
            aria-label="Edit Card Details"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="planner-modal-head">
              <input
                className="input planner-modal-title-input"
                aria-label="Edit card title"
                value={cardDraft.title}
                onChange={(e) => setCardDraft({ ...cardDraft, title: e.target.value })}
              />
              <button
                type="button"
                className="attachment-remove-btn"
                aria-label="Close card dialog"
                onClick={() => {
                  setActiveCardModal(null);
                  setCardDraft(null);
                }}
              >
                <X size={16} />
              </button>
            </div>

            {/* Planner Metadata Grid */}
            <div className="planner-modal-grid">
              <label className="planner-field">
                <span>Status Column</span>
                <select
                  aria-label="Edit card status"
                  className="select"
                  value={cardDraft.status}
                  onChange={(e) => setCardDraft({ ...cardDraft, status: e.target.value })}
                >
                  {ALL_STATUS_OPTIONS.map((col) => (
                    <option key={col.id} value={col.id}>
                      {col.label}
                    </option>
                  ))}
                </select>
              </label>

              <label className="planner-field">
                <span>Epic / Long-Range Goal</span>
                <select
                  aria-label="Edit card epic"
                  className="select"
                  value={cardDraft.epic_id}
                  onChange={(e) => setCardDraft({ ...cardDraft, epic_id: e.target.value })}
                >
                  <option value="">None (Standalone Task)</option>
                  {epics.map((ep) => (
                    <option key={ep.epic_id} value={ep.epic_id}>
                      {ep.title}
                    </option>
                  ))}
                </select>
              </label>

              <label className="planner-field">
                <span>Board</span>
                <select
                  aria-label="Edit card board"
                  className="select"
                  value={cardDraft.board_id}
                  onChange={(e) => setCardDraft({ ...cardDraft, board_id: e.target.value })}
                >
                  {boards.map((b) => (
                    <option key={b.board_id} value={b.board_id}>
                      {b.title}
                    </option>
                  ))}
                </select>
              </label>

              <label className="planner-field">
                <span>Effort Size</span>
                <select
                  aria-label="Edit card effort"
                  className="select"
                  value={cardDraft.effort_size}
                  onChange={(e) => setCardDraft({ ...cardDraft, effort_size: e.target.value })}
                >
                  <option value="quick">Quick (&lt;30m • 1 slot)</option>
                  <option value="half_day">Half-Day (2 slots)</option>
                  <option value="full_day">Full-Day (4 slots)</option>
                  <option value="multi_day">Multi-Day (8 slots)</option>
                </select>
              </label>

              <label className="planner-field">
                <span>Day Slot</span>
                <select
                  aria-label="Edit card day slot"
                  className="select"
                  value={cardDraft.day_slot}
                  onChange={(e) => setCardDraft({ ...cardDraft, day_slot: e.target.value })}
                >
                  <option value="weekday">Weekday</option>
                  <option value="weekend">Weekend</option>
                  <option value="any">Any Day</option>
                </select>
              </label>

              <label className="planner-field">
                <span>Horizon</span>
                <select
                  aria-label="Edit card horizon"
                  className="select"
                  value={cardDraft.horizon}
                  onChange={(e) => setCardDraft({ ...cardDraft, horizon: e.target.value })}
                >
                  <option value="now">Now (This Month)</option>
                  <option value="next">Next (1–3 Mo)</option>
                  <option value="later">Later (3–12 Mo)</option>
                  <option value="someday">Someday</option>
                </select>
              </label>

              <label className="planner-field">
                <span>Planned Date</span>
                <input
                  type="date"
                  className="input"
                  aria-label="Edit planned date"
                  value={cardDraft.planned_date}
                  onChange={(e) => setCardDraft({ ...cardDraft, planned_date: e.target.value })}
                />
              </label>

              <label className="planner-field">
                <span>Due Date</span>
                <input
                  type="date"
                  className="input"
                  aria-label="Edit due date"
                  value={cardDraft.due_date}
                  onChange={(e) => setCardDraft({ ...cardDraft, due_date: e.target.value })}
                />
              </label>
            </div>

            {(cardDraft.status === 'blocked' || cardDraft.blocked_reason) && (
              <label className="planner-field">
                <span>Blocked Reason</span>
                <input
                  className="input"
                  placeholder="What is blocking this card? (e.g. Waiting on carburetor gasket)"
                  aria-label="Edit blocked reason"
                  value={cardDraft.blocked_reason}
                  onChange={(e) => setCardDraft({ ...cardDraft, blocked_reason: e.target.value })}
                />
              </label>
            )}

            {/* Interactive Checklist (MS Planner Checkboxes) */}
            <div className="planner-checklist-box" data-testid="card-checklist-section">
              <div className="planner-checklist-head">
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontWeight: 600 }}>
                  <CheckSquare size={13} /> Checklist (
                  {cardDraft.checklist.filter((i) => i.done).length}/{cardDraft.checklist.length})
                </span>
                {cardDraft.checklist.length > 0 && (
                  <div className="mini-progress-track" style={{ width: 120 }}>
                    <div
                      className="mini-progress-fill"
                      style={{
                        width: `${Math.round(
                          (cardDraft.checklist.filter((i) => i.done).length /
                            cardDraft.checklist.length) *
                            100
                        )}%`,
                      }}
                    />
                  </div>
                )}
              </div>

              {cardDraft.checklist.length > 0 && (
                <div className="checklist-items">
                  {cardDraft.checklist.map((item) => (
                    <div key={item.id} className={`checklist-row ${item.done ? 'done' : ''}`}>
                      <label style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, cursor: 'pointer' }}>
                        <input
                          type="checkbox"
                          aria-label={`Toggle ${item.text}`}
                          checked={item.done}
                          onChange={() => toggleChecklistItem(item.id)}
                        />
                        <span>{item.text}</span>
                      </label>
                      <button
                        type="button"
                        className="attachment-remove-btn"
                        aria-label={`Remove checklist item ${item.text}`}
                        onClick={() => removeChecklistItem(item.id)}
                      >
                        <X size={12} />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                <input
                  className="input"
                  style={{ flex: 1 }}
                  aria-label="New checklist item"
                  placeholder="+ Add checklist sub-step (press Enter)..."
                  value={newCheckItemText}
                  onChange={(e) => setNewCheckItemText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      addChecklistItem();
                    }
                  }}
                />
                <button
                  type="button"
                  className="btn"
                  disabled={!newCheckItemText.trim()}
                  onClick={addChecklistItem}
                >
                  Add Item
                </button>
              </div>
            </div>

            {/* Notes / Details */}
            <label className="planner-field">
              <span>Notes &amp; Details</span>
              <textarea
                className="textarea"
                rows={3}
                aria-label="Edit card notes"
                placeholder="Add notes, parts list, measurements, or context..."
                value={cardDraft.description}
                onChange={(e) => setCardDraft({ ...cardDraft, description: e.target.value })}
              />
            </label>

            {/* Dependency & 15m Unblocker Helper */}
            {activeCardModal.unresolved_deps?.length > 0 && (
              <div className="kanban-card-alert mono">
                Waiting on: {activeCardModal.unresolved_deps.map((d) => d.title).join(', ')}
              </div>
            )}

            {cardDraft.status !== 'done' && (
              <div className="planner-unblocker-bar">
                <input
                  className="input"
                  style={{ flex: 1 }}
                  aria-label="Unblocker step title"
                  placeholder="Feeling stuck? Optional 15-minute first step..."
                  value={modalUnblockerStep}
                  onChange={(e) => setModalUnblockerStep(e.target.value)}
                />
                <button
                  type="button"
                  className="btn"
                  onClick={() => void sliceUnblocker(activeCardModal, modalUnblockerStep)}
                >
                  <Scissors size={12} /> Slice 15m Unblocker
                </button>
              </div>
            )}

            {/* Modal Footer */}
            <div className="planner-modal-foot">
              <button
                type="button"
                className="btn"
                style={{ color: 'var(--status-danger)' }}
                onClick={() => void removeCard(cardDraft.card_id)}
              >
                <Trash2 size={13} /> Delete Card
              </button>
              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  type="button"
                  className="btn"
                  onClick={() => {
                    setActiveCardModal(null);
                    setCardDraft(null);
                  }}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void saveCardModal()}
                >
                  Save Card
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* =====================================================================
          CENTERED "+ NEW" MODAL (Task Card | Epic / Goal | Board)
          ===================================================================== */}
      {createModalTab && (
        <div
          className="modal-backdrop"
          data-testid="boards-create-modal"
          onClick={() => setCreateModalTab(null)}
        >
          <div
            className="planner-modal"
            role="dialog"
            aria-modal="true"
            aria-label="Create Board Item"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="planner-modal-head">
              <div style={{ display: 'flex', gap: 6 }}>
                <button
                  type="button"
                  className={`btn ${createModalTab === 'card' ? 'btn-primary' : ''}`}
                  onClick={() => setCreateModalTab('card')}
                >
                  Task Card
                </button>
                <button
                  type="button"
                  className={`btn ${createModalTab === 'epic' ? 'btn-primary' : ''}`}
                  onClick={() => setCreateModalTab('epic')}
                >
                  {editingEpicId ? 'Edit Epic' : 'Epic / Goal'}
                </button>
                <button
                  type="button"
                  className={`btn ${createModalTab === 'board' ? 'btn-primary' : ''}`}
                  onClick={() => setCreateModalTab('board')}
                >
                  Board
                </button>
              </div>
              <button
                type="button"
                className="attachment-remove-btn"
                aria-label="Close create dialog"
                onClick={() => setCreateModalTab(null)}
              >
                <X size={16} />
              </button>
            </div>

            {createModalTab === 'card' && (
              <>
                <label className="planner-field">
                  <span>Task Title</span>
                  <input
                    className="input"
                    placeholder="e.g. Fix truck brake inspection"
                    aria-label="New card title"
                    value={newCardTitle}
                    onChange={(e) => setNewCardTitle(e.target.value)}
                  />
                </label>
                <div className="planner-modal-grid">
                  <label className="planner-field">
                    <span>Board</span>
                    <select
                      aria-label="Target board"
                      className="select"
                      value={newCardBoard}
                      onChange={(e) => setNewCardBoard(e.target.value)}
                    >
                      {boards.map((b) => (
                        <option key={b.board_id} value={b.board_id}>
                          {b.title}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="planner-field">
                    <span>Initial Column</span>
                    <select
                      aria-label="Initial column"
                      className="select"
                      value={newCardStatus}
                      onChange={(e) => setNewCardStatus(e.target.value)}
                    >
                      <option value="sprint_todo">To-Do (Board)</option>
                      <option value="backlog">Backlog</option>
                      <option value="in_progress">In Progress</option>
                    </select>
                  </label>
                  <label className="planner-field">
                    <span>Epic / Goal</span>
                    <select
                      aria-label="New card epic"
                      className="select"
                      value={newCardEpicId}
                      onChange={(e) => setNewCardEpicId(e.target.value)}
                    >
                      <option value="">None (Standalone)</option>
                      {epics.map((ep) => (
                        <option key={ep.epic_id} value={ep.epic_id}>
                          {ep.title}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="planner-field">
                    <span>Effort Size</span>
                    <select
                      aria-label="Effort size"
                      className="select"
                      value={newCardEffort}
                      onChange={(e) => setNewCardEffort(e.target.value)}
                    >
                      <option value="quick">Quick &lt;30m (1s)</option>
                      <option value="half_day">Half-Day (2s)</option>
                      <option value="full_day">Full-Day (4s)</option>
                      <option value="multi_day">Multi-Day (8s)</option>
                    </select>
                  </label>
                  <label className="planner-field">
                    <span>Day Slot</span>
                    <select
                      aria-label="Day slot"
                      className="select"
                      value={newCardSlot}
                      onChange={(e) => setNewCardSlot(e.target.value)}
                    >
                      <option value="weekday">Weekday</option>
                      <option value="weekend">Weekend</option>
                      <option value="any">Any Day</option>
                    </select>
                  </label>
                  <label className="planner-field">
                    <span>Horizon</span>
                    <select
                      aria-label="Planning horizon"
                      className="select"
                      value={newCardHorizon}
                      onChange={(e) => setNewCardHorizon(e.target.value)}
                    >
                      <option value="now">NOW</option>
                      <option value="next">NEXT</option>
                      <option value="later">LATER</option>
                      <option value="someday">SOMEDAY</option>
                    </select>
                  </label>
                  <label className="planner-field">
                    <span>Planned Date</span>
                    <input
                      type="date"
                      className="input"
                      aria-label="Planned date"
                      value={newCardPlan}
                      onChange={(e) => setNewCardPlan(e.target.value)}
                    />
                  </label>
                  <label className="planner-field">
                    <span>Due Date</span>
                    <input
                      type="date"
                      className="input"
                      aria-label="Due date"
                      value={newCardDue}
                      onChange={(e) => setNewCardDue(e.target.value)}
                    />
                  </label>
                </div>
                <label className="planner-field">
                  <span>Checklist Items (one per line, optional)</span>
                  <textarea
                    className="textarea"
                    rows={3}
                    placeholder={'Clean stalls\nHaul scrap junk\nWinterize trimmer'}
                    value={newCardChecklistText}
                    onChange={(e) => setNewCardChecklistText(e.target.value)}
                  />
                </label>
                <label className="planner-field">
                  <span>Notes (optional)</span>
                  <textarea
                    className="textarea"
                    rows={2}
                    value={newCardDesc}
                    onChange={(e) => setNewCardDesc(e.target.value)}
                  />
                </label>
                <div className="planner-modal-foot" style={{ justifyContent: 'flex-end' }}>
                  <button type="button" className="btn" onClick={() => setCreateModalTab(null)}>
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={!newCardTitle.trim()}
                    onClick={() => void submitFullNewCard()}
                  >
                    <Plus size={13} /> Add Card
                  </button>
                </div>
              </>
            )}

            {createModalTab === 'epic' && (
              <>
                <label className="planner-field">
                  <span>Epic / Long-Running Goal Title</span>
                  <input
                    className="input"
                    aria-label="Modal epic title"
                    placeholder="e.g. Barn cleaning winter project"
                    value={epicFormTitle}
                    onChange={(e) => setEpicFormTitle(e.target.value)}
                  />
                </label>
                <div className="planner-modal-grid">
                  <label className="planner-field">
                    <span>Board</span>
                    <select
                      className="select"
                      value={epicFormBoard}
                      onChange={(e) => setEpicFormBoard(e.target.value)}
                    >
                      {boards.map((b) => (
                        <option key={b.board_id} value={b.board_id}>
                          {b.title}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="planner-field">
                    <span>Horizon</span>
                    <select
                      className="select"
                      value={epicFormHorizon}
                      onChange={(e) => setEpicFormHorizon(e.target.value)}
                    >
                      <option value="now">NOW (This Month)</option>
                      <option value="next">NEXT (1–3 Mo)</option>
                      <option value="later">LATER (3–12 Mo)</option>
                      <option value="someday">SOMEDAY</option>
                    </select>
                  </label>
                  <label className="planner-field">
                    <span>Start Date</span>
                    <input
                      type="date"
                      className="input"
                      aria-label="Modal epic start date"
                      value={epicFormStart}
                      onChange={(e) => setEpicFormStart(e.target.value)}
                    />
                  </label>
                  <label className="planner-field">
                    <span>Target Date (Calendar Milestone)</span>
                    <input
                      type="date"
                      className="input"
                      aria-label="Modal epic target date"
                      value={epicFormTarget}
                      onChange={(e) => setEpicFormTarget(e.target.value)}
                    />
                  </label>
                </div>
                <label className="planner-field">
                  <span>Goal Summary</span>
                  <textarea
                    className="textarea"
                    rows={3}
                    placeholder="What does finished look like for this epic?"
                    value={epicFormSummary}
                    onChange={(e) => setEpicFormSummary(e.target.value)}
                  />
                </label>
                <div className="planner-modal-foot" style={{ justifyContent: 'flex-end' }}>
                  <button type="button" className="btn" onClick={() => setCreateModalTab(null)}>
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={!epicFormTitle.trim()}
                    onClick={() => void submitFullEpicModal()}
                  >
                    Save Epic
                  </button>
                </div>
              </>
            )}

            {createModalTab === 'board' && (
              <>
                <label className="planner-field">
                  <span>Board Title</span>
                  <input
                    className="input"
                    placeholder="e.g. Basement Renovation, Homelab Upgrade"
                    value={newBoardTitle}
                    onChange={(e) => setNewBoardTitle(e.target.value)}
                  />
                </label>
                <label className="planner-field">
                  <span>Board Category</span>
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
                </label>
                <div className="planner-modal-foot" style={{ justifyContent: 'flex-end' }}>
                  <button type="button" className="btn" onClick={() => setCreateModalTab(null)}>
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={!newBoardTitle.trim()}
                    onClick={() => void createBoard()}
                  >
                    Create Board
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

import React, { useEffect, useState } from 'react';
import {
  Calendar as CalendarIcon,
  ChevronLeft,
  ChevronRight,
  Clock,
  GraduationCap,
  List,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react';
import { callAction } from '../api';
import type { CalendarItem, CalendarWindow, StudioId } from '../types';

interface CalendarStudioProps {
  onNavigateStudio: (studio: StudioId) => void;
  onToast: (msg: string) => void;
}

const WEEKDAY_HEADERS = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];

function shiftIsoDate(ymd: string, deltaDays: number): string {
  const parts = ymd.split('-').map((n) => Number(n));
  if (parts.length !== 3 || parts.some((n) => Number.isNaN(n))) return '';
  const dt = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2] + deltaDays));
  return dt.toISOString().slice(0, 10);
}

export const CalendarStudio: React.FC<CalendarStudioProps> = ({ onNavigateStudio, onToast }) => {
  const [viewMode, setViewMode] = useState<'month' | 'week' | 'agenda'>('month');
  const [anchorDate, setAnchorDate] = useState<string>('');
  const [cal, setCal] = useState<CalendarWindow | null>(null);

  // Quick add event form
  const [evTitle, setEvTitle] = useState('');
  const [evStart, setEvStart] = useState(() => new Date().toISOString().slice(0, 10));
  const [evKind, setEvKind] = useState('appointment');
  const [evAllDay, setEvAllDay] = useState(false);

  const daysForMode = viewMode === 'month' ? 35 : viewMode === 'week' ? 7 : 21;

  const loadWindow = async (startOverride = anchorDate, span = daysForMode) => {
    const res = await callAction<CalendarWindow>('calendar', {
      subaction: 'window',
      start_date: startOverride,
      days: span,
    });
    if (res.ok) {
      setCal(res);
      if (!startOverride && res.today) {
        setEvStart((cur) => cur || res.today);
      }
    }
  };

  useEffect(() => {
    void loadWindow(anchorDate, daysForMode);
  }, [viewMode, anchorDate]);

  const stepWindow = (dir: -1 | 1) => {
    const base = cal?.start_date || new Date().toISOString().slice(0, 10);
    const delta = (viewMode === 'month' ? 28 : 7) * dir;
    setAnchorDate(shiftIsoDate(base, delta));
  };

  const scheduleEvent = async () => {
    if (!evTitle.trim() || !evStart.trim()) return;
    const res = await callAction('calendar', {
      subaction: 'add_event',
      title: evTitle.trim(),
      start_at: evStart.trim(),
      kind: evKind,
      all_day: evAllDay || evStart.trim().length <= 10,
    });
    if (res.ok) {
      setEvTitle('');
      await loadWindow();
      onToast(res.summary || 'Scheduled event.');
    } else {
      onToast(res.error || 'Could not schedule event.');
    }
  };

  const deleteEvent = async (eventId: string) => {
    const res = await callAction('calendar', {
      subaction: 'delete_event',
      event_id: eventId,
    });
    if (res.ok) {
      await loadWindow();
      onToast('Removed calendar event.');
    }
  };

  const handleItemClick = (it: CalendarItem) => {
    if (it.source_type === 'sprint_task' || it.source_type === 'deadline') {
      onNavigateStudio('boards');
    } else if (it.source_type === 'study_review') {
      onNavigateStudio('chat');
    } else if (it.source_type === 'routine') {
      onNavigateStudio('jobs');
    }
  };

  const renderPill = (it: CalendarItem) => {
    const cls =
      it.source_type === 'deadline'
        ? 'danger'
        : it.source_type === 'sprint_task'
          ? 'accent'
          : it.source_type === 'study_review'
            ? 'warn'
            : it.source_type === 'routine'
              ? 'routine'
              : 'event';
    return (
      <div key={it.id} className={`cal-pill ${cls}`} title={`${it.source_type}: ${it.title}`}>
        <button
          type="button"
          className="cal-pill-btn"
          onClick={() => handleItemClick(it)}
        >
          {it.time && <span className="mono">{it.time} </span>}
          {it.source_type === 'study_review' && <GraduationCap size={10} />}
          <span>{it.title}</span>
        </button>
        {it.source_type === 'event' && (
          <button
            type="button"
            className="cal-pill-del"
            aria-label={`Delete ${it.title}`}
            onClick={() => void deleteEvent(it.id)}
          >
            <Trash2 size={10} />
          </button>
        )}
      </div>
    );
  };

  const dateKeys = Object.keys(cal?.by_date || {}).sort();
  const todayStr = cal?.today || '';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="studio-header">
        <div className="studio-title-group">
          <h1>Unified Multi-Source Calendar</h1>
          <p>
            Appointments, Active Sprint Windows, Planned Task Days, Hard Deadlines, Routines &amp;
            Due Spaced-Repetition Reviews
          </p>
        </div>
        <div className="studio-actions">
          <button
            type="button"
            className={`btn ${viewMode === 'month' ? 'btn-primary' : ''}`}
            onClick={() => setViewMode('month')}
          >
            <CalendarIcon size={13} /> Month
          </button>
          <button
            type="button"
            className={`btn ${viewMode === 'week' ? 'btn-primary' : ''}`}
            onClick={() => setViewMode('week')}
          >
            <Clock size={13} /> Week
          </button>
          <button
            type="button"
            className={`btn ${viewMode === 'agenda' ? 'btn-primary' : ''}`}
            onClick={() => setViewMode('agenda')}
          >
            <List size={13} /> Agenda
          </button>
          <button type="button" className="btn" onClick={() => stepWindow(-1)} title="Previous">
            <ChevronLeft size={13} />
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => setAnchorDate('')}
          >
            Today
          </button>
          <button type="button" className="btn" onClick={() => stepWindow(1)} title="Next">
            <ChevronRight size={13} />
          </button>
          <button type="button" className="btn" onClick={() => void loadWindow()}>
            <RefreshCw size={13} />
          </button>
        </div>
      </div>

      <div className="studio-scroll">
        {/* Quick Event Scheduler + Legend */}
        <div className="panel">
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <input
              className="input"
              style={{ flex: '2 1 240px' }}
              placeholder="Schedule appointment, study block, or milestone (e.g. Drop truck off at mechanic)..."
              value={evTitle}
              onChange={(e) => setEvTitle(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void scheduleEvent()}
            />
            <input
              className="input"
              style={{ width: 175 }}
              placeholder="YYYY-MM-DD or YYYY-MM-DDTHH:MM"
              aria-label="Event start date or time"
              value={evStart}
              onChange={(e) => setEvStart(e.target.value)}
            />
            <select
              aria-label="Event kind"
              className="select"
              value={evKind}
              onChange={(e) => setEvKind(e.target.value)}
            >
              <option value="appointment">Appointment</option>
              <option value="focus_block">Focus / Study Block</option>
              <option value="milestone">Milestone</option>
              <option value="reminder">Reminder</option>
            </select>
            <label className="hud-chip" style={{ cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={evAllDay}
                onChange={(e) => setEvAllDay(e.target.checked)}
              />
              <span>All Day</span>
            </label>
            <button
              type="button"
              className="btn btn-primary"
              disabled={!evTitle.trim() || !evStart.trim()}
              onClick={() => void scheduleEvent()}
            >
              <Plus size={13} /> Schedule
            </button>
          </div>

          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              gap: 10,
              marginTop: 10,
              paddingTop: 8,
              borderTop: '1px solid var(--border-hairline)',
              flexWrap: 'wrap',
              fontSize: 11.5,
            }}
          >
            <div className="mono" style={{ color: 'var(--text-secondary)' }}>
              WINDOW: {cal?.start_date || '—'} &rarr; {cal?.end_date || '—'}
              {cal?.active_sprint && (
                <span>
                  {' '}
                  • ACTIVE SPRINT: <strong>{cal.active_sprint.title}</strong> (
                  {cal.active_sprint.start_date}..{cal.active_sprint.end_date})
                </span>
              )}
            </div>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <span className="badge ok">Event / Focus</span>
              <span className="badge accent">Planned Task</span>
              <span className="badge danger">Hard Deadline</span>
              <span className="badge">Routine</span>
              <span className="badge warn">Study SRS</span>
            </div>
          </div>
        </div>

        {/* Month or Week Grid */}
        {viewMode !== 'agenda' ? (
          <div className="panel" style={{ padding: 0, overflow: 'hidden' }}>
            <div className="cal-header-row mono">
              {WEEKDAY_HEADERS.map((wd) => (
                <div key={wd} className="cal-header-cell">
                  {wd}
                </div>
              ))}
            </div>
            <div className={`cal-grid ${viewMode === 'week' ? 'week' : 'month'}`}>
              {dateKeys.map((dKey) => {
                const dayItems = cal?.by_date?.[dKey] || [];
                const isToday = dKey === todayStr;
                const isSelected = evStart.slice(0, 10) === dKey;
                const inSprint =
                  cal?.active_sprint &&
                  dKey >= cal.active_sprint.start_date &&
                  dKey <= cal.active_sprint.end_date;
                return (
                  <div
                    key={dKey}
                    className={`cal-day-cell ${isToday ? 'today' : ''} ${inSprint ? 'in-sprint' : ''}`}
                    style={isSelected && !isToday ? { outline: '1px solid var(--accent-primary)' } : undefined}
                    data-testid={`cal-day-${dKey}`}
                  >
                    <button
                      type="button"
                      className="cal-day-head mono"
                      style={{
                        width: '100%',
                        background: 'transparent',
                        border: 'none',
                        padding: 0,
                        cursor: 'pointer',
                        color: 'inherit',
                        textAlign: 'left',
                      }}
                      title={`Click to set scheduler date to ${dKey}`}
                      onClick={() => setEvStart(dKey)}
                    >
                      <span>{dKey.slice(5)}</span>
                      {isToday ? (
                        <span className="badge accent">TODAY</span>
                      ) : isSelected ? (
                        <span className="badge">SELECTED</span>
                      ) : null}
                    </button>
                    <div className="cal-day-items">
                      {dayItems.map((it) => renderPill(it))}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ) : (
          /* Agenda Chronological View */
          <div className="panel">
            <div className="panel-head">
              <span className="panel-title">Chronological Agenda ({(cal?.items || []).length})</span>
            </div>
            {(cal?.items || []).length === 0 ? (
              <div style={{ color: 'var(--text-muted)', fontSize: 12.5 }}>
                No scheduled events, task dates, deadlines, routines, or study reviews in this
                window.
              </div>
            ) : (
              dateKeys
                .filter((dKey) => (cal?.by_date?.[dKey] || []).length > 0)
                .map((dKey) => (
                  <div
                    key={dKey}
                    style={{
                      padding: '8px 0',
                      borderBottom: '1px solid var(--border-hairline)',
                    }}
                  >
                    <div className="mono" style={{ fontSize: 12, fontWeight: 700, marginBottom: 6 }}>
                      {dKey} {dKey === todayStr ? '(TODAY)' : ''}
                    </div>
                    <div style={{ display: 'grid', gap: 5 }}>
                      {(cal?.by_date?.[dKey] || []).map((it) => renderPill(it))}
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

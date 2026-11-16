import React, { useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, Plus, Trash2, X } from 'lucide-react';
import { callAction } from '../api';
import type { AuxInspectorTarget, CalendarItem, CalendarWindow } from '../types';
import { CalendarMonthGrid } from './CalendarMonthGrid';
import {
  addDays,
  addMonths,
  type CalendarRangeMode,
  formatHourLabel,
  formatPeriodTitle,
  formatShortTime,
  getMonthGridDays,
  getWeekDays,
  itemToneClass,
  layoutTimedEvents,
  parseLocalYmd,
  toLocalYmd,
  WEEKDAY_SHORT,
} from './calendarUtils';

interface CalendarSurfaceProps {
  onInspect: (target: AuxInspectorTarget) => void;
  onOpenStudy: () => void;
  onToast: (msg: string) => void;
}

const HOURS = Array.from({ length: 24 }, (_, i) => i);
const HOUR_PX = 48;

export const CalendarSurface: React.FC<CalendarSurfaceProps> = ({
  onInspect,
  onOpenStudy,
  onToast,
}) => {
  const [now, setNow] = useState<Date>(() => new Date());
  const todayYmd = toLocalYmd(now);

  const [range, setRange] = useState<CalendarRangeMode>('week');
  const [anchorDate, setAnchorDate] = useState<Date>(() => parseLocalYmd(toLocalYmd(new Date())));
  const [selectedYmd, setSelectedYmd] = useState<string>(() => toLocalYmd(new Date()));
  const [win, setWin] = useState<CalendarWindow | null>(null);

  // Event composer / editor state
  const [showEditor, setShowEditor] = useState(false);
  const [editEventId, setEditEventId] = useState('');
  const [evTitle, setEvTitle] = useState('');
  const [evDate, setEvDate] = useState(todayYmd);
  const [evAllDay, setEvAllDay] = useState(false);
  const [evStartTime, setEvStartTime] = useState('09:00');
  const [evEndTime, setEvEndTime] = useState('10:00');
  const [evKind, setEvKind] = useState('appointment');
  const [evNotes, setEvNotes] = useState('');

  const timeFrameRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const visibleWindowBounds = (): { startYmd: string; endYmd: string; days: number } => {
    if (range === 'day') {
      const ymd = toLocalYmd(anchorDate);
      return { startYmd: ymd, endYmd: ymd, days: 1 };
    }
    if (range === 'week') {
      const wk = getWeekDays(anchorDate);
      return { startYmd: toLocalYmd(wk[0]), endYmd: toLocalYmd(wk[6]), days: 7 };
    }
    const grid = getMonthGridDays(anchorDate);
    return {
      startYmd: toLocalYmd(grid[0]),
      endYmd: toLocalYmd(grid[grid.length - 1]),
      days: grid.length,
    };
  };

  const loadCalendar = async () => {
    const { startYmd, endYmd, days } = visibleWindowBounds();
    const res = await callAction<CalendarWindow>('calendar', {
      subaction: 'window',
      start_date: startYmd,
      end_date: endYmd,
      days,
    });
    if (res.ok) {
      setWin(res as unknown as CalendarWindow);
    }
  };

  useEffect(() => {
    void loadCalendar();
  }, [range, toLocalYmd(anchorDate)]);

  useEffect(() => {
    if ((range === 'week' || range === 'day') && timeFrameRef.current) {
      // Scroll to 7:00 AM by default so working hours are visible immediately
      timeFrameRef.current.scrollTop = 7 * HOUR_PX;
    }
  }, [range]);

  const stepPeriod = (dir: -1 | 1) => {
    setAnchorDate((prev) => {
      const next =
        range === 'day'
          ? addDays(prev, dir)
          : range === 'week'
            ? addDays(prev, dir * 7)
            : addMonths(prev, dir);
      setSelectedYmd(toLocalYmd(next));
      return next;
    });
  };

  const jumpToToday = () => {
    const fresh = new Date();
    const clean = parseLocalYmd(toLocalYmd(fresh));
    setAnchorDate(clean);
    setSelectedYmd(toLocalYmd(clean));
    if (timeFrameRef.current) {
      timeFrameRef.current.scrollTop = Math.max(0, (fresh.getHours() - 1) * HOUR_PX);
    }
  };

  const openNewEventSlot = (ymd: string, hour?: number) => {
    setEditEventId('');
    setEvTitle('');
    setEvDate(ymd);
    if (hour === undefined) {
      setEvAllDay(true);
      setEvStartTime('09:00');
      setEvEndTime('10:00');
    } else {
      const h1 = String(Math.max(0, Math.min(23, hour))).padStart(2, '0');
      const h2 = String(Math.max(0, Math.min(23, hour + 1))).padStart(2, '0');
      setEvAllDay(false);
      setEvStartTime(`${h1}:00`);
      setEvEndTime(hour >= 23 ? '23:59' : `${h2}:00`);
    }
    setEvKind('appointment');
    setEvNotes('');
    setShowEditor(true);
  };

  const handleClickItem = (e: React.MouseEvent, it: CalendarItem) => {
    e.stopPropagation();
    if (it.card_id) {
      onInspect({ kind: 'work_item', itemId: it.card_id, projectId: it.board_id });
      return;
    }
    if (it.routine_id) {
      onInspect({ kind: 'routine', routineId: it.routine_id });
      return;
    }
    if (it.source_type === 'study_review' || it.source_type.includes('study')) {
      onOpenStudy();
      return;
    }
    if (it.source_type === 'event') {
      setEditEventId(it.id);
      setEvTitle(it.title);
      setEvDate(it.date);
      setEvAllDay(Boolean(it.all_day || !it.time));
      setEvStartTime(it.time || '09:00');
      setEvEndTime(it.end_time || '10:00');
      setEvKind(it.kind || 'appointment');
      setEvNotes(it.notes || '');
      setShowEditor(true);
    }
  };

  const saveEvent = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!evTitle.trim() || !evDate) return;
    const startAt = evAllDay ? evDate : `${evDate}T${evStartTime || '09:00'}`;
    const endAt = evAllDay ? '' : `${evDate}T${evEndTime || '10:00'}`;
    const res = await callAction('calendar', {
      subaction: 'add_event',
      event_id: editEventId,
      title: evTitle.trim(),
      start_at: startAt,
      end_at: endAt,
      all_day: evAllDay,
      kind: evKind,
      notes: evNotes.trim(),
    });
    if (res.ok) {
      setShowEditor(false);
      setEditEventId('');
      setEvTitle('');
      await loadCalendar();
      onToast(res.summary || 'Saved calendar event.');
    } else {
      onToast(res.error || 'Could not save event.');
    }
  };

  const deleteEvent = async (eventId: string) => {
    await callAction('calendar', { subaction: 'delete_event', event_id: eventId });
    setShowEditor(false);
    setEditEventId('');
    await loadCalendar();
    onToast('Deleted calendar event.');
  };

  const dayColumns = range === 'day' ? [anchorDate] : getWeekDays(anchorDate);
  const monthDays = range === 'month' ? getMonthGridDays(anchorDate) : [];
  const colGridTemplate = `56px repeat(${dayColumns.length}, minmax(0, 1fr))`;
  const nowMinutes = now.getHours() * 60 + now.getMinutes();
  const nowTopPx = Math.round((nowMinutes / 60) * HOUR_PX);

  return (
    <div className="cal-shell" data-testid="calendar-surface">
      {/* Google / Outlook Style Top Navigation Bar */}
      <div className="cal-toolbar">
        <div className="cal-nav-left">
          <button
            type="button"
            className="btn btn-sm"
            data-testid="calendar-today-btn"
            onClick={jumpToToday}
          >
            Today
          </button>
          <div style={{ display: 'inline-flex', gap: 2 }}>
            <button
              type="button"
              className="icon-btn"
              data-testid="calendar-prev-btn"
              onClick={() => stepPeriod(-1)}
              title={`Previous ${range}`}
              aria-label={`Previous ${range}`}
            >
              <ChevronLeft size={16} />
            </button>
            <button
              type="button"
              className="icon-btn"
              data-testid="calendar-next-btn"
              onClick={() => stepPeriod(1)}
              title={`Next ${range}`}
              aria-label={`Next ${range}`}
            >
              <ChevronRight size={16} />
            </button>
          </div>
          <h1 className="cal-period-title" data-testid="calendar-period-title">
            {formatPeriodTitle(range, anchorDate)}
          </h1>
          {win?.active_sprint && (
            <span className="badge accent">Timebox: {win.active_sprint.title}</span>
          )}
        </div>

        <div style={{ display: 'inline-flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <div className="segmented-pills">
            {(
              [
                ['day', 'Day'],
                ['week', 'Week'],
                ['month', 'Month'],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                data-testid={`cal-range-${id}`}
                className={`segmented-pill ${range === id ? 'active' : ''}`}
                onClick={() => setRange(id)}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            data-testid="cal-open-event-btn"
            onClick={() => openNewEventSlot(selectedYmd, range === 'month' ? undefined : 9)}
          >
            <Plus size={13} /> Event
          </button>
        </div>
      </div>

      {/* Inline Create / Edit Event Composer */}
      {showEditor && (
        <form onSubmit={(e) => void saveEvent(e)} className="card-group" style={{ padding: 14, marginBottom: 0, flexShrink: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <strong style={{ fontSize: 13 }}>{editEventId ? 'Edit Event' : 'Schedule Event'}</strong>
            <button type="button" className="icon-btn" onClick={() => setShowEditor(false)} aria-label="Close event form">
              <X size={14} />
            </button>
          </div>
          <div className="form-grid-event">
            <div>
              <label className="field-label">Title</label>
              <input
                className="input"
                placeholder="Dentist, Focus block, Milestone..."
                value={evTitle}
                onChange={(e) => setEvTitle(e.target.value)}
                autoFocus
              />
            </div>
            <div>
              <label className="field-label">Date</label>
              <input
                type="date"
                className="input mono"
                value={evDate}
                onChange={(e) => setEvDate(e.target.value)}
              />
            </div>
            <div>
              <label className="field-label">Category</label>
              <select className="select" value={evKind} onChange={(e) => setEvKind(e.target.value)}>
                <option value="appointment">Appointment</option>
                <option value="focus_block">Focus Block</option>
                <option value="milestone">Milestone</option>
                <option value="reminder">Reminder</option>
              </select>
            </div>
            <div style={{ display: 'inline-flex', gap: 6 }}>
              {editEventId && (
                <button
                  type="button"
                  className="btn btn-danger"
                  onClick={() => void deleteEvent(editEventId)}
                  title="Delete event"
                >
                  <Trash2 size={14} />
                </button>
              )}
              <button type="submit" className="btn btn-primary">
                Save
              </button>
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 10, flexWrap: 'wrap' }}>
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12.5 }}>
              <input
                type="checkbox"
                checked={evAllDay}
                onChange={(e) => setEvAllDay(e.target.checked)}
              />
              <span>All-day</span>
            </label>
            {!evAllDay && (
              <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <input
                  type="time"
                  className="input mono"
                  style={{ width: 122, padding: '4px 8px' }}
                  value={evStartTime}
                  onChange={(e) => setEvStartTime(e.target.value)}
                />
                <span style={{ color: 'var(--text-muted)' }}>–</span>
                <input
                  type="time"
                  className="input mono"
                  style={{ width: 122, padding: '4px 8px' }}
                  value={evEndTime}
                  onChange={(e) => setEvEndTime(e.target.value)}
                />
              </div>
            )}
            <input
              className="input"
              style={{ flex: 1, minWidth: 180, padding: '4px 10px' }}
              placeholder="Optional location or notes..."
              value={evNotes}
              onChange={(e) => setEvNotes(e.target.value)}
            />
          </div>
        </form>
      )}

      {/* DAY & WEEK VIEW: Day Columns + All-Day Row + 24-Hour Time-Slot Grid */}
      {(range === 'day' || range === 'week') && (
        <div className="cal-time-frame" ref={timeFrameRef} data-testid={`cal-${range}-grid`}>
          <div className={`cal-time-inner ${range === 'week' ? 'week-cols' : ''}`}>
            {/* Sticky Day Headers + All-Day Strip */}
            <div className="cal-time-head-wrap">
              <div className="cal-time-head-grid" style={{ gridTemplateColumns: colGridTemplate }}>
                <div className="cal-gutter-head" />
                {dayColumns.map((d) => {
                  const ymd = toLocalYmd(d);
                  const isToday = ymd === todayYmd;
                  return (
                    <div
                      key={ymd}
                      className="cal-col-head"
                      onClick={() => {
                        setAnchorDate(d);
                        setSelectedYmd(ymd);
                        if (range === 'week') setRange('day');
                      }}
                      title={range === 'week' ? `Open ${ymd} Day view` : ymd}
                    >
                      <div style={{ fontSize: 11, color: isToday ? 'var(--accent-primary)' : 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase' }}>
                        {WEEKDAY_SHORT[d.getDay()]}
                      </div>
                      <div style={{ marginTop: 2 }}>
                        <span className={`cal-day-num ${isToday ? 'today' : ''}`} style={{ fontSize: 14 }}>
                          {d.getDate()}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* All-Day Events, Deadlines, Tasks, Milestones & Due Study */}
              <div className="cal-allday-wrap">
                <div className="cal-allday-grid" style={{ gridTemplateColumns: colGridTemplate }}>
                  <div
                    className="cal-gutter-head"
                    style={{ fontSize: 10, color: 'var(--text-muted)', display: 'flex', alignItems: 'center', justifyContent: 'flex-end', paddingRight: 6 }}
                  >
                    all-day
                  </div>
                  {dayColumns.map((d) => {
                    const ymd = toLocalYmd(d);
                    const allDayItems = (win?.by_date?.[ymd] || []).filter((it) => it.all_day || !it.time);
                    return (
                      <div
                        key={ymd}
                        className="cal-allday-cell"
                        onClick={() => openNewEventSlot(ymd)}
                        title={`Add all-day event on ${ymd}`}
                      >
                        {allDayItems.map((it) => (
                          <div
                            key={it.id}
                            className={`cal-chip ${itemToneClass(it)}`}
                            onClick={(e) => handleClickItem(e, it)}
                            title={it.title}
                          >
                            {it.title}
                          </div>
                        ))}
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>

            {/* 24-Hour Time Slots (`12 AM` .. `11 PM`) */}
            <div className="cal-body-grid" style={{ gridTemplateColumns: colGridTemplate }}>
              <div className="cal-gutter-col">
                {HOURS.map((h) => (
                  <div key={h} className="cal-hour-label">
                    {h === 0 ? '' : formatHourLabel(h)}
                  </div>
                ))}
              </div>

              {dayColumns.map((d) => {
                const ymd = toLocalYmd(d);
                const isToday = ymd === todayYmd;
                const timedItems = (win?.by_date?.[ymd] || []).filter((it) => !it.all_day && Boolean(it.time));
                const placed = layoutTimedEvents(timedItems, HOUR_PX);

                return (
                  <div key={ymd} className="cal-day-track" data-testid={`cal-track-${ymd}`}>
                    {HOURS.map((h) => (
                      <div
                        key={h}
                        className="cal-hour-slot"
                        onClick={() => openNewEventSlot(ymd, h)}
                        title={`Schedule at ${formatHourLabel(h)} on ${ymd}`}
                      />
                    ))}

                    {isToday && <div className="cal-now-line" style={{ top: nowTopPx }} />}

                    {placed.map(({ item, top, height, colIdx, totalCols }) => {
                      const widthPct = 100 / totalCols;
                      const leftPct = colIdx * widthPct;
                      return (
                        <div
                          key={item.id}
                          className={`cal-timed-block ${itemToneClass(item)}`}
                          style={{
                            top,
                            height,
                            left: `calc(${leftPct}% + 2px)`,
                            width: `calc(${widthPct}% - 4px)`,
                          }}
                          onClick={(e) => handleClickItem(e, item)}
                          title={`${item.time}${item.end_time ? `–${item.end_time}` : ''} ${item.title}`}
                        >
                          <div style={{ fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {formatShortTime(item.time)} {item.title}
                          </div>
                          {height >= 38 && item.notes && (
                            <div style={{ fontSize: 10.5, color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {item.notes}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}

      {/* MONTH VIEW: Full 7-Column Calendar Grid (`Sun .. Sat`) + Selected Day Agenda */}
      {range === 'month' && (
        <CalendarMonthGrid
          anchorDate={anchorDate}
          monthDays={monthDays}
          todayYmd={todayYmd}
          selectedYmd={selectedYmd}
          win={win}
          onSelectDay={(ymd) => setSelectedYmd(ymd)}
          onOpenDayView={(ymd) => {
            setAnchorDate(parseLocalYmd(ymd));
            setRange('day');
          }}
          onOpenNewSlot={openNewEventSlot}
          onClickItem={handleClickItem}
          onDeleteEvent={deleteEvent}
        />
      )}
    </div>
  );
};

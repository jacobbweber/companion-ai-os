import React from 'react';
import { Calendar as CalIcon, Clock, Plus, Trash2 } from 'lucide-react';
import type { CalendarItem, CalendarWindow } from '../types';
import {
  formatPeriodTitle,
  formatShortTime,
  itemToneClass,
  MONTH_SHORT,
  parseLocalYmd,
  toLocalYmd,
  WEEKDAY_SHORT,
} from './calendarUtils';

interface CalendarMonthGridProps {
  anchorDate: Date;
  monthDays: Date[];
  todayYmd: string;
  selectedYmd: string;
  win: CalendarWindow | null;
  onSelectDay: (ymd: string) => void;
  onOpenDayView: (ymd: string) => void;
  onOpenNewSlot: (ymd: string, hour?: number) => void;
  onClickItem: (e: React.MouseEvent, it: CalendarItem) => void;
  onDeleteEvent: (eventId: string) => Promise<void>;
}

export const CalendarMonthGrid: React.FC<CalendarMonthGridProps> = ({
  anchorDate,
  monthDays,
  todayYmd,
  selectedYmd,
  win,
  onSelectDay,
  onOpenDayView,
  onOpenNewSlot,
  onClickItem,
  onDeleteEvent,
}) => {
  const selectedDayItems = win?.by_date?.[selectedYmd] || [];

  return (
    <div className="cal-month-wrap" data-testid="cal-month-grid">
      <div className="cal-month-frame">
        <div className="cal-weekday-bar">
          {WEEKDAY_SHORT.map((wd) => (
            <div key={wd} className="cal-weekday-cell">
              {wd}
            </div>
          ))}
        </div>

        <div className="cal-month-grid">
          {monthDays.map((d) => {
            const ymd = toLocalYmd(d);
            const inMonth = d.getMonth() === anchorDate.getMonth();
            const isToday = ymd === todayYmd;
            const isSelected = ymd === selectedYmd;
            const dayItems = win?.by_date?.[ymd] || [];
            const dayLabel =
              d.getDate() === 1 ? `${MONTH_SHORT[d.getMonth()]} 1` : String(d.getDate());

            return (
              <div
                key={ymd}
                data-testid={`cal-month-cell-${ymd}`}
                className={`cal-month-cell ${inMonth ? '' : 'outside'} ${isSelected ? 'selected' : ''}`}
                onClick={() => onSelectDay(ymd)}
                onDoubleClick={() => onOpenNewSlot(ymd)}
              >
                <div className="cal-day-num-row">
                  <span className={`cal-day-num ${isToday ? 'today' : ''}`}>{dayLabel}</span>
                  {isSelected && (
                    <button
                      type="button"
                      className="icon-btn"
                      style={{ width: 20, height: 20 }}
                      title={`Add event on ${ymd}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        onOpenNewSlot(ymd);
                      }}
                    >
                      <Plus size={11} />
                    </button>
                  )}
                </div>

                {dayItems.slice(0, 3).map((it) => (
                  <div
                    key={it.id}
                    className={`cal-chip ${itemToneClass(it)}`}
                    onClick={(e) => onClickItem(e, it)}
                    title={it.title}
                  >
                    {it.time ? `${formatShortTime(it.time)} ` : ''}
                    {it.title}
                  </div>
                ))}
                {dayItems.length > 3 && (
                  <div style={{ fontSize: 10.5, color: 'var(--text-muted)', paddingLeft: 4 }}>
                    +{dayItems.length - 3} more
                  </div>
                )}

                {dayItems.length > 0 && (
                  <div className="cal-dots-mobile">
                    {dayItems.slice(0, 4).map((it) => (
                      <span key={it.id} className="cal-dot" />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Selected Day Schedule Strip */}
      <div className="card-group" style={{ marginBottom: 0 }}>
        <div className="card-row" style={{ background: 'var(--bg-elevated)' }}>
          <strong style={{ fontSize: 12.5 }}>
            {formatPeriodTitle('day', parseLocalYmd(selectedYmd))} ({selectedDayItems.length})
          </strong>
          <div style={{ display: 'inline-flex', gap: 6 }}>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => onOpenDayView(selectedYmd)}
            >
              Open Day View
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => onOpenNewSlot(selectedYmd, 9)}
            >
              <Plus size={12} /> Add Event
            </button>
          </div>
        </div>
        {selectedDayItems.length === 0 ? (
          <div className="card-row">
            <span style={{ color: 'var(--text-muted)', fontSize: 12.5 }}>
              No events, deadlines, or routines on this day — tap + Add Event to schedule.
            </span>
          </div>
        ) : (
          selectedDayItems.map((it) => (
            <div
              key={it.id}
              className="card-row"
              style={{ cursor: 'pointer' }}
              onClick={(e) => onClickItem(e, it)}
            >
              <div className="card-row-info">
                <div className="card-row-title" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <CalIcon size={13} style={{ color: 'var(--accent-primary)' }} />
                  <span>{it.title}</span>
                  <span className="badge">{it.source_type.replace(/_/g, ' ')}</span>
                </div>
                <div className="card-row-desc">
                  <Clock size={11} style={{ verticalAlign: '-1px', marginRight: 4 }} />
                  {it.all_day || !it.time ? 'All-day' : `${it.time}${it.end_time ? ` – ${it.end_time}` : ''}`}
                </div>
              </div>
              {it.source_type === 'event' && (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={(e) => {
                    e.stopPropagation();
                    void onDeleteEvent(it.id);
                  }}
                >
                  <Trash2 size={13} />
                </button>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
};

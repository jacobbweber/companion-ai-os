import React, { useEffect, useState } from 'react';
import { Calendar as CalIcon, Clock, Plus, Trash2 } from 'lucide-react';
import { callAction } from '../api';
import type { CalendarItem, CalendarWindow } from '../types';

interface CalendarSurfaceProps {
  onToast: (msg: string) => void;
}

export const CalendarSurface: React.FC<CalendarSurfaceProps> = ({ onToast }) => {
  const [range, setRange] = useState<'today' | 'week' | 'month'>('week');
  const [win, setWin] = useState<CalendarWindow | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [title, setTitle] = useState('');
  const [startAt, setStartAt] = useState('');
  const [kind, setKind] = useState('appointment');

  const loadCalendar = async () => {
    const res = await callAction<CalendarWindow>('calendar', { subaction: range });
    if (res.ok) {
      setWin(res as unknown as CalendarWindow);
      if (!startAt && res.today) setStartAt(res.today);
    }
  };

  useEffect(() => {
    void loadCalendar();
  }, [range]);

  const addEvent = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim() || !startAt.trim()) return;
    const res = await callAction('calendar', {
      subaction: 'add_event',
      title: title.trim(),
      start_at: startAt.trim(),
      kind,
    });
    if (res.ok) {
      setTitle('');
      setShowAdd(false);
      await loadCalendar();
      onToast(res.summary || 'Added calendar event.');
    } else {
      onToast(res.error || 'Could not add event.');
    }
  };

  const deleteEvent = async (eventId: string) => {
    await callAction('calendar', { subaction: 'delete_event', event_id: eventId });
    await loadCalendar();
    onToast('Removed event.');
  };

  const dates = Object.keys(win?.by_date || {}).sort();

  return (
    <div className="surface-scroll" data-testid="calendar-surface">
      <div className="surface-container" style={{ maxWidth: 880 }}>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            gap: 12,
            flexWrap: 'wrap',
            marginBottom: 16,
          }}
        >
          <div>
            <h1 style={{ margin: 0, fontSize: 19, fontWeight: 600 }}>Calendar</h1>
            <p style={{ margin: '3px 0 0', fontSize: 12.5, color: 'var(--text-secondary)' }}>
              {win ? `${win.start_date} – ${win.end_date}` : 'Unified timeline'} • Events, goal dates, routines &amp; due study reviews.
            </p>
          </div>

          <div style={{ display: 'inline-flex', gap: 8, alignItems: 'center' }}>
            <div className="segmented-pills">
              {(['today', 'week', 'month'] as const).map((r) => (
                <button
                  key={r}
                  type="button"
                  className={`segmented-pill ${range === r ? 'active' : ''}`}
                  onClick={() => setRange(r)}
                  style={{ textTransform: 'capitalize' }}
                >
                  {r}
                </button>
              ))}
            </div>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => setShowAdd((v) => !v)}
            >
              <Plus size={13} /> Event
            </button>
          </div>
        </div>

        {showAdd && (
          <form onSubmit={(e) => void addEvent(e)} className="card-group" style={{ padding: 14 }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 180px 150px auto', gap: 10, alignItems: 'end' }}>
              <div>
                <label className="field-label">Event Title</label>
                <input className="input" placeholder="Dentist, Focus block, Milestone..." value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
              </div>
              <div>
                <label className="field-label">Date / Time</label>
                <input className="input mono" placeholder="YYYY-MM-DD" value={startAt} onChange={(e) => setStartAt(e.target.value)} />
              </div>
              <div>
                <label className="field-label">Category</label>
                <select className="select" value={kind} onChange={(e) => setKind(e.target.value)}>
                  <option value="appointment">Appointment</option>
                  <option value="focus_block">Focus Block</option>
                  <option value="milestone">Milestone</option>
                  <option value="reminder">Reminder</option>
                </select>
              </div>
              <button type="submit" className="btn btn-primary">
                Save
              </button>
            </div>
          </form>
        )}

        {dates.length === 0 ? (
          <div className="card-group">
            <div className="card-row">
              <span style={{ color: 'var(--text-muted)' }}>
                Clear schedule in this window — no upcoming deadlines, events, or scheduled routines.
              </span>
            </div>
          </div>
        ) : (
          dates.map((dt) => {
            const items: CalendarItem[] = win?.by_date?.[dt] || [];
            if (items.length === 0) return null;
            return (
              <div key={dt} style={{ marginBottom: 14 }}>
                <div className="field-label" style={{ marginBottom: 6 }}>
                  {dt} {dt === win?.today ? '• Today' : ''}
                </div>
                <div className="card-group" style={{ marginBottom: 0 }}>
                  {items.map((it) => (
                    <div key={it.id} className="card-row">
                      <div className="card-row-info">
                        <div className="card-row-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <CalIcon size={13} style={{ color: 'var(--accent-primary)' }} />
                          <span>{it.title}</span>
                          <span className="badge">{it.source_type.replace(/_/g, ' ')}</span>
                        </div>
                        {it.time && (
                          <div className="card-row-desc">
                            <Clock size={11} style={{ verticalAlign: '-1px', marginRight: 4 }} />
                            {it.time}
                          </div>
                        )}
                      </div>
                      {it.source_type === 'event' && (
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm"
                          onClick={() => void deleteEvent(it.id)}
                        >
                          <Trash2 size={13} />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};

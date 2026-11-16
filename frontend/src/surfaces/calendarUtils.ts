import type { CalendarItem } from '../types';

export type CalendarRangeMode = 'day' | 'week' | 'month';

export const WEEKDAY_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as const;
export const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
] as const;
export const MONTH_SHORT = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
] as const;

export function toLocalYmd(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

export function parseLocalYmd(ymd: string): Date {
  const parts = (ymd || '').slice(0, 10).split('-').map(Number);
  if (parts.length === 3 && !parts.some(Number.isNaN)) {
    return new Date(parts[0], parts[1] - 1, parts[2], 12, 0, 0, 0);
  }
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate(), 12, 0, 0, 0);
}

export function addDays(d: Date, delta: number): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + delta, 12, 0, 0, 0);
}

export function addMonths(d: Date, delta: number): Date {
  const targetMonth = d.getMonth() + delta;
  const firstOfTarget = new Date(d.getFullYear(), targetMonth, 1, 12, 0, 0, 0);
  const daysInTarget = new Date(firstOfTarget.getFullYear(), firstOfTarget.getMonth() + 1, 0).getDate();
  return new Date(
    firstOfTarget.getFullYear(),
    firstOfTarget.getMonth(),
    Math.min(d.getDate(), daysInTarget),
    12,
    0,
    0,
    0
  );
}

export function getWeekDays(anchor: Date): Date[] {
  const sun = addDays(anchor, -anchor.getDay());
  return Array.from({ length: 7 }, (_, i) => addDays(sun, i));
}

export function getMonthGridDays(anchor: Date): Date[] {
  const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1, 12, 0, 0, 0);
  const last = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0, 12, 0, 0, 0);
  const startSun = addDays(first, -first.getDay());
  const endSat = addDays(last, 6 - last.getDay());
  const days: Date[] = [];
  let cur = startSun;
  while (cur.getTime() <= endSat.getTime() && days.length < 42) {
    days.push(cur);
    cur = addDays(cur, 1);
  }
  return days;
}

export function formatPeriodTitle(range: CalendarRangeMode, anchor: Date): string {
  if (range === 'month') {
    return `${MONTH_NAMES[anchor.getMonth()]} ${anchor.getFullYear()}`;
  }
  if (range === 'day') {
    return `${WEEKDAY_SHORT[anchor.getDay()]}, ${MONTH_NAMES[anchor.getMonth()]} ${anchor.getDate()}, ${anchor.getFullYear()}`;
  }
  const wk = getWeekDays(anchor);
  const first = wk[0];
  const last = wk[6];
  if (first.getMonth() === last.getMonth()) {
    return `${MONTH_SHORT[first.getMonth()]} ${first.getDate()} – ${last.getDate()}, ${last.getFullYear()}`;
  }
  if (first.getFullYear() === last.getFullYear()) {
    return `${MONTH_SHORT[first.getMonth()]} ${first.getDate()} – ${MONTH_SHORT[last.getMonth()]} ${last.getDate()}, ${last.getFullYear()}`;
  }
  return `${MONTH_SHORT[first.getMonth()]} ${first.getDate()}, ${first.getFullYear()} – ${MONTH_SHORT[last.getMonth()]} ${last.getDate()}, ${last.getFullYear()}`;
}

export function formatHourLabel(hour: number): string {
  if (hour === 0) return '12 AM';
  if (hour < 12) return `${hour} AM`;
  if (hour === 12) return '12 PM';
  return `${hour - 12} PM`;
}

export function formatShortTime(hhmm: string): string {
  if (!hhmm || hhmm.length < 4) return '';
  const [hStr, mStr] = hhmm.split(':');
  const h = Number(hStr);
  const m = Number(mStr || '0');
  if (Number.isNaN(h)) return hhmm;
  const suffix = h >= 12 ? 'p' : 'a';
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return m === 0 ? `${h12}${suffix}` : `${h12}:${String(m).padStart(2, '0')}${suffix}`;
}

export function parseMinutes(hhmm?: string): number | null {
  if (!hhmm || !hhmm.includes(':')) return null;
  const [hStr, mStr] = hhmm.split(':');
  const h = Number(hStr);
  const m = Number(mStr);
  if (Number.isNaN(h) || Number.isNaN(m)) return null;
  return Math.max(0, Math.min(24 * 60, h * 60 + m));
}

export interface PositionedCalendarBlock {
  item: CalendarItem;
  top: number;
  height: number;
  colIdx: number;
  totalCols: number;
}

export function layoutTimedEvents(items: CalendarItem[], hourPx = 48): PositionedCalendarBlock[] {
  const parsed = items
    .map((item) => {
      const startMin = parseMinutes(item.time) ?? 9 * 60;
      const rawEnd = parseMinutes(item.end_time);
      const endMin = rawEnd && rawEnd > startMin ? rawEnd : Math.min(24 * 60, startMin + 60);
      return { item, startMin, endMin };
    })
    .sort((a, b) => a.startMin - b.startMin || b.endMin - a.endMin);

  const columnsEnd: number[] = [];
  const placed = parsed.map((entry) => {
    let colIdx = columnsEnd.findIndex((endAt) => endAt <= entry.startMin);
    if (colIdx === -1) {
      colIdx = columnsEnd.length;
      columnsEnd.push(entry.endMin);
    } else {
      columnsEnd[colIdx] = entry.endMin;
    }
    return { ...entry, colIdx };
  });

  return placed.map((entry) => {
    const overlappingCols = placed
      .filter((other) => other.startMin < entry.endMin && other.endMin > entry.startMin)
      .reduce((mx, o) => Math.max(mx, o.colIdx + 1), 1);
    const top = Math.round((entry.startMin / 60) * hourPx);
    const height = Math.max(22, Math.round(((entry.endMin - entry.startMin) / 60) * hourPx) - 2);
    return {
      item: entry.item,
      top,
      height,
      colIdx: entry.colIdx,
      totalCols: overlappingCols,
    };
  });
}

export function itemToneClass(it: CalendarItem): string {
  if (it.kind === 'focus_block' || it.source_type === 'sprint_task') return 'cal-tone-green';
  if (it.source_type === 'deadline' || it.kind === 'milestone' || it.source_type === 'epic_milestone') {
    return 'cal-tone-amber';
  }
  if (it.source_type === 'routine' || it.source_type === 'study_review') return 'cal-tone-purple';
  return 'cal-tone-blue';
}

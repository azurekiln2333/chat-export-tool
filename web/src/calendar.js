/**
 * Pure calendar helpers for the month grid.
 *
 * The month grid and month stepping use UTC arithmetic (`Date.UTC` and
 * `getUTC*`) only, so the viewer's computer timezone can never shift a day or
 * skip a month. Day buckets themselves come from the server, which computes
 * them in the export timezone.
 */

export const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日'];

const MONTH_PATTERN = /^(\d{4})-(\d{2})$/;
const DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;

export function isMonth(value) {
  return MONTH_PATTERN.test(String(value || ''));
}

export function isDate(value) {
  return DATE_PATTERN.test(String(value || ''));
}

/** `YYYY-MM-DD...` -> `YYYY-MM`, otherwise ''. */
export function monthOf(dateString) {
  const match = /^(\d{4}-\d{2})/.exec(String(dateString || ''));
  return match ? match[1] : '';
}

export function parseMonth(month) {
  const match = MONTH_PATTERN.exec(String(month || ''));
  if (!match) return null;
  const year = Number(match[1]);
  const index = Number(match[2]);
  if (index < 1 || index > 12) return null;
  return { year, month: index };
}

/** Step a month by `delta` months; returns the input when it is not a month. */
export function shiftMonth(month, delta) {
  const parts = parseMonth(month);
  if (!parts) return month;
  let year = parts.year;
  let index = parts.month - 1 + Number(delta || 0);
  year += Math.floor(index / 12);
  index = ((index % 12) + 12) % 12;
  return `${year}-${String(index + 1).padStart(2, '0')}`;
}

export function formatMonth(month) {
  const parts = parseMonth(month);
  return parts ? `${parts.year} 年 ${parts.month} 月` : '—';
}

/** Build the Monday-first grid: leading blanks, then one cell per day. */
export function buildMonthGrid(month, counts = new Map()) {
  const parts = parseMonth(month);
  if (!parts) return [];
  const firstWeekday = new Date(Date.UTC(parts.year, parts.month - 1, 1)).getUTCDay();
  const leading = (firstWeekday + 6) % 7; // 0 = Monday
  const daysInMonth = new Date(Date.UTC(parts.year, parts.month, 0)).getUTCDate();
  const cells = [];
  for (let index = 0; index < leading; index += 1) cells.push(null);
  for (let day = 1; day <= daysInMonth; day += 1) {
    const date = `${parts.year}-${String(parts.month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    const count = Number(counts.get(date)) || 0;
    cells.push({ date, day, count });
  }
  return cells;
}

/** `[{date, message_count}]` -> `Map(date -> count)`. */
export function countsByDate(days) {
  const map = new Map();
  for (const day of Array.isArray(days) ? days : []) {
    if (day && day.date) map.set(day.date, Number(day.message_count) || 0);
  }
  return map;
}

export function totalOfDays(days) {
  return (Array.isArray(days) ? days : []).reduce(
    (sum, day) => sum + (Number(day && day.message_count) || 0),
    0,
  );
}
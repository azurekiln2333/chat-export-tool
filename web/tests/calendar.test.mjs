/**
 * Calendar, day filter and timezone contract tests (web/server/store.mjs).
 *
 * The fixture's dated mode spreads messages across a leap day, a month
 * boundary, a year boundary and a second conversation, and marks the export
 * timezone explicitly. Day bucketing must follow the export timezone, never the
 * machine running the tests.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { ACCOUNT_A, ACCOUNT_B, CONVERSATION_A, CONVERSATION_C, UUID } from './helpers/fixture.mjs';
import { capture, expectStatus, openStoreSet, requireMethods } from './helpers/store-tools.mjs';

let ChatStore = null;
let storeModuleError = null;
try {
  ChatStore = (await import('../server/store.mjs')).ChatStore;
} catch (error) {
  storeModuleError = error;
}
if (storeModuleError) {
  throw new Error(`web/server/store.mjs could not be imported: ${storeModuleError.code || storeModuleError.message}`);
}
requireMethods(ChatStore, ['getCalendar', 'listMessages', 'updateMessage', 'deleteMessage']);

/** Month -> recorded day counts for the dated fixture in Asia/Shanghai. */
const SHANGHAI_MONTHS = {
  '2023-12': { '2023-12-31': 1 },
  '2024-02': { '2024-02-28': 1, '2024-02-29': 3 },
  '2024-03': { '2024-03-01': 1, '2024-03-02': 2 },
  '2026-10': { '2026-10-27': 7 },
};

function dayMap(calendar) {
  return Object.fromEntries(calendar.days.map((day) => [day.date, day.message_count]));
}

function assertCalendar(calendar, expected, { label = '', total, first, last } = {}) {
  assert.equal(typeof calendar.month, 'string', `${label}: month must be a string`);
  assert.deepEqual(dayMap(calendar), expected, `${label}: recorded days mismatch`);
  assert.deepEqual(calendar.days, [...calendar.days].sort((a, b) => a.date.localeCompare(b.date)),
    `${label}: days must be sorted`);
  for (const day of calendar.days) {
    assert.match(day.date, /^\d{4}-\d{2}-\d{2}$/, `${label}: day keys must be dates`);
    assert.equal(typeof day.message_count, 'number');
    assert.ok(day.message_count > 0, `${label}: a recorded day must have a positive count`);
  }
  if (total !== undefined) assert.equal(calendar.total, total, `${label}: month total`);
  if (first !== undefined) assert.equal(calendar.first_date, first, `${label}: first_date`);
  if (last !== undefined) assert.equal(calendar.last_date, last, `${label}: last_date`);
}

test('calendar reports recorded days, month totals and the whole-scope range', async (t) => {
  const { fixture, store } = await openStoreSet(t, { dated: true });
  const scope = { first: '2023-12-31', last: '2026-10-27' };

  for (const [month, expected] of Object.entries(SHANGHAI_MONTHS)) {
    const calendar = store.getCalendar(ACCOUNT_A, { month });
    assertCalendar(calendar, expected, { label: month, total: Object.values(expected)
      .reduce((sum, value) => sum + value, 0), ...scope });
    assert.equal(calendar.timezone, fixture.timeZone);
  }

  const empty = store.getCalendar(ACCOUNT_A, { month: '2024-01' });
  assertCalendar(empty, {}, { label: 'empty month', total: 0, ...scope });

  const other = store.getCalendar(ACCOUNT_B, { month: '2026-10' });
  assertCalendar(other, { '2026-10-27': 1 }, { label: 'account B', total: 1,
    first: '2026-10-27', last: '2026-10-27' });
});

test('month and date filters reject malformed and impossible values', async (t) => {
  const { store } = await openStoreSet(t, { dated: true });

  for (const month of ['', '2024-1', '2024-13', '2024-00', '202402', 'abcd', '2024-02-29', '2024/02']) {
    const outcome = await capture(() => store.getCalendar(ACCOUNT_A, { month }));
    expectStatus(outcome, 400, `month ${JSON.stringify(month)}`);
  }

  const badDates = ['2024-2-01', '2024-02-30', '2023-02-29', '1900-02-29', '2024-04-31',
    '2024-13-01', '01-01-2024', 'not-a-date', '2024-02-29T00:00:00'];
  for (const date of badDates) {
    const outcome = await capture(() => store.listMessages(ACCOUNT_A, { date, limit: 500 }));
    expectStatus(outcome, 400, `date ${JSON.stringify(date)}`);
  }

  // Century leap rules and an empty filter must still be accepted.
  for (const date of ['2000-02-29', '2024-02-29', '']) {
    const outcome = await capture(() => store.listMessages(ACCOUNT_A, { date, limit: 500 }));
    assert.equal(outcome.ok, true, `date ${JSON.stringify(date)} was rejected`);
  }
});

test('the day filter selects one day and paginates inside it', async (t) => {
  const { store } = await openStoreSet(t, { dated: true });

  const all = store.listMessages(ACCOUNT_A, { limit: 500 });
  assert.equal(all.total, 16);
  assert.ok(all.items.some((item) => item.msg_uuid === UUID.undated),
    'a message without a day must stay queryable without a date filter');

  const leap = store.listMessages(ACCOUNT_A, { date: '2024-02-29', limit: 500 });
  assert.equal(leap.total, 3);
  assert.deepEqual([...leap.items.map((item) => item.msg_uuid)].sort(),
    [UUID.datedLeapA, UUID.datedLeapB, UUID.timezoneProbe].sort());
  assert.ok(!leap.items.some((item) => item.msg_uuid === UUID.undated),
    'a day filter must exclude dateless rows');

  const seen = [];
  for (let offset = 0; offset < 3; offset += 1) {
    const page = store.listMessages(ACCOUNT_A, { date: '2024-02-29', limit: 1, offset });
    assert.equal(page.total, 3);
    assert.equal(page.limit, 1);
    assert.equal(page.offset, offset);
    assert.equal(page.items.length, 1);
    seen.push(page.items[0].msg_uuid);
  }
  assert.equal(new Set(seen).size, 3, 'day pagination repeated a row');
  const beyond = store.listMessages(ACCOUNT_A, { date: '2024-02-29', limit: 1, offset: 99 });
  assert.equal(beyond.items.length, 0);

  assert.equal(store.listMessages(ACCOUNT_A, { date: '2026-10-27', limit: 500 }).total, 7);
  assert.equal(store.listMessages(ACCOUNT_A, { date: '2030-01-01', limit: 500 }).total, 0);
});

test('day bucketing follows the export timezone, not the test machine', async (t) => {
  const shanghai = await openStoreSet(t, { dated: true, timeZone: 'Asia/Shanghai' });
  const newYork = await openStoreSet(t, { dated: true, timeZone: 'America/New_York' });

  const eastern = shanghai.store.getCalendar(ACCOUNT_A, { month: '2024-02' });
  const western = newYork.store.getCalendar(ACCOUNT_A, { month: '2024-02' });

  assert.equal(shanghai.fixture.timeZone, 'Asia/Shanghai');
  assert.equal(newYork.fixture.timeZone, 'America/New_York');
  assert.equal(eastern.timezone, 'Asia/Shanghai');
  assert.equal(western.timezone, 'America/New_York');

  // 2024-02-28T16:30:00Z is the leap day in Shanghai but still February 28 in
  // New York, so the same row must land on different days.
  assert.deepEqual(dayMap(eastern), { '2024-02-28': 1, '2024-02-29': 3 });
  assert.deepEqual(dayMap(western), { '2024-02-28': 2, '2024-02-29': 2 });

  assert.equal(shanghai.store.listMessages(ACCOUNT_A, { date: '2024-02-29', limit: 500 }).total, 3);
  assert.equal(shanghai.store.listMessages(ACCOUNT_A, { date: '2024-02-28', limit: 500 }).total, 1);
  assert.equal(newYork.store.listMessages(ACCOUNT_A, { date: '2024-02-29', limit: 500 }).total, 2);
  assert.equal(newYork.store.listMessages(ACCOUNT_A, { date: '2024-02-28', limit: 500 }).total, 2);
});

test('calendar counts follow conversation, search and hidden scope', async (t) => {
  const { store } = await openStoreSet(t, { dated: true });

  const byConversation = store.getCalendar(ACCOUNT_A, {
    month: '2024-03', conversationId: CONVERSATION_C,
  });
  assertCalendar(byConversation, { '2024-03-02': 2 }, {
    label: 'conversation scope', total: 2, first: '2024-03-02', last: '2024-03-02',
  });

  const bySearch = store.getCalendar(ACCOUNT_A, { month: '2024-02', q: 'leap day' });
  assertCalendar(bySearch, { '2024-02-29': 2 }, {
    label: 'search scope', total: 2, first: '2024-02-29', last: '2024-02-29',
  });

  const scopedSearch = store.getCalendar(ACCOUNT_A, {
    month: '2024-03', conversationId: CONVERSATION_A, q: 'second chat',
  });
  assertCalendar(scopedSearch, {}, { label: 'search outside conversation', total: 0 });

  store.updateMessage(ACCOUNT_A, UUID.datedLeapB, { hidden: true, expected_revision: 0 });
  const visible = store.getCalendar(ACCOUNT_A, { month: '2024-02' });
  assertCalendar(visible, { '2024-02-28': 1, '2024-02-29': 2 }, {
    label: 'hidden excluded', total: 3, first: '2023-12-31', last: '2026-10-27',
  });
  const withHidden = store.getCalendar(ACCOUNT_A, { month: '2024-02', includeHidden: true });
  assertCalendar(withHidden, { '2024-02-28': 1, '2024-02-29': 3 },
    { label: 'hidden included', total: 4 });
});

test('day counts change immediately after a hide, an edit and a delete', async (t) => {
  const { store } = await openStoreSet(t, { dated: true });

  store.updateMessage(ACCOUNT_A, UUID.datedFeb28, {
    text_override: 'unique searchable phrase', expected_revision: 0,
  });
  const edited = store.getCalendar(ACCOUNT_A, { month: '2024-02', q: 'unique searchable phrase' });
  assertCalendar(edited, { '2024-02-28': 1 }, { label: 'edited text search' });

  store.updateMessage(ACCOUNT_A, UUID.datedLeapA, { hidden: true, expected_revision: 0 });
  assert.deepEqual(dayMap(store.getCalendar(ACCOUNT_A, { month: '2024-02' })),
    { '2024-02-28': 1, '2024-02-29': 2 });
  assert.equal(store.listMessages(ACCOUNT_A, { date: '2024-02-29', limit: 500 }).total, 2);

  store.deleteMessage(ACCOUNT_A, UUID.datedLeapB, 0);
  assert.deepEqual(dayMap(store.getCalendar(ACCOUNT_A, { month: '2024-02' })),
    { '2024-02-28': 1, '2024-02-29': 1 });
  assert.equal(store.listMessages(ACCOUNT_A, { date: '2024-02-29', limit: 500 }).total, 1);

  // Deleting the whole second conversation removes its day as well.
  store.deleteConversation(ACCOUNT_A, CONVERSATION_C, 2);
  assert.equal(store.listMessages(ACCOUNT_A, { date: '2024-03-02', limit: 500 }).total, 0);
  assert.deepEqual(dayMap(store.getCalendar(ACCOUNT_A, { month: '2024-03' })), { '2024-03-01': 1 });
});

test('the day filter composes with conversation, search and hidden filters', async (t) => {
  const { store } = await openStoreSet(t, { dated: true });

  store.updateMessage(ACCOUNT_A, UUID.datedLeapB, { hidden: true, expected_revision: 0 });

  const conversationDay = store.listMessages(ACCOUNT_A, {
    conversationId: CONVERSATION_A, date: '2024-03-02', limit: 500,
  });
  assert.equal(conversationDay.total, 0, 'conversation C rows must not leak into conversation A');

  const hiddenExcluded = store.listMessages(ACCOUNT_A, { date: '2024-02-29', limit: 500 });
  assert.equal(hiddenExcluded.total, 2);
  const hiddenIncluded = store.listMessages(ACCOUNT_A, {
    date: '2024-02-29', includeHidden: true, limit: 500,
  });
  assert.equal(hiddenIncluded.total, 3);

  const searchDay = store.listMessages(ACCOUNT_A, { date: '2024-02-28', q: 'february', limit: 500 });
  assert.equal(searchDay.total, 1);
  assert.equal(searchDay.items[0].msg_uuid, UUID.datedFeb28);

  assert.equal(store.listMessages(ACCOUNT_A, { date: '2026-10-27', limit: 500 }).total, 7);
  // One row is hidden above, so the default scope excludes it.
  assert.equal(store.listMessages(ACCOUNT_A, { limit: 500 }).total, 15);
  assert.equal(store.listMessages(ACCOUNT_A, { limit: 500, includeHidden: true }).total, 16);
});

// ------------------------------------------------------- persisted timezone
/** Read one `store_meta` value with an independent connection. */
function metaValue(db, key) {
  const row = db.prepare('SELECT value FROM store_meta WHERE key = ?').get(key);
  return row ? String(row.value) : null;
}

test('a summary-only timezone edit re-buckets the existing store', async (t) => {
  const { fixture, store, open, closeCurrent, raw } = await openStoreSet(t, {
    dated: true,
    timeZone: 'Asia/Shanghai',
  });

  assert.equal(store.getCalendar(ACCOUNT_A, { month: '2024-02' }).timezone, 'Asia/Shanghai');
  assert.deepEqual(dayMap(store.getCalendar(ACCOUNT_A, { month: '2024-02' })),
    SHANGHAI_MONTHS['2024-02']);
  closeCurrent();
  assert.equal(metaValue(raw(), 'timezone'), 'Asia/Shanghai');

  // Only the root summary changes: the export was re-run in another timezone.
  const summaryFile = path.join(fixture.dir, 'export_summary.json');
  const summary = JSON.parse(fs.readFileSync(summaryFile, 'utf8'));
  summary.timezone = 'America/New_York';
  fs.writeFileSync(summaryFile, JSON.stringify(summary, null, 2));

  const reopened = await open();
  const february = reopened.getCalendar(ACCOUNT_A, { month: '2024-02' });

  assert.equal(february.timezone, 'America/New_York', 'the new summary zone is adopted');
  // 2024-02-28T16:30:00Z moves from the Shanghai leap day to February 28.
  assert.deepEqual(dayMap(february), { '2024-02-28': 2, '2024-02-29': 2 },
    'the persisted day buckets must be recomputed, not left stale');
  assert.equal(reopened.listMessages(ACCOUNT_A, { date: '2024-02-28', limit: 500 }).total, 2);
  assert.equal(reopened.listMessages(ACCOUNT_A, { date: '2024-02-29', limit: 500 }).total, 2);
  assert.equal(metaValue(raw(), 'timezone'), 'America/New_York', 'the marker follows the summary');
});

test('an index-only reopen keeps the persisted export timezone', async (t) => {
  const { fixture, store, open, closeCurrent, raw } = await openStoreSet(t, {
    dated: true,
    timeZone: 'Asia/Shanghai',
  });

  const before = store.getCalendar(ACCOUNT_A, { month: '2024-02' });
  assert.equal(before.timezone, 'Asia/Shanghai');
  assert.deepEqual(dayMap(before), SHANGHAI_MONTHS['2024-02']);

  const db = raw();
  assert.equal(metaValue(db, 'timezone'), 'Asia/Shanghai',
    'the export timezone must be persisted in store_meta');
  closeCurrent();

  // Make the reader portable: no JSONL, no summary, only reader.sqlite.
  fs.rmSync(path.join(fixture.dir, 'export_summary.json'), { force: true });
  for (const uid of [ACCOUNT_A, ACCOUNT_B]) {
    fs.rmSync(path.join(fixture.dir, uid), { recursive: true, force: true });
  }
  assert.equal(fs.existsSync(path.join(fixture.dir, ACCOUNT_A)), false);

  const reopened = await open();
  const after = reopened.getCalendar(ACCOUNT_A, { month: '2024-02' });

  assert.equal(after.timezone, 'Asia/Shanghai', 'the persisted zone survives without a summary');
  assert.deepEqual(dayMap(after), SHANGHAI_MONTHS['2024-02'],
    'day buckets must not drift to UTC or the viewer timezone');
  assert.equal(reopened.listMessages(ACCOUNT_A, { date: '2024-02-29', limit: 500 }).total, 3);
  assert.equal(reopened.listMessages(ACCOUNT_A, { date: '2024-02-28', limit: 500 }).total, 1);
  assert.equal(after.first_date, '2023-12-31');
  assert.equal(after.last_date, '2026-10-27');
});
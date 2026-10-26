/**
 * Vue store and calendar-helper coverage for permanent deletion and day
 * browsing.
 *
 * These tests import the real `web/src/store.js` and `web/src/calendar.js` and
 * replace `fetch` with a small router, so the assertions are about the requests
 * the UI actually issues and the state it actually keeps: selection across
 * pages and scopes, the confirmation gate, count refresh after a deletion,
 * calendar adoption/stepping and stale-response guards.
 *
 * No DOM and no bundler are needed: `store.js` is plain module state and the
 * components only render it.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import * as calendar from '../src/calendar.js';

// `notify()` schedules a dismissal with `window.setTimeout`; there is no DOM.
globalThis.window = { setTimeout: () => 0, clearTimeout: () => {} };

const { api, ApiError, setCsrfToken } = await import('../src/api.js');
const store = await import('../src/store.js');

const { state } = store;

// Only constructed identifiers: never a real account or conversation id.
const UID = '8100000000000011';
const OTHER_UID = '8210000000000022';
const CSRF = 'csrf-test-token';

setCsrfToken(CSRF);

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

/** Minimal `Response`-alike for the API helper. */
function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => 'application/json' },
    json: async () => payload,
  };
}

/**
 * Install a fetch router. `on(method, 'api/path', reply)` matches on the exact
 * path (query string ignored), so `api/message` and `api/messages/delete` can
 * never shadow each other.
 */
function installFetch(t) {
  const routes = [];
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (input, init = {}) => {
    const url = String(input);
    const path = url.split('?')[0];
    const method = (init && init.method) || 'GET';
    const record = {
      method,
      url,
      path,
      params: new URLSearchParams(url.split('?')[1] || ''),
      body: init && init.body ? JSON.parse(init.body) : null,
      headers: (init && init.headers) || {},
    };
    requests.push(record);
    // Last registered route wins, so a test can override a baseline handler.
    for (let index = routes.length - 1; index >= 0; index -= 1) {
      const route = routes[index];
      if (route.method !== method || route.path !== path) continue;
      const reply = await route.reply(record);
      return jsonResponse(reply.status ?? 200, reply.payload);
    }
    throw new Error(`unexpected request: ${method} ${url}`);
  });
  return {
    requests,
    on(method, path, reply) {
      routes.push({ method, path, reply });
    },
    find(method, path) {
      return requests.filter((record) => record.method === method && record.path === path);
    },
  };
}

/** Baseline read routes so post-deletion refreshes stay predictable. */
function baseline(http, { messageTotal = 0, messageItems = () => [], calendarPayload = {} } = {}) {
  http.on('GET', 'api/accounts', () => ({
    payload: { items: [{ uid: UID, name: '账号甲', message_count: messageTotal, conversation_count: 2 }] },
  }));
  http.on('GET', 'api/conversations', () => ({
    payload: {
      items: [
        { id: 'c1', name: '会话一', message_count: 12 },
        { id: 'c2', name: '会话二', message_count: 5 },
      ],
      total: 2,
    },
  }));
  http.on('GET', 'api/messages', (record) => {
    const offset = Number(record.params.get('offset') || 0);
    return { payload: { items: messageItems(offset, record), total: messageTotal } };
  });
  http.on('GET', 'api/calendar', () => ({
    payload: {
      month: '2026-10',
      timezone: 'Asia/Shanghai',
      days: [{ date: '2026-10-02', message_count: 3 }],
      total: 3,
      first_date: '2026-10-02',
      last_date: '2026-10-02',
      ...calendarPayload,
    },
  }));
}

/** Reset the module state so each test starts from a known workspace. */
function seed(overrides = {}) {
  Object.assign(state, {
    booting: false,
    authRequired: false,
    authenticated: true,
    csrfToken: CSRF,
    loginBusy: false,
    loginError: '',
    accounts: [{ uid: UID, name: '账号甲', message_count: 0, conversation_count: 2 }],
    accountsBusy: false,
    accountUid: UID,
    conversations: [
      { id: 'c1', name: '会话一', message_count: 12 },
      { id: 'c2', name: '会话二', message_count: 5 },
    ],
    conversationsTotal: 2,
    conversationsBusy: false,
    conversationQuery: '',
    conversationOffset: 0,
    conversationLimit: 100,
    conversationId: 'c1',
    messages: [],
    messagesTotal: 0,
    messagesBusy: false,
    messageQuery: '',
    messageOffset: 0,
    messageLimit: 50,
    messageDate: '',
    includeHidden: false,
    calendar: { month: '', timezone: '', days: [], total: 0, first_date: '', last_date: '' },
    calendarMonth: '2026-10',
    calendarBusy: false,
    selectedMessages: {},
    selectedConversations: {},
    selectionScope: '',
    confirm: null,
    deleting: false,
    deleteError: '',
    loadError: '',
    conflicts: {},
    toasts: [],
    ...overrides,
  });
}

/** Approve the confirmation the action has just opened. */
async function approve(pending) {
  await tick();
  assert.ok(state.confirm, 'a confirmation dialog should be open before approval');
  store.resolveConfirm(true);
  return pending;
}

/** Dismiss the confirmation the action has just opened. */
async function dismiss(pending) {
  await tick();
  assert.ok(state.confirm, 'a confirmation dialog should be open before dismissal');
  store.resolveConfirm(false);
  return pending;
}

// --------------------------------------------------------- calendar helpers
test('calendar helpers step months across year boundaries', () => {
  assert.equal(calendar.shiftMonth('2026-01', -1), '2025-12');
  assert.equal(calendar.shiftMonth('2026-12', 1), '2027-01');
  assert.equal(calendar.shiftMonth('2024-02', -14), '2022-12');
  assert.equal(calendar.shiftMonth('2026-07', 0), '2026-07');
  assert.equal(calendar.shiftMonth('bogus', 3), 'bogus');
  assert.equal(calendar.monthOf('2024-02-29 08:15:00'), '2024-02');
  assert.equal(calendar.monthOf(''), '');
  assert.equal(calendar.formatMonth('2024-02'), '2024 年 2 月');
  assert.equal(calendar.formatMonth('nope'), '—');
});

test('calendar helpers build a Monday-first grid from server counts', () => {
  // 2024-02-01 is a Thursday: Monday-first leaves Mon/Tue/Wed blank.
  const february = calendar.buildMonthGrid('2024-02');
  assert.equal(february.length, 3 + 29, 'leap February keeps 29 days');
  assert.equal(february.slice(0, 3).every((cell) => cell === null), true);
  assert.equal(february[3].date, '2024-02-01');
  assert.deepEqual(calendar.buildMonthGrid('2024-13'), []);
  assert.deepEqual(calendar.buildMonthGrid(''), []);

  const counts = calendar.countsByDate([
    { date: '2024-02-29', message_count: 3 },
    { date: '2024-02-01', message_count: '7' },
    { date: '2024-03-01', message_count: 99 },
    { date: '', message_count: 5 },
    null,
  ]);
  const cells = calendar.buildMonthGrid('2024-02', counts).filter((cell) => cell);
  const byDate = new Map(cells.map((cell) => [cell.date, cell.count]));
  assert.equal(byDate.get('2024-02-29'), 3);
  assert.equal(byDate.get('2024-02-01'), 7);
  assert.equal(byDate.get('2024-02-28'), 0);
  assert.equal(byDate.has('2024-03-01'), false, 'counts outside the month are ignored');
  assert.equal(calendar.totalOfDays([{ message_count: 3 }, { message_count: 4 }]), 7);
  assert.equal(calendar.totalOfDays(null), 0);
});

test('calendar helpers only accept the exact YYYY-MM and YYYY-MM-DD shapes', () => {
  assert.equal(calendar.isDate('2024-02-29'), true);
  assert.equal(calendar.isMonth('2024-02'), true);
  assert.equal(calendar.isDate('2024-02'), false);
  assert.equal(calendar.isDate(' 2024-02-29'), false);
  assert.equal(calendar.isDate('2024-2-9'), false);
  assert.equal(calendar.isMonth('2024-2'), false);
  assert.equal(calendar.parseMonth('2024-13'), null);
  assert.equal(calendar.parseMonth('2024-00'), null);
  assert.deepEqual(calendar.parseMonth('2024-02'), { year: 2024, month: 2 });
});

// ---------------------------------------------------------------- selection
test('selecting messages records the revisions used by the batch call', async (t) => {
  const http = installFetch(t);
  baseline(http);
  seed();
  state.messages = [
    { msg_uuid: 'm1', edit: { revision: 0 } },
    { msg_uuid: 'm2', edit: { revision: 6 } },
  ];

  store.toggleMessageSelection('m1', 0);
  store.toggleMessageSelection('m2', 6);
  assert.deepEqual({ ...state.selectedMessages }, { m1: 0, m2: 6 });
  assert.equal(store.selectedMessageCount.value, 2);
  assert.equal(store.pageAllMessagesSelected.value, true);

  store.toggleMessageSelection('m2', 6);
  assert.deepEqual({ ...state.selectedMessages }, { m1: 0 });
  assert.equal(store.pageAllMessagesSelected.value, false);
  assert.equal(store.pageSomeMessagesSelected.value, true);

  store.togglePageMessageSelection();
  assert.deepEqual({ ...state.selectedMessages }, { m1: 0, m2: 6 });
  store.togglePageMessageSelection();
  assert.deepEqual({ ...state.selectedMessages }, {});
  assert.equal(store.pageSomeMessagesSelected.value, false);
});

test('select all and clear only touch the messages on the current page', async (t) => {
  const http = installFetch(t);
  baseline(http);
  seed();
  state.messages = [
    { msg_uuid: 'm1', edit: { revision: 1 } },
    { msg_uuid: 'm2', edit: { revision: 2 } },
  ];
  state.selectedMessages = { other: 0 };

  store.selectAllMessagesOnPage();
  assert.deepEqual({ ...state.selectedMessages }, { other: 0, m1: 1, m2: 2 });
  store.clearMessagesOnPage();
  assert.deepEqual({ ...state.selectedMessages }, { other: 0 }, 'a cross-page selection survives');
  store.clearSelection();
  assert.deepEqual({ ...state.selectedMessages }, {});
});

test('paging keeps the selection while every scope change clears it', async (t) => {
  const http = installFetch(t);
  baseline(http, {
    messageTotal: 60,
    messageItems: (offset) => [{ msg_uuid: `p${offset}`, edit: { revision: 0 } }],
  });
  seed();
  state.messageLimit = 25;

  await store.loadMessages();
  store.toggleMessageSelection('p0', 0);
  assert.equal(store.selectedMessageCount.value, 1);

  await store.goToMessagePage(2);
  assert.equal(state.messageOffset, 25);
  assert.equal(store.selectedMessageCount.value, 1, 'paging must not clear the selection');

  await store.searchConversationMessages('hello');
  assert.equal(store.selectedMessageCount.value, 0, 'a new search clears the selection');

  store.toggleMessageSelection('p25', 0);
  await store.setMessageDate('2026-10-02');
  assert.equal(store.selectedMessageCount.value, 0, 'a new day filter clears the selection');
  assert.equal(state.messageOffset, 0);
  assert.equal(http.find('GET', 'api/messages').at(-1).params.get('date'), '2026-10-02');

  store.toggleMessageSelection('p25', 0);
  await store.setIncludeHidden(true);
  assert.equal(store.selectedMessageCount.value, 0, 'toggling hidden clears the selection');
});

test('one batch can carry selections collected on different pages', async (t) => {
  const http = installFetch(t);
  baseline(http, {
    messageTotal: 60,
    messageItems: (offset) => [{
      msg_uuid: `p${offset}`,
      edit: { revision: offset % 3 },
    }],
  });
  http.on('POST', 'api/messages/delete', (record) => ({
    payload: {
      deleted: true,
      account_uid: UID,
      deleted_messages: record.body.messages.length,
      deleted_conversations: 0,
      msg_uuids: record.body.messages.map((item) => item.msg_uuid),
    },
  }));
  seed();
  state.messageLimit = 25;

  await store.loadMessages();
  store.toggleMessageSelection('p0', 0);
  await store.goToMessagePage(3);
  store.toggleMessageSelection('p50', 2);
  assert.equal(store.selectedMessageCount.value, 2);

  const done = await approve(store.deleteSelectedMessages());
  assert.equal(done, true);

  const [post] = http.find('POST', 'api/messages/delete');
  assert.deepEqual(post.body.messages, [
    { msg_uuid: 'p0', expected_revision: 0 },
    { msg_uuid: 'p50', expected_revision: 2 },
  ]);
  assert.deepEqual({ ...state.selectedMessages }, {});
});

test('conversation selection records message counts for the count guard', async (t) => {
  const http = installFetch(t);
  baseline(http);
  seed();

  store.selectAllConversationsOnPage();
  assert.deepEqual({ ...state.selectedConversations }, { c1: 12, c2: 5 });
  assert.equal(store.selectedConversationCount.value, 2);

  store.toggleConversationSelection('c2', 0);
  assert.deepEqual({ ...state.selectedConversations }, { c1: 12 }, 'zero is coerced to one');

  store.clearConversationsOnPage();
  assert.deepEqual({ ...state.selectedConversations }, {});
});

// ------------------------------------------------------------- deletions
test('batch message delete posts the confirmation and every revision', async (t) => {
  const http = installFetch(t);
  baseline(http, { messageTotal: 2 });
  http.on('POST', 'api/messages/delete', (record) => ({
    payload: {
      deleted: true,
      account_uid: UID,
      deleted_messages: record.body.messages.length,
      deleted_conversations: 0,
      msg_uuids: record.body.messages.map((item) => item.msg_uuid),
    },
  }));
  seed();
  store.toggleMessageSelection('m1', 0);
  store.toggleMessageSelection('m2', 9);

  const done = await approve(store.deleteSelectedMessages());
  assert.equal(done, true);

  const [post] = http.find('POST', 'api/messages/delete');
  assert.equal(post.url, `api/messages/delete?account=${UID}`);
  assert.deepEqual(post.body, {
    confirm_delete: true,
    messages: [
      { msg_uuid: 'm1', expected_revision: 0 },
      { msg_uuid: 'm2', expected_revision: 9 },
    ],
  });
  assert.equal(post.headers['X-CSRF-Token'], CSRF, 'the batch write carries the CSRF token');

  assert.deepEqual({ ...state.selectedMessages }, {}, 'a successful delete clears the selection');
  assert.equal(state.confirm, null, 'the dialog closes once the work finishes');
  assert.equal(state.deleting, false);
  // Counts and the calendar are refreshed so the UI cannot show deleted rows.
  assert.equal(http.find('GET', 'api/accounts').length, 1);
  assert.equal(http.find('GET', 'api/conversations').length, 1);
  assert.equal(http.find('GET', 'api/messages').length, 1);
  assert.equal(http.find('GET', 'api/calendar').length, 1);
});

test('single message delete uses DELETE api/message with an explicit confirmation', async (t) => {
  const http = installFetch(t);
  baseline(http, { messageTotal: 1 });
  http.on('DELETE', 'api/message', () => ({
    payload: {
      deleted: true,
      account_uid: UID,
      deleted_messages: 1,
      deleted_conversations: 0,
      msg_uuids: ['m1'],
      msg_uuid: 'm1',
    },
  }));
  seed();
  state.messages = [{ msg_uuid: 'm1', edit: { revision: 3 } }];

  const pending = store.deleteMessagePermanently('m1', 3, { conversationName: '会话一' });
  await tick();
  assert.equal(state.confirm.title, '永久删除这条消息');
  assert.match(state.confirm.detail, /无法撤销/);
  assert.match(state.confirm.detail, /原始聊天数据库/);
  store.resolveConfirm(true);
  assert.equal(await pending, true);

  const [del] = http.find('DELETE', 'api/message');
  assert.equal(del.url, `api/message?account=${UID}&uuid=m1`);
  assert.deepEqual(del.body, { confirm_delete: true, expected_revision: 3 });
  assert.equal(del.headers['X-CSRF-Token'], CSRF);
  assert.deepEqual(state.selectedMessages, {});
});

test('conversation deletion sends the expected count and clears the open conversation', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('DELETE', 'api/conversation', (record) => ({
    payload: {
      deleted: true,
      account_uid: UID,
      deleted_messages: record.body.expected_message_count,
      deleted_conversations: 1,
      conversation_ids: ['c1'],
    },
  }));
  seed();
  state.conversationId = 'c1';

  const done = await approve(store.deleteConversationPermanently('c1', 12, { name: '会话一' }));
  assert.equal(done, true);

  const [del] = http.find('DELETE', 'api/conversation');
  assert.equal(del.url, `api/conversation?account=${UID}&conversation=c1`);
  assert.deepEqual(del.body, { confirm_delete: true, expected_message_count: 12 });
  assert.equal(state.conversationId, '', 'the deleted conversation is no longer open');
  assert.equal(state.messageDate, '');
});

test('batch conversation deletion posts the documented payload', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('POST', 'api/conversations/delete', (record) => ({
    payload: {
      deleted: true,
      account_uid: UID,
      deleted_messages: 0,
      deleted_conversations: record.body.conversations.length,
      conversation_ids: record.body.conversations.map((item) => item.id),
    },
  }));
  seed();
  store.toggleConversationSelection('c1', 12);
  store.toggleConversationSelection('c2', 5);

  const done = await approve(store.deleteSelectedConversations());
  assert.equal(done, true);

  const [post] = http.find('POST', 'api/conversations/delete');
  assert.equal(post.url, `api/conversations/delete?account=${UID}`);
  assert.deepEqual(post.body, {
    confirm_delete: true,
    conversations: [
      { id: 'c1', expected_message_count: 12 },
      { id: 'c2', expected_message_count: 5 },
    ],
  });
  assert.deepEqual({ ...state.selectedConversations }, {});
});

test('restore posts message/restore and never issues a DELETE', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('POST', 'api/message/restore', () => ({
    payload: {
      item: {
        msg_uuid: 'm1',
        conversation_id: 'c1',
        conversation_name: '会话一',
        text: '原始内容',
        text_original: '原始内容',
        edit: { text_override: null, note: null, hidden: false, revision: 4, updated_at: 'now' },
      },
    },
  }));
  seed();
  state.messages = [{
    msg_uuid: 'm1',
    conversation_id: 'c1',
    edit: { text_override: '改后', note: '备注', hidden: true, revision: 3 },
  }];

  const restored = await store.restoreMessage('m1');
  assert.equal(restored.edit.revision, 4);
  assert.equal(restored.text, '原始内容');

  const [post] = http.find('POST', 'api/message/restore');
  assert.equal(post.url, `api/message/restore?account=${UID}&uuid=m1`);
  assert.deepEqual(post.body, { expected_revision: 3 });
  assert.equal(http.find('DELETE', 'api/message').length, 0, 'restore must not use DELETE');
});

test('cancelling the confirmation sends nothing and keeps the selection', async (t) => {
  const http = installFetch(t);
  baseline(http);
  seed();
  store.toggleMessageSelection('m1', 0);

  const done = await dismiss(store.deleteSelectedMessages());
  assert.equal(done, false);
  assert.equal(state.confirm, null, 'cancelling closes the dialog at once');
  assert.equal(http.find('POST', 'api/messages/delete').length, 0);
  assert.equal(http.find('DELETE', 'api/message').length, 0);
  assert.equal(store.selectedMessageCount.value, 1, 'cancelling keeps the selection');
  assert.equal(state.deleting, false);
});

test('more than five hundred selections are refused before any request', async (t) => {
  const http = installFetch(t);
  baseline(http);
  seed();
  for (let index = 0; index < 501; index += 1) state.selectedMessages[`m${index}`] = 0;

  const done = await store.deleteSelectedMessages();
  assert.equal(done, false);
  assert.equal(state.confirm, null, 'the oversized batch never opens a dialog');
  assert.equal(http.find('POST', 'api/messages/delete').length, 0);
  assert.match(state.toasts.at(-1).text, /一次最多删除 500 条/);
  assert.equal(store.selectedMessageCount.value, 501);
});

test('a failed deletion keeps the dialog, the reason and the selection', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('POST', 'api/messages/delete', () => ({
    status: 409,
    payload: {
      error: {
        code: 'conflict',
        message: 'A message in the batch changed since it was read.',
      },
    },
  }));
  seed();
  store.toggleMessageSelection('m1', 0);
  store.toggleMessageSelection('m2', 4);

  const done = await approve(store.deleteSelectedMessages());
  assert.equal(done, false);
  assert.equal(state.deleteError, '该消息已被更新，请确认最新内容后重新保存。');
  assert.equal(state.deleting, false, 'the busy flag is released after a failure');
  assert.ok(state.confirm, 'a failed deletion keeps the dialog open with the error');
  assert.equal(store.selectedMessageCount.value, 2, 'the selection survives for a retry');
  assert.equal(http.find('GET', 'api/messages').length, 0, 'nothing is refreshed after a failure');
});

test('dismissing a failed deletion closes it without losing the selection', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('DELETE', 'api/message', () => ({
    status: 400,
    payload: {
      error: {
        code: 'confirmation_required',
        message: 'Permanent deletion requires confirm_delete: true.',
      },
    },
  }));
  seed();

  const pending = store.deleteMessagePermanently('m1', 0, { skipConfirm: true });
  assert.equal(await pending, false);
  assert.equal(state.deleteError, '永久删除需要明确确认，请重新确认后再试。');
  assert.equal(state.deleting, false);

  store.resolveConfirm(false);
  assert.equal(state.confirm, null, 'a settled dialog can always be dismissed');
  assert.equal(state.deleteError, '永久删除需要明确确认，请重新确认后再试。');
});

test('a second confirmation cannot stack behind the first', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('POST', 'api/messages/delete', (record) => ({
    payload: {
      deleted: true,
      account_uid: UID,
      deleted_messages: record.body.messages.length,
      deleted_conversations: 0,
    },
  }));
  seed();
  store.toggleMessageSelection('m1', 0);

  const first = store.deleteSelectedMessages();
  const second = await store.deleteSelectedMessages();
  assert.equal(second, false, 'the second action is refused while a dialog is open');

  store.resolveConfirm(true);
  assert.equal(await first, true);
  assert.equal(http.find('POST', 'api/messages/delete').length, 1);
});

test('an account switch mid-delete never refreshes the new account', async (t) => {
  const http = installFetch(t);
  let release;
  baseline(http);
  http.on('POST', 'api/messages/delete', () => new Promise((resolve) => {
    release = resolve;
  }));
  seed();
  store.toggleMessageSelection('m1', 0);

  const pending = store.deleteSelectedMessages();
  await tick();
  store.resolveConfirm(true);
  await tick();
  assert.equal(state.deleting, true);

  // The user switches accounts while the request is still in flight.
  state.accountUid = OTHER_UID;
  state.selectedMessages = { other: 0 };
  release({
    payload: {
      deleted: true,
      account_uid: UID,
      deleted_messages: 1,
      deleted_conversations: 0,
      msg_uuids: ['m1'],
    },
  });

  assert.equal(await pending, true);
  assert.equal(state.confirm, null, 'the dialog closes even across an account switch');
  assert.equal(state.deleting, false, 'the busy flag never stays stuck');
  assert.deepEqual({ ...state.selectedMessages }, { other: 0 }, 'the new account keeps its selection');
  assert.equal(http.find('GET', 'api/accounts').length, 0, 'no refresh touches the new account');
});

// ------------------------------------------------------------- pagination
test('an emptied message page clamps back onto the last page', async (t) => {
  const http = installFetch(t);
  baseline(http, {
    messageTotal: 30,
    messageItems: (offset) => (offset < 30 ? [{ msg_uuid: `p${offset}`, edit: { revision: 0 } }] : []),
  });
  seed();
  state.messageLimit = 25;
  state.messageOffset = 50; // past the end after deletions

  await store.loadMessages();

  const offsets = http.find('GET', 'api/messages').map((record) => record.params.get('offset'));
  assert.deepEqual(offsets, ['50', '25'], 'the clamped offset is refetched');
  assert.equal(state.messageOffset, 25);
  assert.equal(state.messages.length, 1);
  assert.equal(store.messagePageCount.value, 2);
});

test('conversation pagination clamps and exposes page numbers', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('GET', 'api/conversations', (record) => {
    const offset = Number(record.params.get('offset') || 0);
    const all = Array.from({ length: 130 }, (_, index) => ({
      id: `c${index}`,
      name: `会话${index}`,
      message_count: 1,
    }));
    return { payload: { items: all.slice(offset, offset + 100), total: all.length } };
  });
  seed();

  await store.loadConversations();
  assert.equal(store.conversationPageCount.value, 2);
  assert.equal(state.conversations.length, 100);

  await store.goToConversationPage(2);
  assert.equal(state.conversationOffset, 100);
  assert.equal(store.conversationPage.value, 2);
  assert.equal(state.conversations.length, 30);

  await store.goToConversationPage(99);
  assert.equal(state.conversationOffset, 100, 'a page past the end clamps to the last page');
});

// --------------------------------------------------------------- calendar
test('the calendar adopts the first recorded month and refetches once', async (t) => {
  const http = installFetch(t);
  let calls = 0;
  http.on('GET', 'api/accounts', () => ({ payload: { items: [{ uid: UID, name: '账号甲' }] } }));
  http.on('GET', 'api/calendar', (record) => {
    calls += 1;
    const month = record.params.get('month');
    return {
      payload: {
        month,
        timezone: 'Asia/Shanghai',
        days: month === '2023-12' ? [{ date: '2023-12-31', message_count: 4 }] : [],
        total: month === '2023-12' ? 4 : 0,
        first_date: '2023-12-31',
        last_date: '2026-10-27',
      },
    };
  });
  seed({ calendarMonth: '' });

  await store.loadCalendar();

  assert.equal(calls, 2, 'the probe month is discarded once the first recorded month is known');
  assert.equal(state.calendarMonth, '2023-12');
  assert.equal(state.calendar.month, '2023-12');
  assert.equal(state.calendar.timezone, 'Asia/Shanghai');
  assert.equal(state.calendar.first_date, '2023-12-31');
  assert.deepEqual(state.calendar.days, [{ date: '2023-12-31', message_count: 4 }]);

  await store.goToFirstRecordedMonth();
  assert.equal(calls, 2, 'jumping to the month already shown does not refetch');
});

test('calendar requests carry the full scope and step months', async (t) => {
  const http = installFetch(t);
  baseline(http);
  seed();
  state.conversationId = 'c1';
  state.messageQuery = '报告';
  state.includeHidden = true;
  state.calendarMonth = '2026-10';

  await store.loadCalendar();
  const first = http.find('GET', 'api/calendar').at(-1);
  assert.equal(first.params.get('account'), UID);
  assert.equal(first.params.get('month'), '2026-10');
  assert.equal(first.params.get('conversation'), 'c1');
  assert.equal(first.params.get('q'), '报告');
  assert.equal(first.params.get('include_hidden'), '1');

  await store.changeCalendarMonth(-1);
  assert.equal(state.calendarMonth, '2026-09');
  assert.equal(http.find('GET', 'api/calendar').at(-1).params.get('month'), '2026-09');
});

test('clicking the selected day clears the day filter', async (t) => {
  const http = installFetch(t);
  baseline(http, { messageTotal: 3 });
  seed();

  await store.setMessageDate('2026-10-02');
  assert.equal(state.messageDate, '2026-10-02');
  assert.equal(http.find('GET', 'api/messages').at(-1).params.get('date'), '2026-10-02');

  await store.toggleCalendarDate('2026-10-02');
  assert.equal(state.messageDate, '');
  assert.equal(http.find('GET', 'api/messages').at(-1).params.get('date'), null);

  // A value that is not a day is ignored instead of being sent to the server.
  await store.setMessageDate('02/10/2026');
  assert.equal(state.messageDate, '');
  assert.equal(http.find('GET', 'api/messages').at(-1).params.get('date'), null);
});

test('hiding a message re-reads the page and the day counts', async (t) => {
  const http = installFetch(t);
  let hidden = false;
  const message = () => ({
    msg_uuid: 'm1',
    conversation_id: 'c1',
    text: '内容',
    text_original: '内容',
    edit: { text_override: null, note: '', hidden, revision: hidden ? 1 : 0, updated_at: 'now' },
  });
  baseline(http);
  http.on('GET', 'api/messages', () => ({
    payload: { items: hidden ? [] : [message()], total: hidden ? 2 : 3 },
  }));
  http.on('GET', 'api/calendar', () => ({
    payload: {
      month: '2026-10',
      timezone: 'Asia/Shanghai',
      days: [{ date: '2026-10-02', message_count: hidden ? 2 : 3 }],
      total: hidden ? 2 : 3,
      first_date: '2026-10-02',
      last_date: '2026-10-02',
    },
  }));
  http.on('PATCH', 'api/message', () => {
    hidden = true;
    return { payload: { item: message() } };
  });
  seed();
  state.messages = [message()];
  state.messagesTotal = 3;

  await store.loadCalendar();
  assert.equal(calendar.totalOfDays(state.calendar.days), 3);
  assert.equal(http.find('GET', 'api/messages').length, 0);

  await store.saveMessage('m1', { textOverride: null, note: '', hidden: true });

  assert.equal(hidden, true);
  assert.equal(http.find('GET', 'api/messages').length, 1, 'the page is re-read, not patched');
  assert.equal(http.find('GET', 'api/calendar').length, 2, 'the calendar is re-read too');
  assert.equal(state.messages.length, 0, 'a hidden message leaves the default list');
  assert.equal(state.messagesTotal, 2);
  assert.equal(calendar.totalOfDays(state.calendar.days), 2, 'the day count follows the hide');
});

test('restoring a message re-reads the day counts when hidden rows are shown', async (t) => {
  const http = installFetch(t);
  let hidden = true;
  const message = () => ({
    msg_uuid: 'm1',
    conversation_id: 'c1',
    text: '内容',
    text_original: '内容',
    edit: { text_override: '改后', note: '备注', hidden, revision: hidden ? 3 : 4, updated_at: 'now' },
  });
  baseline(http);
  http.on('GET', 'api/messages', () => ({ payload: { items: [message()], total: hidden ? 2 : 3 } }));
  http.on('GET', 'api/calendar', () => ({
    payload: {
      month: '2026-10',
      timezone: 'Asia/Shanghai',
      days: [{ date: '2026-10-02', message_count: hidden ? 2 : 3 }],
      total: hidden ? 2 : 3,
      first_date: '2026-10-02',
      last_date: '2026-10-02',
    },
  }));
  http.on('POST', 'api/message/restore', () => {
    hidden = false;
    return { payload: { item: message() } };
  });
  seed({ includeHidden: true });
  state.messages = [message()];
  state.messagesTotal = 2;

  await store.loadCalendar();
  assert.equal(calendar.totalOfDays(state.calendar.days), 2);

  await store.restoreMessage('m1');

  assert.equal(hidden, false);
  assert.equal(http.find('GET', 'api/calendar').length, 2, 'the calendar is re-read after a restore');
  assert.equal(state.messagesTotal, 3);
  assert.equal(calendar.totalOfDays(state.calendar.days), 3, 'the day count returns on restore');
});

// ------------------------------------------------------- stale responses
test('a stale message response cannot overwrite a newer one', async (t) => {
  const http = installFetch(t);
  const deferred = [];
  baseline(http);
  http.on('GET', 'api/messages', () => new Promise((resolve) => {
    deferred.push(resolve);
  }));
  seed();

  const first = store.loadMessages();
  const second = store.loadMessages();
  await tick();

  deferred[1]({ payload: { items: [{ msg_uuid: 'new', edit: { revision: 0 } }], total: 1 } });
  await tick();
  deferred[0]({ payload: { items: [{ msg_uuid: 'old', edit: { revision: 0 } }], total: 1 } });
  await Promise.all([first, second]);

  assert.deepEqual(state.messages.map((item) => item.msg_uuid), ['new']);
  assert.equal(state.messagesTotal, 1);
  assert.equal(state.messagesBusy, false);
});

test('a stale calendar response cannot overwrite a newer month', async (t) => {
  const http = installFetch(t);
  const deferred = [];
  baseline(http);
  http.on('GET', 'api/calendar', () => new Promise((resolve) => {
    deferred.push(resolve);
  }));
  seed({ calendarMonth: '2026-10' });

  const first = store.loadCalendar();
  await tick();
  state.calendarMonth = '2024-02';
  const second = store.loadCalendar();
  await tick();

  deferred[1]({
    payload: { month: '2024-02', timezone: 'Asia/Shanghai', days: [{ date: '2024-02-29', message_count: 5 }], total: 5 },
  });
  await tick();
  deferred[0]({
    payload: { month: '2026-10', timezone: 'Asia/Shanghai', days: [{ date: '2026-10-02', message_count: 9 }], total: 9 },
  });
  await Promise.all([first, second]);

  assert.equal(state.calendar.month, '2024-02');
  assert.deepEqual(state.calendar.days, [{ date: '2024-02-29', message_count: 5 }]);
  assert.equal(state.calendarBusy, false);
});

test('an ApiError keeps a Chinese message while exposing the machine code', async () => {
  const error = new ApiError('原始英文信息', 409, 'conflict');
  assert.equal(error.code, 'conflict');
  assert.equal(error.status, 409);
  assert.equal(typeof api.get, 'function');
});

// ------------------------------------------------------------- account races
test('a delayed save response never edits another account\'s row', async (t) => {
  const http = installFetch(t);
  let release;
  baseline(http);
  http.on('PATCH', 'api/message', () => new Promise((resolve) => {
    release = resolve;
  }));
  seed();
  state.messages = [{
    msg_uuid: 'm1',
    conversation_id: 'c1',
    text: '内容',
    edit: { text_override: null, note: '', hidden: false, revision: 0 },
  }];

  const pending = store.saveMessage('m1', { textOverride: '改后', note: '', hidden: false });
  await tick();
  assert.equal(typeof release, 'function', 'the save request should be in flight');

  // The user switches accounts; the new account happens to show the same uuid.
  state.accountUid = OTHER_UID;
  state.messages = [{
    msg_uuid: 'm1',
    conversation_id: 'c9',
    text: '另一个账号',
    edit: { text_override: null, note: '', hidden: false, revision: 7 },
  }];

  release({
    payload: {
      item: {
        msg_uuid: 'm1',
        conversation_id: 'c1',
        text: '改后',
        text_original: '内容',
        edit: { text_override: '改后', note: '', hidden: false, revision: 1, updated_at: 'now' },
      },
    },
  });

  assert.equal(await pending, null, 'a save that finishes after an account switch is discarded');
  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].text, '另一个账号', 'the new account keeps its own row');
  assert.equal(state.messages[0].edit.revision, 7);
  assert.match(state.toasts.map((toast) => toast.text).join('\n'), /^(?![\s\S]*已保存修改)/);
});

test('a delayed restore response never edits another account\'s row', async (t) => {
  const http = installFetch(t);
  let release;
  baseline(http);
  http.on('POST', 'api/message/restore', () => new Promise((resolve) => {
    release = resolve;
  }));
  seed();
  state.messages = [{
    msg_uuid: 'm1',
    conversation_id: 'c1',
    text: '内容',
    edit: { text_override: '改后', note: '备注', hidden: false, revision: 3 },
  }];

  const pending = store.restoreMessage('m1');
  await tick();
  assert.equal(typeof release, 'function', 'the restore request should be in flight');

  state.accountUid = OTHER_UID;
  state.messages = [{
    msg_uuid: 'm1',
    conversation_id: 'c9',
    text: '另一个账号',
    edit: { text_override: '保留', note: '', hidden: false, revision: 9 },
  }];

  release({
    payload: {
      item: {
        msg_uuid: 'm1',
        conversation_id: 'c1',
        text: '内容',
        text_original: '内容',
        edit: { text_override: null, note: null, hidden: false, revision: 4, updated_at: 'now' },
      },
    },
  });

  assert.equal(await pending, null, 'a restore that finishes after an account switch is discarded');
  assert.equal(state.messages[0].text_override ?? state.messages[0].edit.text_override, '保留');
  assert.equal(state.messages[0].edit.revision, 9, 'the new account keeps its own row');
});

test('a delayed conflict reload never writes into another account', async (t) => {
  const http = installFetch(t);
  let release;
  baseline(http);
  http.on('PATCH', 'api/message', () => ({
    status: 409,
    payload: { error: { code: 'conflict', message: 'A newer revision exists.' } },
  }));
  http.on('GET', 'api/message', () => new Promise((resolve) => {
    release = resolve;
  }));
  seed();
  state.messages = [{
    msg_uuid: 'm1',
    conversation_id: 'c1',
    text: '内容',
    edit: { text_override: null, note: '', hidden: false, revision: 0 },
  }];

  const pending = store.saveMessage('m1', { textOverride: '改后', note: '', hidden: false });
  await tick();
  await tick();
  assert.equal(typeof release, 'function', 'the conflict reload should be in flight');

  state.accountUid = OTHER_UID;
  state.messages = [{
    msg_uuid: 'm1',
    conversation_id: 'c9',
    text: '另一个账号',
    edit: { text_override: null, note: '', hidden: false, revision: 5 },
  }];

  release({
    payload: {
      item: {
        msg_uuid: 'm1',
        conversation_id: 'c1',
        text: '最新版本',
        text_original: '内容',
        edit: { text_override: '最新版本', note: '', hidden: false, revision: 2, updated_at: 'now' },
      },
    },
  });

  assert.equal(await pending, null);
  assert.equal(state.conflicts.m1, undefined, 'a conflict from the old account is never recorded');
  assert.equal(state.messages[0].text, '另一个账号', 'the new account keeps its own row');
  assert.equal(state.messages[0].edit.revision, 5);
});

test('an account switch cancels a pending permanent confirmation', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('POST', 'api/messages/delete', () => ({
    payload: { deleted: true, account_uid: UID, deleted_messages: 1, deleted_conversations: 0 },
  }));
  seed();
  store.toggleMessageSelection('m1', 0);

  const pending = store.deleteSelectedMessages();
  await tick();
  assert.ok(state.confirm, 'the confirmation is open before the switch');
  assert.equal(state.confirm.danger, true);

  // The user changes account while the dialog is still waiting for an answer.
  await store.selectAccount(OTHER_UID);

  assert.equal(state.accountUid, OTHER_UID);
  assert.deepEqual({ ...state.selectedMessages }, {}, 'the old selection is dropped');
  assert.equal(http.find('POST', 'api/messages/delete').length, 0, 'the switch sends nothing');

  // A late click on the confirm button must be refused, not dispatched.
  store.resolveConfirm(true);
  assert.equal(await pending, false, 'the approved action is cancelled by the stale scope');
  assert.equal(
    http.find('POST', 'api/messages/delete').length,
    0,
    'no destructive request may reach the new account',
  );
  assert.equal(state.confirm, null, 'the cancelled dialog is closed');
  assert.equal(state.deleting, false);
});

test('retrying a failed deletion after an account switch cannot delete the new selection', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('POST', 'api/messages/delete', () => ({
    status: 409,
    payload: { error: { code: 'conflict', message: 'A message in the batch changed.' } },
  }));
  seed();
  store.toggleMessageSelection('m1', 0);

  const done = await approve(store.deleteSelectedMessages());
  assert.equal(done, false);
  assert.ok(state.confirm, 'a failed deletion keeps the dialog for a retry');
  assert.equal(typeof state.confirm.retry, 'function', 'the retry action is armed');

  // The user switches accounts, which clears the old selection.
  await store.selectAccount(OTHER_UID);
  state.selectedMessages = { someoneElse: 0 };

  assert.equal(state.confirm, null, 'the failed dialog does not survive the switch');
  assert.equal(store.retryConfirm(), undefined, 'no retry can run against the new account');
  assert.equal(
    http.find('POST', 'api/messages/delete').length,
    1,
    'only the original failed attempt was sent; the new account is untouched',
  );
  assert.deepEqual({ ...state.selectedMessages }, { someoneElse: 0 });
});

test('retrying a failed deletion in the same scope re-issues the batch', async (t) => {
  const http = installFetch(t);
  let attempt = 0;
  baseline(http);
  http.on('POST', 'api/messages/delete', (record) => {
    attempt += 1;
    if (attempt === 1) {
      return {
        status: 409,
        payload: { error: { code: 'conflict', message: 'A message in the batch changed.' } },
      };
    }
    return {
      payload: {
        deleted: true,
        account_uid: UID,
        deleted_messages: record.body.messages.length,
        deleted_conversations: 0,
      },
    };
  });
  seed();
  store.toggleMessageSelection('m1', 0);

  const done = await approve(store.deleteSelectedMessages());
  assert.equal(done, false);
  assert.equal(state.confirm.settled, true, 'the failed dialog stays open and settled');
  assert.equal(typeof state.confirm.retry, 'function');
  assert.equal(http.find('POST', 'api/messages/delete').length, 1);

  const retried = await store.retryConfirm();
  assert.equal(retried, true, 'the same-scope retry succeeds');
  assert.equal(http.find('POST', 'api/messages/delete').length, 2);
  assert.deepEqual({ ...state.selectedMessages }, {});
  assert.equal(state.confirm, null, 'the dialog closes once the retry succeeds');
  assert.equal(state.deleteError, '');
});

// ------------------------------------------------------- scope of the sidebar
test('a sidebar search clears the message and conversation selections', async (t) => {
  const http = installFetch(t);
  baseline(http);
  seed();
  store.toggleMessageSelection('m1', 0);
  store.toggleConversationSelection('c1', 12);
  assert.equal(store.selectedMessageCount.value, 1);
  assert.equal(store.selectedConversationCount.value, 1);

  await store.searchConversations('报告');

  assert.equal(state.conversationQuery, '报告');
  assert.equal(store.selectedMessageCount.value, 0, 'the sidebar search changes the scope');
  assert.equal(store.selectedConversationCount.value, 0);
  assert.equal(http.find('GET', 'api/conversations').at(-1).params.get('q'), '报告');
});

test('paging the sidebar keeps a selection that is still in scope', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('GET', 'api/conversations', (record) => {
    const offset = Number(record.params.get('offset') || 0);
    const all = Array.from({ length: 130 }, (_, index) => ({
      id: `c${index}`,
      name: `会话${index}`,
      message_count: 1,
    }));
    return { payload: { items: all.slice(offset, offset + 100), total: all.length } };
  });
  seed({ conversationsTotal: 130, conversationLimit: 100 });
  store.toggleConversationSelection('c0', 1);

  await store.loadConversations();
  assert.equal(store.conversationPageCount.value, 2);

  await store.goToConversationPage(2);

  assert.equal(state.conversationOffset, 100);
  assert.equal(state.conversations.length, 30);
  assert.equal(store.selectedConversationCount.value, 1, 'paging is not a scope change');
});

test('the open conversation keeps its heading when the sidebar shows another page', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('GET', 'api/conversations', (record) => {
    const offset = Number(record.params.get('offset') || 0);
    if (offset === 0) {
      return { payload: { items: [{ id: 'c1', name: '会话一', message_count: 12 }], total: 130 } };
    }
    return { payload: { items: [{ id: 'c9', name: '别的会话', message_count: 3 }], total: 130 } };
  });
  seed({ conversations: [{ id: 'c1', name: '会话一', message_count: 12 }], conversationsTotal: 130 });

  await store.loadConversations();
  await store.selectConversation('c1');
  assert.equal(store.currentConversation.value.name, '会话一');

  // The user pages the sidebar; the open conversation is no longer listed.
  await store.goToConversationPage(2);
  assert.equal(state.conversations.some((item) => item.id === 'c1'), false);
  assert.equal(state.conversationId, 'c1', 'the open conversation is unchanged');
  assert.equal(store.currentConversation.value.name, '会话一', 'the heading keeps the real name');
  assert.equal(store.currentConversation.value.message_count, 12, 'the delete scope keeps its count');

  // Even with no cached details the heading keeps a usable scope label.
  state.conversationIndex = {};
  assert.equal(store.currentConversation.value.id, 'c1');
  assert.equal(store.currentConversation.value.name, 'c1');
});

test('a same-account scope change during a conflict reload writes nothing stale', async (t) => {
  const http = installFetch(t);
  let release;
  baseline(http);
  http.on('PATCH', 'api/message', () => ({
    status: 409,
    payload: { error: { code: 'conflict', message: 'A newer revision exists.' } },
  }));
  http.on('GET', 'api/message', () => new Promise((resolve) => {
    release = resolve;
  }));
  seed();
  state.messages = [{
    msg_uuid: 'm1',
    conversation_id: 'c1',
    text: '内容',
    edit: { text_override: null, note: '', hidden: false, revision: 0 },
  }];

  const pending = store.saveMessage('m1', { textOverride: '改后', note: '', hidden: false });
  await tick();
  await tick();
  assert.equal(typeof release, 'function', 'the conflict reload should be in flight');

  // Same account, but the user narrowed the message search in the meantime.
  state.messageQuery = '不同的搜索';

  release({
    payload: {
      item: {
        msg_uuid: 'm1',
        conversation_id: 'c1',
        text: '最新版本',
        text_original: '内容',
        edit: { text_override: '最新版本', note: '', hidden: false, revision: 2, updated_at: 'now' },
      },
    },
  });

  assert.equal(await pending, null);
  assert.equal(state.conflicts.m1, undefined, 'a conflict from another scope is not recorded');
  assert.equal(state.messages[0].text, '内容', 'the visible row is not replaced');
  assert.equal(state.messages[0].edit.revision, 0);
  assert.equal(
    state.toasts.some((toast) => /已被.{0,4}改动更新/.test(toast.text)),
    false,
    'no conflict toast is raised for a scope the user left',
  );
});

test('an invalid_batch response is rendered in Chinese', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('POST', 'api/messages/delete', () => ({
    status: 400,
    payload: { error: { code: 'invalid_batch', message: 'invalid batch payload' } },
  }));
  seed();
  store.toggleMessageSelection('m1', 0);

  const done = await approve(store.deleteSelectedMessages());

  assert.equal(done, false);
  assert.equal(state.deleting, false);
  assert.match(state.deleteError, /[\u4e00-\u9fff]/, 'the failure text must be Chinese');
  assert.doesNotMatch(state.deleteError, /HTTP 400/, 'it must not fall back to the raw status text');
  assert.doesNotMatch(state.deleteError, /invalid batch payload/, 'the server text is not shown');
  assert.equal(state.toasts.at(-1).text, state.deleteError);
  assert.equal(store.selectedMessageCount.value, 1, 'the selection survives for a retry');
});

// ------------------------------------- deleting the last message of a conversation
/**
 * Deleting the last message of a conversation also removes the conversation row
 * on the server (`deleted_conversations: 1`). The UI must then stop filtering by
 * a conversation that no longer exists.
 */
function lastMessageResponse({ conversationsAfter }) {
  return (http) => {
    http.on('GET', 'api/conversations', () => ({ payload: conversationsAfter }));
    http.on('GET', 'api/messages', () => ({ payload: { items: [], total: 0 } }));
  };
}

test('deleting the last message of the open conversation resets the whole scope', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('DELETE', 'api/message', () => ({
    payload: {
      deleted: true,
      account_uid: UID,
      deleted_messages: 1,
      deleted_conversations: 1,
      msg_uuids: ['m1'],
      msg_uuid: 'm1',
    },
  }));
  lastMessageResponse({
    conversationsAfter: { items: [{ id: 'c2', name: '会话二', message_count: 5 }], total: 1 },
  })(http);
  seed();
  state.conversationId = 'c1';
  state.messageDate = '2026-10-02';
  state.messageOffset = 50;
  state.conversationIndex = { c1: { id: 'c1', name: '会话一', message_count: 1 } };
  state.messages = [{ msg_uuid: 'm1', conversation_id: 'c1', edit: { revision: 0 } }];
  state.messagesTotal = 1;

  const done = await approve(store.deleteMessagePermanently('m1', 0, { conversationName: '会话一' }));
  assert.equal(done, true);

  assert.equal(state.conversationId, '', 'the removed conversation is no longer open');
  assert.equal(state.messageDate, '', 'the day filter is cleared with the conversation');
  assert.equal(state.messageOffset, 0, 'the message page is reset');
  assert.equal(state.conversationIndex.c1, undefined, 'the cached heading is dropped');
  assert.equal(store.currentConversation.value, null, 'no heading for a removed conversation');

  // Every aggregate is refreshed, and the message reload is no longer filtered.
  assert.equal(http.find('GET', 'api/accounts').length, 1);
  assert.equal(http.find('GET', 'api/conversations').length, 1);
  assert.equal(http.find('GET', 'api/messages').length, 1);
  assert.equal(http.find('GET', 'api/calendar').length, 1);
  const reload = http.find('GET', 'api/messages').at(-1);
  assert.equal(reload.params.get('conversation'), null, 'the reload covers the whole account');
  assert.equal(reload.params.get('date'), null);
  assert.equal(http.find('GET', 'api/conversations').at(-1).params.get('q'), null);
});

test('a batch that removes the open conversation resets the whole scope', async (t) => {
  const http = installFetch(t);
  baseline(http);
  http.on('POST', 'api/messages/delete', () => ({
    payload: {
      deleted: true,
      account_uid: UID,
      deleted_messages: 1,
      deleted_conversations: 1,
      msg_uuids: ['m1'],
    },
  }));
  lastMessageResponse({
    conversationsAfter: { items: [{ id: 'c2', name: '会话二', message_count: 5 }], total: 1 },
  })(http);
  seed();
  state.conversationId = 'c1';
  state.messageDate = '2026-10-02';
  state.messageOffset = 50;
  state.conversationIndex = { c1: { id: 'c1', name: '会话一', message_count: 1 } };
  state.messages = [{ msg_uuid: 'm1', conversation_id: 'c1', edit: { revision: 0 } }];
  state.messagesTotal = 1;
  store.toggleMessageSelection('m1', 0);

  const done = await approve(store.deleteSelectedMessages());
  assert.equal(done, true);

  assert.equal(state.conversationId, '', 'the removed conversation is no longer open');
  assert.equal(state.messageDate, '', 'the day filter is cleared');
  assert.equal(state.messageOffset, 0);
  assert.equal(state.conversationIndex.c1, undefined, 'the cached heading is dropped');
  assert.equal(store.currentConversation.value, null);
  assert.deepEqual({ ...state.selectedMessages }, {});

  assert.equal(http.find('GET', 'api/accounts').length, 1);
  assert.equal(http.find('GET', 'api/conversations').length, 1);
  assert.equal(http.find('GET', 'api/messages').length, 1);
  assert.equal(http.find('GET', 'api/calendar').length, 1);
  assert.equal(http.find('GET', 'api/messages').at(-1).params.get('conversation'), null);
});

test('deleting a message that leaves the conversation alive keeps the scope', async (t) => {
  const http = installFetch(t);
  baseline(http, {
    messageTotal: 120,
    messageItems: (offset) => [{ msg_uuid: `p${offset}`, edit: { revision: 0 } }],
  });
  http.on('DELETE', 'api/message', () => ({
    payload: {
      deleted: true,
      account_uid: UID,
      deleted_messages: 1,
      deleted_conversations: 0,
      msg_uuids: ['m1'],
      msg_uuid: 'm1',
    },
  }));
  http.on('GET', 'api/conversations', () => ({
    payload: { items: [{ id: 'c1', name: '会话一', message_count: 11 }], total: 1 },
  }));
  seed();
  state.conversationId = 'c1';
  state.messageDate = '2026-10-02';
  state.messageOffset = 50;
  state.conversationIndex = { c1: { id: 'c1', name: '会话一', message_count: 12 } };
  state.messages = [{ msg_uuid: 'p50', conversation_id: 'c1', edit: { revision: 0 } }];
  state.messagesTotal = 120;

  const done = await approve(store.deleteMessagePermanently('p50', 0, { conversationName: '会话一' }));
  assert.equal(done, true);

  assert.equal(state.conversationId, 'c1', 'a surviving conversation stays open');
  assert.equal(state.messageDate, '2026-10-02', 'the day filter is kept');
  assert.equal(state.messageOffset, 50, 'the page is kept');
  assert.equal(store.currentConversation.value.name, '会话一');
  assert.equal(store.currentConversation.value.message_count, 11, 'the refreshed count is shown');

  const reload = http.find('GET', 'api/messages').at(-1);
  assert.equal(reload.params.get('conversation'), 'c1');
  assert.equal(reload.params.get('date'), '2026-10-02');
  assert.equal(reload.params.get('offset'), '50');
});

test('deleting with the account-wide scope open keeps the account scope', async (t) => {
  const http = installFetch(t);
  baseline(http, {
    messageTotal: 120,
    messageItems: (offset) => [{ msg_uuid: `p${offset}`, edit: { revision: 0 } }],
  });
  const emptied = {
    deleted: true,
    account_uid: UID,
    deleted_messages: 1,
    deleted_conversations: 1,
    msg_uuids: ['p50'],
    msg_uuid: 'p50',
  };
  http.on('DELETE', 'api/message', () => ({ payload: emptied }));
  http.on('POST', 'api/messages/delete', () => ({ payload: emptied }));
  // A conversation was emptied and left the sidebar; the account scope is intact.
  http.on('GET', 'api/conversations', () => ({
    payload: { items: [{ id: 'c2', name: '会话二', message_count: 5 }], total: 1 },
  }));
  seed();
  state.conversationId = ''; // whole-account scope
  state.messageDate = '2026-10-02';
  state.messageOffset = 50;
  state.messages = [{ msg_uuid: 'p50', conversation_id: 'c1', edit: { revision: 0 } }];
  state.messagesTotal = 120;

  const single = await approve(store.deleteMessagePermanently('p50', 0));
  assert.equal(single, true);
  assert.equal(state.conversationId, '', 'the account scope stays open');
  assert.equal(state.messageDate, '2026-10-02', 'the day filter is kept');
  assert.equal(state.messageOffset, 50, 'the page is kept');
  const singleReload = http.find('GET', 'api/messages').at(-1);
  assert.equal(singleReload.params.get('conversation'), null);
  assert.equal(singleReload.params.get('date'), '2026-10-02');

  // A batch in the same account-wide scope behaves the same way.
  store.toggleMessageSelection('p50', 0);
  const batch = await approve(store.deleteSelectedMessages());
  assert.equal(batch, true);
  assert.equal(state.conversationId, '', 'the account scope still stays open');
  assert.equal(state.messageDate, '2026-10-02');
  assert.equal(state.messageOffset, 50);
  const batchReload = http.find('GET', 'api/messages').at(-1);
  assert.equal(batchReload.params.get('conversation'), null);
  assert.equal(batchReload.params.get('date'), '2026-10-02');
});

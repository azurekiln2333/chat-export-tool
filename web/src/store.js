/**
 * Reader/editor state and API actions.
 *
 * A single reactive object is shared by every component. Every identifier is
 * treated as an opaque string (never parsed as a number), so large message and
 * account IDs keep their exact value.
 *
 * Two destructive scopes exist and are kept independent:
 *  - annotations (hide / text / note) stay reversible through `message_edits`;
 *  - permanent deletion physically removes rows from `reader.sqlite`.
 */

import { computed, reactive } from 'vue';

import { ApiError, api, describeError, setCsrfToken } from './api.js';
import { monthOf, shiftMonth } from './calendar.js';
import { buildDecorationCss, applyDecorationCss } from './decorations.js';

export const PAGE_SIZES = [25, 50, 100, 200];
export const MAX_BATCH_DELETE = 500;
const DEFAULT_PAGE_SIZE = 50;
const DEFAULT_CONVERSATION_LIMIT = 100;

let toastSeq = 0;
let conversationRequest = 0;
let messageRequest = 0;
let calendarRequest = 0;

export const state = reactive({
  booting: true,
  authRequired: false,
  authenticated: false,
  csrfToken: '',
  loginBusy: false,
  loginError: '',

  accounts: [],
  accountsBusy: false,
  accountUid: '',

  // Conversations are paginated so cross-page selection stays usable.
  conversations: [],
  conversationsTotal: 0,
  conversationsBusy: false,
  conversationQuery: '',
  conversationOffset: 0,
  conversationLimit: DEFAULT_CONVERSATION_LIMIT,
  conversationId: '',
  // Details of every conversation seen in any loaded page, so the active
  // conversation keeps its name/counts after the sidebar pages away from it.
  conversationIndex: {},

  messages: [],
  messagesTotal: 0,
  messagesBusy: false,
  messageQuery: '',
  messageOffset: 0,
  messageLimit: DEFAULT_PAGE_SIZE,
  messageDate: '',
  includeHidden: false,

  // Calendar for the current account/conversation/search/hidden scope.
  calendar: { month: '', timezone: '', days: [], total: 0, first_date: '', last_date: '' },
  calendarMonth: '',
  calendarBusy: false,

  // Selections: uuid -> revision, id -> message_count. Span pages of one scope.
  selectedMessages: {},
  selectedConversations: {},
  selectionScope: '',

  confirm: null,
  deleting: false,
  deleteError: '',

  decorations: { bubbles: {}, inline_emoji: {} },

  loadError: '',
  conflicts: {},
  toasts: [],
});

export const currentAccount = computed(
  () => state.accounts.find((account) => account.uid === state.accountUid) || null,
);

export const currentConversation = computed(() => {
  const id = state.conversationId;
  if (!id) return null;
  const onPage = state.conversations.find((item) => item.id === id);
  if (onPage) return onPage;
  // The sidebar may be showing another page; fall back to the cached details and
  // finally to the bare id so the title and delete scope stay accurate.
  const cached = state.conversationIndex[id];
  if (cached) return cached;
  return { id, name: id, message_count: null };
});

export const messagePage = computed(() => Math.floor(state.messageOffset / state.messageLimit) + 1);

export const messagePageCount = computed(() =>
  Math.max(1, Math.ceil(state.messagesTotal / state.messageLimit)),
);

export const conversationPage = computed(
  () => Math.floor(state.conversationOffset / state.conversationLimit) + 1,
);

export const conversationPageCount = computed(() =>
  Math.max(1, Math.ceil(state.conversationsTotal / state.conversationLimit)),
);

export const selectedMessageCount = computed(() => Object.keys(state.selectedMessages).length);

export const selectedConversationCount = computed(
  () => Object.keys(state.selectedConversations).length,
);

export const selectedMessageIds = computed(() => Object.keys(state.selectedMessages));

export const selectedConversationIds = computed(() => Object.keys(state.selectedConversations));

export const pageAllMessagesSelected = computed(() => {
  const items = state.messages;
  return items.length > 0 && items.every((item) => state.selectedMessages[item.msg_uuid] !== undefined);
});

export const pageSomeMessagesSelected = computed(() =>
  state.messages.some((item) => state.selectedMessages[item.msg_uuid] !== undefined),
);

export const decorationsReady = computed(
  () => Object.keys(state.decorations.bubbles || {}).length > 0
    || Object.keys(state.decorations.inline_emoji || {}).length > 0,
);

export function notify(text, type = 'info') {
  const id = ++toastSeq;
  state.toasts.push({ id, text, type });
  window.setTimeout(() => dismissToast(id), type === 'error' ? 8000 : 4000);
}

export function dismissToast(id) {
  const index = state.toasts.findIndex((toast) => toast.id === id);
  if (index >= 0) state.toasts.splice(index, 1);
}

function report(error, fallback) {
  const message = describeError(error, fallback);
  state.loadError = message;
  notify(message, 'error');
}

function clearError() {
  state.loadError = '';
}

// ------------------------------------------------------------------- confirm
/**
 * Show a modal confirmation and resolve with the user's choice. Returns `false`
 * immediately when another confirmation is already open, so actions cannot
 * stack. Permanent deletion always goes through here.
 */
export function askConfirm(options) {
  if (state.confirm) return Promise.resolve(false);
  return new Promise((resolve) => {
    state.confirm = {
      danger: true,
      confirmLabel: '确认',
      settled: false,
      ...options,
      // Captured at open time so a later account/scope change can dismiss it.
      account: state.accountUid,
      scope: scopeKey(),
      resolve,
    };
  });
}

/**
 * Settle the open confirmation. Cancelling closes the dialog at once; approving
 * keeps it mounted so the caller can show the in-flight state and any error, and
 * the caller closes it with `closeConfirm()` when the work finishes.
 */
export function resolveConfirm(value) {
  const pending = state.confirm;
  if (!pending) return;
  if (!value) {
    // Dismissing always closes, even after approval, so a failed action can be
    // abandoned (the already-resolved promise simply ignores this).
    state.confirm = null;
    if (!pending.settled) {
      pending.settled = true;
      pending.resolve(false);
    }
    return;
  }
  if (pending.settled) return;
  pending.settled = true;
  pending.resolve(true);
}

/** Close the dialog once the approved action has finished. */
export function closeConfirm() {
  if (state.confirm && state.confirm.settled) state.confirm = null;
}

/** Register the "try again" action offered after a failed deletion. */
function armRetry(account, scope, thunk) {
  const pending = state.confirm;
  if (!pending) return;
  pending.account = account;
  pending.scope = scope;
  pending.retry = () => {
    // Never silently regather a different account/scope's selection on retry.
    if (!sameScope(account, scope)) {
      closeConfirm();
      return undefined;
    }
    return thunk();
  };
}

/**
 * Revalidate the captured account/scope after awaiting the confirmation dialog,
 * before any destructive request is dispatched. A changed account cancels.
 */
function confirmStillInScope(account, scope) {
  if (sameScope(account, scope)) return true;
  closeConfirm();
  return false;
}

/**
 * Re-run the approved action in the still-open dialog. Used by the confirm
 * button after a failure, so the user can retry without losing the selection.
 */
export function retryConfirm() {
  const pending = state.confirm;
  if (!pending || !pending.settled || state.deleting) return undefined;
  if (typeof pending.retry !== 'function') return undefined;
  return pending.retry();
}

// ----------------------------------------------------------------- selection
function scopeKey() {
  return [
    state.accountUid,
    state.conversationId,
    state.messageQuery.trim(),
    // The conversation filter is part of the scope: changing the sidebar search
    // changes which conversations (and deletions) the user is looking at.
    state.conversationQuery.trim(),
    state.messageDate,
    state.includeHidden ? '1' : '0',
  ].join('\u0000');
}

/** True when the UI still shows the account and scope an action was started in. */
function sameScope(account, scope) {
  return state.accountUid === account && scopeKey() === scope;
}

/**
 * Close an open confirmation whose captured account/scope no longer matches the
 * UI, so a stale dialog can never dispatch against a different account.
 */
function dismissStaleConfirm() {
  const pending = state.confirm;
  if (!pending || typeof pending.account !== 'string') return;
  if (sameScope(pending.account, pending.scope)) return;
  // Resolve rather than just hide: the awaiting action must not hang.
  resolveConfirm(false);
}

/** Drop selections when the account/conversation/search/date/hidden scope changes. */
function syncSelectionScope() {
  const key = scopeKey();
  const changed = key !== state.selectionScope;
  if (changed) {
    state.selectionScope = key;
    state.selectedMessages = {};
    state.selectedConversations = {};
  }
  dismissStaleConfirm();
  return changed;
}

export function clearSelection() {
  state.selectedMessages = {};
  state.selectedConversations = {};
}

export function isMessageSelected(uuid) {
  return state.selectedMessages[uuid] !== undefined;
}

export function isConversationSelected(id) {
  return state.selectedConversations[id] !== undefined;
}

/** Selecting a message also records its current revision for the batch call. */
export function toggleMessageSelection(uuid, revision) {
  if (state.selectedMessages[uuid] !== undefined) delete state.selectedMessages[uuid];
  else state.selectedMessages[uuid] = Number(revision) || 0;
}

export function toggleConversationSelection(id, messageCount) {
  if (state.selectedConversations[id] !== undefined) delete state.selectedConversations[id];
  else state.selectedConversations[id] = Number(messageCount) || 1;
}

export function selectAllMessagesOnPage() {
  for (const item of state.messages) {
    state.selectedMessages[item.msg_uuid] = Number(item.edit && item.edit.revision) || 0;
  }
}

export function clearMessagesOnPage() {
  for (const item of state.messages) delete state.selectedMessages[item.msg_uuid];
}

export function selectAllConversationsOnPage() {
  for (const item of state.conversations) {
    state.selectedConversations[item.id] = Math.max(1, Number(item.message_count) || 1);
  }
}

export function clearConversationsOnPage() {
  for (const item of state.conversations) delete state.selectedConversations[item.id];
}

export function togglePageMessageSelection() {
  if (pageAllMessagesSelected.value) clearMessagesOnPage();
  else selectAllMessagesOnPage();
}

// ----------------------------------------------------------------- session
async function readSession() {
  const session = await api.get('session');
  state.authRequired = Boolean(session && session.auth_required);
  state.authenticated = Boolean(session && session.authenticated);
  state.csrfToken = (session && session.csrf_token) || '';
  state.appLabel = (session && session.app_label) || '';
  setCsrfToken(state.csrfToken);
  return session;
}

export async function initialize() {
  state.booting = true;
  clearError();
  try {
    await readSession();
    if (state.authenticated) await loadWorkspace();
  } catch (error) {
    report(error, '初始化失败。');
  } finally {
    state.booting = false;
  }
}

export async function login(token) {
  state.loginBusy = true;
  state.loginError = '';
  try {
    await api.post('login', { token });
    await readSession();
    if (!state.authenticated) throw new ApiError('登录状态未生效。', 401, 'login_failed');
    await loadWorkspace();
    notify('已登录。', 'success');
  } catch (error) {
    state.loginError = describeError(error, '登录失败。');
  } finally {
    state.loginBusy = false;
  }
}

export async function logout() {
  try {
    await api.post('logout', {});
  } catch {
    /* Clearing the session locally is enough even if the request failed. */
  }
  setCsrfToken('');
  state.authenticated = false;
  state.accounts = [];
  state.conversations = [];
  state.messages = [];
  state.accountUid = '';
  state.conversationId = '';
  state.calendar = { month: '', timezone: '', days: [], total: 0, first_date: '', last_date: '' };
  state.calendarMonth = '';
  clearSelection();
  state.selectionScope = '';
  notify('已退出登录。');
}

async function loadWorkspace() {
  await Promise.all([loadAccounts(), loadDecorations()]);
}

// ------------------------------------------------------- metadata loading
export async function loadDecorations() {
  try {
    const data = await api.get('decorations');
    const bubbles = (data && data.bubbles) || {};
    const inline = (data && data.inline_emoji) || {};
    state.decorations = { bubbles, inline_emoji: inline };
    applyDecorationCss(buildDecorationCss(state.decorations));
  } catch (error) {
    report(error, '读取气泡素材失败。');
  }
}

export async function loadAccounts() {
  state.accountsBusy = true;
  try {
    const data = await api.get('accounts');
    state.accounts = (data && data.items) || [];
    if (!state.accountUid && state.accounts.length) {
      await selectAccount(state.accounts[0].uid);
    }
  } catch (error) {
    report(error, '读取账号列表失败。');
  } finally {
    state.accountsBusy = false;
  }
}

/** Reload decorations and the current account/conversation view. */
export async function reloadWorkspace() {
  await loadDecorations();
  await loadAccounts();
  if (state.accountUid) {
    await loadConversations();
    if (state.conversationId || state.messageQuery) await loadMessages();
    await loadCalendar();
  }
}

// ----------------------------------------------------------- conversations
export async function selectAccount(uid) {
  if (uid === state.accountUid) return;
  state.accountUid = uid;
  state.conversationId = '';
  state.conversationQuery = '';
  state.conversationOffset = 0;
  state.conversationIndex = {};
  state.messages = [];
  state.messagesTotal = 0;
  state.messageQuery = '';
  state.messageOffset = 0;
  state.messageDate = '';
  state.conflicts = {};
  state.calendar = { month: '', timezone: '', days: [], total: 0, first_date: '', last_date: '' };
  state.calendarMonth = '';
  clearSelection();
  syncSelectionScope();
  await loadConversations();
  await loadCalendar();
}

export async function loadConversations() {
  if (!state.accountUid) return;
  const request = ++conversationRequest;
  const account = state.accountUid;
  state.conversationsBusy = true;
  clearError();
  try {
    const data = await api.get('conversations', {
      account,
      q: state.conversationQuery.trim(),
      limit: state.conversationLimit,
      offset: state.conversationOffset,
      include_hidden: state.includeHidden,
    });
    if (request !== conversationRequest || account !== state.accountUid) return;
    const total = (data && data.total) || 0;
    // Clamp so a page that fell empty after a delete snaps back to the last page.
    if (state.conversationOffset > 0 && (total === 0 || state.conversationOffset >= total)) {
      state.conversationOffset = total === 0
        ? 0
        : Math.floor((total - 1) / state.conversationLimit) * state.conversationLimit;
      await loadConversations();
      return;
    }
    state.conversations = (data && data.items) || [];
    state.conversationsTotal = total;
    // Remember every conversation seen in any page so the active one keeps its
    // details even after the sidebar paged away from it.
    for (const item of state.conversations) {
      if (item && item.id) state.conversationIndex[item.id] = item;
    }
  } catch (error) {
    if (request === conversationRequest && account === state.accountUid) report(error, '读取会话列表失败。');
  } finally {
    if (request === conversationRequest) state.conversationsBusy = false;
  }
}

export async function searchConversations(query) {
  state.conversationQuery = query;
  state.conversationOffset = 0;
  syncSelectionScope();
  await loadConversations();
}

export async function goToConversationPage(page) {
  const target = Math.min(Math.max(1, page), conversationPageCount.value);
  state.conversationOffset = (target - 1) * state.conversationLimit;
  await loadConversations();
}

export async function setConversationPageSize(size) {
  state.conversationLimit = PAGE_SIZES.includes(size) ? size : DEFAULT_CONVERSATION_LIMIT;
  state.conversationOffset = 0;
  await loadConversations();
}

export async function selectConversation(id) {
  state.conversationId = id;
  state.messageQuery = '';
  state.messageOffset = 0;
  state.messageDate = '';
  state.conflicts = {};
  // Register the just-chosen conversation so the header keeps its name and
  // delete scope even if the sidebar page changes later.
  const onPage = state.conversations.find((item) => item.id === id);
  if (onPage) state.conversationIndex[id] = onPage;
  syncSelectionScope();
  await Promise.all([loadMessages(), loadCalendar()]);
}

/** Full-account search: clear the conversation filter and search every message. */
export async function searchAccountMessages(query) {
  state.messageQuery = query;
  state.conversationId = '';
  state.messageOffset = 0;
  syncSelectionScope();
  await Promise.all([loadMessages(), loadCalendar()]);
}

export async function searchConversationMessages(query) {
  state.messageQuery = query;
  state.messageOffset = 0;
  syncSelectionScope();
  await Promise.all([loadMessages(), loadCalendar()]);
}

// ------------------------------------------------------------------ calendar
function probeMonth() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
}

/**
 * Load the calendar for the current scope. Day buckets are computed by the
 * server from the export timezone; the browser timezone is never used.
 */
export async function loadCalendar() {
  if (!state.accountUid) return;
  const request = ++calendarRequest;
  const account = state.accountUid;
  const month = state.calendarMonth || probeMonth();
  state.calendarBusy = true;
  try {
    const data = await api.get('calendar', {
      account,
      month,
      conversation: state.conversationId,
      q: state.messageQuery.trim(),
      include_hidden: state.includeHidden,
    });
    if (request !== calendarRequest || account !== state.accountUid) return;
    // Adopt the first recorded month as the default once, then refetch it.
    const firstMonth = monthOf(data && data.first_date);
    if (!state.calendarMonth && firstMonth && firstMonth !== month) {
      state.calendarMonth = firstMonth;
      await loadCalendar();
      return;
    }
    state.calendar = {
      month: (data && data.month) || month,
      timezone: (data && data.timezone) || '',
      days: (data && data.days) || [],
      total: (data && data.total) || 0,
      first_date: (data && data.first_date) || '',
      last_date: (data && data.last_date) || '',
    };
    if (!state.calendarMonth) state.calendarMonth = state.calendar.month || month;
  } catch (error) {
    if (request === calendarRequest && account === state.accountUid) report(error, '读取日历失败。');
  } finally {
    if (request === calendarRequest) state.calendarBusy = false;
  }
}

export async function changeCalendarMonth(delta) {
  state.calendarMonth = shiftMonth(state.calendarMonth || probeMonth(), delta);
  await loadCalendar();
}

export async function goToFirstRecordedMonth() {
  const firstMonth = monthOf(state.calendar.first_date);
  if (!firstMonth || firstMonth === state.calendarMonth) return;
  state.calendarMonth = firstMonth;
  await loadCalendar();
}

/** Clicking the selected day again clears the date filter. */
export async function toggleCalendarDate(date) {
  if (date === state.messageDate) {
    await clearMessageDate();
    return;
  }
  await setMessageDate(date);
}

export async function setMessageDate(date) {
  const next = /^\d{4}-\d{2}-\d{2}$/.test(String(date || '')) ? date : '';
  state.messageDate = next;
  state.messageOffset = 0;
  syncSelectionScope();
  await loadMessages();
}

export async function clearMessageDate() {
  if (!state.messageDate) return;
  state.messageDate = '';
  state.messageOffset = 0;
  syncSelectionScope();
  await loadMessages();
}

// ---------------------------------------------------------------- messages
export async function loadMessages() {
  if (!state.accountUid) return;
  const request = ++messageRequest;
  const account = state.accountUid;
  state.messagesBusy = true;
  clearError();
  try {
    const data = await api.get('messages', {
      account,
      conversation: state.conversationId,
      q: state.messageQuery.trim(),
      limit: state.messageLimit,
      offset: state.messageOffset,
      include_hidden: state.includeHidden,
      date: state.messageDate,
    });
    if (request !== messageRequest || account !== state.accountUid) return;
    const total = (data && data.total) || 0;
    // Clamp an emptied page back onto the last page after deletions.
    if (state.messageOffset > 0 && (total === 0 || state.messageOffset >= total)) {
      state.messageOffset = total === 0
        ? 0
        : Math.floor((total - 1) / state.messageLimit) * state.messageLimit;
      await loadMessages();
      return;
    }
    state.messages = (data && data.items) || [];
    state.messagesTotal = total;
  } catch (error) {
    if (request !== messageRequest || account !== state.accountUid) return;
    report(error, '读取消息失败。');
    state.messages = [];
    state.messagesTotal = 0;
  } finally {
    if (request === messageRequest) state.messagesBusy = false;
  }
}

export async function goToMessagePage(page) {
  const target = Math.min(Math.max(1, page), messagePageCount.value);
  state.messageOffset = (target - 1) * state.messageLimit;
  await loadMessages();
}

export async function setMessagePageSize(size) {
  state.messageLimit = PAGE_SIZES.includes(size) ? size : DEFAULT_PAGE_SIZE;
  state.messageOffset = 0;
  await loadMessages();
}

export async function setIncludeHidden(value) {
  state.includeHidden = Boolean(value);
  syncSelectionScope();
  await Promise.all([loadConversations(), loadMessages(), loadCalendar()]);
}

// ------------------------------------------------------------- annotations
function findMessageIndex(uuid) {
  return state.messages.findIndex((item) => item.msg_uuid === uuid);
}

function applyUpdatedMessage(message) {
  const index = findMessageIndex(message.msg_uuid);
  if (index >= 0) state.messages.splice(index, 1, message);
  if (state.selectedMessages[message.msg_uuid] !== undefined) {
    state.selectedMessages[message.msg_uuid] = Number(message.edit && message.edit.revision) || 0;
  }
  const conflict = state.conflicts[message.msg_uuid];
  if (conflict) delete state.conflicts[message.msg_uuid];
  return message;
}

async function fetchMessage(account, uuid) {
  const data = await api.get('message', { account, uuid });
  return (data && data.item) || null;
}

function dropFromList(uuid) {
  const index = findMessageIndex(uuid);
  if (index >= 0) state.messages.splice(index, 1);
  if (state.selectedMessages[uuid] !== undefined) delete state.selectedMessages[uuid];
}

function sameText(left, right) {
  return String(left ?? '') === String(right ?? '');
}

/**
 * Whether an annotation change can alter the current result set. The server
 * filters both the message list and the calendar by visibility and, when a
 * search is active, by `text_override`/`note`. So a change to either must be
 * re-read from the server rather than patched locally.
 */
function annotationChangesScope(previous, updated) {
  if (!previous || !updated) return true;
  const hiddenChanged = Boolean(updated.edit.hidden) !== Boolean(previous.edit.hidden);
  if (hiddenChanged) return true;
  const searchActive = state.messageQuery.trim() !== '';
  if (!searchActive) return false;
  return !sameText(updated.edit.text_override, previous.edit.text_override)
    || !sameText(updated.edit.note, previous.edit.note);
}

/** Re-read the visible page and the calendar after a scope-affecting edit. */
async function refreshAfterAnnotation(account) {
  if (state.accountUid !== account) return;
  await Promise.all([loadMessages(), loadCalendar()]);
}

/**
 * Save an annotation. `expected_revision` guards against overwriting a newer
 * change: a stale revision yields 409, on which the latest message is reloaded
 * and the conflict surfaced so the user can review and resubmit.
 */
export async function saveMessage(uuid, { textOverride, note, hidden }) {
  const account = state.accountUid;
  const scope = scopeKey();
  const index = findMessageIndex(uuid);
  const current = index >= 0 ? state.messages[index] : null;
  if (!current) return null;
  try {
    const data = await api.patch(
      'message',
      {
        text_override: textOverride === undefined ? null : textOverride,
        note: note === undefined ? '' : note,
        hidden: hidden === undefined ? false : hidden,
        expected_revision: current.edit.revision,
      },
      { account, uuid },
    );
    // A reply for a different account or scope must never be spliced into the
    // list the user is looking at now (the same uuid can exist in both).
    if (!sameScope(account, scope)) return null;
    const updated = (data && data.item) || current;
    const result = applyUpdatedMessage(updated);
    if (annotationChangesScope(current, updated)) {
      // Authoritative refetch: this recomputes totals, the page and the day
      // buckets, and also removes a message that no longer matches a search.
      await refreshAfterAnnotation(account);
    } else if (updated.edit.hidden && !state.includeHidden) {
      dropFromList(uuid);
      state.messagesTotal = Math.max(0, state.messagesTotal - 1);
    }
    if (sameScope(account, scope)) notify('已保存修改。', 'success');
    return result;
  } catch (error) {
    if (!sameScope(account, scope)) return null;
    if (error instanceof ApiError && error.status === 409) {
      await handleConflict(account, scope, uuid, '该消息已被其他改动更新，请确认最新内容后重新保存。');
      return null;
    }
    report(error, '保存失败。');
    throw error;
  }
}

/**
 * Restore an annotation through `POST message/restore`. This clears text, note
 * and hidden without deleting anything, and always advances the revision so a
 * stale restore request can never be mistaken for a permanent deletion.
 */
export async function restoreMessage(uuid) {
  const account = state.accountUid;
  const scope = scopeKey();
  const index = findMessageIndex(uuid);
  const current = index >= 0 ? state.messages[index] : null;
  if (!current) return null;
  try {
    const data = await api.post(
      'message/restore',
      { expected_revision: current.edit.revision },
      { account, uuid },
    );
    if (!sameScope(account, scope)) return null;
    const updated = (data && data.item) || current;
    const result = applyUpdatedMessage(updated);
    if (annotationChangesScope(current, updated)) {
      // Restoring can change the search match and the visible population, so the
      // list and the calendar are re-read rather than patched in place.
      await refreshAfterAnnotation(account);
    } else if (updated.edit.hidden && !state.includeHidden) {
      dropFromList(uuid);
      state.messagesTotal = Math.max(0, state.messagesTotal - 1);
    }
    if (sameScope(account, scope)) notify('已恢复原始内容。', 'success');
    return result;
  } catch (error) {
    if (!sameScope(account, scope)) return null;
    if (error instanceof ApiError && error.status === 409) {
      await handleConflict(account, scope, uuid, '无法恢复：该消息已被其他改动更新，请确认最新内容后重试。');
      return null;
    }
    report(error, '恢复失败。');
    throw error;
  }
}

async function handleConflict(account, scope, uuid, message) {
  if (!sameScope(account, scope)) return;
  try {
    const latest = await fetchMessage(account, uuid);
    if (!sameScope(account, scope)) return;
    if (latest) applyUpdatedMessage(latest);
    state.conflicts[uuid] = latest ? latest.edit.revision : 0;
  } catch {
    if (!sameScope(account, scope)) return;
    state.conflicts[uuid] = state.conflicts[uuid] || 0;
  }
  if (!sameScope(account, scope)) return;
  notify(message, 'error');
}

export function conflictRevision(uuid) {
  return state.conflicts[uuid];
}

/** Acknowledge a conflict after the user reloaded the latest revision. */
export function clearConflict(uuid) {
  if (uuid in state.conflicts) delete state.conflicts[uuid];
}

// ------------------------------------------------------- permanent deletion
function scopeLabel(conversationName) {
  return conversationName ? `会话「${conversationName}」` : '整个账号';
}

/**
 * Refresh every aggregate that a deletion can change: account counts,
 * conversation list, message page (clamped) and the calendar.
 */
async function refreshAfterDeletion(account, { conversationIds = [] } = {}) {
  if (state.accountUid !== account) return;
  await loadAccounts();
  if (state.accountUid !== account) return;
  if (state.conversationId && conversationIds.includes(state.conversationId)) {
    state.conversationId = '';
    state.messageOffset = 0;
    state.messageDate = '';
    // The message scope just changed, so any lingering selection is stale.
    syncSelectionScope();
  }
  await loadConversations();
  if (state.accountUid !== account) return;
  // Evicted after the refetch: a deleted conversation never comes back, so a
  // stale page response must not be able to re-seed its name in the cache.
  for (const id of conversationIds) delete state.conversationIndex[id];
  await loadMessages();
  if (state.accountUid !== account) return;
  await loadCalendar();
}

async function runDeletion(work) {
  state.deleting = true;
  state.deleteError = '';
  try {
    return await work();
  } catch (error) {
    // Leave the dialog open so the failure stays visible; the user can retry or
    // cancel without losing the selection.
    state.deleteError = describeError(error, '删除失败，请稍后重试。');
    notify(state.deleteError, 'error');
    return null;
  } finally {
    // Always clear: concurrent deletions are blocked by this flag, so leaving it
    // set after an account switch would deadlock the whole UI.
    state.deleting = false;
  }
}

/** Permanently delete one message (independent of hide/restore). */
export async function deleteMessagePermanently(uuid, revision, { conversationName = '', skipConfirm = false } = {}) {
  if (state.deleting) return false;
  const account = state.accountUid;
  const scope = scopeKey();
  if (!skipConfirm) {
    const ok = await askConfirm({
      title: '永久删除这条消息',
      message: '确定要永久删除这条消息吗？',
      detail:
        `删除范围：${scopeLabel(conversationName)}。该消息会从 reader.sqlite 中彻底移除，`
        + '连同它的编辑记录一起删除，无法撤销，也没有回收站。'
        + '原始聊天数据库、JSONL 与共享媒体不会被修改。',
      confirmLabel: '永久删除',
    });
    if (!ok) return false;
  }
  if (!confirmStillInScope(account, scope)) return false;
  armRetry(account, scope, () => deleteMessagePermanently(uuid, revision, { conversationName, skipConfirm: true }));
  // Capture the active filter before awaiting: deleting the last message of a
  // conversation also drops that conversation server-side, and the heading must
  // not stay on a conversation that no longer exists.
  const activeConversationId = state.conversationId;
  const result = await runDeletion(() =>
    api.delete(
      'message',
      { confirm_delete: true, expected_revision: Number(revision) || 0 },
      { account, uuid },
    ));
  if (!result) return false;
  closeConfirm();
  if (state.accountUid !== account) return true;
  delete state.selectedMessages[uuid];
  await refreshAfterDeletion(account, {
    conversationIds:
      Number(result.deleted_conversations) > 0 && activeConversationId ? [activeConversationId] : [],
  });
  notify('已永久删除该消息。', 'success');
  return true;
}

/** Permanently delete one conversation and every message in it. */
export async function deleteConversationPermanently(id, messageCount, { name = '', skipConfirm = false } = {}) {
  if (state.deleting) return false;
  const account = state.accountUid;
  const scope = scopeKey();
  const count = Math.max(1, Number(messageCount) || 1);
  if (!skipConfirm) {
    const ok = await askConfirm({
      title: '永久删除整个会话',
      message: `确定要永久删除会话「${name || id}」吗？`,
      detail:
        `删除范围：整个账号。该会话及其 ${count} 条消息（包含已隐藏的消息）会从 reader.sqlite 中`
        + '彻底移除，无法撤销，也没有回收站。原始聊天数据库、JSONL 与共享媒体不会被修改。',
      confirmLabel: '永久删除会话',
    });
    if (!ok) return false;
  }
  if (!confirmStillInScope(account, scope)) return false;
  armRetry(account, scope, () => deleteConversationPermanently(id, count, { name, skipConfirm: true }));
  const result = await runDeletion(() =>
    api.delete(
      'conversation',
      { confirm_delete: true, expected_message_count: count },
      { account, conversation: id },
    ));
  if (!result) return false;
  closeConfirm();
  if (state.accountUid !== account) return true;
  delete state.selectedConversations[id];
  await refreshAfterDeletion(account, { conversationIds: [id] });
  notify('已永久删除该会话。', 'success');
  return true;
}

/** Permanently delete every selected message, possibly across pages. */
export async function deleteSelectedMessages({ skipConfirm = false } = {}) {
  if (state.deleting) return false;
  const entries = Object.entries(state.selectedMessages);
  if (!entries.length) return false;
  if (entries.length > MAX_BATCH_DELETE) {
    notify(`一次最多删除 ${MAX_BATCH_DELETE} 条消息，请减少选择。`, 'error');
    return false;
  }
  const account = state.accountUid;
  const scope = scopeKey();
  if (!skipConfirm) {
    const ok = await askConfirm({
      title: '批量永久删除消息',
      message: `确定要永久删除选中的 ${entries.length} 条消息吗？`,
      detail:
        `删除范围：${scopeLabel(currentConversation.value ? currentConversation.value.name : '')}。`
        + '这些消息会从 reader.sqlite 中彻底移除，连同它们的编辑记录一起删除，无法撤销，也没有回收站。'
        + '任何一条过期都会导致整批失败。原始聊天数据库、JSONL 与共享媒体不会被修改。',
      confirmLabel: '永久删除所选',
    });
    if (!ok) return false;
  }
  if (!confirmStillInScope(account, scope)) return false;
  // Re-armed on every run (including a retry) so the open dialog can offer it.
  armRetry(account, scope, () => deleteSelectedMessages({ skipConfirm: true }));
  // A message batch always belongs to one query scope, so the captured active
  // conversation identifies the conversation that a cascade would remove.
  const activeConversationId = state.conversationId;
  const result = await runDeletion(() =>
    api.post(
      'messages/delete',
      {
        confirm_delete: true,
        messages: entries.map(([msg_uuid, expected_revision]) => ({
          msg_uuid,
          expected_revision: Number(expected_revision) || 0,
        })),
      },
      { account },
    ));
  if (!result) return false;
  closeConfirm();
  if (state.accountUid !== account) return true;
  clearSelection();
  await refreshAfterDeletion(account, {
    conversationIds:
      Number(result.deleted_conversations) > 0 && activeConversationId ? [activeConversationId] : [],
  });
  const deleted = Number(result.deleted_messages) || entries.length;
  notify(`已永久删除 ${deleted} 条消息。`, 'success');
  return true;
}

/** Permanently delete every selected conversation, possibly across pages. */
export async function deleteSelectedConversations({ skipConfirm = false } = {}) {
  if (state.deleting) return false;
  const entries = Object.entries(state.selectedConversations);
  if (!entries.length) return false;
  if (entries.length > MAX_BATCH_DELETE) {
    notify(`一次最多删除 ${MAX_BATCH_DELETE} 个会话，请减少选择。`, 'error');
    return false;
  }
  const account = state.accountUid;
  const scope = scopeKey();
  const totalMessages = entries.reduce((sum, [, count]) => sum + (Number(count) || 0), 0);
  if (!skipConfirm) {
    const ok = await askConfirm({
      title: '批量永久删除会话',
      message: `确定要永久删除选中的 ${entries.length} 个会话吗？`,
      detail:
        `删除范围：整个账号。这些会话及其 ${totalMessages} 条消息（包含已隐藏的消息）会从 reader.sqlite 中`
        + '彻底移除，无法撤销，也没有回收站。任何一条过期都会导致整批失败。'
        + '原始聊天数据库、JSONL 与共享媒体不会被修改。',
      confirmLabel: '永久删除所选会话',
    });
    if (!ok) return false;
  }
  if (!confirmStillInScope(account, scope)) return false;
  // Re-armed on every run (including a retry) so the open dialog can offer it.
  armRetry(account, scope, () => deleteSelectedConversations({ skipConfirm: true }));
  const deletedIds = entries.map(([id]) => id);
  const result = await runDeletion(() =>
    api.post(
      'conversations/delete',
      {
        confirm_delete: true,
        conversations: entries.map(([id, expected_message_count]) => ({
          id,
          expected_message_count: Math.max(1, Number(expected_message_count) || 1),
        })),
      },
      { account },
    ));
  if (!result) return false;
  closeConfirm();
  if (state.accountUid !== account) return true;
  clearSelection();
  await refreshAfterDeletion(account, { conversationIds: deletedIds });
  const deleted = Number(result.deleted_conversations) || deletedIds.length;
  notify(`已永久删除 ${deleted} 个会话。`, 'success');
  return true;
}
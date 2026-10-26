/**
 * Reader/editor state and API actions.
 *
 * A single reactive object is shared by every component. IDs are treated as
 * opaque strings throughout (never parsed as numbers) so large message and
 * account identifiers keep their exact value.
 */

import { computed, reactive } from 'vue';

import { ApiError, api, setCsrfToken } from './api.js';
import { buildDecorationCss, applyDecorationCss } from './decorations.js';

export const PAGE_SIZES = [25, 50, 100, 200];
const DEFAULT_PAGE_SIZE = 50;
const DEFAULT_CONVERSATION_LIMIT = 200;

let toastSeq = 0;
let conversationRequest = 0;
let messageRequest = 0;

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

  conversations: [],
  conversationsTotal: 0,
  conversationsBusy: false,
  conversationQuery: '',
  conversationOffset: 0,
  conversationId: '',

  messages: [],
  messagesTotal: 0,
  messagesBusy: false,
  messageQuery: '',
  messageOffset: 0,
  messageLimit: DEFAULT_PAGE_SIZE,
  includeHidden: false,

  decorations: { bubbles: {}, inline_emoji: {} },

  loadError: '',
  conflicts: {},
  toasts: [],
});

export const currentAccount = computed(
  () => state.accounts.find((account) => account.uid === state.accountUid) || null,
);

export const currentConversation = computed(
  () => state.conversations.find((item) => item.id === state.conversationId) || null,
);

export const messagePage = computed(() => Math.floor(state.messageOffset / state.messageLimit) + 1);

export const messagePageCount = computed(() =>
  Math.max(1, Math.ceil(state.messagesTotal / state.messageLimit)),
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
  const message = error instanceof ApiError ? error.message : fallback;
  state.loadError = message;
  notify(message, 'error');
}

function clearError() {
  state.loadError = '';
}

// ----------------------------------------------------------------- session
async function readSession() {
  const session = await api.get('session');
  state.authRequired = Boolean(session && session.auth_required);
  state.authenticated = Boolean(session && session.authenticated);
  state.csrfToken = (session && session.csrf_token) || '';
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
    state.loginError = error instanceof ApiError ? error.message : '登录失败。';
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
  }
}

// ----------------------------------------------------------- conversations
export async function selectAccount(uid) {
  if (uid === state.accountUid) return;
  state.accountUid = uid;
  state.conversationId = '';
  state.conversationQuery = '';
  state.conversationOffset = 0;
  state.messages = [];
  state.messagesTotal = 0;
  state.messageQuery = '';
  state.messageOffset = 0;
  state.conflicts = {};
  await loadConversations();
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
      limit: DEFAULT_CONVERSATION_LIMIT,
      offset: state.conversationOffset,
      include_hidden: state.includeHidden,
    });
    if (request !== conversationRequest || account !== state.accountUid) return;
    state.conversations = (data && data.items) || [];
    state.conversationsTotal = (data && data.total) || 0;
  } catch (error) {
    if (request === conversationRequest && account === state.accountUid) report(error, '读取会话列表失败。');
  } finally {
    if (request === conversationRequest) state.conversationsBusy = false;
  }
}

export async function searchConversations(query) {
  state.conversationQuery = query;
  state.conversationOffset = 0;
  await loadConversations();
}

export async function selectConversation(id) {
  state.conversationId = id;
  state.messageQuery = '';
  state.messageOffset = 0;
  state.conflicts = {};
  await loadMessages();
}

/** Full-account search: clear the conversation filter and search every message. */
export async function searchAccountMessages(query) {
  state.messageQuery = query;
  state.conversationId = '';
  state.messageOffset = 0;
  await loadMessages();
}

export async function searchConversationMessages(query) {
  state.messageQuery = query;
  state.messageOffset = 0;
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
    });
    if (request !== messageRequest || account !== state.accountUid) return;
    state.messages = (data && data.items) || [];
    state.messagesTotal = (data && data.total) || 0;
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
  const next = PAGE_SIZES.includes(size) ? size : DEFAULT_PAGE_SIZE;
  state.messageLimit = next;
  state.messageOffset = 0;
  await loadMessages();
}

export async function setIncludeHidden(value) {
  state.includeHidden = Boolean(value);
  await Promise.all([loadConversations(), loadMessages()]);
}

function findMessageIndex(uuid) {
  return state.messages.findIndex((item) => item.msg_uuid === uuid);
}

function applyUpdatedMessage(message) {
  const index = findMessageIndex(message.msg_uuid);
  if (index >= 0) state.messages.splice(index, 1, message);
  const conflict = state.conflicts[message.msg_uuid];
  if (conflict) delete state.conflicts[message.msg_uuid];
  return message;
}

async function fetchMessage(uuid) {
  const data = await api.get('message', { account: state.accountUid, uuid });
  return (data && data.item) || null;
}

/**
 * Save an edit. `expected_revision` guards against overwriting a newer change:
 * a stale revision yields 409, on which the latest message is reloaded and the
 * conflict is surfaced so the user can review and resubmit.
 */
export async function saveMessage(uuid, { textOverride, note, hidden }) {
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
      { account: state.accountUid, uuid },
    );
    const updated = (data && data.item) || current;
    const result = applyUpdatedMessage(updated);
    if (updated.edit.hidden && !state.includeHidden) {
      state.messages.splice(findMessageIndex(uuid), 1);
      state.messagesTotal = Math.max(0, state.messagesTotal - 1);
    }
    notify('已保存修改。', 'success');
    return result;
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      await handleConflict(uuid, '该消息已被其他改动更新，请确认最新内容后重新保存。');
      return null;
    }
    report(error, '保存失败。');
    throw error;
  }
}

/** Restore original text/note/visibility. Reversible; advances the revision. */
export async function restoreMessage(uuid) {
  const index = findMessageIndex(uuid);
  const current = index >= 0 ? state.messages[index] : null;
  if (!current) return null;
  try {
    const data = await api.delete(
      'message',
      { expected_revision: current.edit.revision },
      { account: state.accountUid, uuid },
    );
    const updated = (data && data.item) || current;
    const result = applyUpdatedMessage(updated);
    if (updated.edit.hidden && !state.includeHidden) {
      state.messages.splice(findMessageIndex(uuid), 1);
      state.messagesTotal = Math.max(0, state.messagesTotal - 1);
    }
    notify('已恢复原始内容。', 'success');
    return result;
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      await handleConflict(uuid, '无法恢复：该消息已被其他改动更新，请确认最新内容后重试。');
      return null;
    }
    report(error, '恢复失败。');
    throw error;
  }
}

async function handleConflict(uuid, message) {
  try {
    const latest = await fetchMessage(uuid);
    if (latest) applyUpdatedMessage(latest);
    state.conflicts[uuid] = latest ? latest.edit.revision : 0;
  } catch {
    state.conflicts[uuid] = state.conflicts[uuid] || 0;
  }
  notify(message, 'error');
}

export function conflictRevision(uuid) {
  return state.conflicts[uuid];
}

/** Acknowledge a conflict after the user reloaded the latest revision. */
export function clearConflict(uuid) {
  if (uuid in state.conflicts) delete state.conflicts[uuid];
}

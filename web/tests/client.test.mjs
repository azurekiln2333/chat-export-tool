import assert from 'node:assert/strict';
import test from 'node:test';
import { api, ApiError } from '../src/api.js';
import { loadConversations, loadMessages, state } from '../src/store.js';
import { accountSourceLabel } from '../src/utils.js';

test('account source labels use explicit API identities and leave unidentified accounts honest', () => {
  assert.equal(accountSourceLabel({ source_app: 'wechat', uid: '8100000000000011' }), '微信');
  assert.equal(accountSourceLabel({ source_app: 'duoshan' }), '多闪/抖音');
  assert.equal(accountSourceLabel({ source_app: 'xhs' }), '小红书');
  assert.equal(accountSourceLabel({ uid: '8100000000000011' }), '来源未识别');
  assert.equal(accountSourceLabel({ source_app: '__proto__' }), '来源未识别');
});

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });
}

test('browser errors remain Chinese while retaining machine error codes', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => json({ error: {
    code: 'invalid_token', message: 'The access token is incorrect.',
  } }, 401));
  await assert.rejects(api.post('login', { token: 'bad' }), error => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.code, 'invalid_token');
    assert.equal(error.status, 401);
    assert.equal(error.message, '访问口令不正确。');
    return true;
  });
});

test('switching accounts cannot display a delayed previous-account message response', async (t) => {
  const pending = [];
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => pending.push(resolve)));
  state.accountUid = '8100000000000011';
  const previous = loadMessages();
  state.accountUid = '8210000000000022';
  const current = loadMessages();
  pending[1](json({ items: [{ msg_uuid: 'current', text: 'current' }], total: 1 }));
  await current;
  pending[0](json({ items: [{ msg_uuid: 'previous', text: 'previous' }], total: 1 }));
  await previous;
  assert.deepEqual(state.messages.map(item => item.msg_uuid), ['current']);
  assert.equal(state.messagesBusy, false);
});

test('a delayed earlier conversation search cannot overwrite the latest search', async (t) => {
  const pending = [];
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => pending.push(resolve)));
  state.accountUid = '8100000000000011';
  state.conversationQuery = 'previous';
  const previous = loadConversations();
  state.conversationQuery = 'current';
  const current = loadConversations();
  pending[1](json({ items: [{ id: 'current' }], total: 1 }));
  await current;
  pending[0](json({ items: [{ id: 'previous' }], total: 1 }));
  await previous;
  assert.deepEqual(state.conversations.map(item => item.id), ['current']);
  assert.equal(state.conversationsBusy, false);
});

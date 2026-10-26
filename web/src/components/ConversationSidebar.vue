<script setup>
/** Conversation sidebar: search, list and selection. */
import { ref, watch } from 'vue';

import Icon from './Icon.vue';
import { loadConversations, searchConversations, selectConversation, state } from '../store.js';
import { debounce, shortTime } from '../utils.js';
import { currentAccount } from '../store.js';

const query = ref(state.conversationQuery);

const runSearch = debounce((value) => {
  searchConversations(value);
}, 300);

watch(query, (value) => runSearch(value));

watch(
  () => state.conversationQuery,
  (value) => {
    if (value !== query.value) query.value = value;
  },
);

function clearSearch() {
  query.value = '';
  searchConversations('');
}

function choose(id) {
  selectConversation(id);
}
</script>

<template>
  <aside class="sidebar" aria-label="会话列表">
    <div class="sidebar-head">
      <div class="sidebar-title">
        <Icon name="list" :size="14" />
        会话
        <span class="count">
          {{ state.conversationsTotal.toLocaleString('zh-CN') }}
          <template v-if="state.conversationQuery">个匹配</template>
        </span>
      </div>
      <div class="search">
        <Icon name="search" :size="16" />
        <input
          v-model="query"
          class="input"
          type="search"
          placeholder="搜索会话名称或编号"
          aria-label="搜索会话"
        />
        <button
          v-if="query"
          class="search-clear"
          type="button"
          aria-label="清除会话搜索"
          @click="clearSearch"
        >
          <Icon name="close" :size="14" />
        </button>
      </div>
      <button
        class="btn btn-sm btn-ghost"
        type="button"
        :disabled="!currentAccount"
        @click="loadConversations()"
      >
        <Icon name="refresh" :size="14" />
        刷新
      </button>
    </div>

    <div v-if="state.conversationsBusy" class="loading-bar" />

    <ul v-if="state.conversations.length" class="conversation-list">
      <li v-for="item in state.conversations" :key="item.id">
        <button
          class="conversation-item"
          :class="{ 'is-active': item.id === state.conversationId }"
          type="button"
          :aria-current="item.id === state.conversationId ? 'true' : undefined"
          @click="choose(item.id)"
        >
          <span class="conversation-name">{{ item.name }}</span>
          <span class="conversation-meta">
            <span>{{ Number(item.message_count).toLocaleString('zh-CN') }} 条</span>
            <span v-if="item.last_time">{{ shortTime(item.last_time) }}</span>
          </span>
        </button>
      </li>
    </ul>

    <div v-else-if="!state.conversationsBusy" class="empty">
      <span class="empty-icon"><Icon name="chat" :size="20" /></span>
      <h3>{{ state.conversationQuery ? '没有匹配的会话' : '暂无会话' }}</h3>
      <p>
        {{
          state.conversationQuery
            ? '试试更短的关键词，或清除搜索。'
            : '此账号在索引中还没有会话记录。'
        }}
      </p>
    </div>
  </aside>
</template>
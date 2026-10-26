<script setup>
/**
 * Message pane: title, search (conversation or whole account), day filter,
 * calendar, selection, batch deletion, message list and bounded pagination.
 */
import { computed, ref, watch } from 'vue';

import Icon from './Icon.vue';
import MessageItem from './MessageItem.vue';
import MonthCalendar from './MonthCalendar.vue';
import Pager from './Pager.vue';
import {
  clearMessageDate,
  clearSelection,
  currentAccount,
  currentConversation,
  deleteSelectedMessages,
  goToMessagePage,
  loadMessages,
  messagePage,
  messagePageCount,
  pageAllMessagesSelected,
  searchAccountMessages,
  searchConversationMessages,
  selectedMessageCount,
  setIncludeHidden,
  setMessagePageSize,
  state,
  togglePageMessageSelection,
} from '../store.js';
import { debounce } from '../utils.js';

const query = ref(state.messageQuery);
const calendarOpen = ref(false);

const runSearch = debounce((value) => {
  if (state.conversationId) searchConversationMessages(value);
  else searchAccountMessages(value);
}, 320);

watch(query, (value) => runSearch(value));

watch(
  () => state.messageQuery,
  (value) => {
    if (value !== query.value) query.value = value;
  },
);

const title = computed(() => {
  if (currentConversation.value) return currentConversation.value.name;
  if (state.messageQuery) return '全账号搜索结果';
  return '全部消息';
});

const subtitle = computed(() => {
  const total = state.messagesTotal.toLocaleString('zh-CN');
  if (currentConversation.value) return `${total} 条记录`;
  if (state.messageQuery) return `匹配 ${total} 条`;
  return `${total} 条记录`;
});

const showSearchScopeHint = computed(() => Boolean(state.messageQuery) && !state.conversationId);
const busy = computed(() => state.deleting);

function clearSearch() {
  query.value = '';
  if (state.conversationId) searchConversationMessages('');
  else searchAccountMessages('');
}

function searchWholeAccount() {
  searchAccountMessages(query.value);
}

function toggleHidden(event) {
  setIncludeHidden(event.target.checked);
}
</script>

<template>
  <section class="reader" aria-label="消息列表">
    <div class="reader-head">
      <div class="reader-title">
        <h2>{{ title }}</h2>
        <span class="sub">{{ subtitle }}</span>
        <span v-if="state.messageDate" class="date-chip">
          <Icon name="list" :size="13" />
          {{ state.messageDate }}
          <button class="date-chip-clear" type="button" aria-label="清除日期筛选" @click="clearMessageDate">
            <Icon name="close" :size="12" />
          </button>
        </span>
        <button
          v-if="!state.conversationId && state.messageQuery"
          class="btn btn-sm btn-ghost"
          type="button"
          @click="clearSearch"
        >
          <Icon name="close" :size="14" />
          清除搜索
        </button>
      </div>

      <div class="reader-toolbar">
        <div class="search">
          <Icon name="search" :size="16" />
          <input
            v-model="query"
            class="input"
            type="search"
            :placeholder="state.conversationId ? '在本会话中搜索正文或备注' : '搜索全部消息正文或备注'"
            aria-label="搜索消息"
          />
          <button v-if="query" class="search-clear" type="button" aria-label="清除消息搜索" @click="clearSearch">
            <Icon name="close" :size="14" />
          </button>
        </div>

        <button
          v-if="state.conversationId"
          class="btn btn-sm"
          type="button"
          @click="searchWholeAccount"
        >
          <Icon name="search" :size="14" />
          在整个账号中搜索
        </button>

        <button
          class="btn btn-sm"
          :class="{ 'btn-primary': calendarOpen }"
          type="button"
          :aria-expanded="calendarOpen ? 'true' : 'false'"
          @click="calendarOpen = !calendarOpen"
        >
          <Icon name="list" :size="14" />
          按日期浏览
        </button>

        <label class="toggle">
          <input type="checkbox" :checked="state.includeHidden" @change="toggleHidden" />
          显示已隐藏
        </label>

        <button
          class="btn btn-sm btn-ghost btn-icon"
          type="button"
          aria-label="刷新消息"
          :disabled="state.messagesBusy || !currentAccount"
          @click="loadMessages()"
        >
          <Icon name="refresh" :size="15" />
        </button>
      </div>

      <div class="selection-bar">
        <label class="toggle">
          <input
            type="checkbox"
            :checked="pageAllMessagesSelected"
            :disabled="!state.messages.length || busy"
            @change="togglePageMessageSelection"
          />
          全选本页
        </label>
        <span class="selection-count">已选 {{ selectedMessageCount }} 条</span>
        <button
          class="btn btn-sm btn-danger"
          type="button"
          :disabled="!selectedMessageCount || busy"
          @click="deleteSelectedMessages"
        >
          <Icon name="warning" :size="14" />
          永久删除所选
        </button>
        <button
          class="btn btn-sm btn-ghost"
          type="button"
          :disabled="!selectedMessageCount || busy"
          @click="clearSelection"
        >
          取消选择
        </button>
        <span class="editor-hint">可跨页选择同一查询范围内的消息。</span>
      </div>

      <p v-if="showSearchScopeHint" class="editor-hint">
        正在整个账号中搜索{{ state.messageQuery ? '「' + state.messageQuery + '」' : '' }}。
      </p>

      <MonthCalendar v-if="calendarOpen" />
    </div>

    <div v-if="state.messagesBusy" class="loading-bar" />

    <div class="reader-scroll">
      <p v-if="state.loadError" class="alert" role="alert">
        <Icon name="warning" :size="15" />
        {{ state.loadError }}
      </p>

      <ol v-if="state.messages.length" class="message-list">
        <li v-for="message in state.messages" :key="message.msg_uuid">
          <MessageItem
            :message="message"
            :account-uid="state.accountUid"
            :decorations="state.decorations"
          />
        </li>
      </ol>

      <div v-else-if="!state.messagesBusy" class="empty">
        <span class="empty-icon"><Icon name="chat" :size="20" /></span>
        <h3>
          {{
            state.messageQuery
              ? '没有匹配的消息'
              : state.messageDate
                ? '这一天没有消息'
                : state.conversationId
                  ? '此会话没有消息'
                  : '请选择左侧会话'
          }}
        </h3>
        <p>
          {{
            state.messageQuery
              ? '搜索会同时匹配原始正文、编辑后的正文和备注。'
              : state.messageDate
                ? '可以清除日期筛选，或选择日历中有记录的日期。'
                : state.conversationId
                  ? '该会话在索引中没有可显示的消息记录。'
                  : '选择一个会话查看消息，或直接搜索整个账号。'
          }}
        </p>
        <button
          v-if="state.messageDate"
          class="btn btn-sm"
          type="button"
          @click="clearMessageDate"
        >
          <Icon name="close" :size="14" />
          清除日期筛选
        </button>
      </div>
    </div>

    <div class="reader-foot">
      <Pager
        :page="messagePage"
        :page-count="messagePageCount"
        :total="state.messagesTotal"
        :limit="state.messageLimit"
        :busy="state.messagesBusy"
        @change="goToMessagePage"
        @update:limit="setMessagePageSize"
      />
    </div>
  </section>
</template>
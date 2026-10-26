<script setup>
/** Application shell: boot, login gate and the reader layout. */
import { onMounted, ref, watch } from 'vue';

import ConfirmDialog from './ConfirmDialog.vue';
import ConversationSidebar from './ConversationSidebar.vue';
import Icon from './Icon.vue';
import LoginPanel from './LoginPanel.vue';
import MessagePane from './MessagePane.vue';
import ToastStack from './ToastStack.vue';
import { currentAccount, initialize, logout, reloadWorkspace, selectAccount, state } from '../store.js';

const sidebarOpen = ref(false);

onMounted(() => {
  initialize();
});

watch(
  () => state.conversationId,
  () => {
    sidebarOpen.value = false;
  },
);

function changeAccount(event) {
  selectAccount(event.target.value);
}
</script>

<template>
  <div v-if="state.booting" class="boot">
    <Icon name="chat" :size="28" />
    <p>正在载入聊天记录…</p>
  </div>

  <LoginPanel v-else-if="state.authRequired && !state.authenticated" />

  <div v-else class="app">
    <header class="app-header">
      <button
        class="btn btn-icon btn-ghost sidebar-toggle"
        type="button"
        :aria-expanded="sidebarOpen ? 'true' : 'false'"
        aria-label="切换会话列表"
        @click="sidebarOpen = !sidebarOpen"
      >
        <Icon name="menu" :size="18" />
      </button>

      <div class="brand">
        <span class="brand-mark"><Icon name="chat" :size="17" /></span>
        <span>多闪聊天记录</span>
      </div>

      <div class="header-actions">
        <label class="visually-hidden" for="account-select">选择账号</label>
        <select
          id="account-select"
          class="select"
          style="width: auto; max-width: 240px"
          :value="state.accountUid"
          :disabled="state.accountsBusy || !state.accounts.length"
          @change="changeAccount"
        >
          <option v-if="!state.accounts.length" value="">没有账号</option>
          <option v-for="account in state.accounts" :key="account.uid" :value="account.uid">
            {{ account.name }} · {{ Number(account.message_count).toLocaleString('zh-CN') }} 条
          </option>
        </select>

        <button
          class="btn btn-sm btn-ghost"
          type="button"
          :disabled="state.accountsBusy || !currentAccount"
          aria-label="刷新"
          @click="reloadWorkspace"
        >
          <Icon name="refresh" :size="15" />
          <span class="hide-sm">刷新</span>
        </button>

        <button
          v-if="state.authRequired"
          class="btn btn-sm btn-ghost"
          type="button"
          @click="logout"
        >
          <Icon name="logout" :size="15" />
          <span class="hide-sm">退出</span>
        </button>
      </div>
    </header>

    <div class="app-body">
      <div v-if="sidebarOpen" class="sidebar-backdrop" @click="sidebarOpen = false"></div>
      <ConversationSidebar :class="{ 'is-open': sidebarOpen }" />
      <MessagePane />
    </div>

    <ToastStack />
    <ConfirmDialog />
  </div>
</template>
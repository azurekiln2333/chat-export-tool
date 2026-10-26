<script setup>
import { ref } from 'vue';

import Icon from './Icon.vue';
import { login, state } from '../store.js';

const token = ref('');

async function submit() {
  const value = token.value.trim();
  if (!value) return;
  await login(value);
  token.value = '';
}
</script>

<template>
  <div class="login">
    <form class="login-card" autocomplete="off" @submit.prevent="submit">
      <div class="brand">
        <span class="brand-mark"><Icon name="chat" :size="17" /></span>
        <span>多闪聊天记录</span>
      </div>
      <p>此服务器需要访问口令。口令仅用于本次访问，不会出现在网址中。</p>

      <div class="field">
        <label for="access-token">访问口令</label>
        <input
          id="access-token"
          v-model="token"
          class="input"
          type="password"
          name="token"
          autocomplete="current-password"
          spellcheck="false"
          placeholder="请输入访问口令"
          :disabled="state.loginBusy"
        />
      </div>

      <p v-if="state.loginError" class="alert" role="alert">{{ state.loginError }}</p>

      <button class="btn btn-primary" type="submit" :disabled="state.loginBusy || !token.trim()">
        <Icon name="lock" :size="16" />
        {{ state.loginBusy ? '正在登录…' : '登录' }}
      </button>

      <p class="editor-hint">
        提示：口令由启动服务器时的 <code>--token</code> 或环境变量
        <code>DUOSHAN_ACCESS_TOKEN</code> 提供。
      </p>
    </form>
  </div>
</template>
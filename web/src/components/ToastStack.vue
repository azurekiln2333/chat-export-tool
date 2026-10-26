<script setup>
import Icon from './Icon.vue';
import { dismissToast, state } from '../store.js';

const ICON_BY_TYPE = { success: 'check', error: 'warning', info: 'info' };
</script>

<template>
  <div class="toast-stack" role="status" aria-live="polite">
    <div
      v-for="toast in state.toasts"
      :key="toast.id"
      class="toast"
      :class="'is-' + toast.type"
    >
      <Icon :name="ICON_BY_TYPE[toast.type] || 'info'" :size="16" />
      <span>{{ toast.text }}</span>
      <button class="toast-close" type="button" aria-label="关闭提示" @click="dismissToast(toast.id)">
        <Icon name="close" :size="14" />
      </button>
    </div>
  </div>
</template>
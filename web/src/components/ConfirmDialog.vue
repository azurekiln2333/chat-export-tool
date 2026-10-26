<script setup>
/**
 * Modal confirmation used by every destructive action. Focus starts on the
 * cancel button so a stray Enter cannot trigger a permanent deletion, and the
 * confirm button is disabled while a request is in flight (no double submit).
 */
import { onBeforeUnmount, onMounted, ref } from 'vue';

import Icon from './Icon.vue';
import { resolveConfirm, retryConfirm, state } from '../store.js';

const cancelButton = ref(null);

function cancel() {
  if (state.deleting) return;
  resolveConfirm(false);
}

function confirm() {
  if (state.deleting) return;
  // After a failure the dialog stays open with the error; the same button
  // re-runs the approved action so the user can retry without reselecting.
  if (state.confirm && state.confirm.settled) {
    retryConfirm();
    return;
  }
  resolveConfirm(true);
}

function onKeydown(event) {
  if (!state.confirm) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    cancel();
  }
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown);
  cancelButton.value?.focus?.();
});

onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown));
</script>

<template>
  <div
    v-if="state.confirm"
    class="modal-backdrop"
    role="dialog"
    aria-modal="true"
    :aria-label="state.confirm.title"
  >
    <div class="modal" :class="{ 'is-danger': state.confirm.danger }">
      <div class="modal-head">
        <span class="modal-icon" :class="{ 'is-danger': state.confirm.danger }">
          <Icon :name="state.confirm.danger ? 'warning' : 'info'" :size="18" />
        </span>
        <h2>{{ state.confirm.title }}</h2>
      </div>

      <p class="modal-message">{{ state.confirm.message }}</p>
      <p v-if="state.confirm.detail" class="modal-detail">{{ state.confirm.detail }}</p>

      <p v-if="state.deleteError" class="alert" role="alert">
        <Icon name="warning" :size="15" />
        {{ state.deleteError }}
      </p>

      <div class="modal-actions">
        <button ref="cancelButton" class="btn" type="button" :disabled="state.deleting" @click="cancel">
          取消
        </button>
        <button
          class="btn"
          :class="state.confirm.danger ? 'btn-danger-solid' : 'btn-primary'"
          type="button"
          :disabled="state.deleting"
          @click="confirm"
        >
          <Icon :name="state.confirm.danger ? 'warning' : 'check'" :size="15" />
          {{ state.deleting ? '正在处理…' : state.confirm.settled ? '重试删除' : state.confirm.confirmLabel }}
        </button>
      </div>
    </div>
  </div>
</template>
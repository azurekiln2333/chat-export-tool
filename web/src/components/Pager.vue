<script setup>
import { computed, ref, watch } from 'vue';

import Icon from './Icon.vue';
import { PAGE_SIZES } from '../store.js';

const props = defineProps({
  page: { type: Number, required: true },
  pageCount: { type: Number, required: true },
  total: { type: Number, default: 0 },
  limit: { type: Number, required: true },
  busy: { type: Boolean, default: false },
});

const emit = defineEmits(['change', 'update:limit']);

const jump = ref(String(props.page));
watch(
  () => props.page,
  (value) => {
    jump.value = String(value);
  },
);

const canPrev = computed(() => props.page > 1 && !props.busy);
const canNext = computed(() => props.page < props.pageCount && !props.busy);
const rangeText = computed(() => {
  if (!props.total) return '0 条';
  const start = (props.page - 1) * props.limit + 1;
  const end = Math.min(props.total, props.page * props.limit);
  return `${start}–${end} / 共 ${props.total.toLocaleString('zh-CN')} 条`;
});

function go(page) {
  const target = Math.min(Math.max(1, page), Math.max(1, props.pageCount));
  if (target !== props.page) emit('change', target);
}

function submitJump() {
  const value = Number.parseInt(jump.value, 10);
  if (Number.isFinite(value)) go(value);
  else jump.value = String(props.page);
}

function changeLimit(event) {
  const value = Number.parseInt(event.target.value, 10);
  if (Number.isFinite(value)) emit('update:limit', value);
}
</script>

<template>
  <div class="pager">
    <button class="btn btn-sm btn-ghost" type="button" :disabled="!canPrev" aria-label="第一页" @click="go(1)">
      <Icon name="chevrons-left" :size="15" />
    </button>
    <button class="btn btn-sm btn-ghost" type="button" :disabled="!canPrev" aria-label="上一页" @click="go(page - 1)">
      <Icon name="chevron-left" :size="15" />
    </button>

    <span class="pager-info">第 {{ page }} / {{ pageCount }} 页</span>
    <span class="pager-info">· {{ rangeText }}</span>

    <button
      class="btn btn-sm btn-ghost"
      type="button"
      :disabled="!canNext"
      aria-label="下一页"
      @click="go(page + 1)"
    >
      <Icon name="chevron-right" :size="15" />
    </button>
    <button
      class="btn btn-sm btn-ghost"
      type="button"
      :disabled="!canNext"
      aria-label="最后一页"
      @click="go(pageCount)"
    >
      <Icon name="chevrons-right" :size="15" />
    </button>

    <span class="spacer" />

    <label class="pager-info" for="pager-jump">跳至</label>
    <input
      id="pager-jump"
      v-model="jump"
      class="input"
      style="width: 64px; padding: 5px 8px; font-size: 13px"
      inputmode="numeric"
      @keydown.enter.prevent="submitJump"
      @blur="submitJump"
    />
    <span class="pager-info">页</span>

    <label class="pager-info" for="pager-size">每页</label>
    <select id="pager-size" class="select" :value="limit" @change="changeLimit">
      <option v-for="size in PAGE_SIZES" :key="size" :value="size">{{ size }} 条</option>
    </select>
  </div>
</template>
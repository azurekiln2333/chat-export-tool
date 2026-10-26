<script setup>
/**
 * Raw original detail for one message. `raw_json` is the exact source JSONL
 * line kept as a string, so identifiers are shown without numeric rounding.
 * It is displayed through text interpolation in a `<pre>` block, never parsed
 * into numbers and never rendered as HTML.
 */
import { computed, ref } from 'vue';

import Icon from './Icon.vue';
import { formatBytes } from '../media.js';

const props = defineProps({
  message: { type: Object, required: true },
});

const copied = ref(false);

const meta = computed(() => props.message.meta || {});
const bubble = computed(() => meta.value.bubble || {});
const resource = computed(() => meta.value.media || {});

const metaRows = computed(() => {
  const rows = [
    ['消息标识', props.message.msg_uuid],
    ['会话', props.message.conversation_name || props.message.conversation_id],
    ['发送者', `${props.message.sender_name}（${props.message.sender}）`],
    ['时间', `${props.message.time} · ${meta.value.timezone || ''}`],
    ['类型', `${props.message.type_name}（type ${props.message.message_type}）`],
  ];
  if (bubble.value.id) {
    rows.push(['气泡', `ID ${bubble.value.id} · ${bubble.value.side === '1' ? '己方' : '对方'}`]);
  }
  if (resource.value.path) {
    rows.push(['媒体', `${resource.value.kind || ''} · ${resource.value.status || ''}`]);
    const size = formatBytes(resource.value.bytes);
    if (size) rows.push(['大小', size]);
  }
  return rows;
});

async function copyRaw() {
  try {
    await navigator.clipboard.writeText(String(props.message.raw_json || ''));
    copied.value = true;
    window.setTimeout(() => {
      copied.value = false;
    }, 2000);
  } catch {
    copied.value = false;
  }
}
</script>

<template>
  <details class="detail-block">
    <summary>
      原始数据
      <span v-if="copied" class="badge">已复制</span>
    </summary>
    <div class="detail-body">
      <dl class="meta-grid">
        <template v-for="row in metaRows" :key="row[0]">
          <dt>{{ row[0] }}</dt>
          <dd>{{ row[1] }}</dd>
        </template>
      </dl>

      <div class="editor-row" style="margin: 10px 0 6px">
        <button class="btn btn-sm" type="button" @click="copyRaw">
          <Icon name="file" :size="14" />
          复制原始 JSONL
        </button>
        <span class="editor-hint">原始行按字符串原样保存，超大 ID 不会丢失精度。</span>
      </div>
      <pre class="raw">{{ message.raw_json }}</pre>
    </div>
  </details>
</template>
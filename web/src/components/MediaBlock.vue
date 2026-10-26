<script setup>
/**
 * Local media element for one manifest resource. The kind is authoritative
 * (`kind: 'voice'` describes AAC audio even though the app stores it as
 * video/mpeg), and remote URLs are never loaded: every `src` comes from
 * `resolveMedia`, which only allows contained `attachments/` paths.
 */
import { computed, ref } from 'vue';

import Icon from './Icon.vue';
import { resolveMedia } from '../media.js';

const props = defineProps({
  source: { type: String, default: '' },
  kind: { type: String, default: 'file' },
  alt: { type: String, default: '' },
  fileName: { type: String, default: '' },
});

const failed = ref(false);

const src = computed(() => resolveMedia(props.source));
const label = computed(() => props.alt || props.fileName || '本地媒体');

const ICON_BY_KIND = { audio: 'mic', video: 'video', image: 'image', file: 'file' };
</script>

<template>
  <div v-if="src && !failed" class="bubble-media">
    <audio
      v-if="kind === 'audio'"
      controls
      preload="none"
      :src="src"
      :aria-label="label"
      @error="failed = true"
    ></audio>
    <video
      v-else-if="kind === 'video'"
      controls
      preload="metadata"
      :src="src"
      :aria-label="label"
      @error="failed = true"
    ></video>
    <a
      v-else-if="kind === 'image'"
      class="sticker-link"
      :href="src"
      target="_blank"
      rel="noopener"
    >
      <img class="bubble-image" :src="src" :alt="label" loading="lazy" decoding="async" />
    </a>
    <a v-else class="btn btn-sm" :href="src" target="_blank" rel="noopener" download>
      <Icon :name="ICON_BY_KIND[kind] || 'file'" :size="15" />
      打开附件
    </a>
  </div>
  <p v-else class="playback-error">
    浏览器无法播放或打开此文件，请稍后重试。
  </p>
</template>
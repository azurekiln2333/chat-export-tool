<script setup>
/**
 * Renders message text safely: text runs go through Vue text interpolation and
 * inline-emoji runs become local `<img>` elements. Raw chat is never inserted
 * as HTML.
 */
defineProps({
  segments: { type: Array, required: true },
});
</script>

<template>
  <template v-for="(segment, index) in segments" :key="index">
    <span v-if="segment.type === 'text'">{{ segment.value }}</span>
    <img
      v-else
      class="inline-emoji"
      :src="segment.src"
      :alt="segment.token"
      :title="segment.token"
      loading="lazy"
      decoding="async"
    />
  </template>
</template>
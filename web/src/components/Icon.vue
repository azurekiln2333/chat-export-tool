<script setup>
/**
 * Inline SVG icon set. Every icon is a stroke path drawn with `currentColor`,
 * so no external icon font or CDN is required. Each entry is a list of SVG
 * path `d` strings.
 */
const ICONS = {
  search: ['M10 4a6 6 0 1 0 0 12a6 6 0 1 0 0-12', 'M14.4 14.4L19 19'],
  close: ['M6 6l12 12', 'M18 6L6 18'],
  menu: ['M4 7h16', 'M4 12h16', 'M4 17h16'],
  'chevron-left': ['M14 6l-6 6 6 6'],
  'chevron-right': ['M10 6l6 6-6 6'],
  'chevrons-left': ['M11 6l-6 6 6 6', 'M18 6l-6 6 6 6'],
  'chevrons-right': ['M13 6l6 6-6 6', 'M6 6l6 6-6 6'],
  edit: ['M12 20h9', 'M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z'],
  hide: [
    'M3 3l18 18',
    'M10.6 10.6a2 2 0 0 0 2.8 2.8',
    'M9.9 5.1A10 10 0 0 1 12 5c6 0 10 7 10 7a17 17 0 0 1-3.3 4.2',
    'M6.1 6.1A17 17 0 0 0 2 12s4 7 10 7a10 10 0 0 0 4-.9',
  ],
  show: ['M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7Z', 'M12 9a3 3 0 1 0 0 6a3 3 0 0 0 0-6Z'],
  restore: ['M3 12a9 9 0 1 0 3-6.7', 'M3 4v4h4'],
  refresh: ['M20 11a8 8 0 1 0-2.3 5.7', 'M20 4v5h-5'],
  logout: ['M15 12H3', 'M7 8l-4 4 4 4', 'M11 4h6a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-6'],
  image: ['M3 5h18v14H3z', 'M8.5 11a1.5 1.5 0 1 0 0-3a1.5 1.5 0 0 0 0 3Z', 'M21 15l-5-5L5 21'],
  mic: ['M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Z', 'M5 11a7 7 0 0 0 14 0', 'M12 18v3'],
  video: ['M3 6h12v12H3z', 'M15 10l6-3v10l-6-3'],
  lock: ['M6 10V7a4 4 0 0 1 8 0v3', 'M5 10h14v10H5z'],
  user: ['M12 12a4 4 0 1 0 0-8a4 4 0 0 0 0 8Z', 'M4 21a8 8 0 0 1 16 0'],
  note: ['M4 4h16v16H4z', 'M8 8h8', 'M8 12h8', 'M8 16h5'],
  warning: ['M12 3l9 16H3l9-16Z', 'M12 10v4', 'M12 17h.01'],
  check: ['M5 13l4 4 10-10'],
  info: ['M12 4a8 8 0 1 0 0 16a8 8 0 0 0 0-16Z', 'M12 11v5', 'M12 8h.01'],
  chat: ['M4 5h16v12H7l-3 3V5Z'],
  list: ['M8 6h12', 'M8 12h12', 'M8 18h12', 'M4 6h.01', 'M4 12h.01', 'M4 18h.01'],
  'file': ['M6 3h8l4 4v14H6z', 'M14 3v4h4'],
  download: ['M12 4v11', 'M7 11l5 5 5-5', 'M4 20h16'],
};

const props = defineProps({
  name: { type: String, required: true },
  size: { type: [Number, String], default: 18 },
  title: { type: String, default: '' },
});

const paths = ICONS[props.name] || ICONS.info;
</script>

<template>
  <svg
    class="icon"
    :style="size ? { width: size + 'px', height: size + 'px' } : null"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
    :role="title ? 'img' : undefined"
  >
    <title v-if="title">{{ title }}</title>
    <path v-for="(d, index) in paths" :key="index" :d="d" />
  </svg>
</template>
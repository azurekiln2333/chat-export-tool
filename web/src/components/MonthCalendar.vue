<script setup>
/**
 * Month calendar of recorded days for the current scope.
 *
 * Day counts and the month grid are computed with UTC arithmetic only, so the
 * viewer's computer timezone never shifts a day. The server buckets each
 * message by the export timezone and returns only days that have records.
 */
import { computed } from 'vue';

import Icon from './Icon.vue';
import { buildMonthGrid, countsByDate, formatMonth, monthOf, totalOfDays, WEEKDAYS } from '../calendar.js';
import {
  changeCalendarMonth,
  clearMessageDate,
  goToFirstRecordedMonth,
  state,
  toggleCalendarDate,
} from '../store.js';

const monthLabel = computed(() => formatMonth(state.calendarMonth || state.calendar.month));

const counts = computed(() => countsByDate(state.calendar.days));

/** The month grid as ISO date cells with their counts (blank cells are null). */
const cells = computed(() => buildMonthGrid(state.calendarMonth || state.calendar.month, counts.value));

const recordedTotal = computed(() => totalOfDays(state.calendar.days));

const canGoToFirst = computed(() => {
  const first = monthOf(state.calendar.first_date);
  return Boolean(first) && first !== state.calendarMonth;
});

const firstRecordedLabel = computed(() => {
  const first = monthOf(state.calendar.first_date);
  return first ? `首个记录月（${first}）` : '首个记录月';
});

function select(date, count) {
  if (!count) return;
  toggleCalendarDate(date);
}
</script>

<template>
  <section class="calendar" aria-label="按日期浏览">
    <div class="calendar-head">
      <button class="btn btn-sm btn-icon btn-ghost" type="button" aria-label="上一个月" @click="changeCalendarMonth(-1)">
        <Icon name="chevron-left" :size="16" />
      </button>
      <strong class="calendar-month">{{ monthLabel }}</strong>
      <button class="btn btn-sm btn-icon btn-ghost" type="button" aria-label="下一个月" @click="changeCalendarMonth(1)">
        <Icon name="chevron-right" :size="16" />
      </button>
      <span class="calendar-spacer" />
      <button
        class="btn btn-sm btn-ghost"
        type="button"
        :disabled="!canGoToFirst"
        @click="goToFirstRecordedMonth"
      >
        <Icon name="restore" :size="14" />
        {{ firstRecordedLabel }}
      </button>
    </div>

    <div v-if="state.calendarBusy" class="loading-bar" />

    <div class="calendar-grid" role="grid">
      <span v-for="weekday in WEEKDAYS" :key="weekday" class="calendar-weekday">{{ weekday }}</span>
      <template v-for="(cell, index) in cells" :key="index">
        <span v-if="!cell" class="calendar-day is-empty"></span>
        <button
          v-else
          class="calendar-day"
          :class="{
            'has-records': cell.count > 0,
            'is-selected': cell.date === state.messageDate,
          }"
          type="button"
          :disabled="cell.count === 0"
          :aria-pressed="cell.date === state.messageDate ? 'true' : 'false'"
          :title="cell.count > 0 ? `${cell.date}：${cell.count} 条` : `${cell.date}：无记录`"
          @click="select(cell.date, cell.count)"
        >
          <span class="calendar-day-number">{{ cell.day }}</span>
          <span v-if="cell.count > 0" class="calendar-day-count">{{ cell.count }}</span>
        </button>
      </template>
    </div>

    <div class="calendar-foot">
      <span class="editor-hint">
        本月命中 {{ recordedTotal.toLocaleString('zh-CN') }} 条
        <template v-if="state.calendar.timezone">· 时区 {{ state.calendar.timezone }}</template>
      </span>
      <button
        v-if="state.messageDate"
        class="btn btn-sm"
        type="button"
        @click="clearMessageDate"
      >
        <Icon name="close" :size="14" />
        清除日期
      </button>
    </div>
  </section>
</template>
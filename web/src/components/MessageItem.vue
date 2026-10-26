<script setup>
/**
 * One message: bubble decoration, media, badges and the edit/hide/restore
 * controls. Message text is rendered through `BubbleBody`, so raw chat is never
 * inserted as HTML. Edits are revision-guarded; a stale revision surfaces the
 * latest content instead of overwriting it.
 */
import { computed, ref, watch } from 'vue';

import BubbleBody from './BubbleBody.vue';
import Icon from './Icon.vue';
import MediaBlock from './MediaBlock.vue';
import RawDetail from './RawDetail.vue';
import { describeMessage } from '../message-model.js';
import { clearConflict, restoreMessage, saveMessage, state } from '../store.js';
import { kindFromPath } from '../utils.js';

const props = defineProps({
  message: { type: Object, required: true },
  accountUid: { type: String, default: '' },
  decorations: { type: Object, required: true },
});

const editorOpen = ref(false);
const draftText = ref('');
const draftNote = ref('');
const draftHidden = ref(false);
const saving = ref(false);
const editorError = ref('');

const model = computed(() =>
  describeMessage(props.message, {
    accountUid: props.accountUid,
    decorations: props.decorations,
  }),
);

const meta = computed(() => model.value.meta);
const edit = computed(() => model.value.edit);
const bubble = computed(() => model.value.bubble);
const resource = computed(() => model.value.resource);
const own = computed(() => model.value.own);
const applied = computed(() => model.value.applied);
const isSticker = computed(() => model.value.sticker);
const stickerSrc = computed(() => model.value.stickerSrc);
const mediaSource = computed(() => resource.value.path || '');
const mediaKindValue = computed(() => model.value.media.kind);
const mediaMissing = computed(() => model.value.media.missing);
const mediaMissingLabel = computed(() => model.value.media.label);
const segments = computed(() => model.value.segments);
const corners = computed(() => model.value.corners);
const localAttachments = computed(() => model.value.localAttachments);
const bubbleClasses = computed(() => model.value.classes);

const hidden = computed(() => model.value.flags.hidden);
const edited = computed(() => model.value.flags.edited);
const hasNote = computed(() => model.value.flags.note);
const isModified = computed(() => model.value.flags.modified);
const conflict = computed(() => state.conflicts[props.message.msg_uuid]);

function basename(path) {
  return String(path || '').split('/').pop() || '附件';
}

function syncDraft() {
  draftText.value = props.message.text || '';
  draftNote.value = edit.value.note || '';
  draftHidden.value = Boolean(edit.value.hidden);
  editorError.value = '';
}

function openEditor() {
  syncDraft();
  editorOpen.value = true;
}

function closeEditor() {
  editorOpen.value = false;
  editorError.value = '';
}

async function submitEdit() {
  saving.value = true;
  editorError.value = '';
  const override = draftText.value === props.message.text_original ? null : draftText.value;
  try {
    const result = await saveMessage(props.message.msg_uuid, {
      textOverride: override,
      note: draftNote.value,
      hidden: draftHidden.value,
    });
    if (result) closeEditor();
  } catch {
    editorError.value = '保存失败，请稍后重试。';
  } finally {
    saving.value = false;
  }
}

async function toggleHidden() {
  saving.value = true;
  try {
    await saveMessage(props.message.msg_uuid, {
      textOverride: edit.value.text_override === undefined ? null : edit.value.text_override,
      note: edit.value.note || '',
      hidden: !hidden.value,
    });
  } catch {
    /* Store already reported the failure. */
  } finally {
    saving.value = false;
  }
}

async function restore() {
  saving.value = true;
  try {
    await restoreMessage(props.message.msg_uuid);
  } catch {
    /* Store already reported the failure. */
  } finally {
    saving.value = false;
  }
}

function reloadLatest() {
  syncDraft();
  closeEditor();
  clearConflict(props.message.msg_uuid);
}

watch(
  () => props.message.text,
  (value) => {
    if (!editorOpen.value) draftText.value = value;
  },
);
</script>

<template>
  <article class="message" :class="{ own, 'is-hidden': hidden }">
    <header class="message-header">
      <strong>{{ message.sender_name }}</strong>
      <span class="message-time">{{ message.time }}</span>
      <span class="badge">{{ message.type_name }}</span>
      <span v-if="applied" class="badge">原始气泡</span>
      <span v-if="edited" class="badge is-edited">已编辑</span>
      <span v-if="hasNote" class="badge is-note">备注</span>
      <span v-if="hidden" class="badge is-hidden">已隐藏</span>
    </header>

    <div :class="bubbleClasses">
      <div class="bubble-content">
        <template v-if="isSticker">
          <a class="sticker-link" :href="stickerSrc" target="_blank" rel="noopener">
            <img
              class="sticker"
              :src="stickerSrc"
              :alt="message.text || '表情'"
              :title="message.text || '点击查看原图'"
              loading="lazy"
              decoding="async"
            />
          </a>
        </template>
        <BubbleBody v-else :segments="segments" />

        <MediaBlock
          v-if="!isSticker && !mediaMissing && mediaSource && resource.status === 'ok'"
          :source="mediaSource"
          :kind="mediaKindValue"
          :alt="message.text"
          :file-name="basename(mediaSource)"
        />
        <p v-else-if="mediaMissing" class="media-unavailable">
          {{ mediaMissingLabel }}
        </p>

        <MediaBlock
          v-for="path in localAttachments"
          :key="path"
          :source="path"
          :kind="kindFromPath(path)"
          :file-name="basename(path)"
        />
      </div>

      <img
        v-for="corner in corners"
        :key="corner.corner"
        class="bubble-corner"
        :class="corner.className"
        :src="corner.src"
        alt=""
        aria-hidden="true"
        loading="lazy"
        :style="{ width: corner.width, height: corner.height }"
      />
    </div>

    <p v-if="hasNote && !editorOpen" class="note-preview">{{ edit.note }}</p>

    <div class="message-actions">
      <button class="btn btn-sm btn-ghost" type="button" @click="editorOpen ? closeEditor() : openEditor()">
        <Icon :name="editorOpen ? 'close' : 'edit'" :size="14" />
        {{ editorOpen ? '取消编辑' : '编辑' }}
      </button>
      <button class="btn btn-sm btn-ghost" type="button" :disabled="saving" @click="toggleHidden">
        <Icon :name="hidden ? 'show' : 'hide'" :size="14" />
        {{ hidden ? '取消隐藏' : '隐藏' }}
      </button>
      <button
        v-if="isModified"
        class="btn btn-sm btn-ghost"
        type="button"
        :disabled="saving"
        @click="restore"
      >
        <Icon name="restore" :size="14" />
        恢复原文
      </button>

      <RawDetail class="message-extra" :message="message" />
    </div>

    <form v-if="editorOpen" class="editor" @submit.prevent="submitEdit">
      <p v-if="conflict !== undefined" class="conflict">
        该消息已被其他改动更新（版本 {{ conflict }}）。请先载入最新内容，再重新保存。
        <button class="btn btn-sm" type="button" style="margin-left: 6px" @click="reloadLatest">
          载入最新内容
        </button>
      </p>

      <div class="field">
        <label :for="'edit-text-' + message.msg_uuid">正文（留空保存后按原文显示）</label>
        <textarea
          :id="'edit-text-' + message.msg_uuid"
          v-model="draftText"
          class="textarea"
          spellcheck="false"
        ></textarea>
      </div>

      <div class="field">
        <label :for="'edit-note-' + message.msg_uuid">备注</label>
        <input
          :id="'edit-note-' + message.msg_uuid"
          v-model="draftNote"
          class="input"
          type="text"
          placeholder="给自己看的备注，可被搜索"
        />
      </div>

      <label class="toggle">
        <input v-model="draftHidden" type="checkbox" />
        隐藏此消息
      </label>

      <p v-if="editorError" class="alert" role="alert">{{ editorError }}</p>

      <div class="editor-actions">
        <button class="btn btn-primary" type="submit" :disabled="saving">
          <Icon name="check" :size="15" />
          {{ saving ? '正在保存…' : '保存修改' }}
        </button>
        <button class="btn" type="button" :disabled="saving" @click="closeEditor">取消</button>
        <span class="editor-hint">修改不会删除或改动原始数据，可随时恢复。</span>
      </div>
    </form>
  </article>
</template>
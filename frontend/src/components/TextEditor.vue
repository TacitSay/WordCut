<script setup>
/**
 * 文本编辑区（I1 键入删除 + I2 分段显示）。
 *
 * 渲染模型：
 *  - 按 /api/projects/{pid}/tokens 返回的 segments（SegmentOut[]）分段，
 *    每个 Segment 一个 <p class="segment">，段内 Token 连续拼接；
 *  - 未被任何段落覆盖的 Token（例如 segments 为空、或后端过滤掉空文本 Token）
 *    统一追加到末尾的兜底段落，保证一个 Token 都不会丢。
 *
 * 光标模型（由 App.vue 持有，本组件只负责渲染与点击）：
 *  - cursorIndex ∈ [0, tokens.length]：光标位于「第 cursorIndex 个 Token 之前」，
 *    等于 tokens.length 时表示文末；
 *  - selectionAnchor：Shift+方向键 / Shift+点击扩展选区时记录锚点，
 *    选区为 [min(anchor, cursor), max(anchor, cursor)) 的 Token 区间。
 *
 * 交互：
 *  - 点击 Token = 设置光标（左半边落在词前，右半边落在词后），不删除；
 *  - 点击已删除 Token（删除线红字）= 恢复它；
 *  - 删除/恢复的按键由 App.vue 的全局键盘处理（Delete / Backspace / Shift+方向键），
 *    本组件只把点击意图通过事件抛给父组件。
 */
import { computed, nextTick, ref, watch } from 'vue'

/** 低置信度阈值（虚线下划线提示）。 */
const LOW_CONFIDENCE = 0.6

const props = defineProps({
  /** TokenOut[]：{ id, text, start, end, is_deleted, confidence } */
  tokens: { type: Array, default: () => [] },
  /** SegmentOut[]：{ index, start, end, duration, text, token_ids } */
  segments: { type: Array, default: () => [] },
  /** 当前被删的 Token id 列表 */
  deletedIds: { type: Array, default: () => [] },
  /** 正在请求删除 / 恢复的 Token id（避免连点） */
  pendingIds: { type: Array, default: () => [] },
  /** 光标位置：0..tokens.length */
  cursorIndex: { type: Number, default: 0 },
  /** 选区锚点（null 表示没有选区） */
  selectionAnchor: { type: Number, default: null },
  /** 是否处于忙碌状态（转写 / 导出时只读） */
  disabled: { type: Boolean, default: false },
  /** 正在播放的 Token 下标（-1 表示没有）；用于播放头跟随高亮 */
  activeIndex: { type: Number, default: -1 },
})

const emit = defineEmits(['cursor-change', 'restore', 'seek'])

const rootRef = ref(null)

const deletedSet = computed(() => new Set(props.deletedIds))
const pendingSet = computed(() => new Set(props.pendingIds))
const indexById = computed(() => {
  const map = new Map()
  props.tokens.forEach((token, index) => {
    if (token && token.id) map.set(token.id, index)
  })
  return map
})

/** 把 TokenOut 转成渲染项（索引即 Token 在 tokens 数组中的位置）。 */
function toItem(token, index) {
  const text = typeof token?.text === 'string' ? token.text : ''
  const confidence = Number(token?.confidence ?? 1)
  const start = Number(token?.start ?? 0)
  const end = Number(token?.end ?? 0)
  return {
    id: token?.id ?? `idx-${index}`,
    index,
    gap: /^\s/.test(text),
    glyph: text.trim(),
    confidence: Number.isFinite(confidence) ? confidence : 1,
    start: Number.isFinite(start) ? start : 0,
    end: Number.isFinite(end) ? end : 0,
    deleted: deletedSet.value.has(token?.id),
    pending: pendingSet.value.has(token?.id),
  }
}

/** 段落列表：[{ key, index, items, isLast }]。 */
const paragraphs = computed(() => {
  if (!props.tokens.length) return []

  const covered = new Set()
  const groups = []
  const segmentList = Array.isArray(props.segments) ? props.segments : []

  for (const segment of segmentList) {
    const ids = Array.isArray(segment?.token_ids) ? segment.token_ids : []
    const items = []
    for (const id of ids) {
      const index = indexById.value.get(id)
      if (index === undefined || covered.has(index)) continue
      covered.add(index)
      items.push(toItem(props.tokens[index], index))
    }
    if (items.length) {
      groups.push({ key: `seg-${segment?.index ?? groups.length}`, index: segment?.index ?? groups.length, items })
    }
  }

  // 兜底：segments 缺失或漏掉的 Token 追加成一段，避免文本丢失。
  const rest = []
  props.tokens.forEach((token, index) => {
    if (!covered.has(index)) rest.push(toItem(token, index))
  })
  if (rest.length) groups.push({ key: 'seg-rest', index: -1, items: rest })

  return groups.map((group, order) => ({ ...group, isLast: order === groups.length - 1 }))
})

/** 选区区间（half-open：[start, end)）。 */
const selection = computed(() => {
  const anchor = props.selectionAnchor
  const cursor = props.cursorIndex
  if (anchor === null || anchor === undefined || anchor === cursor) {
    return { start: cursor, end: cursor, empty: true }
  }
  return { start: Math.min(anchor, cursor), end: Math.max(anchor, cursor), empty: false }
})

const selectedCount = computed(() =>
  selection.value.empty ? 0 : Math.max(0, selection.value.end - selection.value.start),
)

function isSelected(index) {
  const range = selection.value
  return !range.empty && index >= range.start && index < range.end
}

function isSelectionEdge(index) {
  const range = selection.value
  if (range.empty) return false
  return index === range.start || index === range.end - 1
}

function tokenClasses(item) {
  return {
    'is-deleted': item.deleted,
    'is-low': item.confidence < LOW_CONFIDENCE,
    'is-pending': item.pending,
    'is-selected': isSelected(item.index),
    'is-edge': isSelectionEdge(item.index),
    'is-active': item.index === props.activeIndex,
  }
}

/** 光标：渲染在第 cursorIndex 个 Token 之前。 */
function showCaretBefore(index) {
  return props.cursorIndex === index
}

/** 光标：文末位置（cursorIndex === tokens.length）渲染在最后一段尾部。 */
function showCaretAfter(para) {
  return para.isLast && props.cursorIndex >= props.tokens.length && props.tokens.length > 0
}

function emitCursor(index, event) {
  emit('cursor-change', { index, extend: Boolean(event?.shiftKey) })
}

/** 点击 Token：按左右半边决定光标落点；已删 Token 顺带恢复；未删 Token 顺带定位播放头。 */
function onTokenClick(item, event) {
  if (props.disabled || item.pending) return
  const el = event?.currentTarget
  const rect = typeof el?.getBoundingClientRect === 'function' ? el.getBoundingClientRect() : null
  const rightHalf = rect ? event.clientX - rect.left > rect.width / 2 : true
  emitCursor(item.index + (rightHalf ? 1 : 0), event)
  if (item.deleted) emit('restore', [item.id])
  else emit('seek', item.start)
}

/** 点击段落空白处：光标落到该段末尾。 */
function onParagraphClick(para, event) {
  if (props.disabled) return
  if (event.target !== event.currentTarget) return
  const last = para.items[para.items.length - 1]
  emitCursor((last?.index ?? 0) + 1, event)
}

/** 光标移动后把光标线滚进可视区（吸顶波形不参与滚动）。 */
watch(
  () => props.cursorIndex,
  async () => {
    await nextTick()
    const caret = rootRef.value?.querySelector?.('.caret')
    caret?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
  },
)

/**
 * 播放头跟随：当前播放词变化时高亮并滚入可视区。
 * 只在播放中高亮，暂停后保留最后一个高亮位置直到下次播放。
 */
watch(
  () => props.activeIndex,
  async (index) => {
    if (index < 0) return
    await nextTick()
    const el = rootRef.value?.querySelector?.(`[data-token-index="${index}"]`)
    el?.scrollIntoView?.({ block: 'center', inline: 'nearest', behavior: 'smooth' })
  },
)
</script>

<template>
  <section ref="rootRef" class="text-editor">
    <header class="pane-header">
      <h2 class="pane-title">文本</h2>
      <span class="pane-hint">
        点击 = 定位光标 · Delete/Backspace 删除 · Shift+方向键 选区 · 点击红字恢复
      </span>
      <span class="pane-count">
        {{ tokens.length }} Token · {{ segments.length }} 段 · 已删 {{ deletedIds.length }}
        <template v-if="selectedCount"> · 选中 {{ selectedCount }}</template>
      </span>
    </header>

    <div class="editor-body" :class="{ 'is-disabled': disabled }">
      <p v-if="!tokens.length" class="pane-empty">
        暂无词级单元。请先在工具栏选择音频 → 上传 → 点击「转写」。
      </p>

      <template v-else>
        <p
          v-for="para in paragraphs"
          :key="para.key"
          class="segment"
          @click="onParagraphClick(para, $event)"
        >
          <template v-for="item in para.items" :key="item.id">
            <span v-if="showCaretBefore(item.index)" class="caret" aria-hidden="true" />
            <span v-if="item.gap" class="token-gap">{{ ' ' }}</span>
            <span
              v-if="item.glyph"
              class="token"
              :class="tokenClasses(item)"
              role="button"
              tabindex="-1"
              :data-token-index="item.index"
              :title="`${item.id} · ${item.start.toFixed(2)}s–${item.end.toFixed(2)}s · 置信度 ${item.confidence.toFixed(2)}`"
              @click.stop="onTokenClick(item, $event)"
              >{{ item.glyph }}</span
            >
          </template>
          <span v-if="showCaretAfter(para)" class="caret" aria-hidden="true" />
        </p>
      </template>
    </div>
  </section>
</template>

<style scoped>
.text-editor {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  flex: 1 1 auto;
}

.editor-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 22px 48px;
}

.editor-body.is-disabled {
  opacity: 0.6;
}

/* 分段显示（I2）：段间距 12px，行高 1.9。 */
.segment {
  margin: 0 0 12px;
  font-size: 16px;
  line-height: 1.9;
  letter-spacing: 0;
  word-break: break-word;
  color: var(--text);
  user-select: text;
  cursor: text;
  border-radius: 3px;
}

.segment:last-child {
  margin-bottom: 0;
}

.token {
  display: inline;
  cursor: pointer;
  border-radius: 2px;
  padding: 1px 0;
  transition:
    background-color 0.12s ease,
    color 0.12s ease;
}

.token:hover {
  background: var(--accent-dim);
  color: var(--text-strong);
}

.token.is-low {
  border-bottom: 1px dotted var(--warn);
}

/* 已删除：删除线 + 红字，点击可恢复。 */
.token.is-deleted {
  color: var(--danger);
  text-decoration: line-through;
  text-decoration-thickness: 1.5px;
  border-bottom-color: transparent;
}

.token.is-deleted:hover {
  background: var(--danger-dim);
}

.token.is-pending {
  opacity: 0.5;
  cursor: progress;
}

/* 选区高亮。 */
.token.is-selected {
  background: rgba(59, 130, 246, 0.3);
  color: var(--text-strong);
}

.token.is-selected.is-edge {
  background: rgba(59, 130, 246, 0.42);
}

/* 播放头跟随：当前正在播放的词（琥珀色，与波形播放头同色系） */
.token.is-active {
  background: rgba(245, 158, 11, 0.45);
  color: var(--text-strong);
  box-shadow: 0 0 0 1px rgba(245, 158, 11, 0.65);
}

.token.is-active.is-deleted {
  background: rgba(245, 158, 11, 0.25);
}

/* 光标：2px 蓝色竖线。 */
.caret {
  display: inline-block;
  width: 2px;
  height: 1.15em;
  margin: 0 -1px;
  vertical-align: text-bottom;
  background: var(--accent);
  animation: caret-blink 1.05s steps(2, start) infinite;
}

@keyframes caret-blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.12;
  }
}

.token-gap {
  white-space: pre;
}
</style>

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
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

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

/**
 * 由屏幕坐标解析出「第几个 Token + 光标落在词前还是词后」。
 *
 * 为什么不用事件目标：用户常常从词与词之间的空白、段首段尾、甚至两个字符中间
 * 开始拖拽。只看 `event.target` 会解析失败（空白处不是 Token），导致"必须先点
 * 一下定位光标，再拖"——这正是之前难用的根因。
 *
 * 优先用 `caretRangeFromPoint` / `caretPositionFromPoint`（浏览器原生的"这个坐标
 * 对应文本的哪个位置"），它能精确到字符偏移，也能跨段落命中；不支持时退回
 * 元素命中 + 左右半边估算。
 *
 * @returns {{index: number, position: number}|null}
 */
function resolvePoint(x, y) {
  let textNode = null
  let offset = 0

  if (typeof document.caretRangeFromPoint === 'function') {
    const range = document.caretRangeFromPoint(x, y)
    if (range) {
      textNode = range.startContainer
      offset = range.startOffset
    }
  } else if (typeof document.caretPositionFromPoint === 'function') {
    const pos = document.caretPositionFromPoint(x, y)
    if (pos) {
      textNode = pos.offsetNode
      offset = pos.offset
    }
  }

  if (textNode) {
    const holder = textNode.nodeType === 3 ? textNode.parentElement : textNode
    const tokenEl = holder?.closest?.('[data-token-index]')
    if (tokenEl) {
      const index = Number(tokenEl.dataset.tokenIndex)
      if (Number.isFinite(index)) {
        const text = textNode.nodeType === 3 ? String(textNode.textContent ?? '') : ''
        const rightHalf = text.length ? offset * 2 >= text.length : false
        return { index, position: index + (rightHalf ? 1 : 0) }
      }
    }
  }

  // 退化路径：坐标处正好是 Token 元素
  const target = document.elementFromPoint?.(x, y)
  const tokenEl = target?.closest?.('[data-token-index]')
  if (tokenEl) {
    const index = Number(tokenEl.dataset.tokenIndex)
    if (Number.isFinite(index)) {
      const rect = tokenEl.getBoundingClientRect()
      const rightHalf = x - rect.left > rect.width / 2
      return { index, position: index + (rightHalf ? 1 : 0) }
    }
  }
  return null
}

/**
 * 拖拽选区状态。
 *
 * 交互设计（重做后）：
 *  - **任意位置按下即可拖选**：按下时用 :func:`resolvePoint` 解析锚点，
 *    不再要求先点一个 Token 定位光标；
 *  - 指针移动超过 ``DRAG_THRESHOLD_PX`` 才算拖拽，否则仍按点击处理
 *    （手抖 2–3px 不能把"点击红字恢复"吞掉）；
 *  - 拖动中持续扩展选区（Shift+方向键同款语义），跨段落也有效；
 *  - 松开若没超过阈值 → 点击语义：已删词恢复、未删词定位播放头；
 *  - 拖动过程 `preventDefault`，避免触发浏览器原生文字选择。
 */
const DRAG_THRESHOLD_PX = 4

const drag = ref({ active: false, moved: false, position: 0, index: -1 })

/** 刚拖拽结束时置位：用来吞掉紧随其后的段落 click（否则会清空选区）。 */
let suppressParagraphClick = false

/** 拖拽期间阻止原生 selectstart。 */
function onSelectStart(event) {
  if (drag.value.active) event.preventDefault()
}

/** 按下：解析锚点、落光标、进入"待定"状态（还没算拖拽）。 */
function beginDragAt(event) {
  if (props.disabled || event.button !== 0) return
  const hit = resolvePoint(event.clientX, event.clientY)
  if (!hit) return
  const token = props.tokens[hit.index]
  if (!token || pendingSet.value.has(token.id)) return

  event.preventDefault()
  drag.value = {
    active: true,
    moved: false,
    position: hit.position,
    index: hit.index,
    startX: event.clientX,
    startY: event.clientY,
  }
  emitCursor(hit.position, { shiftKey: event.shiftKey })
}

/** 拖动中：超过阈值后才生效，坐标变化就把光标移过去（保持锚点 → 形成选区）。 */
function onDocumentMouseMove(event) {
  const state = drag.value
  if (!state.active) return
  const dx = event.clientX - (state.startX ?? event.clientX)
  const dy = event.clientY - (state.startY ?? event.clientY)
  if (!state.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return

  const hit = resolvePoint(event.clientX, event.clientY)
  if (!hit) {
    // 拖到文本区外（波形/面板）：保持现有选区，等继续移动或松开
    if (!state.moved) drag.value = { ...state, moved: true }
    return
  }
  if (hit.position === state.position && state.moved) return
  drag.value = { ...state, position: hit.position, moved: true }
  emitCursor(hit.position, { shiftKey: true })
}

/** 松开：没拖动就是点击语义；拖过就保留选区，不触发恢复/定位。 */
function finishDrag(clicked) {
  const state = drag.value
  if (!state.active) return
  drag.value = { ...state, active: false }

  // 无论拖动还是点击，都吞掉紧随其后的段落 click：
  // 否则"点在段落空白处"会把刚解析好的光标/选区又改成段尾。
  suppressParagraphClick = true
  setTimeout(() => {
    suppressParagraphClick = false
  }, 0)

  if (state.moved || !clicked) return
  const token = props.tokens[state.index]
  if (!token) return
  if (token.deleted) emit('restore', [token.id])
  else emit('seek', Number(token.start ?? 0))
}

function onDocumentMouseUp() {
  finishDrag(true)
}

/** 窗口失焦（拖到别的程序后松手）：按拖拽结束处理，避免状态卡住。 */
function onWindowBlur() {
  if (!drag.value.active) return
  finishDrag(false)
}

/** 点击段落空白处：光标落到该段末尾（拖拽刚结束时忽略，保住选区）。 */
function onParagraphClick(para, event) {
  if (props.disabled) return
  if (suppressParagraphClick) return
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
/** 是否让文本跟随播放头自动滚动（播放中用户手动滚动会自动关掉它）。 */
const followPlayback = ref(true)
/** 用户滚动过、跟随被自动暂停时，用来提示"点此恢复跟随"。 */
const followPausedByUser = ref(false)

/**
 * 播放头跟随：只在当前词**滚出可视区**时才滚，且用 `nearest` 而不是 `center`。
 *
 * 之前的实现每个词都 `center` 一次：词大约每 0.2–0.4 秒变一次，等于每几百毫秒
 * 把视口强行居中一次，用户根本没法往下翻。现在的规则：
 *  - 当前词还在可视区内 → 完全不动；
 *  - 滚出可视区 → 最小幅度滚到边缘（`nearest`）；
 *  - 用户一旦主动滚动（wheel / touchmove）→ 暂停跟随，直到点「恢复跟随」。
 */
function scrollToActive(index) {
  const el = rootRef.value?.querySelector?.(`[data-token-index="${index}"]`)
  const container = el?.closest?.('.editor-body')
  if (!el || !container) return

  const rect = el.getBoundingClientRect()
  const view = container.getBoundingClientRect()
  const margin = 48
  const visible = rect.top >= view.top + margin && rect.bottom <= view.bottom - margin
  if (visible) return

  el.scrollIntoView?.({ block: 'nearest', inline: 'nearest', behavior: 'auto' })
}

watch(
  () => props.activeIndex,
  async (index) => {
    if (index < 0 || !followPlayback.value) return
    await nextTick()
    scrollToActive(index)
  },
)

/** 用户主动滚动 → 暂停跟随（这是"播放时高亮把视口拽回去"的根治办法）。 */
function onUserScrollIntent() {
  if (!followPlayback.value) return
  if (props.activeIndex < 0) return
  followPlayback.value = false
  followPausedByUser.value = true
}

/** 恢复跟随并立刻对齐到当前词。 */
function resumeFollow() {
  followPlayback.value = true
  followPausedByUser.value = false
  const index = props.activeIndex
  if (index >= 0) {
    nextTick(() => {
      const el = rootRef.value?.querySelector?.(`[data-token-index="${index}"]`)
      el?.scrollIntoView?.({ block: 'center', inline: 'nearest', behavior: 'smooth' })
    })
  }
}

/** 切换跟随（面板按钮）。 */
function toggleFollow() {
  if (followPlayback.value) {
    followPlayback.value = false
    followPausedByUser.value = false
  } else {
    resumeFollow()
  }
}

// 拖拽选区：mousemove / mouseup 挂在 document 上，鼠标移出编辑器也不会丢事件。
onMounted(() => {
  document.addEventListener('mousemove', onDocumentMouseMove)
  document.addEventListener('mouseup', onDocumentMouseUp)
  document.addEventListener('selectstart', onSelectStart)
  window.addEventListener('blur', onWindowBlur)
  const body = rootRef.value?.querySelector?.('.editor-body')
  body?.addEventListener('wheel', onUserScrollIntent, { passive: true })
  body?.addEventListener('touchmove', onUserScrollIntent, { passive: true })
})

onBeforeUnmount(() => {
  document.removeEventListener('mousemove', onDocumentMouseMove)
  document.removeEventListener('mouseup', onDocumentMouseUp)
  document.removeEventListener('selectstart', onSelectStart)
  window.removeEventListener('blur', onWindowBlur)
  const body = rootRef.value?.querySelector?.('.editor-body')
  body?.removeEventListener('wheel', onUserScrollIntent)
  body?.removeEventListener('touchmove', onUserScrollIntent)
})
</script>

<template>
  <section ref="rootRef" class="text-editor">
    <header class="pane-header">
      <h2 class="pane-title">文本</h2>
      <span class="pane-hint">
        点击 = 定位光标 · 拖拽 = 选一大片 · Delete/Backspace 删除 · 点击红字恢复
      </span>
      <span class="pane-count">
        {{ tokens.length }} Token · {{ segments.length }} 段 · 已删 {{ deletedIds.length }}
        <template v-if="selectedCount"> · 选中 {{ selectedCount }}</template>
      </span>
      <button
        class="follow-toggle"
        type="button"
        :class="{ 'is-off': !followPlayback, 'is-paused': followPausedByUser }"
        :title="
          followPlayback
            ? '播放时文本会跟随高亮自动滚动（点击关闭）'
            : '已暂停跟随（点击恢复）'
        "
        @click="toggleFollow"
      >
        {{ followPausedByUser ? '跟随已暂停 · 恢复' : followPlayback ? '跟随播放 ✓' : '跟随已关闭' }}
      </button>
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
          @mousedown="beginDragAt($event)"
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
  /* 父级 editor-dock 是 flex 列（顶部可能还有搜索栏），这里靠 flex 撑满剩余空间，
     不能写 height:100%（会叠加上方搜索栏高度而溢出） */
  flex: 1 1 0;
  overflow: hidden;
}

/* 跟随播放的开关：默认开启；用户滚动后变"已暂停"样式 */
.follow-toggle {
  flex: 0 0 auto;
  margin-left: 10px;
  padding: 2px 8px;
  font-size: 11px;
  color: var(--text-dim);
  background: transparent;
  border: 1px solid var(--border-strong);
  border-radius: 3px;
  cursor: pointer;
  white-space: nowrap;
}

.follow-toggle:hover {
  color: var(--text);
  border-color: var(--accent);
}

.follow-toggle.is-paused {
  color: #f0b429;
  border-color: rgba(240, 180, 41, 0.6);
  background: rgba(240, 180, 41, 0.12);
}

.follow-toggle.is-off {
  color: var(--text-faint);
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
  /* 拖拽选区由组件自己接管，禁掉原生文字选择，避免两层高亮打架 */
  user-select: none;
  -webkit-user-select: none;
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

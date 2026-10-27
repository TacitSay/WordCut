<script setup>
/**
 * 文本编辑区：把 Token 列表渲染成可点击的 span。
 *
 * - 连续 Token 无缝拼接（水平方向不加 margin / padding，空格由 token 原文的
 *   前导空白单独渲染，保证点击区域只覆盖词本身）；
 * - 点击 Token 发出 toggle 事件，由 App.vue 调用 /delete 或 /restore；
 * - 被删 Token：删除线 + 红色；
 * - 置信度 < 0.6：加点下划线提示。
 */
import { computed } from 'vue'

/** 低置信度阈值（与界面提示文案保持一致）。 */
const LOW_CONFIDENCE = 0.6

const props = defineProps({
  /** TokenOut[]：{ id, text, start, end, is_deleted, confidence } */
  tokens: { type: Array, default: () => [] },
  /** 当前被删的 Token id 列表 */
  deletedIds: { type: Array, default: () => [] },
  /** 正在请求删除 / 恢复的 Token id（避免连点） */
  pendingIds: { type: Array, default: () => [] },
  /** 是否处于忙碌状态（转写 / 导出时只读） */
  disabled: { type: Boolean, default: false },
})

const emit = defineEmits(['toggle'])

const deletedSet = computed(() => new Set(props.deletedIds))
const pendingSet = computed(() => new Set(props.pendingIds))

const items = computed(() =>
  props.tokens.map((token) => {
    const text = typeof token.text === 'string' ? token.text : ''
    const confidence = Number(token.confidence ?? 1)
    return {
      id: token.id,
      gap: /^\s/.test(text),
      glyph: text.trim(),
      confidence: Number.isFinite(confidence) ? confidence : 1,
      start: Number(token.start ?? 0),
      end: Number(token.end ?? 0),
      deleted: deletedSet.value.has(token.id),
      pending: pendingSet.value.has(token.id),
    }
  }),
)

const deletedCount = computed(
  () => props.tokens.filter((token) => deletedSet.value.has(token.id)).length,
)

function onClick(item) {
  if (props.disabled) return
  emit('toggle', item)
}
</script>

<template>
  <section class="text-editor">
    <header class="pane-header">
      <h2 class="pane-title">文本编辑</h2>
      <span class="pane-hint">
        点击词级单元切换删除状态 · 删除线红字=已删 · 虚线下划线=置信度 &lt; 0.6
      </span>
      <span class="pane-count">{{ tokens.length }} Token · 已删 {{ deletedCount }}</span>
    </header>

    <div class="editor-body" :class="{ 'is-disabled': disabled }">
      <p v-if="!tokens.length" class="pane-empty">
        暂无词级单元。请先在工具栏选择音频 → 上传 → 点击「转写」。
      </p>

      <div v-else class="token-stream">
        <template v-for="item in items" :key="item.id">
          <span v-if="item.gap" class="token-gap">{{ ' ' }}</span>
          <span
            v-if="item.glyph"
            class="token"
            :class="{
              'is-deleted': item.deleted,
              'is-low': item.confidence < LOW_CONFIDENCE,
              'is-pending': item.pending,
            }"
            role="button"
            tabindex="0"
            :title="`${item.id} · ${item.start.toFixed(2)}s–${item.end.toFixed(2)}s · 置信度 ${item.confidence.toFixed(2)}`"
            @click="onClick(item)"
            @keydown.enter.prevent="onClick(item)"
            @keydown.space.prevent="onClick(item)"
            >{{ item.glyph }}</span
          >
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.text-editor {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}

.editor-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 18px 40px;
}

.editor-body.is-disabled {
  opacity: 0.6;
}

.token-stream {
  font-size: 16px;
  line-height: 2;
  letter-spacing: 0;
  word-break: break-word;
  color: var(--text);
  user-select: text;
}

.token {
  display: inline;
  cursor: pointer;
  border-radius: 2px;
  padding: 1px 0;
  transition: background-color 0.12s ease, color 0.12s ease;
}

.token:hover {
  background: var(--accent-dim);
  color: var(--text-strong);
}

.token:focus-visible {
  outline: 1px solid var(--accent);
  outline-offset: 1px;
}

.token.is-low {
  border-bottom: 1px dotted var(--warn);
}

.token.is-deleted {
  color: var(--danger);
  text-decoration: line-through;
  text-decoration-thickness: 1.5px;
  border-bottom-color: transparent;
}

.token.is-deleted:hover {
  background: var(--danger-dim);
  color: var(--danger);
}

.token.is-pending {
  opacity: 0.5;
  cursor: progress;
}

.token-gap {
  white-space: pre;
}
</style>

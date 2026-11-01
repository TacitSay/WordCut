<script setup>
/**
 * 波形视图（L2：吸顶，固定 180px 高）。
 *
 * 约定（见 docs/术语表.md / docs/界面问题清单.md L2）：
 *  - 波形展示的是 source_audio（源音频，只读）；
 *  - DeleteSpan 用红色半透明区域标注，不可拖动 / 不可缩放；
 *  - 点击波形只移动播放头（playhead），不改变任何删除状态；
 *  - 播放控制统一走 composables/usePlayback.js 的共享实例，
 *    因此 App.vue 的「空格 / 菜单命令」与这里的按钮操作的是同一个播放器。
 *
 * 音频来源：后端未暴露源音频下载接口，因此直接使用用户选择的本地文件
 * （URL.createObjectURL 生成的 blob URL），与上传给后端的是同一个文件。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { formatClock, usePlayback } from '../composables/usePlayback.js'

const props = defineProps({
  /** 源音频的 blob URL（由 App.vue 用本地 File 生成） */
  audioUrl: { type: String, default: '' },
  /** DeleteSpanOut[]：{ start, end, token_id, cause } */
  deletes: { type: Array, default: () => [] },
  /**
   * 预计算波形峰值（二维：[[归一化振幅...]]）。有它就不解码音频，
   * 长音频（2 小时）也能瞬间出图；没有则退回 WaveSurfer 自行解码。
   */
  peaks: { type: Array, default: () => [] },
  /** 音频总时长（秒），与 peaks 配套使用 */
  duration: { type: Number, default: 0 },
})

const emit = defineEmits(['ready'])

const container = ref(null)
/** 缩放条（Audition 风格）：整段音频的缩略条 + 可拖动/可拉伸的视野窗 */
const zoomBar = ref(null)
const {
  state,
  attach,
  detach,
  setDeletes,
  toggle,
  play,
  pause,
  seekTo,
  seekBy,
  zoomBy,
  zoomTo,
  zoomToFit,
  getScroll,
  setScroll,
  getScrollWidth,
} = usePlayback()

/**
 * 波形容器的滚动状态（缩放条要按它画视野窗）。
 *
 * 用响应式对象而不是 computed，是因为 scrollLeft 变化不会触发 Vue 更新，
 * 需要在 scroll 事件里手动同步。
 */
const scroll = ref({ left: 0, width: 0, total: 0 })

/** 视野窗在缩放条上的位置与宽度（百分比）。 */
const windowStyle = computed(() => {
  const { left, width, total } = scroll.value
  if (!total || !width) return { left: '0%', width: '100%' }
  const ratio = Math.min(1, width / total)
  return {
    left: `${(left / total) * 100}%`,
    width: `${Math.max(1.5, ratio * 100)}%`,
  }
})

/** 当前缩放倍率（相对"适应宽度"）。 */
const zoomLabel = computed(() => {
  if (!state.zoomed) return '适应'
  const fit = (container.value?.clientWidth || 1) / (state.duration || 1)
  const ratio = fit > 0 ? state.pxPerSec / fit : 1
  return `${ratio.toFixed(1)}×`
})

/**
 * 同步缩放条所需的滚动信息。
 *
 * 注意数据来源：滚动位置用 WaveSurfer 的 `getScroll()`，总宽用
 * `时长 × 每秒像素`。**不能**读外层容器的 scrollLeft/scrollWidth——波形渲染在
 * shadow DOM 里，外层永远 scrollLeft=0、scrollWidth=clientWidth，
 * 那样缩放条就永远显示"全宽"，拖动也不会有反应。
 */
function syncScroll() {
  const element = container.value
  if (!element) return
  const total = getScrollWidth() || element.clientWidth
  scroll.value = {
    left: getScroll(),
    width: element.clientWidth,
    total,
  }
}

/** 把视野窗滚到指定时间区间。 */
function scrollToTime(startSeconds, pxPerSec) {
  setScroll(Math.max(0, startSeconds * pxPerSec))
  // 下一帧再同步一次：WaveSurfer 的滚动是异步落位的
  requestAnimationFrame(syncScroll)
  syncScroll()
}

// ----------------------------------------------------------------------
// 缩放条交互（Audition 式：拖窗移动、拖两端改范围、点击居中）
// ----------------------------------------------------------------------
/** 正在拖拽的类型：'window' | 'left' | 'right' | null */
let dragMode = null
let dragStartX = 0
let dragStartLeft = 0
let dragStartRange = { start: 0, end: 0 }
/** 本次拖拽是否真的移动过（用于抑制拖拽末尾的 click） */
let dragMoved = false

/** 当前视野对应的时间区间（秒）。 */
function visibleRange() {
  const element = container.value
  const total = getScrollWidth()
  if (!element || !state.duration || total <= 0) {
    return { start: 0, end: state.duration || 0 }
  }
  const perPx = element.clientWidth / total // 可见宽度（像素）对应的时长比例
  const start = (getScroll() / total) * state.duration
  return { start, end: Math.min(state.duration, start + perPx * state.duration) }
}

function beginDrag(event, mode) {
  if (!state.ready) return
  const bar = zoomBar.value
  if (!bar) return
  event.preventDefault()
  dragMode = mode
  dragMoved = false
  dragStartX = event.clientX
  dragStartLeft = container.value?.scrollLeft || 0
  dragStartRange = visibleRange()
  bar.setPointerCapture?.(event.pointerId)
}

/** 把像素位移换算成时间位移。 */
function pixelsToSeconds(dx) {
  const bar = zoomBar.value
  const duration = state.duration || 0
  if (!bar || bar.clientWidth <= 0) return 0
  return (dx / bar.clientWidth) * duration
}

function onBarPointerMove(event) {
  if (!dragMode) return
  const containerEl = container.value
  if (!containerEl) return
  const dx = event.clientX - dragStartX
  if (Math.abs(dx) > 1) dragMoved = true
  const duration = state.duration || 0
  if (duration <= 0) return

  if (dragMode === 'window') {
    // 平移：窗口移动多少秒，视野就跟着走多少秒（不改变缩放）
    const shift = pixelsToSeconds(dx)
    const range = dragStartRange.end - dragStartRange.start
    const start = Math.min(
      Math.max(0, dragStartRange.start + shift),
      Math.max(0, duration - range),
    )
    const perSec = range > 0 ? containerEl.clientWidth / range : 1
    scrollToTime(start, perSec)
    return
  }

  // 拉伸边缘：改变可见时长，即改变缩放
  const deltaSeconds = pixelsToSeconds(dx)
  const minRange = 0.5 // 最短显示 0.5 秒，避免无限放大
  if (dragMode === 'right') {
    const end = Math.min(
      duration,
      Math.max(dragStartRange.start + minRange, dragStartRange.end + deltaSeconds),
    )
    applyRange(dragStartRange.start, end)
  } else {
    const start = Math.max(
      0,
      Math.min(dragStartRange.end - minRange, dragStartRange.start + deltaSeconds),
    )
    applyRange(start, dragStartRange.end)
  }
}

/** 让 [start, end] 正好铺满可视宽度。 */
function applyRange(start, end) {
  const containerEl = container.value
  if (!containerEl) return
  const range = Math.max(1e-3, end - start)
  const pxPerSec = containerEl.clientWidth / range
  if (!zoomTo(pxPerSec)) return
  // 用生效后的实际密度：zoomTo 会把倍率夹到 [适应宽度, 400px/s]，
  // 若用请求值换算滚动位置，拖到极限时视野窗会跑偏。
  scrollToTime(start, state.pxPerSec || pxPerSec)
  syncScroll()
}

function endDrag(event) {
  if (!dragMode) return
  dragMode = null
  zoomBar.value?.releasePointerCapture?.(event.pointerId)
}

/** 点在空白处：把该时间点居中。 */
function onBarClick(event) {
  if (dragMoved) {
    dragMoved = false
    return
  }
  if (!state.ready || state.duration <= 0) return
  const bar = zoomBar.value
  if (!bar) return
  const rect = bar.getBoundingClientRect()
  const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width))
  const { start, end } = visibleRange()
  const range = end - start
  const target = Math.min(
    Math.max(0, ratio * state.duration - range / 2),
    Math.max(0, state.duration - range),
  )
  scrollToTime(target, range > 0 ? (container.value?.clientWidth || 1) / range : 1)
}

/** 播放/暂停按钮（也可由父组件通过 ref 调用 expose 出来的方法）。 */
function togglePlay() {
  if (!state.ready) return
  toggle()
}

/** 挂载/切换音频时重建实例。 */
function mount(url) {
  const hasPeaks = Array.isArray(props.peaks) && props.peaks.length > 0
  const instance = attach(container.value, url, {
    peaks: hasPeaks ? props.peaks : undefined,
    duration: props.duration || undefined,
  })
  if (instance) {
    instance.on('ready', () => {
      emit('ready', state.duration)
      // 先"适应宽度"，把每秒像素（pxPerSec）确定下来——缩放条要按它算视野窗，
      // 否则 pxPerSec=0 时缩放条永远显示"全宽"。
      zoomToFit()
      requestAnimationFrame(syncScroll)
    })
    // 横向滚动（拖缩放条、autoScroll 跟随播放、滚轮）都同步到缩放条。
    // WaveSurfer 会给出 (可见起点, 可见终点, scrollLeft, scrollRight)，
    // 这里只用它触发重算，具体数值统一由 syncScroll 从 API 读，避免两套口径。
    instance.on('scroll', () => syncScroll())
    instance.on('zoom', () => requestAnimationFrame(syncScroll))
    instance.on('resize', () => requestAnimationFrame(syncScroll))
    instance.on('redrawcomplete', () => syncScroll())
  }
}

// 缩放变化（按钮 / Ctrl+滚轮 / 缩放条）后，重算视野窗位置
watch(
  () => [state.pxPerSec, state.ready, state.duration],
  () => requestAnimationFrame(syncScroll),
)

watch(
  // flush: 'post' —— 等 v-show 生效、容器有真实高度后再挂载波形
  () => [props.audioUrl, props.peaks, props.duration],
  () => mount(props.audioUrl),
  { flush: 'post' },
)

watch(
  () => props.deletes,
  (spans) => setDeletes(spans),
  { deep: true },
)

/**
 * 滚轮交互：
 *  - Ctrl / Alt + 滚轮 → 以指针位置为中心缩放；
 *  - 普通滚轮 / Shift + 滚轮 → 横向滚动（长音频只看局部时最常用）。
 */
function onWheel(event) {
  if (!state.ready) return
  const element = container.value
  if (!element) return

  if (event.ctrlKey || event.altKey) {
    event.preventDefault()
    // 记录指针位置对应的时间点，缩放后把同一时间点滚回指针下方
    const rect = element.getBoundingClientRect()
    const ratio = rect.width > 0 ? (event.clientX - rect.left) / rect.width : 0.5
    const { start, end } = visibleRange()
    const timeAtPointer = start + (end - start) * ratio
    const anchorOffset = element.clientWidth * ratio

    zoomBy(event.deltaY < 0 ? 1.25 : 0.8)

    requestAnimationFrame(() => {
      const total = getScrollWidth()
      if (!total || state.duration <= 0) return
      const targetLeft = (timeAtPointer / state.duration) * total - anchorOffset
      setScroll(Math.max(0, targetLeft))
      syncScroll()
    })
    return
  }

  if (!state.zoomed) return // 未放大时没有可滚动内容
  if (event.deltaY === 0 && event.deltaX === 0) return
  event.preventDefault()
  const delta = event.deltaY !== 0 ? event.deltaY : event.deltaX
  setScroll(getScroll() + delta)
}

onMounted(() => {
  setDeletes(props.deletes)
  mount(props.audioUrl)
  container.value?.addEventListener('wheel', onWheel, { passive: false })
  container.value?.addEventListener('scroll', syncScroll, { passive: true })
  window.addEventListener('resize', syncScroll)
  syncScroll()
})

onBeforeUnmount(() => {
  container.value?.removeEventListener('wheel', onWheel)
  container.value?.removeEventListener('scroll', syncScroll)
  window.removeEventListener('resize', syncScroll)
  detach()
})

// 供父组件调用（App.vue 的命令也可以直接走 usePlayback，二选一均可）。
defineExpose({
  play,
  pause,
  toggle,
  isPlaying: () => state.playing,
  getCurrentTime: () => state.currentTime,
  getDuration: () => state.duration,
  seekTo,
  seekBy,
})
</script>

<template>
  <section class="waveform-view">
    <header class="wave-header">
      <button
        class="wave-play"
        type="button"
        :disabled="!state.ready"
        :title="state.playing ? '暂停（空格）' : '播放（空格）'"
        @click="togglePlay"
      >
        <svg v-if="!state.playing" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
          <path d="M4 2.5v11l9-5.5z" fill="currentColor" />
        </svg>
        <svg v-else viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
          <path d="M4 2.5h3.2v11H4zM8.8 2.5H12v11H8.8z" fill="currentColor" />
        </svg>
      </button>

      <span class="wave-title">源波形</span>
      <span class="wave-time">
        {{ formatClock(state.currentTime) }} / {{ formatClock(state.duration) }}
      </span>

      <!-- 缩放控制：适应 / 缩小 / 放大（Ctrl+滚轮也能缩放，滚轮横向滚动） -->
      <span class="wave-zoom">
        <button type="button" title="缩小（Ctrl+滚轮）" :disabled="!state.ready" @click="zoomBy(0.7)">
          −
        </button>
        <button type="button" title="适应宽度" :disabled="!state.ready" @click="zoomToFit()">
          {{ zoomLabel }}
        </button>
        <button type="button" title="放大（Ctrl+滚轮）" :disabled="!state.ready" @click="zoomBy(1.4)">
          +
        </button>
      </span>
      <span class="wave-hint">红色 = 删除区间 · 点击移动播放头 · Ctrl+滚轮缩放</span>

      <ul class="wave-legend">
        <li><span class="legend-swatch legend-deleted" />删除区间</li>
        <li><span class="legend-swatch legend-played" />已播放</li>
        <li><span class="legend-swatch legend-cursor" />播放头</li>
      </ul>
    </header>

    <div class="wave-body">
      <div v-show="audioUrl" ref="container" class="wave-container" />
      <p v-if="!audioUrl" class="wave-empty">尚未载入音频：点击「选择文件」后这里显示源波形。</p>
      <p v-else-if="state.errorText" class="wave-empty wave-error">
        音频加载失败：{{ state.errorText }}
      </p>
      <p v-else-if="!state.ready" class="wave-empty">正在解码音频…</p>
    </div>

    <!--
      Audition 式缩放条：整条代表全片时长，其中的亮框是当前可见范围。
      拖框体 = 平移；拖左右边缘 = 缩放；点空白 = 该处居中。
    -->
    <div
      v-show="audioUrl && state.ready"
      ref="zoomBar"
      class="wave-zoombar"
      title="拖动中间的框平移视野，拖两端缩放，点击空白处居中"
      @pointerdown.self="beginDrag($event, 'window')"
      @pointermove="onBarPointerMove"
      @pointerup="endDrag"
      @pointercancel="endDrag"
      @click.self="onBarClick"
    >
      <div
        class="wave-zoombar-window"
        :style="windowStyle"
        @pointerdown.stop="beginDrag($event, 'window')"
      >
        <span
          class="zoombar-handle zoombar-handle-left"
          title="拖动缩放左边界"
          @pointerdown.stop="beginDrag($event, 'left')"
        />
        <span
          class="zoombar-handle zoombar-handle-right"
          title="拖动缩放右边界"
          @pointerdown.stop="beginDrag($event, 'right')"
        />
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 固定 180px：垂直布局里吸顶不滚动（L2）。 */
.waveform-view {
  display: flex;
  flex-direction: column;
  height: 180px;
  min-height: 180px;
  background: var(--bg-panel);
}

.wave-header {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
  height: 28px;
  padding: 0 12px;
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
  overflow: hidden;
}

.wave-play {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  padding: 0;
  color: var(--text);
  background: #37373d;
  border: 1px solid var(--border-strong);
  border-radius: 3px;
  cursor: pointer;
}

.wave-play:hover:not(:disabled) {
  color: #fff;
  background: var(--accent);
  border-color: var(--accent);
}

.wave-play:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.wave-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-strong);
}

.wave-time {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-dim);
  white-space: nowrap;
}

.wave-hint {
  font-size: 11px;
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
}

.wave-legend {
  display: flex;
  gap: 10px;
  margin: 0 0 0 auto;
  padding: 0;
  list-style: none;
  font-size: 11px;
  color: var(--text-faint);
}

.wave-legend li {
  display: flex;
  align-items: center;
  gap: 5px;
}

.legend-swatch {
  width: 10px;
  height: 9px;
  border-radius: 2px;
}

.legend-deleted {
  background: rgba(239, 68, 68, 0.28);
}

.legend-played {
  background: var(--accent);
}

.legend-cursor {
  width: 2px;
  background: var(--warn);
}

.wave-body {
  position: relative;
  flex: 1 1 auto;
  min-height: 0;
  padding: 6px 12px 8px;
}

.wave-container {
  width: 100%;
  height: 100%;
  min-height: 0;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 4px;
  box-sizing: border-box;
  /* 横向滚动由 WaveSurfer 内部滚动层负责，外层不要再滚，否则双滚动条 */
  overflow: hidden;
}

/* ---------------- Audition 式缩放条 ---------------- */
.wave-zoombar {
  position: relative;
  flex: 0 0 auto;
  height: 14px;
  margin: 4px 12px 6px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 3px;
  cursor: pointer;
  user-select: none;
  touch-action: none;
}

.wave-zoombar-window {
  position: absolute;
  top: 0;
  bottom: 0;
  min-width: 8px;
  background: rgba(59, 130, 246, 0.22);
  border: 1px solid var(--accent);
  border-radius: 2px;
  box-sizing: border-box;
  cursor: grab;
}

.wave-zoombar-window:active {
  cursor: grabbing;
}

/* 两端的拉伸把手：做出"可抓"的视觉提示 */
.zoombar-handle {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 7px;
  cursor: ew-resize;
}

.zoombar-handle::after {
  content: '';
  position: absolute;
  top: 3px;
  bottom: 3px;
  width: 1px;
  background: var(--accent);
  opacity: 0.85;
}

.zoombar-handle-left {
  left: 0;
}

.zoombar-handle-left::after {
  left: 3px;
}

.zoombar-handle-right {
  right: 0;
}

.zoombar-handle-right::after {
  right: 3px;
}

.wave-zoom {
  display: inline-flex;
  gap: 2px;
  flex: 0 0 auto;
}

.wave-zoom button {
  min-width: 22px;
  height: 20px;
  padding: 0 6px;
  font-size: 11px;
  font-family: var(--mono);
  color: var(--text-dim);
  background: transparent;
  border: 1px solid var(--border-strong);
  border-radius: 3px;
  cursor: pointer;
}

.wave-zoom button:hover:not(:disabled) {
  color: #fff;
  border-color: var(--accent);
}

.wave-zoom button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.wave-empty {
  position: absolute;
  inset: 6px 12px 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0;
  font-size: 12px;
  color: var(--text-faint);
  border: 1px dashed var(--border-strong);
  border-radius: 4px;
}

.wave-error {
  color: var(--danger);
}
</style>

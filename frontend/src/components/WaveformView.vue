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
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { formatClock, usePlayback } from '../composables/usePlayback.js'

const props = defineProps({
  /** 源音频的 blob URL（由 App.vue 用本地 File 生成） */
  audioUrl: { type: String, default: '' },
  /** DeleteSpanOut[]：{ start, end, token_id, cause } */
  deletes: { type: Array, default: () => [] },
})

const emit = defineEmits(['ready'])

const container = ref(null)
const { state, attach, detach, setDeletes, toggle, play, pause, seekTo, seekBy } = usePlayback()

/** 播放/暂停按钮（也可由父组件通过 ref 调用 expose 出来的方法）。 */
function togglePlay() {
  if (!state.ready) return
  toggle()
}

/** 挂载/切换音频时重建实例。 */
function mount(url) {
  const instance = attach(container.value, url)
  if (instance) {
    instance.on('ready', () => emit('ready', state.duration))
  }
}

watch(
  // flush: 'post' —— 等 v-show 生效、容器有真实高度后再挂载波形
  () => props.audioUrl,
  (url) => mount(url),
  { flush: 'post' },
)

watch(
  () => props.deletes,
  (spans) => setDeletes(spans),
  { deep: true },
)

onMounted(() => {
  setDeletes(props.deletes)
  mount(props.audioUrl)
})

onBeforeUnmount(() => {
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
      <span class="wave-hint">红色 = 删除区间 · 点击波形只移动播放头</span>

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
  overflow: hidden;
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

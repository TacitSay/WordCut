<script setup>
/**
 * 波形视图：用 WaveSurfer 显示源音频波形。
 *
 * 约定（见 docs/术语表.md）：
 *  - 波形展示的是 source_audio（源音频，只读）；
 *  - DeleteSpan 用红色半透明区域标注，不可拖动 / 不可缩放；
 *  - 点击波形只移动播放头（playhead），不改变任何删除状态。
 *
 * 音频来源：后端未暴露源音频下载接口，因此直接使用用户选择的本地文件
 * （URL.createObjectURL 生成的 blob URL），与上传的是同一个文件。
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import WaveSurfer from 'wavesurfer.js'
import RegionsPlugin from 'wavesurfer.js/plugins/regions'

const props = defineProps({
  /** 源音频的 blob URL（由 App.vue 通过本地 File 生成） */
  audioUrl: { type: String, default: '' },
  /** DeleteSpanOut[]：{ start, end, token_id, cause } */
  deletes: { type: Array, default: () => [] },
})

const emit = defineEmits(['ready'])

const container = ref(null)
const wave = shallowRef(null)
const regions = shallowRef(null)
const ready = ref(false)
const errorText = ref('')
const playing = ref(false)
const currentTime = ref(0)
const duration = ref(0)

/** 当前播放进度百分比（0–100）。 */
const progress = computed(() => {
  if (!duration.value) return 0
  return Math.min(100, Math.max(0, (currentTime.value / duration.value) * 100))
})

function formatClock(seconds) {
  const value = Number(seconds)
  if (!Number.isFinite(value) || value <= 0) return '0:00.0'
  const minutes = Math.floor(value / 60)
  const rest = value - minutes * 60
  return `${minutes}:${rest.toFixed(1).padStart(4, '0')}`
}

/** 销毁旧实例，避免重复挂载导致多个 canvas 堆叠。 */
function destroyWave() {
  if (wave.value) {
    try {
      wave.value.destroy()
    } catch {
      /* 忽略卸载异常 */
    }
  }
  wave.value = null
  regions.value = null
  ready.value = false
  playing.value = false
  currentTime.value = 0
  duration.value = 0
}

/** 按当前 DeleteSpan 列表重绘红色区间。 */
function renderRegions() {
  const plugin = regions.value
  if (!plugin || !ready.value) return
  plugin.clearRegions()
  for (const span of props.deletes) {
    const start = Number(span?.start)
    const end = Number(span?.end)
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue
    plugin.addRegion({
      start,
      end,
      color: 'rgba(239, 68, 68, 0.28)',
      drag: false,
      resize: false,
    })
  }
}

/** 创建 WaveSurfer 实例（音频地址变化时重建）。 */
function createWave(url) {
  destroyWave()
  errorText.value = ''
  if (!url || !container.value) return

  const instance = WaveSurfer.create({
    container: container.value,
    url,
    height: 180,
    waveColor: '#4b5563',
    progressColor: '#3b82f6',
    cursorColor: '#f59e0b',
    cursorWidth: 2,
    barWidth: 2,
    barGap: 1,
    barRadius: 1,
    normalize: true,
    autoScroll: true,
    autoCenter: false,
    hideScrollbar: true,
  })

  const plugin = instance.registerPlugin(RegionsPlugin.create())

  wave.value = instance
  regions.value = plugin

  instance.on('ready', () => {
    ready.value = true
    duration.value = instance.getDuration()
    renderRegions()
    emit('ready', duration.value)
  })
  instance.on('timeupdate', (time) => {
    currentTime.value = time
  })
  instance.on('seeking', (time) => {
    currentTime.value = time
  })
  instance.on('play', () => {
    playing.value = true
  })
  instance.on('pause', () => {
    playing.value = false
  })
  instance.on('finish', () => {
    playing.value = false
  })
  instance.on('error', (error) => {
    errorText.value = String(error?.message ?? error ?? '未知错误')
    ready.value = false
  })

  // 点击删除区间也只在区间起点移动播放头，不改变删除状态。
  plugin.on('region-clicked', (region, event) => {
    event?.stopPropagation?.()
    if (region && Number.isFinite(Number(region.start))) {
      instance.setTime(Number(region.start))
    }
  })
}

function togglePlay() {
  if (!wave.value || !ready.value) return
  wave.value.playPause()
}

watch(
  () => props.audioUrl,
  (url) => {
    createWave(url)
  },
)

watch(
  () => props.deletes,
  () => {
    renderRegions()
  },
  { deep: true },
)

onMounted(() => {
  createWave(props.audioUrl)
})

onBeforeUnmount(() => {
  destroyWave()
})
</script>

<template>
  <section class="waveform-view">
    <header class="pane-header">
      <h2 class="pane-title">波形视图</h2>
      <span class="pane-hint">红色半透明区域 = 删除区间 · 点击波形仅移动播放头</span>
      <div class="wave-controls">
        <button
          class="btn btn-small"
          type="button"
          :disabled="!ready"
          :title="playing ? '暂停' : '播放'"
          @click="togglePlay"
        >
          {{ playing ? '暂停' : '播放' }}
        </button>
        <span class="wave-time">{{ formatClock(currentTime) }} / {{ formatClock(duration) }}</span>
      </div>
    </header>

    <div class="wave-body">
      <div v-show="audioUrl" ref="container" class="wave-container" />

      <p v-if="!audioUrl" class="pane-empty">
        尚未载入音频。在工具栏点击「选择音频」后，这里会直接显示本地音频的源波形。
      </p>
      <p v-else-if="errorText" class="pane-empty pane-error">
        音频加载失败：{{ errorText }}
      </p>

      <div class="wave-progress" :aria-hidden="true">
        <div class="wave-progress-bar" :style="{ width: `${progress}%` }" />
      </div>

      <ul class="wave-legend">
        <li><span class="legend-swatch legend-deleted" />删除区间</li>
        <li><span class="legend-swatch legend-played" />已播放</li>
        <li><span class="legend-swatch legend-cursor" />播放头</li>
      </ul>

      <p class="wave-note">波形读取的是你选择的本地源音频（与上传给后端的是同一个文件）。</p>
    </div>
  </section>
</template>

<style scoped>
.waveform-view {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}

.wave-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 16px 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.wave-container {
  width: 100%;
  min-height: 180px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 0;
  box-sizing: border-box;
}

.wave-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

.wave-time {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--text-dim);
  white-space: nowrap;
}

.wave-progress {
  height: 3px;
  width: 100%;
  background: var(--border);
  border-radius: 2px;
  overflow: hidden;
}

.wave-progress-bar {
  height: 100%;
  background: var(--accent);
  transition: width 0.08s linear;
}

.wave-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
  color: var(--text-dim);
}

.wave-legend li {
  display: flex;
  align-items: center;
  gap: 6px;
}

.legend-swatch {
  width: 12px;
  height: 10px;
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

.wave-note {
  margin: 0;
  font-size: 12px;
  color: var(--text-faint);
}
</style>

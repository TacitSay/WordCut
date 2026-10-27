/**
 * 播放控制 composable —— 全应用共享同一个 WaveSurfer 实例。
 *
 * 为什么是「模块级单例」而不是每个组件各建一份：
 *  - 波形画布只能有一个（否则播放头/进度会分叉，见 docs/界面问题清单.md L2）；
 *  - App.vue 需要在不碰 WaveformView 内部实现的前提下执行
 *    「空格播放暂停 / 前进后退」等命令（docs/架构预留设计.md 第 4 节命令注册表）；
 *  - 所以实例与状态放在模块作用域，任何组件调用 usePlayback() 拿到的都是同一份。
 *
 * 术语（见 docs/术语表.md）：
 *  - 播放头 = playhead，对应 WaveSurfer 的 currentTime；
 *  - 波形展示的是 source_audio（源音频，只读）；
 *  - 删除区间 = DeleteSpan，以红色半透明区域标注（只显示，不可拖动）。
 *
 * 状态字段（playbackState）：
 *  attached    Boolean  是否已创建 WaveSurfer 实例
 *  ready       Boolean  音频是否解码完成（未 ready 时禁止播放）
 *  playing     Boolean  是否正在播放
 *  currentTime Number   播放头位置（秒）
 *  duration    Number   源音频时长（秒）
 *  errorText   String   加载失败信息
 *  url         String   当前音频地址
 */

import { reactive } from 'vue'
import WaveSurfer from 'wavesurfer.js'
import RegionsPlugin from 'wavesurfer.js/plugins/regions'

/** 删除区间填充色（红色半透明，与界面图例一致）。 */
const REGION_COLOR = 'rgba(239, 68, 68, 0.28)'

/** 容器高度量不到时的兜底高度（px）。 */
const DEFAULT_WAVE_HEIGHT = 140

/** 全局共享的播放状态。 */
export const playbackState = reactive({
  attached: false,
  ready: false,
  playing: false,
  currentTime: 0,
  duration: 0,
  errorText: '',
  url: '',
})

/** WaveSurfer 实例（module 作用域单例）。 */
let wave = null
/** RegionsPlugin 实例。 */
let regions = null
/** 当前挂载的容器元素，用于判断「同一容器 + 同一音频」时无需重建。 */
let currentContainer = null
/** 当前要标注的删除区间（跨 attach 保留，便于重建后立即重绘）。 */
let deleteSpans = []

/** 安全转数字：非法值返回 fallback。 */
function toNumber(value, fallback = 0) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

/** 把时间限制在 [0, duration] 内（duration 未知时只保证非负）。 */
function clampTime(seconds) {
  const value = toNumber(seconds, 0)
  const upper = playbackState.duration > 0 ? playbackState.duration : value
  return Math.min(Math.max(value, 0), upper)
}

/** 销毁实例并复位状态（不会清空 deleteSpans，重建后仍会重绘）。 */
export function destroyWaveform() {
  if (wave) {
    try {
      wave.destroy()
    } catch {
      /* 忽略卸载异常 */
    }
  }
  wave = null
  regions = null
  currentContainer = null
  playbackState.attached = false
  playbackState.ready = false
  playbackState.playing = false
  playbackState.currentTime = 0
  playbackState.duration = 0
  playbackState.url = ''
}

/** 按当前删除区间重绘红色区域（未 ready 时跳过，ready 事件里会再调）。 */
function renderRegions() {
  const plugin = regions
  if (!plugin || !playbackState.ready) return
  plugin.clearRegions()
  for (const span of deleteSpans) {
    const start = toNumber(span?.start, Number.NaN)
    const end = toNumber(span?.end, Number.NaN)
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue
    plugin.addRegion({
      start,
      end,
      color: REGION_COLOR,
      drag: false,
      resize: false,
    })
  }
}

/**
 * 挂载/重建波形实例。
 * @param {HTMLElement|null} container 波形容器
 * @param {string} url 源音频地址（blob URL）
 * @returns {object|null} WaveSurfer 实例
 */
export function attachWaveform(container, url) {
  if (!container) return null
  // 同一容器 + 同一音频：复用现有实例（避免重复 create 造成 canvas 堆叠）。
  if (wave && container === currentContainer && url === playbackState.url) return wave

  destroyWaveform()
  playbackState.errorText = ''
  if (!url) return null

  playbackState.url = url
  currentContainer = container

  let instance = null
  try {
    instance = WaveSurfer.create({
      container,
      url,
      height: Math.max(64, Math.round(container.clientHeight || DEFAULT_WAVE_HEIGHT)),
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
  } catch (error) {
    playbackState.errorText = String(error?.message ?? error ?? '未知错误')
    playbackState.url = ''
    currentContainer = null
    return null
  }

  const plugin = instance.registerPlugin(RegionsPlugin.create())
  wave = instance
  regions = plugin
  playbackState.attached = true

  instance.on('ready', () => {
    playbackState.ready = true
    playbackState.duration = toNumber(instance.getDuration(), 0)
    playbackState.currentTime = toNumber(instance.getCurrentTime(), 0)
    renderRegions()
  })
  instance.on('timeupdate', (time) => {
    playbackState.currentTime = toNumber(time, 0)
  })
  instance.on('seeking', (time) => {
    playbackState.currentTime = toNumber(time, 0)
  })
  instance.on('play', () => {
    playbackState.playing = true
  })
  instance.on('pause', () => {
    playbackState.playing = false
  })
  instance.on('finish', () => {
    playbackState.playing = false
  })
  instance.on('error', (error) => {
    playbackState.errorText = String(error?.message ?? error ?? '未知错误')
    playbackState.ready = false
    playbackState.playing = false
  })

  // 点击删除区域：只把播放头移到区间起点，不改变任何删除状态。
  plugin.on('region-clicked', (region, event) => {
    event?.stopPropagation?.()
    if (region) seekTo(region.start)
  })

  return instance
}

/** 播放（需要 ready）。返回是否真的发出了播放请求。 */
export function play() {
  if (!wave || !playbackState.ready) return false
  try {
    const result = wave.play()
    if (result && typeof result.catch === 'function') result.catch(() => {})
  } catch {
    return false
  }
  return true
}

/** 暂停。 */
export function pause() {
  if (!wave || !playbackState.ready) return false
  try {
    wave.pause()
  } catch {
    return false
  }
  return true
}

/** 播放/暂停切换（空格键与菜单命令都走这里）。 */
export function toggle() {
  if (!wave || !playbackState.ready) return false
  return playbackState.playing ? pause() : play()
}

/** 把播放头移到指定秒数。 */
export function seekTo(seconds) {
  if (!wave || !playbackState.ready) return false
  try {
    wave.setTime(clampTime(seconds))
  } catch {
    return false
  }
  playbackState.currentTime = clampTime(seconds)
  return true
}

/** 相对移动播放头（秒，可为负）。 */
export function seekBy(delta) {
  const base = toNumber(playbackState.currentTime, 0)
  return seekTo(base + toNumber(delta, 0))
}

/** 更新删除区间（红色标注）。 */
export function setDeletes(spans) {
  deleteSpans = Array.isArray(spans)
    ? spans.map((span) => ({
        start: toNumber(span?.start, Number.NaN),
        end: toNumber(span?.end, Number.NaN),
      }))
    : []
  renderRegions()
}

/** 当前是否在播放。 */
export function isPlaying() {
  return playbackState.playing
}

/** 当前播放头位置（秒）。 */
export function getCurrentTime() {
  return playbackState.currentTime
}

/** 源音频时长（秒）。 */
export function getDuration() {
  return playbackState.duration
}

/** 把秒格式化成 m:ss.d（播放器风格）。 */
export function formatClock(seconds) {
  const value = toNumber(seconds, 0)
  if (value <= 0) return '0:00.0'
  const minutes = Math.floor(value / 60)
  const rest = value - minutes * 60
  return `${minutes}:${rest.toFixed(1).padStart(4, '0')}`
}

/** 共享 API（同一份对象，App.vue 与 WaveformView.vue 都拿这个）。 */
let sharedApi = null

/**
 * 取播放控制 API。
 * @returns {{
 *   state: object,
 *   attach: (container: HTMLElement|null, url: string) => object|null,
 *   detach: () => void,
 *   play: () => boolean,
 *   pause: () => boolean,
 *   toggle: () => boolean,
 *   seekTo: (seconds: number) => boolean,
 *   seekBy: (delta: number) => boolean,
 *   seekBack: (step?: number) => boolean,
 *   seekForward: (step?: number) => boolean,
 *   setDeletes: (spans: object[]) => void,
 *   isPlaying: () => boolean,
 *   getCurrentTime: () => number,
 *   getDuration: () => number,
 *   formatClock: (seconds: number) => string,
 * }}
 */
export function usePlayback() {
  if (!sharedApi) {
    sharedApi = {
      state: playbackState,
      attach: attachWaveform,
      detach: destroyWaveform,
      play,
      pause,
      toggle,
      seekTo,
      seekBy,
      seekBack: (step = 2) => seekBy(-Math.abs(toNumber(step, 2))),
      seekForward: (step = 2) => seekBy(Math.abs(toNumber(step, 2))),
      setDeletes,
      isPlaying,
      getCurrentTime,
      getDuration,
      formatClock,
    }
  }
  return sharedApi
}

export default usePlayback

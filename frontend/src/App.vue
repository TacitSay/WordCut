<script setup>
/**
 * WordCut 主界面（M4 重构版）。
 *
 * 布局（见 docs/界面问题清单.md L1 / L2 / L3）：
 *   ┌──────────────────────────────────────────────────────────┐
 *   │ MenuBar（命令注册表驱动：文件/编辑/视图/工具/播放/插件/帮助） │
 *   │ Toolbar（项目名 + 选择文件/上传/转写/导出，紧凑图标+文字）    │
 *   ├───────────────────────────────────┬──────────────────────┤
 *   │ WaveformView（固定 180px，sticky） │ 参数面板（240px，可折叠，默认收起）│
 *   │ TextEditor（分段文本流，滚动）     │                      │
 *   ├───────────────────────────────────┴──────────────────────┤
 *   │ 状态栏：提示 · 源时长 · 输出时长 · 删除 Token · 删除总时长 · 版本 │
 *   └──────────────────────────────────────────────────────────┘
 *
 * 数据流（字段名严格按 src/api.py 的 Pydantic 模型消费，时间单位秒）：
 *   选择文件 → 上传(POST /api/projects) → 转写(POST .../transcribe)
 *   → 拉取(GET .../tokens → { tokens, deleted_ids, segments })
 *   → 删除/恢复(POST .../delete | .../restore → EditStateResponse)
 *   → 导出(POST .../export → ExportResponse)
 *
 * 命令注册（docs/架构预留设计.md 第 4 节）：所有内置能力都注册进
 * core/commands.js，菜单栏 / 快捷键 / （将来的右键菜单）共用同一张表，
 * 全部 handler 都是真实实现，不存在「只注册不干活」的命令。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { createHistory, createTokenEditEntry } from './core/history.js'
import MenuBar from './components/MenuBar.vue'
import Toolbar from './components/Toolbar.vue'
import ExportDialog from './components/ExportDialog.vue'
import ProjectDialog from './components/ProjectDialog.vue'

import TextEditor from './components/TextEditor.vue'
import WaveformView from './components/WaveformView.vue'
import { registerCommands, runCommand, shortcutFromEvent, shortcutMap } from './core/commands.js'
import { formatClock, usePlayback } from './composables/usePlayback.js'
import {
  createProject,
  deleteTokens,
  detectSilences,
  exportFileUrl,
  startExport,
  fetchExportStatus,
  fetchAudioInfo,
  fetchAudioParams,
  fetchAudition,
  fetchTokens,
  buildProjectFile,
  health,
  listProjects,
  fetchProject,
  loadProject,
  loadProjectFile,
  restoreTokens,
  saveAudioParams,
  sourceAudioUrl,
  saveProject,
  startTranscription,
  fetchTranscriptionStatus,
  fetchWaveformPeaks,
} from './api/client.js'

/** 播放头前进/后退步长（秒）。 */
const SEEK_STEP = 2

// ----------------------------------------------------------------------
// 状态
// ----------------------------------------------------------------------
const projectName = ref('')
/** ProjectCreated：{ id, name, sample_rate, duration } */
const project = ref(null)
/** 用户选择的本地音视频文件（同时用于波形渲染与上传） */
const audioFile = ref(null)
/** 源音频的 blob URL */
const audioUrl = ref('')
/**
 * 预计算波形峰值（[[0..1...]]）与对应时长。
 * 交给 WaveformView 直接绘制，避免 WaveSurfer 整段解码——2 小时音频解码到
 * 48kHz 要 ~1.3GB，浏览器会抛 "Unable to decode audio data"。
 */
const waveformPeaks = ref([])
const waveformDuration = ref(0)
/** TokenOut[]：{ id, text, start, end, is_deleted, confidence } */
const tokens = ref([])
/** SegmentOut[]：{ index, start, end, duration, text, token_ids } */
const segments = ref([])
/** 被删 Token id 列表 */
const deletedIds = ref([])
/** DeleteSpanOut[]：{ start, end, token_id, cause } */
const deletes = ref([])
/** 删除总时长（秒，已合并重叠） */
const totalDeleted = ref(0)
/** 导出结果（ExportResponse） */
const exportInfo = ref(null)
/** 导出对话框是否打开 */
const exportDialogOpen = ref(false)
/** 项目管理对话框是否打开 */
const projectDialogOpen = ref(false)
/** 工程文件保存中 */
const saving = ref(false)
/** 正在检测静音 */
const detecting = ref(false)
/** 自上次保存工程后是否有编辑（用于提示用户显式保存） */
const dirtySinceSave = ref(false)
/** 工程文件加载中 */
const loadingProject = ref(false)
/** 后端 API 版本号（状态栏展示） */
const apiVersion = ref('')

const uploading = ref(false)
const transcribing = ref(false)
const exporting = ref(false)
/** 正在请求删除 / 恢复的 Token id */
const pendingIds = ref([])
/** 状态提示：{ text, type } */
const status = ref({ text: '正在连接后端…', type: 'info' })

/** 光标位置：0..tokens.length（位于第 cursorIndex 个 Token 之前） */
const cursorIndex = ref(0)
/** 选区锚点；null 表示没有选区 */
const selectionAnchor = ref(null)
/** 撤销栈：[{ kind: 'delete' | 'restore', ids: string[] }] */
/** 统一的编辑历史（撤销/重做命令栈） */
const history = createHistory()

/** 波形是否显示（视图菜单） */
const waveformVisible = ref(true)
/** 右侧参数面板是否展开（默认收起，L3） */
const panelOpen = ref(false)

/**
 * 音频后处理参数（增益 / 峰值标准化 / 首尾淡入淡出）。
 * 与后端 AudioParams 对齐；保存后导出按此渲染，不改源文件。
 */
const audioParams = ref({
  gain_db: 0,
  normalize: false,
  fade_in: 0,
  fade_out: 0,
  denoise: false,
  denoise_db: -30,
})
/** 后端返回的参数上限（供控件约束） */
const audioLimits = ref({
  min_gain_db: -24,
  max_gain_db: 24,
  max_fade_seconds: 5,
  min_denoise_db: -80,
  max_denoise_db: -5,
  default_denoise_db: -30,
})
/** 参数保存中 */
const savingParams = ref(false)
/** 音频信息统计（AudioInfoResponse）：源音频与剪辑后的指标 */
const audioInfo = ref(null)
/** 统计加载中 */
const loadingInfo = ref(false)

// ---- 关键词搜索 ----
/** 搜索栏是否展开 */
const searchOpen = ref(false)
/** 搜索关键词 */
const searchQuery = ref('')
/** 当前命中的序号（0 起） */
const searchIndex = ref(0)
/** 搜索输入框（用于打开时自动聚焦） */
const searchInputRef = ref(null)

// ---- 编辑后即时试听 ----
/** 是否在删除/恢复后自动播一小段剪辑结果 */
const auditionOnEdit = ref(true)
/** 试听是否正在播放（状态栏提示用） */
const auditionPlaying = ref(false)
/** 隐藏的 <audio> 播放器（懒创建，复用同一个元素） */
let auditionAudio = null
let auditionUrl = ''

const toolbarRef = ref(null)
const playback = usePlayback()

// ----------------------------------------------------------------------
// 派生值
// ----------------------------------------------------------------------
const deletedSet = computed(() => new Set(deletedIds.value))

/** tokenApi：供历史记录回放时调用的后端接口集合（projectId 用函数延迟取值）。 */
const tokenApi = {
  deleteTokens,
  restoreTokens,
  projectId: () => project.value?.id ?? '',
}
const sourceDuration = computed(() => Number(project.value?.duration ?? 0))
const outputDuration = computed(() =>
  Math.max(0, sourceDuration.value - Number(totalDeleted.value || 0)),
)
const deletedCount = computed(() => deletedIds.value.length)
const hasTokens = computed(() => tokens.value.length > 0)
const busy = computed(
  () =>
    uploading.value ||
    transcribing.value ||
    exporting.value ||
    pendingIds.value.length > 0,
)

/** 选区区间（half-open：[start, end)）。 */
const selectionRange = computed(() => {
  const anchor = selectionAnchor.value
  const cursor = cursorIndex.value
  if (anchor === null || anchor === cursor) {
    return { start: cursor, end: cursor, empty: true }
  }
  return { start: Math.min(anchor, cursor), end: Math.max(anchor, cursor), empty: false }
})

/** 统计所用参数的一句话摘要（面板里回显，避免"改了但不知道有没有生效"）。 */
const paramSummary = computed(() => {
  const params = audioInfo.value?.audio_params || {}
  const parts = []
  if (params.normalize) {
    parts.push(`标准化 ${Number(params.target_dbfs ?? -1).toFixed(1)} dBFS`)
  }
  if (params.denoise) parts.push(`降噪 ${Number(params.denoise_db ?? -30).toFixed(0)} dB`)
  if (Number(params.gain_db)) parts.push(`增益 ${Number(params.gain_db).toFixed(1)} dB`)
  if (Number(params.fade_in)) parts.push(`淡入 ${Number(params.fade_in).toFixed(2)} s`)
  if (Number(params.fade_out)) parts.push(`淡出 ${Number(params.fade_out).toFixed(2)} s`)
  return parts.length ? parts.join(' · ') : '无（原样输出）'
})

/** 成片是否削顶（超出 0 dBFS 会被硬截，听感破音）。 */
const clipWarning = computed(() => Number(audioInfo.value?.edited?.clipped_samples) > 0)

/**
 * 正在播放的 Token 下标（播放头跟随）。
 *
 * 用二分查找定位 currentTime 落在哪个 Token 的 [start, end) 内；
 * 区间之间的空隙（停顿）返回 -1，此时不高亮——这比"永远高亮最近的词"
 * 更符合直觉：停顿处就是没有人在说话。
 */
const activeTokenIndex = computed(() => {
  if (!playback.state.playing) return -1
  const time = Number(playback.state.currentTime)
  const list = tokens.value
  if (!Number.isFinite(time) || !list.length) return -1
  let low = 0
  let high = list.length - 1
  while (low <= high) {
    const mid = (low + high) >> 1
    const start = Number(list[mid].start)
    const end = Number(list[mid].end)
    if (!Number.isFinite(start) || !Number.isFinite(end)) return -1
    if (time < start) high = mid - 1
    else if (time >= end) low = mid + 1
    else return mid
  }
  return -1
})

// ----------------------------------------------------------------------
// 工具函数
// ----------------------------------------------------------------------
function setStatus(text, type = 'info') {
  status.value = { text, type }
}

function formatSeconds(seconds) {
  const value = Number(seconds)
  if (!Number.isFinite(value) || value < 0) return '0.00 秒'
  return `${value.toFixed(2)} 秒`
}

/** 合并重叠区间并求总时长（秒），用于本地即时反馈。 */
function mergeSpans(spans) {
  const sorted = [...spans].sort((a, b) => a.start - b.start)
  const merged = []
  for (const span of sorted) {
    const last = merged[merged.length - 1]
    if (last && span.start <= last.end) {
      last.end = Math.max(last.end, span.end)
    } else {
      merged.push({ ...span })
    }
  }
  return merged
}

function totalOf(spans) {
  return spans.reduce((sum, span) => sum + Math.max(0, span.end - span.start), 0)
}

// ----------------------------------------------------------------------
// 编辑后即时试听
// ----------------------------------------------------------------------
/**
 * 播一小段"剪辑后的结果"，用来确认刚才删/恢复得对不对。
 *
 * @param {number} sourceTime 源时间轴上的关注点（秒），通常是改动词的起点
 */
async function playAudition(sourceTime) {
  if (!auditionOnEdit.value || !project.value?.id) return
  const time = Number(sourceTime)
  if (!Number.isFinite(time) || time < 0) return
  try {
    const blob = await fetchAudition(project.value.id, { source_time: time, pad: 1.2 })
    if (auditionUrl) URL.revokeObjectURL(auditionUrl)
    auditionUrl = URL.createObjectURL(blob)
    if (!auditionAudio) {
      auditionAudio = new Audio()
      auditionAudio.addEventListener('ended', () => {
        auditionPlaying.value = false
      })
      auditionAudio.addEventListener('pause', () => {
        auditionPlaying.value = false
      })
    } else {
      auditionAudio.pause()
    }
    auditionAudio.src = auditionUrl
    // 与主播放器互斥：两路声音叠在一起会听不清
    if (playback.state.playing) playback.pause()
    auditionPlaying.value = true
    await auditionAudio.play()
  } catch (error) {
    auditionPlaying.value = false
    // 试听是辅助功能，失败不能打断删除/恢复主流程
    console.warn('[audition] 试听失败', error)
  }
}

/** 开关「播放时跳过删除区间」。 */
function toggleSkipDeleted() {
  const enabled = playback.setSkipDeleted(!playback.isSkipDeleted())
  setStatus(
    enabled
      ? '已开启：播放时自动跳过已删除区间（听到的是剪辑结果）'
      : '已关闭：播放的是原始音频（删除区间只做标注）',
    'info',
  )
}

/** 停止正在播放的试听片段。 */
function stopAudition() {
  if (auditionAudio) auditionAudio.pause()
  auditionPlaying.value = false
}

/** 开关「编辑后自动试听」。 */
function toggleAuditionOnEdit() {
  auditionOnEdit.value = !auditionOnEdit.value
  if (!auditionOnEdit.value) stopAudition()
  setStatus(
    auditionOnEdit.value
      ? '已开启：删除/恢复后自动试听剪辑结果'
      : '已关闭：删除/恢复后不再自动试听',
    'info',
  )
}

// ----------------------------------------------------------------------
// 关键词搜索
// ----------------------------------------------------------------------
/**
 * 搜索命中列表。
 *
 * 支持跨 Token 的关键词：中文常被 whisper 切成单字，用户搜"我们"时可能落在
 * 两个 Token 上。做法是把全部 Token 文本拼成一整串，并维护"字符 → Token 下标"
 * 的映射，匹配到子串后再反查首尾 Token，从而得到一个 Token 区间。
 */
const searchMatches = computed(() => {
  const query = searchQuery.value.trim()
  if (!query) return []
  const list = tokens.value
  if (!list.length) return []

  let text = ''
  const charToToken = []
  for (let index = 0; index < list.length; index += 1) {
    const tokenText = String(list[index]?.text ?? '')
    for (let c = 0; c < tokenText.length; c += 1) charToToken.push(index)
    text += tokenText
  }

  const haystack = text.toLowerCase()
  const needle = query.toLowerCase()
  if (!needle) return []

  const matches = []
  let from = 0
  while (from <= haystack.length - needle.length) {
    const at = haystack.indexOf(needle, from)
    if (at < 0) break
    const startToken = charToToken[at]
    const endToken = charToToken[at + needle.length - 1]
    if (startToken !== undefined && endToken !== undefined) {
      matches.push({ start: startToken, end: endToken + 1, at })
    }
    from = at + Math.max(1, needle.length)
  }
  return matches
})

/** 命中数量（空查询为 0）。 */
const searchCount = computed(() => searchMatches.value.length)

/** 关键词变化后回到第一个命中，避免序号越界。 */
watch(searchMatches, () => {
  searchIndex.value = 0
})

async function openSearch() {
  searchOpen.value = true
  await nextTick()
  searchInputRef.value?.focus?.()
  searchInputRef.value?.select?.()
}

function closeSearch() {
  searchOpen.value = false
  searchQuery.value = ''
}

/** 跳到第 index 个命中：选中对应 Token 区间，播放头移到起点。 */
function jumpToMatch(index) {
  const matches = searchMatches.value
  if (!matches.length) return
  const size = matches.length
  const target = ((index % size) + size) % size
  searchIndex.value = target
  const match = matches[target]
  // 直接构造选区：锚点=命中起点，光标=命中终点
  selectionAnchor.value = match.start
  cursorIndex.value = match.end
  const start = Number(tokens.value[match.start]?.start)
  if (Number.isFinite(start) && playback.state.ready) playback.seekTo(start)
}

function nextMatch() {
  if (!searchMatches.value.length) return
  jumpToMatch(searchIndex.value + 1)
}

function prevMatch() {
  if (!searchMatches.value.length) return
  jumpToMatch(searchIndex.value - 1)
}

/** 删除全部命中（文本驱动剪辑的高频操作：清掉口癖、重复词）。 */
async function deleteSearchMatches() {
  const matches = searchMatches.value
  if (!matches.length) {
    setStatus('没有可删除的匹配', 'info')
    return
  }
  const ids = []
  for (const match of matches) {
    for (let i = match.start; i < match.end; i += 1) {
      const id = tokens.value[i]?.id
      if (id) ids.push(id)
    }
  }
  const unique = [...new Set(ids)]
  await performEdit(unique, 'delete', { nextCursor: matches[0].start })
  setStatus(`已删除 ${unique.length} 个 Token（${matches.length} 处匹配）`, 'success')
}

// ----------------------------------------------------------------------
// 音频后处理参数 + 音频信息统计
// ----------------------------------------------------------------------
/** 把 dBFS 数值格式化成 xx.x dB；-inf（全静音）显示为“静音”。 */
function formatDbfs(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '静音'
  return `${number.toFixed(1)} dB`
}

/** 把 0..1 的比例格式化成百分比。 */
function formatPercent(ratio) {
  const number = Number(ratio)
  if (!Number.isFinite(number)) return '—'
  return `${(number * 100).toFixed(1)}%`
}

/** 读取项目的后处理参数（打开/加载工程后调用）。 */
async function loadAudioParams() {
  if (!project.value?.id) return
  try {
    const data = await fetchAudioParams(project.value.id)
    audioParams.value = { ...audioParams.value, ...(data?.audio_params || {}) }
    if (data?.limits) audioLimits.value = { ...audioLimits.value, ...data.limits }
  } catch (error) {
    setStatus(`读取音频参数失败：${error.message}`, 'error')
  }
}

/** 保存后处理参数；exporting 时不允许改，避免导出中途参数变动。 */
async function handleSaveAudioParams() {
  if (!project.value?.id) {
    setStatus('请先创建或打开项目', 'info')
    return
  }
  savingParams.value = true
  const beforePeak = audioInfo.value?.edited?.peak_dbfs
  setStatus('正在保存音频参数…', 'info')
  try {
    const data = await saveAudioParams(project.value.id, audioParams.value)
    audioParams.value = { ...audioParams.value, ...(data?.audio_params || {}) }
    await loadAudioInfo()
    const after = audioInfo.value?.edited?.peak_dbfs
    const delta =
      Number.isFinite(Number(beforePeak)) && Number.isFinite(Number(after))
        ? `（成片峰值 ${formatDbfs(beforePeak)} → ${formatDbfs(after)}）`
        : ''
    setStatus(
      clipWarning.value
        ? `参数已保存，但成片削顶 ${audioInfo.value?.edited?.clipped_samples} 个样本，建议开启标准化或降低增益`
        : `音频参数已保存，导出时按此渲染${delta}`,
      clipWarning.value ? 'error' : 'success',
    )
  } catch (error) {
    setStatus(`保存音频参数失败：${error.message}`, 'error')
  } finally {
    savingParams.value = false
  }
}

/**
 * 拉取波形峰值（供 WaveformView 直接绘制，不解码音频）。
 *
 * 失败不阻断使用：拿不到峰值就退回 WaveSurfer 自己解码（短视频没问题），
 * 只是会把 errorText 显示在波形区。
 */
async function loadWaveformPeaks() {
  if (!project.value?.id) {
    waveformPeaks.value = []
    waveformDuration.value = 0
    return
  }
  try {
    const data = await fetchWaveformPeaks(project.value.id, 4000)
    waveformPeaks.value = Array.isArray(data?.peaks) ? data.peaks : []
    waveformDuration.value = Number(data?.duration) || sourceDuration.value || 0
  } catch (error) {
    waveformPeaks.value = []
    waveformDuration.value = 0
    console.warn('[waveform] 获取峰值失败，退回解码模式', error)
  }
}

/** 重新统计音频指标（源音频 + 当前删除状态下渲染后的成片）。 */
async function loadAudioInfo() {
  if (!project.value?.id) {
    audioInfo.value = null
    return
  }
  loadingInfo.value = true
  try {
    audioInfo.value = await fetchAudioInfo(project.value.id)
  } catch (error) {
    audioInfo.value = null
    setStatus(`读取音频统计失败：${error.message}`, 'error')
  } finally {
    loadingInfo.value = false
  }
}

/**
 * 由 Token 列表 + 被删 id 反推删除区间（本地近似，仅用于首屏重绘）。
 * 之后每次删除 / 恢复都以后端 EditStateResponse.deletes 为准。
 */
function buildDeletesFromTokens(list, ids) {
  const set = new Set(ids)
  const spans = []
  for (const token of list) {
    if (!set.has(token.id)) continue
    const start = Number(token.start)
    const end = Number(token.end)
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue
    spans.push({ start, end, token_id: token.id, cause: 'word' })
  }
  return mergeSpans(spans)
}

function applyEditState(state) {
  deletedIds.value = Array.isArray(state?.deleted_ids) ? [...state.deleted_ids] : []
  deletes.value = Array.isArray(state?.deletes) ? [...state.deletes] : []
  const total = Number(state?.total_deleted)
  totalDeleted.value = Number.isFinite(total) ? total : totalOf(deletes.value)
  // 编辑后工程与磁盘不一致：标记为脏，由用户显式 Ctrl+S 保存
  if (project.value) dirtySinceSave.value = true
}

// ----------------------------------------------------------------------
// 光标 / 选区
// ----------------------------------------------------------------------
function clampCursor(value) {
  const max = tokens.value.length
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return 0
  return Math.min(max, Math.max(0, Math.round(parsed)))
}

/**
 * 设置光标。
 * @param {number} index 目标位置（0..tokens.length）
 * @param {{ extend?: boolean }} [options] extend=true 时保留选区锚点（Shift）
 */
function setCursor(index, { extend = false } = {}) {
  const target = clampCursor(index)
  if (extend) {
    if (selectionAnchor.value === null) selectionAnchor.value = cursorIndex.value
  } else {
    selectionAnchor.value = null
  }
  cursorIndex.value = target
}

function moveCursor(delta, { extend = false } = {}) {
  setCursor(cursorIndex.value + delta, { extend })
}

/** 选区内（未删除也算）的全部 Token id。 */
function selectionTokenIds() {
  const range = selectionRange.value
  if (range.empty) return []
  return tokens.value.slice(range.start, range.end).map((token) => token.id)
}

function handleSelectAll() {
  if (!hasTokens.value) {
    setStatus('还没有可选择的 Token，请先转写', 'info')
    return
  }
  selectionAnchor.value = 0
  cursorIndex.value = tokens.value.length
  setStatus(`已全选 ${tokens.value.length} 个 Token（Delete 批量删除）`, 'info')
}

// ----------------------------------------------------------------------
// 删除 / 恢复（I1）
// ----------------------------------------------------------------------
/**
 * 提交一次删除或恢复。
 * @param {string[]} ids Token id
 * @param {'delete'|'restore'} action
 * @param {{ record?: boolean, nextCursor?: number|null }} [options]
 */
async function performEdit(ids, action, { record = true, nextCursor = null } = {}) {
  if (!project.value) {
    setStatus('项目尚未创建，无法编辑：请先上传音频', 'error')
    return
  }
  const isDelete = action === 'delete'
  const deleted = deletedSet.value
  const targets = [...new Set(ids)].filter(
    (id) => id && (isDelete ? !deleted.has(id) : deleted.has(id)),
  )
  if (!targets.length) {
    setStatus(
      isDelete
        ? '选中的 Token 已全部删除（再按 Delete 即可恢复）'
        : '选中的 Token 没有可恢复的',
      'info',
    )
    return
  }

  pendingIds.value = targets
  try {
    const state = isDelete
      ? await deleteTokens(project.value.id, targets)
      : await restoreTokens(project.value.id, targets)
    applyEditState(state)
    if (record) {
    history.push(
      createTokenEditEntry(targets, isDelete ? 'delete' : 'restore', tokenApi),
    )
  }

    const hasTarget =
      nextCursor !== null && nextCursor !== undefined && Number.isFinite(Number(nextCursor))
    cursorIndex.value = clampCursor(hasTarget ? Number(nextCursor) : cursorIndex.value)
    selectionAnchor.value = null

    setStatus(
      isDelete
        ? `已删除 ${targets.length} 个 Token（累计 ${deletedIds.value.length} 个）`
        : `已恢复 ${targets.length} 个 Token（仍删除 ${deletedIds.value.length} 个）`,
      'success',
    )

    // 及时反馈：按刚改动的位置，播一小段"剪辑后的结果"
    const anchorToken = tokens.value.find((token) => targets.includes(token.id))
    const anchorTime = Number(anchorToken?.start)
    if (Number.isFinite(anchorTime)) playAudition(anchorTime)
  } catch (error) {
    setStatus(`${isDelete ? '删除' : '恢复'}失败：${error.message}`, 'error')
  } finally {
    pendingIds.value = []
  }
}

/** 光标之后第一个仍存活的 Token 下标；没有则 -1。 */
function nextAliveIndex(from) {
  for (let index = Math.max(0, from); index < tokens.value.length; index += 1) {
    const token = tokens.value[index]
    if (token && !deletedSet.value.has(token.id)) return index
  }
  return -1
}

/** 光标之前第一个仍存活的 Token 下标；没有则 -1。 */
function prevAliveIndex(from) {
  for (let index = Math.min(from, tokens.value.length - 1); index >= 0; index -= 1) {
    const token = tokens.value[index]
    if (token && !deletedSet.value.has(token.id)) return index
  }
  return -1
}

/** Delete 键 / 编辑菜单「删除选中」：选区优先，否则删光标所在 Token。 */
async function handleDeleteCommand() {
  if (!hasTokens.value) {
    setStatus('还没有可删除的文本，请先转写', 'info')
    return
  }
  if (busy.value) {
    setStatus('正在处理中，请稍候', 'busy')
    return
  }
  const range = selectionRange.value
  if (!range.empty) {
    await applyDeleteOrRestore(selectionTokenIds(), range.start)
    return
  }
  // 光标处的 Token 可能已被删除：向后找到第一个仍存活的 Token。
  const index = nextAliveIndex(cursorIndex.value)
  if (index < 0) {
    setStatus('光标之后没有可删除的 Token（按 Backspace 向前删除）', 'info')
    return
  }
  const token = tokens.value[index]
  if (!token) return
  await performEdit([token.id], 'delete', { nextCursor: index })
}

/**
 * Delete / Backspace 的统一语义：**已删的再按一次就恢复**。
 *
 * 用户的直觉是"选中删掉的东西，再按删除就还原"——这也让撤销不必去翻菜单。
 * 规则：
 *  - 选区里**全部已删** → 恢复；否则删除其中仍存活的（混合选区按删除处理，
 *    避免"删一半又恢复一半"的歧义）；
 *  - 光标态没有选区，仍按"删下一个存活词"处理。
 */
async function applyDeleteOrRestore(ids, nextCursor) {
  const allDeleted =
    ids.length > 0 && ids.every((id) => deletedSet.value.has(id))
  if (allDeleted) {
    await performEdit(ids, 'restore', { nextCursor })
    setStatus(`已恢复选中的 ${ids.length} 个 Token（再按 Delete 可重新删除）`, 'success')
    return
  }
  await performEdit(ids, 'delete', { nextCursor })
}

/** Backspace：选区优先，否则删光标前一个 Token。 */
async function handleBackspace() {
  if (!hasTokens.value) return
  if (busy.value) return
  const range = selectionRange.value
  if (!range.empty) {
    await applyDeleteOrRestore(selectionTokenIds(), range.start)
    return
  }
  const index = prevAliveIndex(cursorIndex.value - 1)
  if (index < 0) {
    setStatus('光标之前没有可删除的 Token', 'info')
    return
  }
  const token = tokens.value[index]
  if (!token) return
  await performEdit([token.id], 'delete', { nextCursor: index })
}

/** 编辑菜单「恢复选中」：恢复选区里已删除的 Token。 */
async function handleRestoreSelection() {
  if (!hasTokens.value) {
    setStatus('还没有可恢复的文本，请先转写', 'info')
    return
  }
  const selected = selectionTokenIds()
  const targets = selected.filter((id) => deletedSet.value.has(id))
  if (!targets.length) {
    setStatus('请先用 Shift+方向键选中已删除的 Token（红色删除线）', 'info')
    return
  }
  await performEdit(targets, 'restore', { nextCursor: selectionRange.value.start })
}

/** 恢复全部已删除的 Token（"删错了想全撤"的兜底入口）。 */
async function handleRestoreAll() {
  if (!deletedIds.value.length) {
    setStatus('当前没有被删除的 Token', 'info')
    return
  }
  if (busy.value) {
    setStatus('正在处理中，请稍候', 'busy')
    return
  }
  const count = deletedIds.value.length
  await performEdit([...deletedIds.value], 'restore', { nextCursor: cursorIndex.value })
  setStatus(`已恢复全部 ${count} 个被删除的 Token`, 'success')
}

/** 把 /api/projects/{pid} 的详情恢复成界面状态。 */
async function applyProjectDetail(detail) {
  resetProjectState()

  project.value = {
    id: detail.project_id,
    name: detail.name,
    sample_rate: detail.sample_rate,
    duration: detail.duration,
    source_kind: detail.source_kind,
  }
  projectName.value = detail.name
  tokens.value = Array.isArray(detail.tokens) ? [...detail.tokens] : []
  segments.value = Array.isArray(detail.segments) ? [...detail.segments] : []
  deletedIds.value = Array.isArray(detail.deleted_ids) ? [...detail.deleted_ids] : []
  deletes.value = buildDeletesFromTokens(tokens.value, deletedIds.value)
  totalDeleted.value = totalOf(deletes.value)
  playback.setDeletes(deletes.value)
  exportInfo.value = null

  // 源音频：后端按项目记录的路径重新转码一份 WAV，前端换成新的 blob URL
  try {
    const response = await fetch(`${sourceAudioUrl(detail.project_id)}?t=${Date.now()}`)
    if (response.ok) {
      const blob = await response.blob()
      if (audioUrl.value) URL.revokeObjectURL(audioUrl.value)
      audioUrl.value = URL.createObjectURL(blob)
    }
  } catch {
    /* 音频拿不到时仍可编辑文本，只是没有波形 */
  }

  setStatus(
    `已打开「${detail.name}」：${tokens.value.length} 个 Token，删除 ${deletedIds.value.length} 个`,
    'success',
  )
  await loadAudioParams()
  await loadAudioInfo()
  await loadWaveformPeaks()
}

/** 从项目管理里打开某个已有项目（不用重新上传）。 */
async function handleOpenProjectById(pid) {
  if (busy.value) {
    setStatus('当前有任务在进行，请稍后再打开项目', 'error')
    return
  }
  loadingProject.value = true
  setStatus('正在打开项目…', 'busy')
  try {
    const detail = await fetchProject(pid)
    await applyProjectDetail(detail)
    projectDialogOpen.value = false
  } catch (error) {
    setStatus(`打开项目失败：${error.message}`, 'error')
  } finally {
    loadingProject.value = false
  }
}

/** 项目被重命名/删除后核对当前项目（名字同步、被删则清空状态）。 */
async function handleProjectDialogChanged() {
  const current = project.value
  if (!current?.id) return
  try {
    const list = await listProjects()
    const found = (Array.isArray(list) ? list : []).find((item) => item.id === current.id)
    if (!found) {
      resetProjectState()
      setStatus('当前项目已在项目管理中被删除，状态已清空', 'info')
      return
    }
    if (found.name !== current.name) {
      project.value = { ...current, name: found.name }
      projectName.value = found.name
      setStatus(`项目已重命名为「${found.name}」`, 'success')
    }
  } catch (error) {
    setStatus(`刷新项目信息失败：${error.message}`, 'error')
  }
}

/** 静音检测：调用后端分析并批量删除无发音的长停顿。 */
async function handleRemoveSilences() {
  if (!project.value || !hasTokens.value) {
    setStatus('请先转写，再检测静音', 'info')
    return
  }
  if (busy.value) return

  detecting.value = true
  setStatus('正在分析音频中的静音段…', 'busy')
  try {
    const result = await detectSilences(project.value.id, { min_silence: 0.8 })
    const deletes = Array.isArray(result?.deletes) ? result.deletes : []
    if (!deletes.length) {
      setStatus(
        `未发现可删除的静音段（检出 ${result?.silences?.length ?? 0} 段，均与发音重叠）`,
        'info',
      )
      return
    }

    // 静音区间落在两个 token 之间，按时间相邻关系映射回 token：
    // 把"完全落在静音区间内"的 token 视为要删除的对象。
    const ids = tokens.value
      .filter((token) =>
        deletes.some(
          (span) =>
            Number(token.start) >= Number(span.start) - 0.02 &&
            Number(token.end) <= Number(span.end) + 0.02,
        ),
      )
      .map((token) => token.id)
      .filter((id) => !deletedSet.value.has(id))

    if (!ids.length) {
      setStatus(
        `检出 ${deletes.length} 段静音（共 ${Number(result.total_deleted).toFixed(1)}s），但没有整词落在其中，已跳过`,
        'info',
      )
      return
    }

    await performEdit(ids, 'delete', { nextCursor: cursorIndex.value })
    setStatus(
      `静音处理完成：删除 ${ids.length} 个 Token（${deletes.length} 段静音，共 ${Number(result.total_deleted).toFixed(1)}s）`,
      'success',
    )
  } catch (error) {
    setStatus(`静音检测失败：${error.message}`, 'error')
  } finally {
    detecting.value = false
  }
}

/** 点击已删除 Token：立即恢复它。 */
async function handleRestoreToken(ids) {
  const targets = (Array.isArray(ids) ? ids : [ids]).filter((id) => deletedSet.value.has(id))
  if (!targets.length) return
  await performEdit(targets, 'restore', { nextCursor: cursorIndex.value })
}

/** 撤销上一步（删除↔恢复互逆）。 */
async function handleUndo() {
  if (busy.value) {
    setStatus('正在处理中，请稍候', 'busy')
    return
  }
  if (!history.canUndo) {
    setStatus('没有可撤销的操作', 'info')
    return
  }
  const label = history.undoLabel
  try {
    const state = await history.undo()
    applyEditState(state)
    setStatus(`已撤销：${label}`, 'success')
  } catch (error) {
    setStatus(`撤销失败：${error.message}`, 'error')
  }
}

async function handleRedo() {
  if (busy.value) {
    setStatus('正在处理中，请稍候', 'busy')
    return
  }
  if (!history.canRedo) {
    setStatus('没有可重做的操作', 'info')
    return
  }
  const label = history.redoLabel
  try {
    const state = await history.redo()
    applyEditState(state)
    setStatus(`已重做：${label}`, 'success')
  } catch (error) {
    setStatus(`重做失败：${error.message}`, 'error')
  }
}

// ----------------------------------------------------------------------
// 文件 / 上传 / 转写 / 导出
// ----------------------------------------------------------------------
/** 清空所有项目级状态（新建/切换项目、上传失败时统一走这里，防止残留）。 */
function resetProjectState() {
  if (audioUrl.value) URL.revokeObjectURL(audioUrl.value)
  project.value = null
  audioFile.value = null
  audioUrl.value = ''
  projectName.value = ''
  tokens.value = []
  segments.value = []
  deletedIds.value = []
  deletes.value = []
  totalDeleted.value = 0
  exportInfo.value = null
  waveformPeaks.value = []
  waveformDuration.value = 0
  history.clear()
  cursorIndex.value = 0
  selectionAnchor.value = null
  playback.setDeletes([])
  stopAudition()
  if (auditionUrl) {
    URL.revokeObjectURL(auditionUrl)
    auditionUrl = ''
  }
}

/** 选择本地文件：清空旧项目状态 → 生成 blob URL → 自动上传。 */
async function handleSelectFile(file) {
  if (!file) return

  // 1. 先彻底清空上一个项目的状态（修复换项目后文本/删除点残留）
  resetProjectState()

  // 2. 建立新文件上下文
  audioFile.value = file
  audioUrl.value = URL.createObjectURL(file)
  projectName.value = file.name.replace(/\.[^.\\/]+$/, '')

  const sizeMb = (file.size / 1024 / 1024).toFixed(2)
  setStatus(`已选择：${file.name}（${sizeMb} MB），正在自动上传…`, 'busy')

  // 3. 选完即上传，不再需要单独点「上传」
  await handleUpload()
}

/** file.import：打开系统文件选择器。 */
function handleImportCommand() {
  const toolbar = toolbarRef.value
  if (toolbar && typeof toolbar.pickFile === 'function') {
    toolbar.pickFile()
    return
  }
  setStatus('文件选择器未就绪，请稍后重试', 'error')
}

async function handleUpload() {
  if (uploading.value) return
  const name = projectName.value.trim()
  if (!audioFile.value) {
    setStatus('请先选择音频/视频文件', 'error')
    return
  }
  if (!name) {
    setStatus('请填写项目名', 'error')
    return
  }

  uploading.value = true
  setStatus('正在上传（后端用 ffmpeg 统一转 16kHz 单声道 WAV）…', 'busy')
  try {
    const created = await createProject({ file: audioFile.value, name })
    project.value = created
    tokens.value = []
    segments.value = []
    deletedIds.value = []
    deletes.value = []
    totalDeleted.value = 0
    exportInfo.value = null
    history.clear()
    cursorIndex.value = 0
    selectionAnchor.value = null
    playback.setDeletes([])
    setStatus(
      `上传成功：项目「${created.name}」（${formatSeconds(created.duration)}，${created.sample_rate} Hz），下一步点击「转写」`,
      'success',
    )
    await loadAudioParams()
    await loadAudioInfo()
    await loadWaveformPeaks()
  } catch (error) {
    setStatus(`上传失败：${error.message}`, 'error')
  } finally {
    uploading.value = false
  }
}

async function handleTranscribe() {
  if (!project.value) {
    setStatus('请先选择文件并上传，创建项目', 'error')
    return
  }
  if (transcribing.value) return

  transcribing.value = true
  setStatus('正在转写（首次加载模型可能需要十几秒）…', 'busy')
  try {
    // 分段增量：转写过程中每完成一块就把已有词表刷出来，用户不用干等
    const refreshPartial = async () => {
      const list = await fetchTokens(project.value.id)
      tokens.value = Array.isArray(list?.tokens) ? [...list.tokens] : []
      segments.value = Array.isArray(list?.segments) ? [...list.segments] : []
      // 注意：增量阶段不改 deletedIds，避免把已保存的删除点冲掉
    }
    const result = await runTranscriptionJob(project.value.id, {
      onPartial: refreshPartial,
    })
    if (!result) return

    const list = await fetchTokens(project.value.id)
    tokens.value = Array.isArray(list?.tokens) ? [...list.tokens] : []
    segments.value = Array.isArray(list?.segments) ? [...list.segments] : []
    deletedIds.value = Array.isArray(list?.deleted_ids) ? [...list.deleted_ids] : []
    deletes.value = buildDeletesFromTokens(tokens.value, deletedIds.value)
    totalDeleted.value = totalOf(deletes.value)
    exportInfo.value = null
    history.clear()
    cursorIndex.value = 0
    selectionAnchor.value = null
    setStatus(
      `转写完成：${tokens.value.length} 个词级单元 / ${segments.value.length} 个段落，耗时 ${Number(
        result?.elapsed_seconds ?? 0,
      ).toFixed(1)} 秒（${result?.device || 'cpu'}/${result?.compute_type || 'int8'}）`,
      'success',
    )
  } catch (error) {
    setStatus(`转写失败：${error.message}`, 'error')
  } finally {
    transcribing.value = false
  }
}

/**
 * 跑一次异步转写并轮询进度，成功返回最终状态，失败返回 null。
 *
 * 长音频（2 小时）在 CPU 上要几十分钟，同步请求会一直挂着；
 * 改成"启动任务 + 轮询状态"后，界面能显示百分比，网络抖动也不丢进度
 * （结果落在后端数据库里）。
 *
 * 后端按 10 分钟一块转写，每块完成会把累计词表落库（``partial``）。这里在
 * 词数明显增长时顺手刷新一次 Token，用户就能"边转边看"，不必等全部跑完。
 *
 * @param {string} projectId
 * @param {{ onPartial?: (job: object) => Promise<void> }} [options]
 */
async function runTranscriptionJob(projectId, { onPartial } = {}) {
  const started = await startTranscription(projectId)
  let job = started
  let lastPaint = 0
  let lastPartialWords = 0
  let lastPartialAt = 0

  while (job && job.status === 'running') {
    await new Promise((resolve) => setTimeout(resolve, 1500))
    try {
      job = await fetchTranscriptionStatus(projectId)
    } catch (error) {
      // 404 = 后端没有这个任务的记录：通常是服务被重启（任务状态在内存里）。
      // 已完成的块已经落库，再次点击"转写"会从断点续转，所以这里给出可操作提示。
      if (error?.status === 404) {
        throw new Error(
          '转写任务已中断（后端可能重启过）。已完成的进度已保存，再点一次「转写」会从中断处继续',
        )
      }
      continue
    }

    const now = Date.now()
    if (now - lastPaint > 900) {
      lastPaint = now
      const pct = Math.round(Number(job.progress || 0) * 100)
      const resumeNote = job.resumed_from > 0 ? `（续转自 ${formatClock(job.resumed_from)}）` : ''
      setStatus(
        `转写中 ${pct}% · 已出 ${job.words} 词 · ${Number(job.elapsed_seconds).toFixed(0)}s` +
          `（${job.device || 'cpu'}/${job.compute_type || 'int8'}）${resumeNote}`,
        'busy',
      )
    }

    // 分段增量：词数涨了 300+ 且距上次刷新超过 10 秒，就把已有结果先渲染出来
    if (
      onPartial &&
      job.partial &&
      job.words - lastPartialWords >= 300 &&
      now - lastPartialAt > 10000
    ) {
      lastPartialWords = job.words
      lastPartialAt = now
      try {
        await onPartial(job)
      } catch {
        /* 增量刷新失败不影响转写主流程 */
      }
    }
  }

  if (job?.status === 'error') {
    setStatus(`转写失败：${job.error || '未知错误'}`, 'error')
    return null
  }
  return job
}

/** Ctrl+S：保存工程文件（.wcproj）。 */
async function handleSaveProject() {
  if (!project.value || !hasTokens.value) {
    setStatus('请先转写，再保存工程', 'error')
    return
  }

  const suggested = `${project.value.name || 'untitled'}.wcproj`
  const supportsNativePicker = Boolean(window.showSaveFilePicker)

  // 原生保存器可用：让后端生成内容，前端写进用户选的路径（无需绝对路径）
  if (supportsNativePicker) {
    let handle = null
    try {
      handle = await window.showSaveFilePicker({
        suggestedName: suggested,
        types: [
          {
            description: 'WordCut 工程文件',
            accept: { 'application/json': ['.wcproj'] },
          },
        ],
      })
    } catch (error) {
      if (error?.name === 'AbortError') {
        setStatus('已取消保存', 'info')
        return
      }
      handle = null
    }

    if (handle) {
      saving.value = true
      setStatus('正在保存工程…', 'busy')
      try {
        const response = await buildProjectFile(project.value.id)
        // 后端返回 { document, suggested_name }，写盘只写 document 本体
        const document = response?.document ?? response
        const payload = JSON.stringify(document, null, 2) + '\n'
        const writable = await handle.createWritable()
        await writable.write(payload)
        await writable.close()
        const size = payload.length
        dirtySinceSave.value = false
        setStatus(
          `工程已保存：${handle.name}（约 ${(size / 1024).toFixed(1)} KB）`,
          'success',
        )
      } catch (error) {
        setStatus(`保存工程失败：${error.message}`, 'error')
      } finally {
        saving.value = false
      }
      return
    }
  }

  // 兜底（不支持原生保存器的浏览器）：由后端写文件，需要用户给路径
  const target = window.prompt(`当前浏览器不支持选择保存位置，请输入服务器上的保存路径：`, suggested) || ''
  if (!target.trim()) {
    setStatus('已取消保存', 'info')
    return
  }

  saving.value = true
  setStatus('正在保存工程…', 'busy')
  try {
    const result = await saveProject(project.value.id, target.trim())
    dirtySinceSave.value = false
    setStatus(
      `工程已保存：${result.path}（${(result.size_bytes / 1024).toFixed(1)} KB）`,
      'success',
    )
  } catch (error) {
    setStatus(`保存工程失败：${error.message}`, 'error')
  } finally {
    saving.value = false
  }
}

/** 把加载结果应用为当前项目（成功路径，供路径加载与文件上传共用）。 */
async function applyLoadedProject(loaded) {
  resetProjectState()

  project.value = {
    id: loaded.project_id,
    name: loaded.name,
    sample_rate: loaded.sample_rate,
    duration: 0,
    source_kind: loaded.source_kind,
  }
  projectName.value = loaded.name
  tokens.value = Array.isArray(loaded.tokens) ? [...loaded.tokens] : []
  segments.value = Array.isArray(loaded.segments) ? [...loaded.segments] : []
  deletedIds.value = Array.isArray(loaded.deleted_ids) ? [...loaded.deleted_ids] : []
  deletes.value = buildDeletesFromTokens(tokens.value, deletedIds.value)
  totalDeleted.value = totalOf(deletes.value)
  playback.setDeletes(deletes.value)
  exportInfo.value = null

  const integrity = loaded.integrity || {}

  // 按工程记录的路径让后端重新提供源音频，恢复波形与播放
  let audioRestored = false
  try {
    const response = await fetch(
      `${sourceAudioUrl(loaded.project_id)}?t=${Date.now()}`,
    )
    if (response.ok) {
      const blob = await response.blob()
      if (audioUrl.value) URL.revokeObjectURL(audioUrl.value)
      audioUrl.value = URL.createObjectURL(blob)
      audioRestored = true
    }
  } catch {
    audioRestored = false
  }

  if (!integrity.audio_ok) {
    setStatus(
      `已打开「${loaded.name}」但源文件有变动：${integrity.message || ''}`,
      'error',
    )
    await loadAudioParams()
    return
  }

  setStatus(
    audioRestored
      ? `已打开「${loaded.name}」：${tokens.value.length} 个 Token，删除 ${deletedIds.value.length} 个，音频已恢复`
      : `已打开「${loaded.name}」：${tokens.value.length} 个 Token（音频未恢复，请重新导入源文件）`,
    audioRestored ? 'success' : 'error',
  )
  await loadAudioParams()
  await loadAudioInfo()
  await loadWaveformPeaks()
}

/** 直接读取工程文件内容并加载（无需绝对路径）。 */
async function openProjectFromFile(file) {
  if (!file) return
  if (busy.value) {
    setStatus('当前有任务在进行，请稍后再打开工程', 'error')
    return
  }

  loadingProject.value = true
  setStatus(`正在读取工程文件「${file.name}」…`, 'busy')
  try {
    const loaded = await loadProjectFile(file)
    await applyLoadedProject(loaded)
  } catch (error) {
    setStatus(`打开工程失败：${error.message}`, 'error')
  } finally {
    loadingProject.value = false
  }
}

/** Ctrl+Shift+O：打开工程文件（.wcproj）。 */
async function handleOpenProject() {
  if (busy.value) {
    setStatus('当前有任务在进行，请稍后再打开工程', 'error')
    return
  }

  // 首选：原生文件选择器，直接读文件内容上传（不需要绝对路径）
  if (window.showOpenFilePicker) {
    try {
      const [handle] = await window.showOpenFilePicker({
        types: [
          {
            description: 'WordCut 工程文件',
            accept: { 'application/json': ['.wcproj'] },
          },
        ],
        multiple: false,
      })
      const file = await handle.getFile()
      await openProjectFromFile(file)
      return
    } catch (error) {
      if (error?.name === 'AbortError') {
        setStatus('已取消打开', 'info')
        return
      }
    }
  }

  // 兜底：隐藏的 <input type=file> 选择器
  openProjectInput()
}

/** 兜底方案：触发隐藏 input 选择 .wcproj。 */
function openProjectInput() {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.wcproj,application/json'
  input.onchange = () => {
    const file = input.files && input.files[0]
    if (file) openProjectFromFile(file)
  }
  input.click()
}

/** 旧路径加载入口（保留：后端与浏览器同机时可用绝对路径）。 */
async function handleOpenProjectByPath() {
  if (busy.value) {
    setStatus('当前有任务在进行，请稍后再打开工程', 'error')
    return
  }

  const target = window.prompt('工程文件路径（含 .wcproj）：', '') || ''
  if (!target.trim()) {
    setStatus('已取消打开', 'info')
    return
  }

  loadingProject.value = true
  setStatus('正在打开工程…', 'busy')
  try {
    const loaded = await loadProject(target.trim())
    await applyLoadedProject(loaded)
  } catch (error) {
    setStatus(`打开工程失败：${error.message}`, 'error')
  } finally {
    loadingProject.value = false
  }
}

/** 打开导出对话框（Ctrl+E / 菜单 文件 → 导出）。 */
function handleExport() {
  if (!project.value) {
    setStatus('请先上传并转写', 'error')
    return
  }
  if (!hasTokens.value) {
    setStatus('请先转写，再导出', 'error')
    return
  }
  if (exporting.value) return
  exportInfo.value = null
  exportDialogOpen.value = true
}

/** 导出对话框确认：按用户选择的格式与视频选项导出。 */
async function handleExportConfirm(options) {
  if (!project.value || exporting.value) return

  exporting.value = true
  const wantsVideo = Boolean(options?.export_video)
  setStatus(
    wantsVideo
      ? '正在导出音频 / 字幕 / EDL / 视频（视频剪辑可能需要几十秒）…'
      : '正在导出音频 / 字幕 / EDL…',
    'busy',
  )
  try {
    // 异步导出：先启动任务，再轮询阶段/进度，避免长视频导出时界面像卡死
    const result = await runExportJob(project.value.id, options)
    if (!result) return
    exportInfo.value = result
    const total = Number(result?.total_deleted)
    if (Number.isFinite(total)) totalDeleted.value = total

    const files = ['audio_path', 'srt_path', 'edl_path']
    if (result?.paths?.video_path) files.push('video_path')

    // 让用户选择保存位置并把产物写到自己机器上（无需绝对路径）
    const saved = await saveExportFilesToUser(result)

    setStatus(
      `导出完成：${result?.audio_format?.toUpperCase() || 'WAV'} ${formatSeconds(result?.output_duration)}` +
        (result?.paths?.video_path ? ' + 成品视频' : '') +
        (saved.selectionDir
          ? `，已保存到 ${saved.selectionDir}`
          : saved.pickerUsed
            ? '，已保存到你选择的位置'
            : `（${files.length} 个文件，服务器路径见导出结果）`) +
        (dirtySinceSave.value ? ' · 工程未保存（Ctrl+S 固化本次编辑）' : ''),
      'success',
    )
  } catch (error) {
    setStatus(`导出失败：${error.message}`, 'error')
  } finally {
    exporting.value = false
  }
}

/**
 * 跑一次异步导出并轮询进度，返回导出结果；失败返回 null。
 *
 * 2 小时视频导出要重编码上千个片段（实测 2–4 分钟）。同步等待时界面完全没有
 * 反馈，容易被当成卡死；改成"启动 + 轮询"后，状态栏能显示当前阶段与百分比。
 *
 * @param {string} projectId
 * @param {object} options 导出选项
 */
async function runExportJob(projectId, options) {
  const started = await startExport(projectId, options)
  let job = started
  const stageNames = {
    audio: '渲染/编码音频',
    video: '裁剪视频',
    mux: '合流音视频',
    done: '完成',
  }

  while (job && job.status === 'running') {
    await new Promise((resolve) => setTimeout(resolve, 700))
    try {
      job = await fetchExportStatus(projectId)
    } catch (error) {
      if (error?.status === 404) throw error
      continue
    }
    const stage = stageNames[job.stage] || job.stage
    const pct = Math.round(Number(job.progress || 0) * 100)
    setStatus(
      `正在导出：${stage} ${pct}% · ${Number(job.elapsed_seconds).toFixed(0)}s（可继续操作，完成后会自动保存）`,
      'busy',
    )
  }

  if (job?.status === 'error') {
    setStatus(`导出失败：${job.error || '未知错误'}`, 'error')
    return null
  }
  return job?.result || null
}

/** 把服务端产出的文件取回浏览器，让用户选择保存目录后写入本地。 */
async function saveExportFilesToUser(result) {  const paths = result?.paths || {}
  const order = ['audio_path', 'srt_path', 'edl_path', 'video_path']

  // 支持选择目录的浏览器（Chrome / Edge）：一次选目录，全部写入
  if (window.showDirectoryPicker) {
    let dirHandle = null
    try {
      dirHandle = await window.showDirectoryPicker({ mode: 'readwrite' })
    } catch (error) {
      if (error?.name === 'AbortError') return { pickerUsed: false, selectionDir: '' }
      dirHandle = null
    }

    if (dirHandle) {
      for (const key of order) {
        const serverPath = paths[key]
        if (!serverPath) continue
        try {
          const response = await fetch(exportFileUrl(project.value.id, serverPath))
          if (!response.ok) continue
          const blob = await response.blob()
          const name = String(serverPath).split(/[\\/]/).pop()
          const fileHandle = await dirHandle.getFileHandle(name, { create: true })
          const writable = await fileHandle.createWritable()
          await writable.write(blob)
          await writable.close()
        } catch {
          // 单个文件失败不影响其他文件
        }
      }
      return { pickerUsed: true, selectionDir: dirHandle.name || '' }
    }
  }

  // 兜底：逐个触发下载
  for (const key of order) {
    const serverPath = paths[key]
    if (!serverPath) continue
    const link = document.createElement('a')
    link.href = exportFileUrl(project.value.id, serverPath)
    link.download = String(serverPath).split(/[\\/]/).pop()
    document.body.appendChild(link)
    link.click()
    link.remove()
  }
  return { pickerUsed: false, selectionDir: '' }
}

// ----------------------------------------------------------------------
// 播放 / 视图
// ----------------------------------------------------------------------
function handlePlaybackToggle() {
  if (!audioUrl.value) {
    setStatus('还没有音频：请先选择文件', 'info')
    return
  }
  if (!playback.state.ready) {
    setStatus('音频还在解码，稍后再试', 'busy')
    return
  }
  playback.toggle()
}

function handleSeekBack() {
  if (!playback.state.ready) return
  playback.seekBy(-SEEK_STEP)
}

function handleSeekForward() {
  if (!playback.state.ready) return
  playback.seekBy(SEEK_STEP)
}

function handleToggleWaveform() {
  waveformVisible.value = !waveformVisible.value
  setStatus(waveformVisible.value ? '已显示波形' : '已隐藏波形', 'info')
}

function handleTogglePanel() {
  panelOpen.value = !panelOpen.value
}

/** 文本编辑器点击 Token → 播放头定位到该词起点。 */
function handleEditorSeek(seconds) {
  if (!playback.state.ready) return
  playback.seekTo(seconds)
}

/** 光标所在 Token 的音频起点（秒）；无 Token 时返回 null。 */
function timeAtCursor() {
  if (!tokens.value.length) return null
  const index = Math.min(Math.max(cursorIndex.value, 0), tokens.value.length - 1)
  const token = tokens.value[index]
  const start = Number(token?.start)
  return Number.isFinite(start) ? start : null
}

/** Enter：从光标所在 Token 开始播放到结尾。 */
function handlePlayFromCursor() {
  if (!audioUrl.value) {
    setStatus('还没有音频：请先选择文件', 'info')
    return
  }
  if (!playback.state.ready) {
    setStatus('音频还在解码，稍后再试', 'busy')
    return
  }
  const start = timeAtCursor()
  if (start === null) {
    // 没有 Token（未转写）：退化为普通播放 / 暂停
    playback.toggle()
    return
  }
  playback.clearRange()
  playback.seekTo(start)
  playback.play()
  setStatus(`从 ${formatSeconds(start)} 开始播放`, 'info')
}

/** Ctrl+Enter：试听选区（选区内首 Token 起点 → 末 Token 终点）。 */
function handlePlaySelection() {
  if (!audioUrl.value) {
    setStatus('还没有音频：请先选择文件', 'info')
    return
  }
  if (!playback.state.ready) {
    setStatus('音频还在解码，稍后再试', 'busy')
    return
  }
  const range = selectionRange.value
  if (range.empty) {
    setStatus('请先用 Shift+点击 或 Shift+方向键 选一段文字', 'info')
    return
  }
  const first = tokens.value[range.start]
  const last = tokens.value[range.end - 1]
  const start = Number(first?.start)
  const end = Number(last?.end)
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    setStatus('选区的音频区间无效，无法试听', 'error')
    return
  }
  if (playback.playRange(start, end)) {
    setStatus(`试听选区 ${formatSeconds(end - start)}（到终点自动停止）`, 'info')
  } else {
    setStatus('试听启动失败，请稍后重试', 'error')
  }
}

/** 文本编辑器点击 Token → 光标变更（Shift 时扩展选区）。 */
function handleCursorChange(payload) {
  setCursor(payload?.index ?? cursorIndex.value, { extend: Boolean(payload?.extend) })
}

// ----------------------------------------------------------------------
// 命令注册表（docs/架构预留设计.md 第 4 节）
// ----------------------------------------------------------------------
/** 命令执行 / 可用性判断共享的上下文（getter 保持响应式）。 */
const commandCtx = {
  get project() {
    return project.value
  },
  get tokens() {
    return tokens.value
  },
  get segments() {
    return segments.value
  },
  get hasProject() {
    return Boolean(project.value)
  },
  get hasTokens() {
    return hasTokens.value
  },
  get hasAudio() {
    return Boolean(audioUrl.value)
  },
  get hasSelection() {
    return !selectionRange.value.empty
  },
  get canUndo() {
    return history.canUndo
  },
  get playing() {
    return playback.state.playing
  },
  get waveformVisible() {
    return waveformVisible.value
  },
  get panelOpen() {
    return panelOpen.value
  },
  get busy() {
    return busy.value
  },
  get cursorIndex() {
    return cursorIndex.value
  },
}

registerCommands([
  {
    id: 'file.import',
    title: '导入媒体…',
    menu: 'file',
    shortcut: 'Ctrl+O',
    handler: () => handleImportCommand(),
  },
  {
    id: 'file.upload',
    title: '上传并创建项目',
    menu: 'file',
    handler: () => handleUpload(),
    enabled: () => Boolean(audioFile.value) && !uploading.value,
  },
  {
    id: 'file.export',
    title: '导出…',
    menu: 'file',
    shortcut: 'Ctrl+E',
    handler: () => handleExport(),
  },
  {
    id: 'file.saveProject',
    title: '保存工程…',
    menu: 'file',
    shortcut: 'Ctrl+S',
    handler: () => handleSaveProject(),
    enabled: () => Boolean(project.value) && hasTokens.value && !busy.value,
  },
  {
    id: 'file.openProject',
    title: '打开工程…',
    menu: 'file',
    shortcut: 'Ctrl+Shift+O',
    handler: () => handleOpenProject(),
    enabled: () => !busy.value,
  },
  {
    id: 'file.manageProjects',
    title: '项目管理（重命名 / 删除）…',
    menu: 'file',
    handler: () => {
      projectDialogOpen.value = true
    },
  },
  {
    id: 'edit.delete',
    title: '删除选中 Token',
    menu: 'edit',
    shortcut: 'Delete',
    handler: () => handleDeleteCommand(),
  },
  {
    id: 'edit.restore',
    title: '恢复选中 Token',
    menu: 'edit',
    shortcut: 'Ctrl+Shift+Z',
    handler: () => handleRestoreSelection(),
  },
  {
    id: 'edit.restoreAll',
    title: '恢复全部删除',
    menu: 'edit',
    handler: () => handleRestoreAll(),
    enabled: () => Boolean(project.value) && deletedCount.value > 0 && !busy.value,
  },
  {
    id: 'edit.removeSilences',
    title: '检测静音并批量删除…',
    menu: 'edit',
    handler: () => handleRemoveSilences(),
    enabled: () => Boolean(project.value) && hasTokens.value && !busy.value,
  },
  {
    id: 'edit.find',
    title: '搜索关键词…',
    menu: 'edit',
    shortcut: 'Ctrl+F',
    handler: () => openSearch(),
  },
  {
    id: 'edit.selectAll',
    title: '全选',
    menu: 'edit',
    shortcut: 'Ctrl+A',
    handler: () => handleSelectAll(),
  },
  {
    id: 'edit.undo',
    title: '撤销上一步',
    menu: 'edit',
    shortcut: 'Ctrl+Z',
    handler: () => handleUndo(),
  },
  {
    id: 'view.toggleWaveform',
    title: '显示 / 隐藏波形',
    menu: 'view',
    handler: () => handleToggleWaveform(),
  },
  {
    id: 'view.togglePanel',
    title: '显示 / 隐藏参数面板',
    menu: 'view',
    shortcut: 'Ctrl+B',
    handler: () => handleTogglePanel(),
  },
  {
    id: 'playback.toggle',
    title: '播放 / 暂停',
    menu: 'playback',
    shortcut: 'Space',
    handler: () => handlePlaybackToggle(),
  },
  {
    id: 'playback.seekBack',
    title: `后退 ${SEEK_STEP} 秒`,
    menu: 'playback',
    shortcut: 'Alt+ArrowLeft',
    handler: () => handleSeekBack(),
    enabled: () => playback.state.ready,
  },
  {
    id: 'playback.seekForward',
    title: `前进 ${SEEK_STEP} 秒`,
    menu: 'playback',
    shortcut: 'Alt+ArrowRight',
    handler: () => handleSeekForward(),
    enabled: () => playback.state.ready,
  },
  {
    id: 'playback.playFromCursor',
    title: '从光标处播放',
    menu: 'playback',
    shortcut: 'Enter',
    handler: () => handlePlayFromCursor(),
    enabled: () => Boolean(audioUrl.value),
  },
  {
    id: 'playback.playSelection',
    title: '试听选区',
    menu: 'playback',
    shortcut: 'Ctrl+Enter',
    handler: () => handlePlaySelection(),
    enabled: () => Boolean(audioUrl.value) && !selectionRange.value.empty,
  },
  {
    id: 'playback.toggleAudition',
    title: '编辑后自动试听（开关）',
    menu: 'playback',
    handler: () => toggleAuditionOnEdit(),
  },
  {
    id: 'playback.toggleSkipDeleted',
    title: '播放时跳过删除区间（开关）',
    menu: 'playback',
    handler: () => toggleSkipDeleted(),
  },
  {
    id: 'tools.audioInfo',
    title: '音频统计与后处理…',
    menu: 'tools',
    handler: () => {
      panelOpen.value = true
      loadAudioParams()
      loadAudioInfo()
    },
    enabled: () => Boolean(project.value),
  },
  {
    id: 'transcribe.run',
    title: '转写（faster-whisper）…',
    menu: 'tools',
    shortcut: 'Ctrl+R',
    handler: () => handleTranscribe(),
  },
])

function handleMenuRun(commandId) {
  runCommand(commandId, commandCtx)
}

// ----------------------------------------------------------------------
// 全局键盘（I1 / I3）
// ----------------------------------------------------------------------
/** 输入类元素聚焦时不拦截键盘，避免抢走用户的输入。 */
function isTypingTarget(target) {
  if (!(target instanceof HTMLElement)) return false
  const tag = target.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable
}

function onKeydown(event) {
  if (isTypingTarget(event.target)) return
  if (event.isComposing) return

  const key = event.key
  const plain = !event.ctrlKey && !event.metaKey && !event.altKey

  // Space：播放 / 暂停（I3）。按住不放不重复切换。
  if (key === ' ' && plain) {
    event.preventDefault()
    if (event.repeat) return
    runCommand('playback.toggle', commandCtx)
    return
  }

  // Backspace：删除光标前一个 Token（I1）。
  if (key === 'Backspace' && plain) {
    event.preventDefault()
    handleBackspace()
    return
  }

  // 方向键：移动光标；Shift 扩展选区（Alt 留给播放跳转）。
  if ((key === 'ArrowLeft' || key === 'ArrowRight') && plain) {
    event.preventDefault()
    moveCursor(key === 'ArrowLeft' ? -1 : 1, { extend: event.shiftKey })
    return
  }

  // Home / End：跳到文首 / 文末。
  if ((key === 'Home' || key === 'End') && plain) {
    event.preventDefault()
    setCursor(key === 'Home' ? 0 : tokens.value.length, { extend: event.shiftKey })
    return
  }

  // Enter 落在按钮上时交给按钮本身（避免「按钮聚焦 + Enter」被当成播放命令）
  if (key === 'Enter' && event.target?.tagName === 'BUTTON') return

  // 其余交给命令注册表的快捷键表（Delete / Ctrl+O / Ctrl+E / Ctrl+R …）。
  const commandId = shortcutMap().get(shortcutFromEvent(event))
  if (commandId) {
    event.preventDefault()
    runCommand(commandId, commandCtx)
  }
}

// ----------------------------------------------------------------------
// 生命周期
// ----------------------------------------------------------------------
watch(
  () => tokens.value.length,
  (length) => {
    if (cursorIndex.value > length) cursorIndex.value = length
    if (selectionAnchor.value !== null && selectionAnchor.value > length) {
      selectionAnchor.value = length
    }
  },
)

// 删除区间变化 → 同步给播放器（"播放时跳过删除区间"依赖它）
watch(
  () => deletes.value,
  (spans) => playback.setDeletes(spans),
  { deep: true },
)

// 选区变化 → 波形上同步高亮（蓝色区间），便于确认试听范围
watch(
  () => [selectionRange.value.empty, selectionRange.value.start, selectionRange.value.end],
  () => {
    const range = selectionRange.value
    if (range.empty) {
      playback.setSelection(null)
      return
    }
    const first = tokens.value[range.start]
    const last = tokens.value[range.end - 1]
    const start = Number(first?.start)
    const end = Number(last?.end)
    playback.setSelection(
      Number.isFinite(start) && Number.isFinite(end) && end > start
        ? { start, end }
        : null,
    )
  },
)

// ----------------------------------------------------------------------
// 拖拽上传
// ----------------------------------------------------------------------
const dragging = ref(false)
let dragDepth = 0

function hasFiles(event) {
  const types = event.dataTransfer?.types
  return Boolean(types && Array.from(types).includes('Files'))
}

function onDragEnter(event) {
  if (!hasFiles(event)) return
  event.preventDefault()
  dragDepth += 1
  dragging.value = true
}

function onDragOver(event) {
  if (!hasFiles(event)) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
}

function onDragLeave(event) {
  if (!hasFiles(event)) return
  dragDepth = Math.max(0, dragDepth - 1)
  if (dragDepth === 0) dragging.value = false
}

async function onDrop(event) {
  event.preventDefault()
  dragDepth = 0
  dragging.value = false
  const file = event.dataTransfer?.files?.[0]
  if (!file) return

  // 拖入 .wcproj 工程文件：直接读取内容加载工程
  if (file.name.toLowerCase().endsWith('.wcproj')) {
    await openProjectFromFile(file)
    return
  }

  if (busy.value) {
    setStatus('当前有任务在进行，请等待完成后再导入新文件', 'error')
    return
  }
  await handleSelectFile(file)
}

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  try {
    const info = await health()
    apiVersion.value = String(info?.version ?? '')
    setStatus(
      `后端已连接（v${info?.version ?? '?'}，内存中 ${info?.project_count ?? 0} 个项目），请选择音视频文件`,
      'success',
    )
  } catch (error) {
    setStatus(`后端未就绪：${error.message}。请先运行 uvicorn api:app --port 8000`, 'error')
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  if (audioUrl.value) URL.revokeObjectURL(audioUrl.value)
  stopAudition()
  if (auditionUrl) URL.revokeObjectURL(auditionUrl)
})
</script>

<template>
  <div
    class="app-shell"
    @dragenter="onDragEnter"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <!-- 拖拽提示遮罩 -->
    <div v-if="dragging" class="drop-overlay">
      <div class="drop-hint">
        <span class="drop-icon">⬇</span>
        <strong>松手导入</strong>
        <span class="drop-sub">支持音频 / 视频（mp4、mov、mkv、mp3、wav、flac、m4a…）</span>
        <span class="drop-sub">也可拖入 .wcproj 工程文件直接打开</span>
      </div>
    </div>

    <!-- L1：菜单栏从命令注册表渲染 -->
    <MenuBar :ctx="commandCtx" @run="handleMenuRun" />

    <Toolbar
      ref="toolbarRef"
      :project-name="projectName"
      :busy-upload="uploading"
      :busy-transcribe="transcribing"
      :busy-export="exporting"
      :has-project="Boolean(project)"
      :has-tokens="hasTokens"
      @update-project-name="projectName = $event"
      @select-file="handleSelectFile"
      @upload="handleUpload"
      @transcribe="handleTranscribe"
      @export="handleExport"
    />

    <div class="layout">
      <main class="main-column">
        <!-- L2：波形吸顶固定 180px，不随文本滚动 -->
        <div v-show="waveformVisible" class="wave-dock">
          <WaveformView
            :audio-url="audioUrl"
            :deletes="deletes"
            :peaks="waveformPeaks"
            :duration="waveformDuration"
          />
        </div>

        <!-- I1 / I2：分段文本流，独立滚动 -->
        <div class="editor-dock">
          <div v-if="searchOpen" class="search-bar">
            <input
              ref="searchInputRef"
              v-model="searchQuery"
              class="search-input"
              type="text"
              placeholder="搜索关键词（跨词也能匹配）"
              @keydown.enter.prevent="nextMatch"
              @keydown.shift.enter.prevent="prevMatch"
              @keydown.esc.prevent="closeSearch"
            />
            <span class="search-count">
              {{ searchQuery ? `${searchMatches.length ? searchIndex + 1 : 0}/${searchCount}` : '—' }}
            </span>
            <button class="btn btn-small" type="button" :disabled="!searchCount" @click="prevMatch">
              ↑
            </button>
            <button class="btn btn-small" type="button" :disabled="!searchCount" @click="nextMatch">
              ↓
            </button>
            <button
              class="btn btn-small btn-danger"
              type="button"
              :disabled="!searchCount || busy"
              title="把当前所有匹配的 Token 标记为删除"
              @click="deleteSearchMatches"
            >
              删除全部匹配
            </button>
            <button class="btn btn-small" type="button" @click="closeSearch">×</button>
          </div>

          <TextEditor
            :tokens="tokens"
            :segments="segments"
            :deleted-ids="deletedIds"
            :pending-ids="pendingIds"
            :cursor-index="cursorIndex"
            :selection-anchor="selectionAnchor"
            :active-index="activeTokenIndex"
            :disabled="transcribing || exporting"
            @cursor-change="handleCursorChange"
            @restore="handleRestoreToken"
            @seek="handleEditorSeek"
          />
        </div>
      </main>

      <!-- L3：可折叠参数面板（240px，默认收起），内容后定 -->
      <aside v-show="panelOpen" class="side-panel">
        <header class="panel-header">
          <span class="panel-title">参数</span>
          <button
            class="panel-close"
            type="button"
            title="收起参数面板（Ctrl+B）"
            @click="panelOpen = false"
          >
            ×
          </button>
        </header>

        <div class="panel-body">
          <section class="panel-section">
            <h3 class="panel-section-title">项目信息</h3>
            <dl class="panel-list">
              <div class="panel-row">
                <dt>项目名</dt>
                <dd>{{ project?.name || '未创建' }}</dd>
              </div>
              <div class="panel-row">
                <dt>项目 ID</dt>
                <dd class="mono">{{ project?.id || '—' }}</dd>
              </div>
              <div class="panel-row">
                <dt>采样率</dt>
                <dd class="mono">{{ project ? `${project.sample_rate} Hz` : '—' }}</dd>
              </div>
              <div class="panel-row">
                <dt>源时长</dt>
                <dd class="mono">{{ formatSeconds(sourceDuration) }}</dd>
              </div>
              <div class="panel-row">
                <dt>输出时长</dt>
                <dd class="mono">{{ formatSeconds(outputDuration) }}</dd>
              </div>
              <div class="panel-row">
                <dt>段落 / Token</dt>
                <dd class="mono">{{ segments.length }} / {{ tokens.length }}</dd>
              </div>
              <div class="panel-row">
                <dt>已删除</dt>
                <dd class="mono">{{ deletedCount }} 个（{{ formatSeconds(totalDeleted) }}）</dd>
              </div>
            </dl>
          </section>

          <!-- 音频后处理：增益 / 标准化 / 首尾淡入淡出（导出时生效，不改源文件） -->
          <section class="panel-section">
            <h3 class="panel-section-title">音频后处理</h3>
            <div class="param-field">
              <label class="param-label" for="param-denoise">
                <input
                  id="param-denoise"
                  v-model="audioParams.denoise"
                  class="param-check"
                  type="checkbox"
                />
                降噪
              </label>
              <span class="param-hint">ffmpeg afftdn，压制底噪（导出时处理，较慢）</span>
            </div>

            <div class="param-field" :class="{ 'param-disabled': !audioParams.denoise }">
              <label class="param-label" for="param-denoise-db">
                噪声底噪
                <span class="param-value">{{ Number(audioParams.denoise_db).toFixed(0) }} dB</span>
              </label>
              <input
                id="param-denoise-db"
                v-model.number="audioParams.denoise_db"
                class="param-range"
                type="range"
                :min="audioLimits.min_denoise_db"
                :max="audioLimits.max_denoise_db"
                step="1"
                :disabled="!audioParams.denoise"
              />
            </div>

            <div class="param-field">
              <label class="param-label" for="param-normalize">
                <input
                  id="param-normalize"
                  v-model="audioParams.normalize"
                  class="param-check"
                  type="checkbox"
                />
                峰值标准化
              </label>
              <span class="param-hint">拉到 -1 dBFS</span>
            </div>

            <div class="param-field" :class="{ 'param-disabled': audioParams.normalize }">
              <label class="param-label" for="param-gain">
                增益
                <span class="param-value">{{ Number(audioParams.gain_db).toFixed(1) }} dB</span>
              </label>
              <input
                id="param-gain"
                v-model.number="audioParams.gain_db"
                class="param-range"
                type="range"
                :min="audioLimits.min_gain_db"
                :max="audioLimits.max_gain_db"
                step="0.5"
                :disabled="audioParams.normalize"
              />
            </div>

            <div class="param-field">
              <label class="param-label" for="param-fade-in">
                淡入
                <span class="param-value">{{ Number(audioParams.fade_in).toFixed(2) }} s</span>
              </label>
              <input
                id="param-fade-in"
                v-model.number="audioParams.fade_in"
                class="param-range"
                type="range"
                min="0"
                :max="audioLimits.max_fade_seconds"
                step="0.05"
              />
            </div>

            <div class="param-field">
              <label class="param-label" for="param-fade-out">
                淡出
                <span class="param-value">{{ Number(audioParams.fade_out).toFixed(2) }} s</span>
              </label>
              <input
                id="param-fade-out"
                v-model.number="audioParams.fade_out"
                class="param-range"
                type="range"
                min="0"
                :max="audioLimits.max_fade_seconds"
                step="0.05"
              />
            </div>

            <button
              class="btn btn-small param-apply"
              type="button"
              :disabled="!project?.id || savingParams"
              @click="handleSaveAudioParams"
            >
              {{ savingParams ? '保存中…' : '应用参数' }}
            </button>
          </section>

          <!-- 音频信息统计：源音频 与 当前编辑后的成片 -->
          <section class="panel-section">
            <h3 class="panel-section-title">
              音频统计
              <button
                class="btn btn-small btn-link"
                type="button"
                :disabled="!project?.id || loadingInfo"
                @click="loadAudioInfo"
              >
                {{ loadingInfo ? '统计中…' : '刷新' }}
              </button>
            </h3>
            <template v-if="audioInfo">
              <p class="panel-note">源素材只读，不随参数变化；只看「成片」那一栏。</p>
              <p v-if="clipWarning" class="panel-warn">
                ⚠ 成片有 {{ audioInfo.edited?.clipped_samples }} 个样本削顶（超出 0 dBFS），
                听感会破音。建议勾选「峰值标准化」或降低增益。
              </p>
              <dl class="panel-list">
                <div class="panel-row panel-row-sub">
                  <dt>源 · 时长</dt>
                  <dd class="mono">{{ formatSeconds(audioInfo.source?.duration) }}</dd>
                </div>
                <div class="panel-row">
                  <dt>源 · 峰值 / RMS</dt>
                  <dd class="mono">
                    {{ formatDbfs(audioInfo.source?.peak_dbfs) }} /
                    {{ formatDbfs(audioInfo.source?.rms_dbfs) }}
                  </dd>
                </div>
                <div class="panel-row">
                  <dt>源 · 静音占比</dt>
                  <dd class="mono">{{ formatPercent(audioInfo.source?.silence_ratio) }}</dd>
                </div>
                <template v-if="audioInfo.edited">
                  <div class="panel-row panel-row-hl">
                    <dt>成片 · 时长</dt>
                    <dd class="mono">{{ formatSeconds(audioInfo.edited?.duration) }}</dd>
                  </div>
                  <div class="panel-row panel-row-hl">
                    <dt>成片 · 峰值 / RMS</dt>
                    <dd class="mono">
                      {{ formatDbfs(audioInfo.edited?.peak_dbfs) }} /
                      {{ formatDbfs(audioInfo.edited?.rms_dbfs) }}
                    </dd>
                  </div>
                  <div class="panel-row panel-row-hl">
                    <dt>成片 · 波峰因数</dt>
                    <dd class="mono">{{ formatDbfs(audioInfo.edited?.crest_factor_db) }}</dd>
                  </div>
                  <div class="panel-row panel-row-hl">
                    <dt>成片 · 静音占比</dt>
                    <dd class="mono">{{ formatPercent(audioInfo.edited?.silence_ratio) }}</dd>
                  </div>
                  <div class="panel-row panel-row-hl">
                    <dt>成片 · 削顶样本</dt>
                    <dd
                      class="mono"
                      :class="{ 'param-warn': Number(audioInfo.edited?.clipped_samples) > 0 }"
                    >
                      {{ audioInfo.edited?.clipped_samples ?? 0 }}
                    </dd>
                  </div>
                  <div class="panel-row">
                    <dt>统计所用参数</dt>
                    <dd class="mono param-summary">{{ paramSummary }}</dd>
                  </div>
                </template>
              </dl>
            </template>
            <p v-else class="panel-placeholder">
              {{ loadingInfo ? '正在统计音频指标…' : '创建或打开项目后显示音频指标。' }}
            </p>
          </section>

          <!-- 预留：参数面板插槽（分段策略 / 剪辑点精修 / 主题等后定） -->
          <section class="panel-section">
            <h3 class="panel-section-title">预留</h3>
            <p class="panel-placeholder">
              预留区域：分段策略、剪辑点精修、主题等后续在此挂载。
            </p>
          </section>
        </div>
      </aside>
    </div>

    <!-- L1：底部状态栏（源时长 / 输出时长 / 删除 Token / 删除总时长 / 版本号） -->
    <footer class="statusbar">
      <span class="status-item status-message" :class="`status-${status.type}`" :title="status.text">
        <span class="status-dot" />
        <span class="status-text">{{ status.text || '就绪' }}</span>
      </span>
      <span class="status-sep">|</span>
      <span class="status-item">
        <span class="status-key">源时长</span>
        <span class="status-value">{{ formatSeconds(sourceDuration) }}</span>
      </span>
      <span class="status-sep">|</span>
      <span class="status-item">
        <span class="status-key">输出时长</span>
        <span class="status-value status-accent">{{ formatSeconds(outputDuration) }}</span>
      </span>
      <span class="status-sep">|</span>
      <span class="status-item">
        <span class="status-key">删除 Token</span>
        <span class="status-value" :class="{ 'status-danger': deletedCount > 0 }">
          {{ deletedCount }} / {{ tokens.length }}
        </span>
      </span>
      <span class="status-sep">|</span>
      <span class="status-item">
        <span class="status-key">删除总时长</span>
        <span class="status-value" :class="{ 'status-danger': totalDeleted > 0 }">
          {{ formatSeconds(totalDeleted) }}
        </span>
      </span>
      <span class="status-spacer" />
      <span v-if="auditionPlaying" class="status-item status-accent">▶ 试听剪辑结果</span>
      <span class="status-item status-muted">
        <span class="status-key">播放头</span>
        <span class="status-value">
          {{ formatClock(playback.state.currentTime) }} / {{ formatClock(playback.state.duration) }}
        </span>
      </span>
      <span class="status-sep">|</span>
      <span v-if="busy" class="status-item status-muted">处理中…</span>
      <span class="status-item status-version" :title="project?.id ?? ''">
        WordCut v{{ apiVersion || '0.1.0' }}
      </span>
    </footer>

    <!-- 导出对话框 -->
    <ExportDialog
      :open="exportDialogOpen"
      :has-source-video="Boolean(project?.source_kind === 'video')"
      :busy="exporting"
      :result="exportInfo"
      @close="exportDialogOpen = false"
      @confirm="handleExportConfirm"
    />

    <!-- 项目管理对话框（打开 / 重命名 / 删除） -->
    <ProjectDialog
      v-if="projectDialogOpen"
      @close="projectDialogOpen = false"
      @changed="handleProjectDialogChanged"
      @open="handleOpenProjectById"
    />
  </div>
</template>

<style scoped>
.drop-overlay {
  position: fixed;
  inset: 0;
  z-index: 999;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(30, 30, 30, 0.86);
  backdrop-filter: blur(2px);
  pointer-events: none;
}

.drop-hint {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 42px 64px;
  border: 2px dashed var(--accent);
  border-radius: 12px;
  background: rgba(59, 130, 246, 0.08);
  color: var(--text-strong);
  font-size: 16px;
}

.drop-icon {
  font-size: 34px;
  line-height: 1;
  color: var(--accent);
}

.drop-sub {
  font-size: 12px;
  color: var(--text-dim);
}

.app-shell {
  display: flex;
  flex-direction: column;
  height: 100vh;
  min-height: 0;
  background: var(--bg);
}

.layout {
  position: relative;
  flex: 1 1 auto;
  display: flex;
  min-height: 0;
}

.main-column {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  background: var(--bg);
}

/* 波形吸顶：固定 180px，不参与下方文本滚动。 */
.wave-dock {
  position: sticky;
  top: 0;
  z-index: 5;
  flex: 0 0 180px;
  height: 180px;
  min-height: 180px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-panel);
}

.editor-dock {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}

/* 关键词搜索栏（文本编辑区顶部，不占用波形区域） */
.search-bar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: var(--bg-toolbar);
  border-bottom: 1px solid var(--border);
}

.search-input {
  flex: 1 1 auto;
  min-width: 0;
  height: 24px;
  padding: 0 8px;
  font-family: inherit;
  font-size: 12px;
  color: var(--text);
  background: var(--bg-input, rgba(255, 255, 255, 0.06));
  border: 1px solid var(--border-strong);
  border-radius: 3px;
  outline: none;
}

.search-input:focus {
  border-color: var(--accent, #4f8cff);
}

.search-count {
  flex: 0 0 auto;
  min-width: 52px;
  text-align: center;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-dim);
}

.btn-danger {
  color: #ffb4ae;
  border-color: rgba(229, 83, 75, 0.5);
}

.btn-danger:hover:not(:disabled) {
  background: rgba(229, 83, 75, 0.18);
}

/* 右侧参数面板（240px，默认收起）。 */
.side-panel {
  display: flex;
  flex-direction: column;
  flex: 0 0 240px;
  width: 240px;
  min-height: 0;
  background: var(--bg-panel);
  border-left: 1px solid var(--border);
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex: 0 0 auto;
  height: 30px;
  padding: 0 10px;
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border);
}

.panel-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-strong);
}

.panel-close {
  width: 18px;
  height: 18px;
  padding: 0;
  font-size: 14px;
  line-height: 1;
  color: var(--text-dim);
  background: transparent;
  border: none;
  border-radius: 3px;
  cursor: pointer;
}

.panel-close:hover {
  color: var(--text-strong);
  background: #3a3a40;
}

.panel-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 10px;
}

.panel-section {
  margin-bottom: 14px;
}

.panel-section-title {
  margin: 0 0 6px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-dim);
  letter-spacing: 0.3px;
}

.panel-list {
  margin: 0;
  font-size: 12px;
}

.panel-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 3px 0;
  border-bottom: 1px dashed var(--border);
}

.panel-row dt {
  color: var(--text-faint);
  white-space: nowrap;
}

.panel-row dd {
  margin: 0;
  color: var(--text);
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
}

.panel-row dd.mono {
  font-family: var(--mono);
  font-size: 11.5px;
}

.panel-placeholder {
  margin: 0;
  padding: 10px;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--text-faint);
  border: 1px dashed var(--border-strong);
  border-radius: 4px;
}

/* 参数面板：音频后处理控件 */
.param-field {
  margin-bottom: 8px;
}

.param-field.param-disabled {
  opacity: 0.45;
}

.param-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  font-size: 11.5px;
  color: var(--text-dim);
  margin-bottom: 3px;
  cursor: pointer;
}

.param-label .param-check {
  margin-right: 5px;
}

.param-value {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text);
}

.param-range {
  width: 100%;
  height: 16px;
  accent-color: var(--accent, #4f8cff);
  cursor: pointer;
}

.param-range:disabled {
  cursor: not-allowed;
}

.param-hint {
  font-size: 10.5px;
  color: var(--text-faint);
}

.param-apply {
  width: 100%;
  margin-top: 2px;
}

.btn-link {
  padding: 0 4px;
  background: transparent;
  border: none;
  color: var(--accent, #4f8cff);
  font-size: 10.5px;
  font-weight: 400;
  cursor: pointer;
}

.btn-link:disabled {
  color: var(--text-faint);
  cursor: not-allowed;
}

.param-warn {
  color: var(--danger, #e5534b);
}

/* 削顶警告条：成片峰值超过 0 dBFS 时出现 */
.panel-warn {
  margin: 0 0 8px;
  padding: 6px 8px;
  font-size: 10.5px;
  line-height: 1.6;
  color: var(--danger, #e5534b);
  background: rgba(229, 83, 75, 0.12);
  border: 1px solid rgba(229, 83, 75, 0.4);
  border-radius: 4px;
}

/* 统计表：源 / 成片 两组口径的视觉区分 */
.panel-note {
  margin: 0 0 6px;
  font-size: 10.5px;
  line-height: 1.6;
  color: var(--text-faint);
}

.panel-row-sub dt,
.panel-row-sub dd {
  color: var(--text-faint);
}

.panel-row-hl dt {
  color: var(--text-dim);
}

.panel-row-hl dd {
  color: var(--text-strong);
  font-weight: 600;
}

.param-summary {
  font-size: 10.5px;
  white-space: normal;
  word-break: break-all;
}

/* 底部状态栏 */
.statusbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
  height: 28px;
  padding: 0 12px;
  background: var(--bg-toolbar);
  border-top: 1px solid var(--border);
  font-size: 12px;
  color: var(--text-dim);
  white-space: nowrap;
  overflow: hidden;
}

.status-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex: 0 0 auto;
}

.status-message {
  min-width: 0;
  flex: 0 1 auto;
  overflow: hidden;
}

.status-text {
  overflow: hidden;
  text-overflow: ellipsis;
}

.status-dot {
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-faint);
}

.status-success .status-dot {
  background: var(--success);
}

.status-error .status-dot {
  background: var(--danger);
}

.status-busy .status-dot {
  background: var(--accent);
  animation: status-pulse 1s ease-in-out infinite;
}

.status-success .status-text {
  color: var(--success);
}

.status-error .status-text {
  color: var(--danger);
}

.status-busy .status-text {
  color: var(--accent);
}

@keyframes status-pulse {
  0%,
  100% {
    opacity: 0.35;
  }
  50% {
    opacity: 1;
  }
}

.status-key {
  color: var(--text-faint);
}

.status-value {
  font-family: var(--mono);
  color: var(--text);
}

.status-accent {
  color: var(--accent);
}

.status-danger {
  color: var(--danger);
}

.status-sep {
  color: var(--border-strong);
}

.status-spacer {
  flex: 1 1 auto;
}

.status-muted {
  color: var(--text-faint);
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.status-version {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-faint);
}

/* 窄屏：参数面板改为覆盖在文本之上，避免挤压编辑区。 */
@media (max-width: 900px) {
  .side-panel {
    position: absolute;
    top: 68px;
    right: 0;
    bottom: 28px;
    z-index: 20;
    box-shadow: -6px 0 18px rgba(0, 0, 0, 0.45);
  }
}
</style>

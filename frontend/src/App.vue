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
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { createHistory, createTokenEditEntry } from './core/history.js'
import MenuBar from './components/MenuBar.vue'
import Toolbar from './components/Toolbar.vue'
import ExportDialog from './components/ExportDialog.vue'

import TextEditor from './components/TextEditor.vue'
import WaveformView from './components/WaveformView.vue'
import { registerCommands, runCommand, shortcutFromEvent, shortcutMap } from './core/commands.js'
import { formatClock, usePlayback } from './composables/usePlayback.js'
import {
  createProject,
  deleteTokens,
  detectSilences,
  exportFileUrl,
  exportProject,
  fetchTokens,
  buildProjectFile,
  health,
  loadProject,
  loadProjectFile,
  restoreTokens,
  sourceAudioUrl,
  saveProject,
  transcribeProject,
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
/** 工程文件保存中 */
const saving = ref(false)
/** 正在检测静音 */
const detecting = ref(false)
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
    setStatus(isDelete ? '选中的 Token 已全部删除' : '选中的 Token 没有可恢复的', 'info')
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
    await performEdit(selectionTokenIds(), 'delete', { nextCursor: range.start })
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

/** Backspace：选区优先，否则删光标前一个 Token。 */
async function handleBackspace() {
  if (!hasTokens.value) return
  if (busy.value) return
  const range = selectionRange.value
  if (!range.empty) {
    await performEdit(selectionTokenIds(), 'delete', { nextCursor: range.start })
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
  history.clear()
  cursorIndex.value = 0
  selectionAnchor.value = null
  playback.setDeletes([])
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
    const result = await transcribeProject(project.value.id)
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
      ).toFixed(1)} 秒`,
      'success',
    )
  } catch (error) {
    setStatus(`转写失败：${error.message}`, 'error')
  } finally {
    transcribing.value = false
  }
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
    return
  }

  setStatus(
    audioRestored
      ? `已打开「${loaded.name}」：${tokens.value.length} 个 Token，删除 ${deletedIds.value.length} 个，音频已恢复`
      : `已打开「${loaded.name}」：${tokens.value.length} 个 Token（音频未恢复，请重新导入源文件）`,
    audioRestored ? 'success' : 'error',
  )
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
    const result = await exportProject(project.value.id, options)
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
            : `（${files.length} 个文件，服务器路径见导出结果）`),
      'success',
    )
  } catch (error) {
    setStatus(`导出失败：${error.message}`, 'error')
  } finally {
    exporting.value = false
  }
}

/** 把服务端产出的文件取回浏览器，让用户选择保存目录后写入本地。 */
async function saveExportFilesToUser(result) {
  const paths = result?.paths || {}
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
    id: 'edit.removeSilences',
    title: '检测静音并批量删除…',
    menu: 'edit',
    handler: () => handleRemoveSilences(),
    enabled: () => Boolean(project.value) && hasTokens.value && !busy.value,
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
          <WaveformView :audio-url="audioUrl" :deletes="deletes" />
        </div>

        <!-- I1 / I2：分段文本流，独立滚动 -->
        <div class="editor-dock">
          <TextEditor
            :tokens="tokens"
            :segments="segments"
            :deleted-ids="deletedIds"
            :pending-ids="pendingIds"
            :cursor-index="cursorIndex"
            :selection-anchor="selectionAnchor"
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

          <!-- 预留：参数面板插槽（分段策略 / 剪辑点精修 / 导出参数 / 主题等后定） -->
          <section class="panel-section">
            <h3 class="panel-section-title">参数</h3>
            <p class="panel-placeholder">
              预留区域（240px 占位）：分段策略、剪辑点精修、导出参数、主题等后续在此挂载。
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
  min-height: 0;
  overflow: hidden;
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

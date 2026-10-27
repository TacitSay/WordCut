<script setup>
/**
 * WordCut 主界面：顶部工具栏 + 三栏（左 60% 文本编辑 / 右 40% 波形）+ 底部状态栏。
 *
 * 数据流：
 *   选择文件 → 上传(POST /api/projects) → 转写(POST .../transcribe 再 GET .../tokens)
 *   → 点击 Token(POST .../delete | .../restore → EditStateResponse)
 *   → 导出(POST .../export → ExportResponse)
 *
 * 字段名严格按 src/api.py 的 Pydantic 模型消费，时间单位统一为秒。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import Toolbar from './components/Toolbar.vue'
import TextEditor from './components/TextEditor.vue'
import WaveformView from './components/WaveformView.vue'
import {
  createProject,
  deleteTokens,
  exportProject,
  fetchTokens,
  health,
  restoreTokens,
  transcribeProject,
} from './api/client.js'

// ----------------------------------------------------------------------
// 状态
// ----------------------------------------------------------------------
const projectName = ref('')
/** ProjectCreated：{ id, name, sample_rate, duration } */
const project = ref(null)
/** 用户选择的本地音频文件（同时用于波形渲染与上传） */
const audioFile = ref(null)
/** 源音频的 blob URL */
const audioUrl = ref('')
/** TokenOut[]：{ id, text, start, end, is_deleted, confidence } */
const tokens = ref([])
/** 被删 Token id 列表 */
const deletedIds = ref([])
/** DeleteSpanOut[]：{ start, end, token_id, cause } */
const deletes = ref([])
/** 删除总时长（秒，已合并重叠） */
const totalDeleted = ref(0)
/** 导出结果（ExportResponse），用于展示输出时长与落盘路径 */
const exportInfo = ref(null)

const uploading = ref(false)
const transcribing = ref(false)
const exporting = ref(false)
/** 正在请求删除 / 恢复的 Token id */
const pendingIds = ref([])
/** 状态提示：{ text, type } */
const status = ref({ text: '请选择音频文件并填写项目名', type: 'info' })

// ----------------------------------------------------------------------
// 派生值
// ----------------------------------------------------------------------
const sourceDuration = computed(() => Number(project.value?.duration ?? 0))
const outputDuration = computed(() =>
  Math.max(0, sourceDuration.value - Number(totalDeleted.value || 0)),
)
const deletedCount = computed(() => deletedIds.value.length)
const busy = computed(
  () => uploading.value || transcribing.value || exporting.value || pendingIds.value.length > 0,
)
const hasTokens = computed(() => tokens.value.length > 0)

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
// 交互
// ----------------------------------------------------------------------
/** 选择本地文件：立刻生成 blob URL 供波形使用（上传后仍是同一个文件）。 */
function handleSelectFile(file) {
  if (!file) return
  if (audioUrl.value) URL.revokeObjectURL(audioUrl.value)
  audioFile.value = file
  audioUrl.value = URL.createObjectURL(file)
  if (!projectName.value.trim()) {
    projectName.value = file.name.replace(/\.[^.\\/]+$/, '')
  }
  setStatus(`已选择音频：${file.name}（${(file.size / 1024 / 1024).toFixed(2)} MB），请点击「上传」`, 'info')
}

async function handleUpload() {
  if (uploading.value) return
  const name = projectName.value.trim()
  if (!audioFile.value) {
    setStatus('请先选择音频文件', 'error')
    return
  }
  if (!name) {
    setStatus('请填写项目名', 'error')
    return
  }

  uploading.value = true
  setStatus('正在上传音频（后端会转码为 16kHz 单声道 WAV）…', 'busy')
  try {
    const created = await createProject({ file: audioFile.value, name })
    project.value = created
    tokens.value = []
    deletedIds.value = []
    deletes.value = []
    totalDeleted.value = 0
    exportInfo.value = null
    setStatus(
      `上传成功：项目「${created.name}」（${formatSeconds(created.duration)}，${created.sample_rate} Hz），请点击「转写」`,
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
    setStatus('请先上传音频创建项目', 'error')
    return
  }
  if (transcribing.value) return

  transcribing.value = true
  setStatus('正在转写（首次加载模型可能需要十几秒）…', 'busy')
  try {
    const result = await transcribeProject(project.value.id)
    const list = await fetchTokens(project.value.id)
    tokens.value = Array.isArray(list?.tokens) ? [...list.tokens] : []
    deletedIds.value = Array.isArray(list?.deleted_ids) ? [...list.deleted_ids] : []
    deletes.value = buildDeletesFromTokens(tokens.value, deletedIds.value)
    totalDeleted.value = totalOf(deletes.value)
    exportInfo.value = null
    setStatus(
      `转写完成：${tokens.value.length} 个词级单元，耗时 ${Number(result?.elapsed_seconds ?? 0).toFixed(1)} 秒`,
      'success',
    )
  } catch (error) {
    setStatus(`转写失败：${error.message}`, 'error')
  } finally {
    transcribing.value = false
  }
}

/** 点击 Token：已删则恢复，未删则删除。 */
async function handleToggleToken(item) {
  if (!project.value) {
    setStatus('项目尚未创建，无法编辑', 'error')
    return
  }
  if (!item?.id || pendingIds.value.includes(item.id)) return

  pendingIds.value = [...pendingIds.value, item.id]
  try {
    const state = item.deleted
      ? await restoreTokens(project.value.id, [item.id])
      : await deleteTokens(project.value.id, [item.id])
    applyEditState(state)
    setStatus(
      item.deleted
        ? `已恢复「${item.glyph || item.id}」`
        : `已删除「${item.glyph || item.id}」（当前共删除 ${deletedCount.value} 个 Token）`,
      'info',
    )
  } catch (error) {
    setStatus(`操作失败：${error.message}`, 'error')
  } finally {
    pendingIds.value = pendingIds.value.filter((id) => id !== item.id)
  }
}

async function handleExport() {
  if (!project.value) {
    setStatus('请先上传并转写', 'error')
    return
  }
  if (!hasTokens.value) {
    setStatus('请先转写，再导出', 'error')
    return
  }
  if (exporting.value) return

  exporting.value = true
  setStatus('正在导出目标音频 / 字幕 / EDL…', 'busy')
  try {
    const result = await exportProject(project.value.id)
    exportInfo.value = result
    const total = Number(result?.total_deleted)
    if (Number.isFinite(total)) totalDeleted.value = total
    const audioPath = result?.paths?.audio_path ?? ''
    setStatus(
      `导出完成：目标音频 ${formatSeconds(result?.output_duration)}，删除 ${result?.deleted_count} 个 Token（${formatSeconds(result?.total_deleted)}）` +
        (audioPath ? ` → ${audioPath}` : ''),
      'success',
    )
  } catch (error) {
    setStatus(`导出失败：${error.message}`, 'error')
  } finally {
    exporting.value = false
  }
}

/** 启动时探测后端是否可用，避免用户面对一个静默失败的界面。 */
onMounted(async () => {
  try {
    const info = await health()
    setStatus(
      `后端已连接（v${info?.version ?? '?'}，内存中 ${info?.project_count ?? 0} 个项目），请选择音频文件`,
      'success',
    )
  } catch (error) {
    setStatus(`后端未就绪：${error.message}。请先运行 uvicorn api:app --port 8000`, 'error')
  }
})

onBeforeUnmount(() => {
  if (audioUrl.value) URL.revokeObjectURL(audioUrl.value)
})
</script>

<template>
  <div class="app-shell">
    <Toolbar
      :project-name="projectName"
      :busy-upload="uploading"
      :busy-transcribe="transcribing"
      :busy-export="exporting"
      :has-project="Boolean(project)"
      :has-tokens="hasTokens"
      :message="status.text"
      :message-type="status.type"
      @update-project-name="projectName = $event"
      @select-file="handleSelectFile"
      @upload="handleUpload"
      @transcribe="handleTranscribe"
      @export="handleExport"
    />

    <main class="workspace">
      <div class="pane pane-text">
        <TextEditor
          :tokens="tokens"
          :deleted-ids="deletedIds"
          :pending-ids="pendingIds"
          :disabled="transcribing || exporting"
          @toggle="handleToggleToken"
        />
      </div>

      <div class="pane pane-wave">
        <WaveformView :audio-url="audioUrl" :deletes="deletes" />
      </div>
    </main>

    <footer class="statusbar">
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
      <span class="status-item status-muted" :title="project?.id ?? ''">
        {{ project ? `项目 ${project.id} · ${project.sample_rate} Hz` : '未创建项目' }}
      </span>
      <span v-if="exportInfo" class="status-item status-muted" :title="exportInfo.paths?.audio_path ?? ''">
        已导出 {{ formatSeconds(exportInfo.output_duration) }}
      </span>
      <span v-if="busy" class="status-item status-muted">处理中…</span>
    </footer>
  </div>
</template>

<style scoped>
.app-shell {
  display: flex;
  flex-direction: column;
  height: 100vh;
  min-height: 0;
}

.workspace {
  flex: 1 1 auto;
  display: flex;
  min-height: 0;
}

.pane {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  background: var(--bg);
}

/* 左 60%：文本编辑区 */
.pane-text {
  flex: 0 0 60%;
  border-right: 1px solid var(--border);
}

/* 右 40%：波形视图 */
.pane-wave {
  flex: 1 1 40%;
  background: var(--bg-panel);
}

.statusbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
  height: 28px;
  padding: 0 14px;
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
</style>

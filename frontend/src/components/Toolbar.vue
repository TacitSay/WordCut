<script setup>
/**
 * 顶部工具栏：项目名输入 + 选择音频 + 上传 + 转写 + 导出 + 状态提示。
 *
 * 只负责「收集输入 / 发出事件」，所有网络请求由 App.vue 统一调度。
 */
import { computed, ref } from 'vue'

const props = defineProps({
  /** 项目名（受控） */
  projectName: { type: String, default: '' },
  /** 正在上传 */
  busyUpload: { type: Boolean, default: false },
  /** 正在转写 */
  busyTranscribe: { type: Boolean, default: false },
  /** 正在导出 */
  busyExport: { type: Boolean, default: false },
  /** 是否已创建项目 */
  hasProject: { type: Boolean, default: false },
  /** 是否已有 Token（转写完成） */
  hasTokens: { type: Boolean, default: false },
  /** 状态提示文本 */
  message: { type: String, default: '' },
  /** 状态类型：info / success / error / busy */
  messageType: { type: String, default: 'info' },
})

const emit = defineEmits([
  'update-project-name',
  'select-file',
  'upload',
  'transcribe',
  'export',
])

const fileInput = ref(null)
const fileName = ref('')

const hasFile = computed(() => Boolean(fileName.value))
const canUpload = computed(
  () => hasFile.value && props.projectName.trim().length > 0 && !props.busyUpload,
)
const canTranscribe = computed(() => props.hasProject && !props.busyTranscribe)
const canExport = computed(() => props.hasTokens && !props.busyExport)

function pickFile() {
  fileInput.value?.click()
}

function onFileChange(event) {
  const input = event.target
  const file = input.files && input.files[0]
  if (!file) return
  fileName.value = file.name
  emit('select-file', file)
  // 允许重复选择同一个文件
  input.value = ''
}

function onNameInput(event) {
  emit('update-project-name', event.target.value)
}
</script>

<template>
  <header class="toolbar">
    <div class="toolbar-brand">
      <span class="brand-mark">WC</span>
      <span class="brand-name">WordCut</span>
    </div>

    <div class="toolbar-field">
      <label class="field-label" for="project-name">项目名</label>
      <input
        id="project-name"
        class="input input-name"
        type="text"
        placeholder="例如：访谈_第 3 集"
        :value="projectName"
        @input="onNameInput"
      />
    </div>

    <div class="toolbar-field">
      <button class="btn" type="button" title="选择本地音频文件" @click="pickFile">
        选择音频
      </button>
      <span
        class="file-name"
        :class="{ 'is-empty': !hasFile }"
        :title="fileName || '尚未选择音频文件'"
      >
        {{ hasFile ? fileName : '未选择文件' }}
      </span>
      <input
        ref="fileInput"
        class="hidden-input"
        type="file"
        accept="audio/*,.wav,.mp3,.flac,.ogg,.m4a,.aac"
        @change="onFileChange"
      />
    </div>

    <div class="toolbar-actions">
      <button
        class="btn btn-primary"
        type="button"
        :disabled="!canUpload"
        title="上传音频并创建项目"
        @click="emit('upload')"
      >
        {{ busyUpload ? '上传中…' : '上传' }}
      </button>
      <button
        class="btn"
        type="button"
        :disabled="!canTranscribe"
        title="用 faster-whisper 做词级对齐转写"
        @click="emit('transcribe')"
      >
        {{ busyTranscribe ? '转写中…' : '转写' }}
      </button>
      <button
        class="btn"
        type="button"
        :disabled="!canExport"
        title="导出目标音频 / 字幕 / EDL"
        @click="emit('export')"
      >
        {{ busyExport ? '导出中…' : '导出' }}
      </button>
    </div>

    <div class="toolbar-status" :class="`status-${messageType}`" :title="message">
      <span class="status-dot" />
      <span class="status-text">{{ message || '就绪' }}</span>
    </div>
  </header>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 14px;
  flex: 0 0 auto;
  padding: 8px 14px;
  background: var(--bg-toolbar);
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}

.toolbar-brand {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-right: 6px;
}

.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 4px;
  background: var(--accent);
  color: #ffffff;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.4px;
}

.brand-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-strong);
}

.toolbar-field {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.field-label {
  font-size: 12px;
  color: var(--text-dim);
}

.input-name {
  width: 190px;
}

.file-name {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 12px;
  color: var(--text-dim);
}

.file-name.is-empty {
  color: var(--text-faint);
  font-style: italic;
}

.hidden-input {
  display: none;
}

.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.toolbar-status {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
  min-width: 0;
  flex: 1 1 auto;
  font-size: 12px;
  color: var(--text-dim);
}

.status-text {
  overflow: hidden;
  text-overflow: ellipsis;
}

.status-dot {
  flex: 0 0 auto;
  width: 7px;
  height: 7px;
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
</style>

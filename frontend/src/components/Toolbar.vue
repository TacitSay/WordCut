<script setup>
/**
 * 顶部紧凑工具栏：项目名 + 选择文件 / 上传 / 转写 / 导出（图标 + 文字）。
 *
 * 定位（见 docs/界面问题清单.md L1）：
 *  - 分类命令交给 MenuBar（命令注册表驱动），这里只保留最高频的 4 个动作 + 项目名；
 *  - 只负责「收集输入 / 发出事件」，网络请求全部由 App.vue 统一调度；
 *  - 通过 defineExpose 暴露 pickFile()，让 file.import 命令（Ctrl+O）能复用同一个文件选择器。
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
})

const emit = defineEmits(['update-project-name', 'select-file', 'upload', 'transcribe', 'export'])

const fileInput = ref(null)
const fileName = ref('')

const hasFile = computed(() => Boolean(fileName.value))
const canUpload = computed(
  () => hasFile.value && props.projectName.trim().length > 0 && !props.busyUpload,
)
const canTranscribe = computed(() => props.hasProject && !props.busyTranscribe)
const canExport = computed(() => props.hasTokens && !props.busyExport)

/** 打开系统文件选择器（file.import / Ctrl+O 也调这个）。 */
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

defineExpose({ pickFile })
</script>

<template>
  <header class="toolbar">
    <div class="toolbar-brand">
      <span class="brand-mark">WC</span>
      <span class="brand-name">WordCut</span>
    </div>

    <label class="toolbar-field">
      <span class="field-label">项目</span>
      <input
        class="input input-name"
        type="text"
        placeholder="项目名，例如：访谈_第 3 集"
        :value="projectName"
        @input="onNameInput"
      />
    </label>

    <div class="toolbar-actions">
      <button class="tool-btn" type="button" title="选择音频/视频文件（Ctrl+O）" @click="pickFile">
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path
            d="M1.6 3.7h4.1l1.3 1.6h7.4v7.5a1 1 0 0 1-1 1H2.6a1 1 0 0 1-1-1z"
            fill="none"
            stroke="currentColor"
            stroke-width="1.3"
            stroke-linejoin="round"
          />
        </svg>
        <span>选择文件</span>
      </button>

      <button
        class="tool-btn tool-btn-primary"
        type="button"
        :disabled="!canUpload"
        title="上传音频并创建项目（后端统一转 16kHz 单声道 WAV）"
        @click="emit('upload')"
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path
            d="M8 11V2.8M8 2.8 5 5.8M8 2.8l3 3M2.8 12.4v1.1h10.4v-1.1"
            fill="none"
            stroke="currentColor"
            stroke-width="1.3"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
        <span>{{ busyUpload ? '上传中…' : '上传' }}</span>
      </button>

      <button
        class="tool-btn"
        type="button"
        :disabled="!canTranscribe"
        title="用 faster-whisper 做词级对齐转写（Ctrl+R）"
        @click="emit('transcribe')"
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path
            d="M1.8 8h1.9l1.5-4.2 2.1 9 2-6.4 1.4 3.4h1.5"
            fill="none"
            stroke="currentColor"
            stroke-width="1.3"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
        <span>{{ busyTranscribe ? '转写中…' : '转写' }}</span>
      </button>

      <button
        class="tool-btn"
        type="button"
        :disabled="!canExport"
        title="导出目标音频 / 字幕 / EDL（Ctrl+E）"
        @click="emit('export')"
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path
            d="M8 2.8v8.2M8 11 5 8M8 11l3-3M2.8 12.4v1.1h10.4v-1.1"
            fill="none"
            stroke="currentColor"
            stroke-width="1.3"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
        <span>{{ busyExport ? '导出中…' : '导出' }}</span>
      </button>
    </div>

    <span class="file-name" :class="{ 'is-empty': !hasFile }" :title="fileName || '尚未选择文件'">
      {{ hasFile ? fileName : '未选择文件' }}
    </span>

    <input
      ref="fileInput"
      class="hidden-input"
      type="file"
      accept="audio/*,video/*,.wav,.mp3,.flac,.ogg,.m4a,.aac,.mp4,.mov,.mkv"
      @change="onFileChange"
    />
  </header>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 0 0 auto;
  height: 38px;
  padding: 0 12px;
  background: var(--bg-toolbar);
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
  overflow: hidden;
}

.toolbar-brand {
  display: flex;
  align-items: center;
  gap: 7px;
  flex: 0 0 auto;
}

.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 4px;
  background: var(--accent);
  color: #ffffff;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.3px;
}

.brand-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-strong);
}

.toolbar-field {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex: 0 1 auto;
}

.field-label {
  font-size: 12px;
  color: var(--text-dim);
}

.input-name {
  width: 200px;
  min-height: 24px;
  padding: 2px 8px;
  font-size: 12.5px;
}

.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
}

/* 紧凑图标 + 文字按钮 */
.tool-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 9px;
  font-family: inherit;
  font-size: 12.5px;
  color: var(--text);
  background: #37373d;
  border: 1px solid var(--border-strong);
  border-radius: 4px;
  cursor: pointer;
  transition:
    background-color 0.12s ease,
    border-color 0.12s ease,
    color 0.12s ease;
}

.tool-btn:hover:not(:disabled) {
  background: #45454c;
  border-color: var(--accent);
  color: var(--text-strong);
}

.tool-btn:focus-visible {
  outline: 1px solid var(--accent);
  outline-offset: 1px;
}

.tool-btn:disabled {
  opacity: 0.42;
  cursor: not-allowed;
}

.tool-btn-primary {
  color: #ffffff;
  background: var(--accent);
  border-color: var(--accent);
}

.tool-btn-primary:hover:not(:disabled) {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
  color: #ffffff;
}

.file-name {
  margin-left: auto;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 11.5px;
  color: var(--text-dim);
}

.file-name.is-empty {
  color: var(--text-faint);
  font-style: italic;
}

.hidden-input {
  display: none;
}
</style>

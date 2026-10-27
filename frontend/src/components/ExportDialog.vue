<script setup>
/**
 * 导出对话框：选择音频格式、是否导出成品视频。
 *
 * 字段严格对齐后端 ExportRequest：
 *   { audio_format: 'wav'|'mp3'|'flac'|'m4a', export_video: bool, video_mode: 'precise' }
 */
import { computed, ref, watch } from 'vue'

const props = defineProps({
  /** 是否显示 */
  open: { type: Boolean, default: false },
  /** 项目是否来自视频上传（决定视频导出的可选项） */
  hasSourceVideo: { type: Boolean, default: false },
  /** 是否正在导出 */
  busy: { type: Boolean, default: false },
  /** 导出结果（ExportResponse），用于展示产物 */
  result: { type: Object, default: null },
})

const emit = defineEmits(['close', 'confirm'])

const audioFormat = ref('wav')
const exportVideo = ref(true)
const videoMode = ref('precise')

const AUDIO_FORMATS = [
  { value: 'wav', label: 'WAV（无损 PCM，体积大）', hint: '16kHz 单声道，剪辑精度最高' },
  { value: 'flac', label: 'FLAC（无损压缩，约 50% 体积）', hint: '音质与 WAV 相同，体积更小' },
  { value: 'mp3', label: 'MP3（VBR q2，通用）', hint: '兼容性最好，适合发布' },
  { value: 'm4a', label: 'M4A（AAC 192k）', hint: '苹果生态常用' },
]

const videoEnabled = computed(() => props.hasSourceVideo)

watch(
  () => props.open,
  (value) => {
    if (value) {
      exportVideo.value = props.hasSourceVideo
      videoMode.value = 'precise'
    }
  },
)

function confirm() {
  if (props.busy) return
  emit('confirm', {
    audio_format: audioFormat.value,
    export_video: videoEnabled.value && exportVideo.value,
    video_mode: videoMode.value,
  })
}

function fileName(path) {
  if (!path) return ''
  return String(path).split(/[\\/]/).pop()
}

function sizeText(path) {
  return fileName(path)
}
</script>

<template>
  <div v-if="open" class="dialog-mask" @click.self="emit('close')">
    <section class="dialog">
      <header class="dialog-header">
        <h2>导出</h2>
        <button class="dialog-x" type="button" @click="emit('close')">×</button>
      </header>

      <div class="dialog-body">
        <fieldset class="field-group">
          <legend>音频格式</legend>
          <label
            v-for="item in AUDIO_FORMATS"
            :key="item.value"
            class="radio-row"
            :class="{ active: audioFormat === item.value }"
          >
            <input v-model="audioFormat" type="radio" :value="item.value" />
            <span class="radio-label">{{ item.label }}</span>
            <span class="radio-hint">{{ item.hint }}</span>
          </label>
        </fieldset>

        <fieldset class="field-group" :class="{ disabled: !videoEnabled }">
          <legend>视频</legend>
          <label class="check-row" :class="{ disabled: !videoEnabled }">
            <input
              v-model="exportVideo"
              type="checkbox"
              :disabled="!videoEnabled"
            />
            <span class="radio-label">同时导出成品视频（.mp4）</span>
          </label>
          <p v-if="!videoEnabled" class="field-note">
            当前项目来自音频文件，没有原始视频可供剪辑。
          </p>
          <p v-else class="field-note">
            按删除区间剪辑原始视频，并用剪辑后的音频替换音轨。
          </p>

          <div v-if="videoEnabled && exportVideo" class="mode-row">
            <label class="radio-row small">
              <input v-model="videoMode" type="radio" value="precise" />
              <span class="radio-label">精确模式</span>
              <span class="radio-hint">剪辑点局部重编码，偏差约 160ms（推荐）</span>
            </label>
            <label class="radio-row small">
              <input v-model="videoMode" type="radio" value="fast" />
              <span class="radio-label">快速模式</span>
              <span class="radio-hint">已禁用：实测时间偏差可达 +24s，会自动回退精确模式</span>
            </label>
          </div>
        </fieldset>

        <section v-if="result" class="result-box">
          <h3>导出结果</h3>
          <ul class="result-list">
            <li>
              <span class="result-key">音频</span>
              <span class="result-value mono">{{ sizeText(result.paths?.audio_path) }}</span>
            </li>
            <li>
              <span class="result-key">字幕</span>
              <span class="result-value mono">{{ sizeText(result.paths?.srt_path) }}</span>
            </li>
            <li>
              <span class="result-key">EDL</span>
              <span class="result-value mono">{{ sizeText(result.paths?.edl_path) }}</span>
            </li>
            <li v-if="result.paths?.video_path">
              <span class="result-key">视频</span>
              <span class="result-value mono">{{ sizeText(result.paths?.video_path) }}</span>
            </li>
          </ul>
          <p class="result-meta">
            输出时长 {{ Number(result.output_duration ?? 0).toFixed(2) }}s
            <template v-if="result.video_info">
              · 视频 {{ result.video_info.segments }} 段 / {{ Math.round(result.video_info.elapsed_ms) }}ms
            </template>
          </p>
        </section>
      </div>

      <footer class="dialog-footer">
        <button class="btn" type="button" @click="emit('close')">关闭</button>
        <button class="btn btn-primary" type="button" :disabled="busy" @click="confirm">
          {{ busy ? '导出中…' : '开始导出' }}
        </button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.dialog-mask {
  position: fixed;
  inset: 0;
  z-index: 500;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.55);
}

.dialog {
  width: 520px;
  max-height: 84vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.6);
}

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.dialog-header h2 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-strong);
}

.dialog-x {
  width: 24px;
  height: 24px;
  font-size: 18px;
  line-height: 1;
  color: var(--text-dim);
  background: transparent;
  border: none;
  border-radius: 3px;
  cursor: pointer;
}

.dialog-x:hover {
  background: #3a3a40;
  color: var(--text-strong);
}

.dialog-body {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 14px 16px;
}

.field-group {
  margin: 0 0 16px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
}

.field-group.disabled {
  opacity: 0.62;
}

.field-group legend {
  padding: 0 6px;
  font-size: 12px;
  color: var(--text-dim);
}

.radio-row,
.check-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 5px 8px;
  border-radius: 4px;
  cursor: pointer;
}

.radio-row:hover,
.check-row:hover {
  background: rgba(255, 255, 255, 0.04);
}

.radio-row.active {
  background: var(--accent-dim);
}

.radio-row.small {
  padding: 4px 8px;
}

.radio-label {
  font-size: 13px;
  color: var(--text);
  white-space: nowrap;
}

.radio-hint {
  font-size: 11px;
  color: var(--text-faint);
}

.field-note {
  margin: 6px 8px 0;
  font-size: 11.5px;
  color: var(--text-faint);
  line-height: 1.5;
}

.mode-row {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--border);
}

.result-box {
  padding: 10px 12px;
  background: rgba(59, 130, 246, 0.08);
  border: 1px solid rgba(59, 130, 246, 0.28);
  border-radius: 6px;
}

.result-box h3 {
  margin: 0 0 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--accent);
}

.result-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.result-list li {
  display: flex;
  gap: 10px;
  padding: 2px 0;
  font-size: 12px;
}

.result-key {
  flex: 0 0 44px;
  color: var(--text-faint);
}

.result-value {
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.result-meta {
  margin: 8px 0 0;
  font-size: 11.5px;
  color: var(--text-dim);
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 10px 16px;
  border-top: 1px solid var(--border);
}

.mono {
  font-family: var(--mono);
}
</style>

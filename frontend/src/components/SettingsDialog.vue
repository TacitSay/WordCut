<script setup>
/**
 * 设置对话框：语义分段。
 *
 * 三种模式，对应"切段"这件事的三种做法：
 *   1. 规则（机器）分段 —— 标点 / 停顿 / 时长，零依赖、零内存；
 *   2. 本地小模型 —— ONNX 向量模型算句间落差；可换模型、可指定已有模型目录；
 *   3. API —— 把编号句子交给 OpenAI 兼容接口，由模型决定断点。
 *
 * 设计约定：
 *   - 保存是显式的（关掉不保存）；「测试」只验证不落盘；
 *   - API key 不回显：留空表示不修改，输入 `-` 表示清除；
 *   - 保存后由父组件决定是否立即重新分段。
 */
import { computed, ref, watch } from 'vue'
import {
  fetchSegmentSettings,
  saveSegmentSettings,
  testSegmentBackend,
} from '../api/client.js'

const props = defineProps({
  /** 是否显示 */
  open: { type: Boolean, default: false },
})

const emit = defineEmits(['close', 'saved'])

/** 三种模式（界面顺序即推荐顺序）。 */
const MODES = [
  {
    id: 'rule',
    name: '规则分段（机器）',
    detail: '按标点、停顿、时长切分。零依赖、零内存，段落偏碎但不会出错。',
  },
  {
    id: 'local',
    name: '本地小模型分段',
    detail: '本地 ONNX 向量模型判断句间语义落差。首次使用会下载模型（约 23MB）。',
  },
  {
    id: 'api',
    name: 'API 分段',
    detail: '把句子交给 OpenAI 兼容接口分段，质量最好，需要联网与密钥。',
  },
]

const loading = ref(false)
const saving = ref(false)
const testing = ref('')
const message = ref('')
const messageType = ref('info')

const form = ref({
  mode: 'rule',
  local_model: '',
  local_model_dir: '',
  models_dir: '',
  api_base: '',
  api_model: '',
  api_timeout: 60,
  api_key: '',
  sentence_per_line: true,
  titles_enabled: false,
  title_max_chars: 16,
})

const models = ref([])
const apiKeySet = ref(false)
const settingsPath = ref('')
const customModel = ref('')

/** 当前模式说明。 */
const modeDetail = computed(() => MODES.find((m) => m.id === form.value.mode)?.detail || '')

/** 本地模型下拉项（含"当前在用但不在清单里"的情况）。 */
const modelOptions = computed(() => {
  const list = [...models.value]
  const current = form.value.local_model
  if (current && !list.some((item) => item.id === current)) {
    list.unshift({ id: current, name: current, detail: '自定义仓库', ready: false })
  }
  return list
})

function notify(text, type = 'info') {
  message.value = text
  messageType.value = type
}

/** 拉取当前设置。 */
async function load() {
  loading.value = true
  try {
    const data = await fetchSegmentSettings()
    form.value = {
      mode: data.mode || 'rule',
      local_model: data.local_model || '',
      local_model_dir: data.local_model_dir || '',
      models_dir: data.models_dir || '',
      api_base: data.api_base || '',
      api_model: data.api_model || '',
      api_timeout: Number(data.api_timeout) || 60,
      api_key: '',
      sentence_per_line: data.sentence_per_line !== false,
      titles_enabled: Boolean(data.titles_enabled),
      title_max_chars: Number(data.title_max_chars) || 16,
    }
    models.value = Array.isArray(data.models) ? data.models : []
    apiKeySet.value = Boolean(data.api_key_set)
    settingsPath.value = data.settings_path || ''
    customModel.value = ''
    notify('')
  } catch (error) {
    notify(`读取设置失败：${error.message}`, 'error')
  } finally {
    loading.value = false
  }
}

watch(
  () => props.open,
  (open) => {
    if (open) load()
  },
)

/** 选择"自定义仓库"。 */
function applyCustomModel() {
  const value = customModel.value.trim()
  if (!value) return
  form.value.local_model = value
  customModel.value = ''
  notify(`已选择自定义仓库：${value}`)
}

/** 测试当前模式是否真的可用（不保存）。 */
async function testBackend() {
  if (testing.value) return
  const target = form.value.mode === 'api' ? 'api' : 'local'
  testing.value = target
  notify(target === 'api' ? '正在测试 API…' : '正在加载本地模型（首次可能较慢）…', 'info')
  try {
    if (target === 'api') {
      // 测试要用"当前填的"配置，先保存一次（key 留空则沿用已存的）
      await saveSegmentSettings(collect(false))
    }
    const result = await testSegmentBackend(target)
    notify(
      `${result.ok ? '可用' : '不可用'}：${result.detail}（${Math.round(result.elapsed_ms)}ms）`,
      result.ok ? 'success' : 'error',
    )
  } catch (error) {
    notify(`测试失败：${error.message}`, 'error')
  } finally {
    testing.value = ''
  }
}

/** 收集要提交的字段。 */
function collect(includeKey = true) {
  const payload = {
    mode: form.value.mode,
    local_model: form.value.local_model,
    local_model_dir: form.value.local_model_dir,
    models_dir: form.value.models_dir,
    api_base: form.value.api_base,
    api_model: form.value.api_model,
    api_timeout: Number(form.value.api_timeout) || 60,
  }
  if (includeKey && form.value.api_key.trim()) {
    payload.api_key = form.value.api_key.trim()
  }
  payload.sentence_per_line = form.value.sentence_per_line
  payload.titles_enabled = form.value.titles_enabled
  payload.title_max_chars = Number(form.value.title_max_chars) || 16
  return payload
}

async function save() {
  if (saving.value) return
  saving.value = true
  try {
    const data = await saveSegmentSettings(collect(true))
    apiKeySet.value = Boolean(data.api_key_set)
    form.value.api_key = ''
    notify('已保存', 'success')
    emit('saved', data)
  } catch (error) {
    notify(`保存失败：${error.message}`, 'error')
  } finally {
    saving.value = false
  }
}

/** 清除已保存的 API key。 */
async function clearKey() {
  try {
    const data = await saveSegmentSettings({ api_key: '-' })
    apiKeySet.value = Boolean(data.api_key_set)
    notify('已清除 API key', 'success')
  } catch (error) {
    notify(`清除失败：${error.message}`, 'error')
  }
}
</script>

<template>
  <div v-if="open" class="dialog-mask" @click.self="emit('close')">
    <section class="dialog dialog-settings">
      <header class="dialog-header">
        <h2>设置 · 语义分段</h2>
        <button class="dialog-x" type="button" @click="emit('close')">×</button>
      </header>

      <div class="dialog-body">
        <p v-if="loading" class="panel-placeholder">正在读取设置…</p>

        <template v-else>
          <fieldset class="field-group">
            <legend>分段方式</legend>
            <div class="mode-grid">
              <label
                v-for="mode in MODES"
                :key="mode.id"
                class="mode-card"
                :class="{ active: form.mode === mode.id }"
              >
                <input v-model="form.mode" type="radio" :value="mode.id" name="segment-mode" />
                <span class="mode-name">{{ mode.name }}</span>
                <span class="mode-detail">{{ mode.detail }}</span>
              </label>
            </div>
          </fieldset>

          <!-- 本地小模型 -->
          <fieldset v-if="form.mode === 'local'" class="field-group">
            <legend>本地模型</legend>
            <label class="field-row">
              <span>模型</span>
              <select v-model="form.local_model" class="input">
                <option v-for="item in modelOptions" :key="item.id" :value="item.id">
                  {{ item.name }}{{ item.ready ? '（已下载）' : '' }}
                </option>
              </select>
            </label>
            <label class="field-row">
              <span>自定义仓库</span>
              <input
                v-model="customModel"
                class="input"
                type="text"
                placeholder="例如 Xenova/bge-base-zh-v1.5"
                @keydown.enter.prevent="applyCustomModel"
              />
            </label>
            <label class="field-row">
              <span>模型目录</span>
              <input
                v-model="form.local_model_dir"
                class="input"
                type="text"
                placeholder="可选：已有的模型目录（含 onnx/ 与 tokenizer.json）"
              />
            </label>
            <label class="field-row">
              <span>下载到</span>
              <input
                v-model="form.models_dir"
                class="input"
                type="text"
                placeholder="可选：模型根目录，留空用 <项目>/models"
              />
            </label>
          </fieldset>

          <!-- API -->
          <fieldset v-if="form.mode === 'api'" class="field-group">
            <legend>API（OpenAI 兼容）</legend>
            <label class="field-row">
              <span>Base URL</span>
              <input
                v-model="form.api_base"
                class="input"
                type="text"
                placeholder="https://api.openai.com/v1"
              />
            </label>
            <label class="field-row">
              <span>模型</span>
              <input v-model="form.api_model" class="input" type="text" placeholder="gpt-4o-mini" />
            </label>
            <label class="field-row">
              <span>API Key</span>
              <input
                v-model="form.api_key"
                class="input"
                type="password"
                :placeholder="apiKeySet ? '已保存（留空则不修改，填 - 清除）' : '未设置'"
              />
            </label>
            <label class="field-row">
              <span>超时（秒）</span>
              <input v-model.number="form.api_timeout" class="input input-small" type="number" min="1" max="600" />
            </label>
            <div class="field-row">
              <span />
              <span class="dialog-hint">
                {{ apiKeySet ? '密钥已保存' : '密钥未设置' }}
                <button v-if="apiKeySet" class="btn btn-small" type="button" @click="clearKey">
                  清除密钥
                </button>
              </span>
            </div>
          </fieldset>

          <fieldset class="field-group">
            <legend>段落显示</legend>
            <label class="check-row">
              <input v-model="form.sentence_per_line" type="checkbox" />
              <span>一句一行（句末标点后换行，阅读更接近逐句稿）</span>
            </label>
            <label class="check-row">
              <input v-model="form.titles_enabled" type="checkbox" />
              <span>生成分段小标题</span>
            </label>
            <label v-if="form.titles_enabled" class="field-row">
              <span>标题字数</span>
              <input
                v-model.number="form.title_max_chars"
                class="input input-small"
                type="number"
                min="4"
                max="40"
              />
            </label>
          </fieldset>

          <p v-if="modeDetail" class="dialog-hint">{{ modeDetail }}</p>
          <p v-if="message" class="dialog-message" :class="`is-${messageType}`">{{ message }}</p>
          <p v-if="settingsPath" class="dialog-hint mono">配置文件：{{ settingsPath }}</p>
        </template>
      </div>

      <footer class="dialog-footer">
        <button class="btn" type="button" @click="emit('close')">取消</button>
        <button class="btn" type="button" :disabled="Boolean(testing) || loading" @click="testBackend">
          {{ testing ? '测试中…' : '测试' }}
        </button>
        <button class="btn btn-primary" type="button" :disabled="saving || loading" @click="save">
          {{ saving ? '保存中…' : '保存' }}
        </button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.dialog-settings {
  width: min(720px, 94vw);
}

.field-group {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 14px 12px;
  margin: 0 0 14px;
}

.field-group legend {
  padding: 0 6px;
  font-size: 12px;
  color: var(--text-dim);
  letter-spacing: 1px;
}

/* 分段方式：三张卡片，选中高亮 */
.mode-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.mode-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  cursor: pointer;
  transition:
    border-color 0.12s ease,
    background-color 0.12s ease;
}

.mode-card:hover {
  border-color: var(--accent);
}

.mode-card.active {
  border-color: var(--accent);
  background: rgba(59, 130, 246, 0.08);
}

.mode-card input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}

.mode-name {
  font-weight: 600;
  font-size: 13px;
}

.mode-detail {
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--text-dim);
}

/* 字段行：统一左标签宽，输入框对齐 */
.field-row {
  display: grid;
  grid-template-columns: 92px 1fr;
  gap: 10px;
  align-items: center;
  margin: 8px 0;
}

.field-row > span:first-child {
  color: var(--text-dim);
  font-size: 12px;
  text-align: right;
}

.check-row {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 4px 0;
  cursor: pointer;
  font-size: 13px;
}

.dialog-hint {
  color: var(--text-dim);
  font-size: 12px;
  line-height: 1.6;
  display: flex;
  gap: 8px;
  align-items: center;
}

.dialog-message {
  font-size: 12px;
  padding: 6px 8px;
  border-radius: 4px;
  background: var(--bg-elevated);
}

.dialog-message.is-error {
  color: #f87171;
}

.dialog-message.is-success {
  color: #34d399;
}
</style>

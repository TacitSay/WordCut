<script setup>
/**
 * 设置对话框（IDE 风格）：左侧分类、右侧内容。
 *
 * 分类：
 *   - 分段：规则 / 本地小模型 / API 三种切法，及各自配置
 *   - 显示：一句一行、分段小标题
 *   - 内存：空闲自动卸载阈值、模型占用与手动释放
 *
 * 设计约定：
 *   - 保存是显式的（关掉不保存）；「测试」只验证不落盘；
 *   - API key 不回显：留空表示不修改，输入 `-` 表示清除；
 *   - 保存按钮一次提交「分段 + 显示 + 内存」三个节。
 */
import { computed, ref, watch } from 'vue'
import {
  fetchSegmentSettings,
  saveSegmentSettings,
  testSegmentBackend,
  fetchModelStatus,
  unloadModels,
  fetchMemorySettings,
  saveMemorySettings,
} from '../api/client.js'

const props = defineProps({
  /** 是否显示 */
  open: { type: Boolean, default: false },
})

const emit = defineEmits(['close', 'saved'])

/** 左侧分类（IDE 风格）。 */
const CATEGORIES = [
  { id: 'segment', label: '分段', glyph: '≡', hint: '切分方式与模型' },
  { id: 'display', label: '显示', glyph: '¶', hint: '一句一行与标题' },
  { id: 'memory', label: '内存', glyph: '▦', hint: '卸载与占用' },
]

/** 分段模式说明。 */
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

const activeCategory = ref('segment')
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

const memory = ref({
  whisper_idle_seconds: 900,
  semantic_idle_seconds: 600,
})

const modelStatus = ref({ rss_mb: 0, cuda_allocated_mb: 0, models: [] })
const models = ref([])
const apiKeySet = ref(false)
const settingsPath = ref('')
const customModel = ref('')

const modeDetail = computed(() => MODES.find((m) => m.id === form.value.mode)?.detail || '')

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

async function load() {
  loading.value = true
  try {
    const [data, mem] = await Promise.all([fetchSegmentSettings(), fetchMemorySettings()])
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
    memory.value = {
      whisper_idle_seconds: Number(mem.whisper_idle_seconds) || 900,
      semantic_idle_seconds: Number(mem.semantic_idle_seconds) || 600,
    }
    models.value = Array.isArray(data.models) ? data.models : []
    apiKeySet.value = Boolean(data.api_key_set)
    settingsPath.value = data.settings_path || ''
    customModel.value = ''
    notify('')
    loadModelStatus()
  } catch (error) {
    notify(`读取设置失败：${error.message}`, 'error')
  } finally {
    loading.value = false
  }
}

async function loadModelStatus() {
  try {
    modelStatus.value = await fetchModelStatus()
  } catch {
    /* 辅助信息，失败不打断 */
  }
}

watch(
  () => props.open,
  (open) => {
    if (open) load()
  },
)

function selectCategory(id) {
  activeCategory.value = id
}

function applyCustomModel() {
  const value = customModel.value.trim()
  if (!value) return
  form.value.local_model = value
  customModel.value = ''
  notify(`已选择自定义仓库：${value}`)
}

async function testBackend() {
  if (testing.value) return
  const target = form.value.mode === 'api' ? 'api' : 'local'
  testing.value = target
  notify(target === 'api' ? '正在测试 API…' : '正在加载本地模型（首次可能较慢）…', 'info')
  try {
    if (target === 'api') {
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

function collect(includeKey = true) {
  const payload = {
    mode: form.value.mode,
    local_model: form.value.local_model,
    local_model_dir: form.value.local_model_dir,
    models_dir: form.value.models_dir,
    api_base: form.value.api_base,
    api_model: form.value.api_model,
    api_timeout: Number(form.value.api_timeout) || 60,
    sentence_per_line: form.value.sentence_per_line,
    titles_enabled: form.value.titles_enabled,
    title_max_chars: Number(form.value.title_max_chars) || 16,
  }
  if (includeKey && form.value.api_key.trim()) {
    payload.api_key = form.value.api_key.trim()
  }
  return payload
}

async function save() {
  if (saving.value) return
  saving.value = true
  try {
    const [data] = await Promise.all([
      saveSegmentSettings(collect(true)),
      saveMemorySettings({
        whisper_idle_seconds: Number(memory.value.whisper_idle_seconds) || 900,
        semantic_idle_seconds: Number(memory.value.semantic_idle_seconds) || 600,
      }),
    ])
    apiKeySet.value = Boolean(data.api_key_set)
    form.value.api_key = ''
    notify('设置已保存', 'success')
    emit('saved', data)
  } catch (error) {
    notify(`保存失败：${error.message}`, 'error')
  } finally {
    saving.value = false
  }
}

async function clearKey() {
  try {
    const data = await saveSegmentSettings({ api_key: '-' })
    apiKeySet.value = Boolean(data.api_key_set)
    notify('已清除 API key', 'success')
  } catch (error) {
    notify(`清除失败：${error.message}`, 'error')
  }
}

async function handleUnload() {
  try {
    const data = await unloadModels('all')
    const list = Array.isArray(data?.unloaded) ? data.unloaded : []
    notify(list.length ? `已卸载：${list.join('、')}` : '当前没有已加载的模型', 'info')
  } catch (error) {
    notify(`卸载失败：${error.message}`, 'error')
  } finally {
    await loadModelStatus()
  }
}
</script>

<template>
  <div v-if="open" class="dialog-mask" @click.self="emit('close')">
    <section class="dialog settings-window">
      <header class="dialog-header">
        <h2>设置</h2>
        <button class="dialog-x" type="button" @click="emit('close')">×</button>
      </header>

      <div class="settings-layout">
        <!-- 左侧分类 -->
        <nav class="settings-nav">
          <button
            v-for="cat in CATEGORIES"
            :key="cat.id"
            type="button"
            class="settings-nav-item"
            :class="{ active: activeCategory === cat.id }"
            @click="selectCategory(cat.id)"
          >
            <span class="nav-glyph">{{ cat.glyph }}</span>
            <span class="nav-text">
              {{ cat.label }}
              <small>{{ cat.hint }}</small>
            </span>
          </button>
        </nav>

        <!-- 右侧内容 -->
        <div class="settings-content">
          <p v-if="loading" class="panel-placeholder">正在读取设置…</p>

          <!-- ============ 分段 ============ -->
          <template v-else-if="activeCategory === 'segment'">
            <h3 class="settings-title">分段</h3>

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
                  placeholder="例如 Xenova/bge-base-zh-v1.5，回车应用"
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
                <input
                  v-model.number="form.api_timeout"
                  class="input input-small"
                  type="number"
                  min="1"
                  max="600"
                />
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
          </template>

          <!-- ============ 显示 ============ -->
          <template v-else-if="activeCategory === 'display'">
            <h3 class="settings-title">显示</h3>

            <fieldset class="field-group">
              <legend>段落排版</legend>
              <label class="check-row">
                <input v-model="form.sentence_per_line" type="checkbox" />
                <span>
                  一句一行
                  <small class="check-detail">段内句末标点后换行，阅读更接近逐句稿</small>
                </span>
              </label>
              <label class="check-row">
                <input v-model="form.titles_enabled" type="checkbox" />
                <span>
                  分段小标题
                  <small class="check-detail">每段上方显示 § 标题；规则模式取首句，语义模式自动概括</small>
                </span>
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
            <p class="dialog-hint">显示设置在下次「重新分段」或刷新文本时生效，不影响词与时间戳。</p>
          </template>

          <!-- ============ 内存 ============ -->
          <template v-else>
            <h3 class="settings-title">内存</h3>

            <fieldset class="field-group">
              <legend>空闲自动卸载</legend>
              <label class="field-row">
                <span>转写模型</span>
                <input
                  v-model.number="memory.whisper_idle_seconds"
                  class="input input-small"
                  type="number"
                  min="0"
                  max="86400"
                  step="30"
                />
                <span class="unit">秒（0 = 不自动）</span>
              </label>
              <label class="field-row">
                <span>语义模型</span>
                <input
                  v-model.number="memory.semantic_idle_seconds"
                  class="input input-small"
                  type="number"
                  min="0"
                  max="86400"
                  step="30"
                />
                <span class="unit">秒（0 = 不自动）</span>
              </label>
              <p class="dialog-hint">空闲检查每 30 秒进行一次；手动释放立即生效。</p>
            </fieldset>

            <fieldset class="field-group">
              <legend>当前占用</legend>
              <div class="mem-row">
                <span>进程常驻</span>
                <span class="mono">{{ modelStatus.rss_mb ? modelStatus.rss_mb + ' MB' : '—' }}</span>
              </div>
              <div v-for="m in modelStatus.models" :key="m.name" class="mem-row">
                <span class="mono">{{ m.name }}</span>
                <span class="mono">
                  {{ m.loaded ? `已加载 ${m.detail ? '（' + m.detail + '）' : ''}` : '未加载' }}
                </span>
              </div>
              <div class="field-row">
                <span />
                <button class="btn" type="button" @click="handleUnload">卸载全部模型</button>
              </div>
            </fieldset>
          </template>

          <p v-if="message" class="dialog-message" :class="`is-${messageType}`">{{ message }}</p>
        </div>
      </div>

      <footer class="dialog-footer">
        <span class="footer-path mono" :title="settingsPath">{{ settingsPath }}</span>
        <span class="footer-actions">
          <button class="btn" type="button" @click="emit('close')">取消</button>
          <button class="btn" type="button" :disabled="Boolean(testing) || loading" @click="testBackend">
            {{ testing ? '测试中…' : '测试' }}
          </button>
          <button class="btn btn-primary" type="button" :disabled="saving || loading" @click="save">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </span>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.settings-window {
  width: min(860px, 94vw);
  height: min(620px, 90vh);
  display: flex;
  flex-direction: column;
}

/* 左右布局：分类栏 + 内容 */
.settings-layout {
  display: flex;
  flex: 1 1 auto;
  min-height: 0;
}

.settings-nav {
  flex: 0 0 176px;
  border-right: 1px solid var(--border);
  padding: 10px 6px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  background: var(--bg-elevated);
  overflow-y: auto;
}

.settings-nav-item {
  display: flex;
  gap: 10px;
  align-items: center;
  width: 100%;
  padding: 9px 10px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text);
  text-align: left;
  cursor: pointer;
  font-size: 13px;
}

.settings-nav-item:hover {
  background: var(--bg-panel);
}

.settings-nav-item.active {
  background: rgba(59, 130, 246, 0.14);
  color: #fff;
}

.nav-glyph {
  width: 18px;
  text-align: center;
  color: var(--accent);
  font-size: 13px;
}

.nav-text {
  display: flex;
  flex-direction: column;
  line-height: 1.35;
}

.nav-text small {
  color: var(--text-dim);
  font-size: 11px;
}

/* 内容区 */
.settings-content {
  flex: 1 1 auto;
  min-width: 0;
  overflow-y: auto;
  padding: 14px 20px 20px;
}

.settings-title {
  margin: 0 0 12px;
  font-size: 15px;
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

/* 分段方式：三张卡片 */
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

/* 字段行 */
.field-row {
  display: grid;
  grid-template-columns: 92px 1fr auto;
  gap: 10px;
  align-items: center;
  margin: 8px 0;
}

.field-row > span:first-child {
  color: var(--text-dim);
  font-size: 12px;
  text-align: right;
}

.unit {
  color: var(--text-dim);
  font-size: 12px;
}

.check-row {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  padding: 5px 0;
  cursor: pointer;
  font-size: 13px;
}

.check-row input {
  margin-top: 2px;
}

.check-detail {
  display: block;
  color: var(--text-dim);
  font-size: 11.5px;
  margin-top: 2px;
}

/* 内存行 */
.mem-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 5px 2px;
  font-size: 12.5px;
  border-bottom: 1px dashed var(--border);
}

.mem-row:last-of-type {
  border-bottom: none;
}

.mem-row > span:first-child {
  color: var(--text-dim);
}

/* 底栏 */
.footer-path {
  color: var(--text-dim);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 46%;
}

.footer-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
}

/* 底栏布局在全局 .dialog-footer 之上微调：路径占左、按钮居右 */
.dialog-footer {
  justify-content: space-between;
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

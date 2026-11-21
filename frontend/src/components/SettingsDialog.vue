<script setup>
/**
 * 设置对话框（IDE 风格：左侧分类，右侧内容）。
 *
 * 分类：
 *   - 分段：规则 / 本地模型 / API 三种切法
 *   - 显示：一句一行、分段小标题
 *   - 模型：本地模型目录扫描、废弃模型清理、内存占用
 *
 * 本轮重点（用户反馈）：
 *   1. 三种模式**看不出当前在跑哪个** → 顶部常驻"当前生效"状态条
 *   2. 本地模型**靠读文件夹选择** → 扫描 models/ 目录，下拉选，不用手填仓库名
 *   3. 旧模型要能**一键删除** → 模型分类里列出所有目录，废弃的标红
 *   4. API 要"填 Key 就能用" → 内置服务商预设，选服务商自动带出 Base 与模型，
 *      并有"测试连接"按钮与申请 Key 的链接
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
  fetchProviders,
  testApiConnection,
  fetchLocalModels,
  removeLocalModel,
} from '../api/client.js'

const props = defineProps({
  /** 是否显示 */
  open: { type: Boolean, default: false },
})

const emit = defineEmits(['close', 'saved'])

/** 左侧分类。 */
const CATEGORIES = [
  { id: 'segment', label: '分段', glyph: '≡', hint: '切分方式' },
  { id: 'api', label: 'AI 接口', glyph: '⇄', hint: '服务商与 Key' },
  { id: 'models', label: '模型', glyph: '▣', hint: '本地模型管理' },
  { id: 'display', label: '显示', glyph: '¶', hint: '排版与标题' },
  { id: 'memory', label: '内存', glyph: '▦', hint: '卸载与占用' },
]

/** 分段模式。 */
const MODES = [
  {
    id: 'rule',
    name: '规则分段',
    detail: '按标点、停顿、时长切分。零依赖、零内存，段落偏碎但不会出错。',
  },
  {
    id: 'local',
    name: '本地模型',
    detail: '用 models/ 下的本地模型做语义分段。需已下载模型。',
  },
  {
    id: 'api',
    name: 'AI 接口',
    detail: '调用 OpenAI 兼容接口分段与起标题。质量最好，需要 Key。',
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
  provider: '',
  api_base: '',
  api_model: '',
  api_timeout: 60,
  api_key: '',
  sentence_per_line: true,
  titles_enabled: true,
  title_max_chars: 0,
  title_backend: 'auto',
  summarizer_dir: '',
})

const memory = ref({ whisper_idle_seconds: 900, semantic_idle_seconds: 600 })

const providers = ref([])
const localModels = ref([])
const modelsRoot = ref('')
const modelStatus = ref({ rss_mb: 0, models: [] })
const apiKeySet = ref(false)
const settingsPath = ref('')
const connResult = ref(null)

/** 当前生效的后端（来自后端运行时状态）。 */
const runtimeBackend = ref({ segment: '', title: '', titleErrors: [] })

const modeDetail = computed(() => MODES.find((m) => m.id === form.value.mode)?.detail || '')

/** 选中的服务商预设。 */
const currentProvider = computed(() =>
  providers.value.find((item) => item.id === form.value.provider),
)

/** 可用的本地模型（只列出 ready 且未废弃的）。 */
const usableModels = computed(() => localModels.value.filter((m) => m.ready && !m.deprecated))

/** 废弃的本地模型（可一键清理）。 */
const deprecatedModels = computed(() => localModels.value.filter((m) => m.deprecated))

/** 当前生效状态：一句话说清"现在跑的是哪个"。 */
const activeSummary = computed(() => {
  const mode = form.value.mode
  if (mode === 'rule') return { label: '规则分段', tone: 'neutral', detail: '纯本地规则，不调用模型' }
  if (mode === 'local') {
    const name = form.value.summarizer_dir || form.value.local_model || '（未选择模型）'
    return { label: '本地模型', tone: 'local', detail: name }
  }
  const pv = currentProvider.value
  const model = form.value.api_model || '（未填模型）'
  return {
    label: pv ? pv.name : 'AI 接口',
    tone: 'api',
    detail: `${model}${apiKeySet.value ? '' : ' · 未配置 Key'}`,
  }
})

function notify(text, type = 'info') {
  message.value = text
  messageType.value = type
}

/** 加载全部数据。 */
async function load() {
  loading.value = true
  try {
    const [data, mem, pv, models, status] = await Promise.all([
      fetchSegmentSettings(),
      fetchMemorySettings(),
      fetchProviders().catch(() => ({ providers: [] })),
      fetchLocalModels().catch(() => ({ models: [], root: '' })),
      fetchModelStatus().catch(() => ({ rss_mb: 0, models: [] })),
    ])

    form.value = {
      mode: data.mode || 'rule',
      local_model: data.local_model || '',
      local_model_dir: data.local_model_dir || '',
      models_dir: data.models_dir || '',
      provider: data.provider || '',
      api_base: data.api_base || '',
      api_model: data.api_model || '',
      api_timeout: Number(data.api_timeout) || 60,
      api_key: '',
      sentence_per_line: data.sentence_per_line !== false,
      titles_enabled: data.titles_enabled !== false,
      title_max_chars: Number(data.title_max_chars) || 0,
      title_backend: data.title_backend || 'auto',
      summarizer_dir: data.summarizer_dir || '',
    }
    memory.value = {
      whisper_idle_seconds: Number(mem.whisper_idle_seconds) || 900,
      semantic_idle_seconds: Number(mem.semantic_idle_seconds) || 600,
    }
    providers.value = Array.isArray(pv.providers) ? pv.providers : []
    localModels.value = Array.isArray(models.models) ? models.models : []
    modelsRoot.value = models.root || ''
    modelStatus.value = status
    apiKeySet.value = Boolean(data.api_key_set)
    settingsPath.value = data.settings_path || ''

    // 没存过服务商时按 base 反推，让下拉能对上
    if (!form.value.provider && form.value.api_base) {
      const hit = providers.value.find(
        (item) => item.base && form.value.api_base.startsWith(item.base.replace(/\/$/, '')),
      )
      if (hit) form.value.provider = hit.id
    }
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

function selectCategory(id) {
  activeCategory.value = id
}

/** 切换服务商：自动带出 Base 与默认模型。 */
function onProviderChange() {
  const pv = currentProvider.value
  if (!pv) return
  if (pv.base) form.value.api_base = pv.base
  if (pv.models?.length) form.value.api_model = pv.models[0]
  connResult.value = null
  notify(`已套用 ${pv.name} 的配置，填好 Key 后点「测试连接」`)
}

/** 测试 AI 连接（用当前填的，不要求已保存）。 */
async function testConnection() {
  if (testing.value) return
  testing.value = 'api'
  connResult.value = null
  notify('正在测试连接…')
  try {
    const result = await testApiConnection({
      provider: form.value.provider || undefined,
      api_base: form.value.api_base || undefined,
      api_model: form.value.api_model || undefined,
      api_key: form.value.api_key || undefined,
    })
    connResult.value = result
    notify(
      `${result.ok ? '✓ ' : '✗ '}${result.detail}${result.model_reply ? ` · 回复「${result.model_reply}」` : ''}`,
      result.ok ? 'success' : 'error',
    )
  } catch (error) {
    notify(`测试失败：${error.message}`, 'error')
  } finally {
    testing.value = ''
  }
}

/** 测试本地模型是否可用。 */
async function testLocal() {
  if (testing.value) return
  testing.value = 'local'
  notify('正在加载本地模型（首次较慢）…')
  try {
    const result = await testSegmentBackend('local')
    notify(
      `${result.ok ? '✓ ' : '✗ '}${result.detail}（${Math.round(result.elapsed_ms)}ms）`,
      result.ok ? 'success' : 'error',
    )
  } catch (error) {
    notify(`测试失败：${error.message}`, 'error')
  } finally {
    testing.value = ''
  }
}

/** 收集要保存的字段。 */
function collect(includeKey = true) {
  const payload = {
    mode: form.value.mode,
    local_model: form.value.local_model,
    local_model_dir: form.value.local_model_dir,
    models_dir: form.value.models_dir,
    provider: form.value.provider,
    api_base: form.value.api_base,
    api_model: form.value.api_model,
    api_timeout: Number(form.value.api_timeout) || 60,
    sentence_per_line: form.value.sentence_per_line,
    titles_enabled: form.value.titles_enabled,
    title_max_chars: Number(form.value.title_max_chars) || 0,
    title_backend: form.value.title_backend,
    summarizer_dir: form.value.summarizer_dir,
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
    notify('已清除 API Key', 'success')
  } catch (error) {
    notify(`清除失败：${error.message}`, 'error')
  }
}

/** 删除一个本地模型目录。 */
async function handleRemoveModel(model) {
  const label = model.deprecated ? '废弃模型' : '模型'
  if (!window.confirm(`确定删除${label}「${model.name}」（${model.size_mb} MB）？\n此操作不可撤销。`)) {
    return
  }
  try {
    const result = await removeLocalModel(model.name)
    if (result.removed) {
      notify(`已删除 ${model.name}，释放 ${result.freed_mb} MB`, 'success')
      const models = await fetchLocalModels()
      localModels.value = Array.isArray(models.models) ? models.models : []
      if (form.value.summarizer_dir === model.path) form.value.summarizer_dir = ''
    } else {
      notify(`删除失败：${result.error}`, 'error')
    }
  } catch (error) {
    notify(`删除失败：${error.message}`, 'error')
  }
}

/** 一键清理所有废弃模型。 */
async function handleCleanDeprecated() {
  const list = deprecatedModels.value
  if (!list.length) return
  const total = list.reduce((sum, item) => sum + item.size_mb, 0)
  if (!window.confirm(`删除 ${list.length} 个废弃模型，释放约 ${Math.round(total)} MB？`)) return

  let freed = 0
  let failed = 0
  for (const model of list) {
    try {
      const result = await removeLocalModel(model.name)
      if (result.removed) freed += result.freed_mb
      else failed += 1
    } catch {
      failed += 1
    }
  }
  const models = await fetchLocalModels()
  localModels.value = Array.isArray(models.models) ? models.models : []
  notify(
    `清理完成：释放 ${Math.round(freed)} MB${failed ? `，${failed} 个失败` : ''}`,
    failed ? 'error' : 'success',
  )
}

/** 把某个本地模型目录设为当前使用的模型。 */
function useLocalModel(model) {
  form.value.summarizer_dir = model.path
  form.value.mode = 'local'
  notify(`已选择本地模型：${model.name}（记得保存）`)
}

async function handleUnload() {
  try {
    const data = await unloadModels('all')
    const list = Array.isArray(data?.unloaded) ? data.unloaded : []
    notify(list.length ? `已卸载：${list.join('、')}` : '当前没有已加载的模型', 'info')
    modelStatus.value = await fetchModelStatus().catch(() => modelStatus.value)
  } catch (error) {
    notify(`卸载失败：${error.message}`, 'error')
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

      <!-- 常驻状态条：一眼看出"现在跑的是哪个" -->
      <div class="active-bar" :class="`tone-${activeSummary.tone}`">
        <span class="active-dot" />
        <span class="active-label">当前生效</span>
        <strong>{{ activeSummary.label }}</strong>
        <span class="active-detail">{{ activeSummary.detail }}</span>
        <span v-if="!loading" class="active-hint">
          {{ form.mode === 'api' && !apiKeySet ? '· 尚未配置 Key' : '' }}
        </span>
      </div>

      <div class="settings-layout">
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

        <div class="settings-content">
          <p v-if="loading" class="panel-placeholder">正在读取设置…</p>

          <!-- ==================== 分段 ==================== -->
          <template v-else-if="activeCategory === 'segment'">
            <h3 class="settings-title">分段方式</h3>
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
            <p class="dialog-hint">{{ modeDetail }}</p>

            <fieldset v-if="form.mode === 'local'" class="field-group">
              <legend>本地模型</legend>
              <p v-if="!usableModels.length" class="dialog-hint warn">
                还没有可用的本地模型。请把模型文件夹放到 <code>{{ modelsRoot }}</code> 下。
              </p>
              <label v-else class="field-row">
                <span>选择</span>
                <select v-model="form.summarizer_dir" class="input">
                  <option value="">（未选择）</option>
                  <option v-for="m in usableModels" :key="m.path" :value="m.path">
                    {{ m.name }} · {{ m.size_mb }} MB
                  </option>
                </select>
              </label>
              <div class="field-row">
                <span />
                <button class="btn btn-small" type="button" :disabled="Boolean(testing)" @click="testLocal">
                  {{ testing === 'local' ? '加载中…' : '测试本地模型' }}
                </button>
              </div>
            </fieldset>
          </template>

          <!-- ==================== AI 接口 ==================== -->
          <template v-else-if="activeCategory === 'api'">
            <h3 class="settings-title">AI 接口</h3>

            <fieldset class="field-group">
              <legend>服务商</legend>
              <label class="field-row">
                <span>选择</span>
                <select v-model="form.provider" class="input" @change="onProviderChange">
                  <option value="">（手动填写）</option>
                  <option v-for="pv in providers" :key="pv.id" :value="pv.id">
                    {{ pv.name }}{{ pv.note ? ' · ' + pv.note : '' }}
                  </option>
                </select>
              </label>

              <label class="field-row">
                <span>模型</span>
                <template v-if="currentProvider && currentProvider.models && currentProvider.models.length">
                  <select v-model="form.api_model" class="input">
                    <option v-for="m in currentProvider.models" :key="m" :value="m">{{ m }}</option>
                  </select>
                </template>
                <input v-else v-model="form.api_model" class="input" type="text" placeholder="deepseek-chat" />
              </label>

              <label class="field-row">
                <span>API Key</span>
                <input
                  v-model="form.api_key"
                  class="input"
                  type="password"
                  :placeholder="apiKeySet ? '已保存（留空不修改，填 - 清除）' : '粘贴你的 Key'"
                />
              </label>

              <div class="field-row">
                <span />
                <span class="dialog-hint">
                  <a
                    v-if="currentProvider && currentProvider.key_url"
                    :href="currentProvider.key_url"
                    target="_blank"
                    rel="noreferrer noopener"
                    class="key-link"
                  >
                    去 {{ currentProvider.name }} 获取 Key ↗
                  </a>
                  <button v-if="apiKeySet" class="btn btn-small" type="button" @click="clearKey">
                    清除已保存的 Key
                  </button>
                </span>
              </div>
            </fieldset>

            <fieldset class="field-group">
              <legend>高级</legend>
              <label class="field-row">
                <span>Base URL</span>
                <input v-model="form.api_base" class="input" type="text" placeholder="https://api.deepseek.com/v1" />
              </label>
              <label class="field-row">
                <span>超时（秒）</span>
                <input v-model.number="form.api_timeout" class="input input-small" type="number" min="1" max="600" />
              </label>
              <p class="dialog-hint">Base URL 会随服务商自动填写，一般不需要改。</p>
            </fieldset>

            <div class="test-row">
              <button class="btn" type="button" :disabled="Boolean(testing)" @click="testConnection">
                {{ testing === 'api' ? '测试中…' : '测试连接' }}
              </button>
              <span v-if="connResult" class="test-result" :class="connResult.ok ? 'ok' : 'bad'">
                {{ connResult.ok ? '✓' : '✗' }} {{ connResult.detail }}
                <template v-if="connResult.model_reply"> · 回复「{{ connResult.model_reply }}」</template>
                <span class="test-ms">{{ Math.round(connResult.elapsed_ms) }}ms</span>
              </span>
            </div>
          </template>

          <!-- ==================== 模型 ==================== -->
          <template v-else-if="activeCategory === 'models'">
            <h3 class="settings-title">本地模型</h3>
            <p class="dialog-hint">
              扫描目录：<code>{{ modelsRoot }}</code>
            </p>

            <fieldset v-if="deprecatedModels.length" class="field-group deprecated-box">
              <legend>可清理（{{ deprecatedModels.length }}）</legend>
              <p class="dialog-hint warn">
                这些模型已废弃（早期方案，本机不可用或已被取代），删除不影响功能。
              </p>
              <div v-for="m in deprecatedModels" :key="m.path" class="model-row">
                <span class="model-name">{{ m.name }}</span>
                <span class="model-meta">{{ m.size_mb }} MB · {{ m.detail }}</span>
                <button class="btn btn-small danger" type="button" @click="handleRemoveModel(m)">删除</button>
              </div>
              <div class="field-row">
                <span />
                <button class="btn btn-small danger" type="button" @click="handleCleanDeprecated">
                  一键清理全部
                </button>
              </div>
            </fieldset>

            <fieldset class="field-group">
              <legend>全部模型（{{ localModels.length }}）</legend>
              <p v-if="!localModels.length" class="dialog-hint">没有找到模型目录。</p>
              <div v-for="m in localModels" :key="m.path" class="model-row">
                <span class="model-name">
                  {{ m.name }}
                  <small class="model-kind">{{ m.kind }}</small>
                </span>
                <span class="model-meta">{{ m.size_mb }} MB</span>
                <button
                  v-if="m.ready && !m.deprecated"
                  class="btn btn-small"
                  type="button"
                  @click="useLocalModel(m)"
                >
                  使用
                </button>
                <button class="btn btn-small danger" type="button" @click="handleRemoveModel(m)">删除</button>
              </div>
            </fieldset>
          </template>

          <!-- ==================== 显示 ==================== -->
          <template v-else-if="activeCategory === 'display'">
            <h3 class="settings-title">显示</h3>

            <fieldset class="field-group">
              <legend>段落排版</legend>
              <label class="check-row">
                <input v-model="form.sentence_per_line" type="checkbox" />
                <span>
                  一句一行
                  <small class="check-detail">段内每句单独成行，阅读更接近逐句稿</small>
                </span>
              </label>
              <label class="check-row">
                <input v-model="form.titles_enabled" type="checkbox" />
                <span>
                  段落标题
                  <small class="check-detail">每段上方显示概括性标题</small>
                </span>
              </label>
              <label v-if="form.titles_enabled" class="field-row">
                <span>标题来源</span>
                <select v-model="form.title_backend" class="input">
                  <option value="auto">自动（接口优先，失败用本地）</option>
                  <option value="api">只用 AI 接口</option>
                  <option value="local">只用本地模型</option>
                  <option value="keyword">只用关键词（快，但只是一个词）</option>
                </select>
              </label>
              <label v-if="form.titles_enabled" class="field-row">
                <span>标题字数</span>
                <input
                  v-model.number="form.title_max_chars"
                  class="input input-small"
                  type="number"
                  min="0"
                  max="60"
                />
                <span class="unit">0 = 不限</span>
              </label>
            </fieldset>
            <p class="dialog-hint">保存后点「重新分段」即可生效。</p>
          </template>

          <!-- ==================== 内存 ==================== -->
          <template v-else>
            <h3 class="settings-title">内存</h3>

            <fieldset class="field-group">
              <legend>空闲自动卸载</legend>
              <label class="field-row">
                <span>转写模型</span>
                <input v-model.number="memory.whisper_idle_seconds" class="input input-small" type="number" min="0" step="30" />
                <span class="unit">秒（0 = 不自动）</span>
              </label>
              <label class="field-row">
                <span>语义模型</span>
                <input v-model.number="memory.semantic_idle_seconds" class="input input-small" type="number" min="0" step="30" />
                <span class="unit">秒（0 = 不自动）</span>
              </label>
            </fieldset>

            <fieldset class="field-group">
              <legend>当前占用</legend>
              <div class="mem-row">
                <span>进程常驻</span>
                <span class="mono">{{ modelStatus.rss_mb ? modelStatus.rss_mb + ' MB' : '—' }}</span>
              </div>
              <div v-for="m in modelStatus.models" :key="m.name" class="mem-row">
                <span class="mono">{{ m.name }}</span>
                <span class="mono">{{ m.loaded ? '已加载' + (m.detail ? `（${m.detail}）` : '') : '未加载' }}</span>
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
  width: min(900px, 95vw);
  height: min(680px, 90vh);
  display: flex;
  flex-direction: column;
}

/* 常驻状态条 */
.active-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
  padding: 8px 16px;
  font-size: 12.5px;
  background: var(--bg-elevated);
  box-shadow: var(--nm-inset-sm);
}

.active-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-faint);
  box-shadow: 0 0 0 3px var(--nm-dark);
}

.tone-api .active-dot { background: var(--accent); }
.tone-local .active-dot { background: var(--success); }

.active-label { color: var(--text-dim); }
.active-bar strong { color: var(--text-strong); }
.active-detail { color: var(--text-dim); }
.active-hint { color: var(--danger); margin-left: auto; }

.settings-layout {
  display: flex;
  flex: 1 1 auto;
  min-height: 0;
}

.settings-nav {
  flex: 0 0 164px;
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

.settings-nav-item:hover { background: var(--bg-panel); }
.settings-nav-item.active { background: var(--accent-dim); color: var(--text-strong); }

.nav-glyph { width: 18px; text-align: center; color: var(--accent); }
.nav-text { display: flex; flex-direction: column; line-height: 1.35; }
.nav-text small { color: var(--text-dim); font-size: 11px; }

.settings-content {
  flex: 1 1 auto;
  min-width: 0;
  overflow-y: auto;
  padding: 14px 20px 20px;
}

.settings-title { margin: 0 0 12px; font-size: 15px; }

.field-group {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 14px 12px;
  margin: 0 0 14px;
}

.field-group legend { padding: 0 6px; font-size: 12px; color: var(--text-dim); letter-spacing: 1px; }

.mode-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }

.mode-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  cursor: pointer;
}

.mode-card:hover { border-color: var(--accent); }
.mode-card.active { border-color: var(--accent); background: var(--accent-dim); }
.mode-card input { position: absolute; opacity: 0; pointer-events: none; }
.mode-name { font-weight: 600; font-size: 13px; }
.mode-detail { font-size: 11.5px; line-height: 1.55; color: var(--text-dim); }

.field-row {
  display: grid;
  grid-template-columns: 88px 1fr auto;
  gap: 10px;
  align-items: center;
  margin: 8px 0;
}

.field-row > span:first-child { color: var(--text-dim); font-size: 12px; text-align: right; }
.unit { color: var(--text-dim); font-size: 12px; }

.check-row { display: flex; gap: 8px; align-items: flex-start; padding: 5px 0; cursor: pointer; font-size: 13px; }
.check-row input { margin-top: 2px; }
.check-detail { display: block; color: var(--text-dim); font-size: 11.5px; margin-top: 2px; }

/* 模型行 */
.model-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 2px;
  border-bottom: 1px dashed var(--border);
  font-size: 12.5px;
}

.model-row:last-of-type { border-bottom: none; }
.model-name { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.model-kind { color: var(--text-faint); font-size: 11px; margin-left: 6px; }
.model-meta { color: var(--text-dim); flex: 0 0 auto; }

.deprecated-box { border-color: var(--danger); }
.dialog-hint.warn { color: var(--danger); }

.btn.danger { color: var(--danger); }
.btn.danger:hover:not(:disabled) { box-shadow: var(--nm-raised-sm), 0 0 0 1px var(--danger); }

.mem-row {
  display: flex;
  justify-content: space-between;
  padding: 5px 2px;
  font-size: 12.5px;
  border-bottom: 1px dashed var(--border);
}

.mem-row:last-of-type { border-bottom: none; }
.mem-row > span:first-child { color: var(--text-dim); }

.test-row { display: flex; align-items: center; gap: 12px; margin: 4px 0 12px; }
.test-result { font-size: 12px; }
.test-result.ok { color: var(--success); }
.test-result.bad { color: var(--danger); }
.test-ms { color: var(--text-faint); margin-left: 6px; }

.key-link { color: var(--accent); text-decoration: none; font-size: 12px; }
.key-link:hover { text-decoration: underline; }

.dialog-footer { justify-content: space-between; }
.footer-path { color: var(--text-dim); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 46%; }
.footer-actions { display: flex; gap: 8px; }

.dialog-hint { color: var(--text-dim); font-size: 12px; line-height: 1.6; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.dialog-message { font-size: 12px; padding: 6px 8px; border-radius: 4px; background: var(--bg-elevated); }
.dialog-message.is-error { color: var(--danger); }
.dialog-message.is-success { color: var(--success); }
</style>

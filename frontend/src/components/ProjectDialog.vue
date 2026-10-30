<script setup>
/**
 * 项目管理对话框：列出全部项目，支持重命名 / 删除。
 *
 * 背景：试用阶段每次重新上传都会生成一个新项目，很快就堆出一堆同名项目，
 * 而之前没有任何入口可以清理（只能手改 SQLite）。本组件补上这个缺口。
 *
 * 交互约定：
 *  - 重命名：原地编辑，回车或点「保存」提交，Esc 取消；
 *  - 删除：二次确认；默认只删数据库记录（不动源素材），
 *    勾选「同时清理上传/导出副本」才删 uploads/{pid}.* 与 outputs/{pid}/；
 *  - 任何操作完成后 emit('changed')，由 App 刷新项目列表/当前项目状态。
 */
import { computed, onMounted, ref } from 'vue'
import {
  deleteProject,
  listProjects,
  renameProject,
} from '../api/client.js'

const emit = defineEmits(['close', 'changed', 'open'])

const projects = ref([])
const loading = ref(false)
const errorText = ref('')
const busyId = ref('')
const editingId = ref('')
const editingName = ref('')
const purgeOnDelete = ref(false)

/** 当前处于"编辑中"的项目名是否合法。 */
const editingValid = computed(() => editingName.value.trim().length > 0)

async function load() {
  loading.value = true
  errorText.value = ''
  try {
    const list = await listProjects()
    projects.value = Array.isArray(list) ? list : []
  } catch (error) {
    errorText.value = error.message
  } finally {
    loading.value = false
  }
}

function startRename(project) {
  editingId.value = project.id
  editingName.value = project.name
}

function cancelRename() {
  editingId.value = ''
  editingName.value = ''
}

async function submitRename(project) {
  const name = editingName.value.trim()
  if (!name) return
  if (name === project.name) {
    cancelRename()
    return
  }
  busyId.value = project.id
  try {
    const updated = await renameProject(project.id, name)
    const index = projects.value.findIndex((item) => item.id === project.id)
    if (index >= 0) projects.value[index] = { ...projects.value[index], ...updated }
    cancelRename()
    emit('changed')
  } catch (error) {
    errorText.value = `重命名失败：${error.message}`
  } finally {
    busyId.value = ''
  }
}

async function removeProject(project) {
  const hint = purgeOnDelete.value
    ? `删除项目「${project.name}」并清理上传/导出副本？源素材不受影响。`
    : `删除项目「${project.name}」的工程记录？源音频与导出副本会保留。`
  if (!window.confirm(hint)) return

  busyId.value = project.id
  errorText.value = ''
  try {
    await deleteProject(project.id, { purgeFiles: purgeOnDelete.value })
    projects.value = projects.value.filter((item) => item.id !== project.id)
    emit('changed')
  } catch (error) {
    errorText.value = `删除失败：${error.message}`
  } finally {
    busyId.value = ''
  }
}

function formatDuration(seconds) {
  const value = Number(seconds)
  if (!Number.isFinite(value) || value <= 0) return '—'
  const minutes = Math.floor(value / 60)
  const rest = Math.round(value - minutes * 60)
  return `${minutes}:${String(rest).padStart(2, '0')}`
}

function formatDate(iso) {
  const text = String(iso || '')
  const date = new Date(text)
  if (Number.isNaN(date.getTime())) return text || '—'
  return date.toLocaleString('zh-CN', { hour12: false })
}

onMounted(load)
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <section class="modal project-modal" role="dialog" aria-label="项目管理">
      <header class="modal-header">
        <h2 class="modal-title">项目管理</h2>
        <button class="modal-close" type="button" title="关闭（Esc）" @click="emit('close')">
          ×
        </button>
      </header>

      <div class="modal-body">
        <p class="modal-hint">
          可直接打开已有项目继续编辑；重命名只改工程记录；删除默认保留源音频与导出副本。
        </p>

        <p v-if="errorText" class="modal-error">{{ errorText }}</p>

        <p v-if="loading" class="modal-empty">正在读取项目列表…</p>
        <p v-else-if="!projects.length" class="modal-empty">还没有项目。</p>

        <table v-else class="project-table">
          <thead>
            <tr>
              <th>项目名</th>
              <th class="col-num">时长</th>
              <th class="col-num">Token</th>
              <th class="col-num">已删</th>
              <th>创建时间</th>
              <th class="col-ops">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="project in projects" :key="project.id">
              <td>
                <template v-if="editingId === project.id">
                  <input
                    v-model="editingName"
                    class="rename-input"
                    type="text"
                    maxlength="80"
                    @keydown.enter.prevent="submitRename(project)"
                    @keydown.esc.prevent="cancelRename"
                  />
                </template>
                <template v-else>
                  <span class="project-name" :title="project.id">{{ project.name }}</span>
                  <span v-if="!project.transcribed" class="tag">未转写</span>
                </template>
              </td>
              <td class="col-num">{{ formatDuration(project.duration) }}</td>
              <td class="col-num">{{ project.word_count }}</td>
              <td class="col-num">{{ project.deleted_count }}</td>
              <td class="col-date">{{ formatDate(project.created_at) }}</td>
              <td class="col-ops">
                <template v-if="editingId === project.id">
                  <button
                    class="btn btn-small"
                    type="button"
                    :disabled="!editingValid || busyId === project.id"
                    @click="submitRename(project)"
                  >
                    保存
                  </button>
                  <button class="btn btn-small" type="button" @click="cancelRename">取消</button>
                </template>
                <template v-else>
                  <button
                    class="btn btn-small"
                    type="button"
                    :disabled="Boolean(busyId)"
                    @click="emit('open', project.id)"
                  >
                    打开
                  </button>
                  <button
                    class="btn btn-small"
                    type="button"
                    :disabled="Boolean(busyId)"
                    @click="startRename(project)"
                  >
                    重命名
                  </button>
                  <button
                    class="btn btn-small btn-danger"
                    type="button"
                    :disabled="Boolean(busyId)"
                    @click="removeProject(project)"
                  >
                    删除
                  </button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>

        <label class="purge-toggle">
          <input v-model="purgeOnDelete" type="checkbox" />
          删除时同时清理 uploads/ 与 outputs/ 下的副本（源素材永不改动）
        </label>
      </div>

      <footer class="modal-footer">
        <button class="btn btn-small" type="button" @click="load">刷新列表</button>
        <button class="btn btn-small" type="button" @click="emit('close')">关闭</button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.55);
}

.project-modal {
  width: min(860px, 94vw);
  max-height: 82vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-panel, #1f242c);
  border: 1px solid var(--border-strong, #3a4150);
  border-radius: 8px;
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.5);
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border, #2c3340);
}

.modal-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-strong, #eef2f8);
}

.modal-close {
  width: 26px;
  height: 26px;
  font-size: 18px;
  line-height: 1;
  color: var(--text-dim, #9aa4b2);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}

.modal-close:hover {
  background: rgba(255, 255, 255, 0.08);
}

.modal-body {
  padding: 14px 16px;
  overflow: auto;
}

.modal-hint {
  margin: 0 0 10px;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--text-faint, #7d8794);
}

.modal-error {
  margin: 0 0 10px;
  padding: 6px 8px;
  font-size: 11.5px;
  color: #ffb4ae;
  background: rgba(229, 83, 75, 0.12);
  border: 1px solid rgba(229, 83, 75, 0.4);
  border-radius: 4px;
}

.modal-empty {
  margin: 12px 0;
  font-size: 12px;
  color: var(--text-faint, #7d8794);
}

.project-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.project-table th,
.project-table td {
  padding: 7px 6px;
  text-align: left;
  border-bottom: 1px solid var(--border, #2c3340);
}

.project-table th {
  font-weight: 600;
  font-size: 11px;
  color: var(--text-faint, #7d8794);
}

.project-table td {
  color: var(--text, #d6dde8);
}

.col-num {
  text-align: right;
  font-family: var(--mono, monospace);
  width: 64px;
}

.col-date {
  font-size: 11px;
  color: var(--text-dim, #9aa4b2);
  white-space: nowrap;
}

.col-ops {
  text-align: right;
  white-space: nowrap;
  width: 196px;
}

.col-ops .btn + .btn {
  margin-left: 6px;
}

.project-name {
  margin-right: 6px;
}

.tag {
  padding: 0 5px;
  font-size: 10px;
  color: var(--text-faint, #7d8794);
  border: 1px solid var(--border-strong, #3a4150);
  border-radius: 3px;
}

.rename-input {
  width: 100%;
  height: 24px;
  padding: 0 6px;
  font-family: inherit;
  font-size: 12px;
  color: var(--text, #d6dde8);
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid var(--accent, #4f8cff);
  border-radius: 3px;
  outline: none;
}

.purge-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 12px;
  font-size: 11.5px;
  color: var(--text-dim, #9aa4b2);
  cursor: pointer;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 10px 16px;
  border-top: 1px solid var(--border, #2c3340);
}

.btn-danger {
  color: #ffb4ae;
  border-color: rgba(229, 83, 75, 0.5);
}

.btn-danger:hover:not(:disabled) {
  background: rgba(229, 83, 75, 0.18);
}
</style>

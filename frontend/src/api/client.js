/**
 * WordCut 前端 API 客户端。
 *
 * 说明：
 *  - 所有字段名严格对齐 src/api.py 里的 Pydantic 响应模型：
 *      ProjectCreated   -> { id, name, sample_rate, duration }
 *      TranscribeResponse -> { project_id, words[], duration, elapsed_seconds, model }
 *      TokenListResponse  -> { project_id, sample_rate, duration, tokens[],
 *                              deleted_ids[], segments[] }
 *      TokenOut           -> { id, text, start, end, is_deleted, confidence }
 *      SegmentOut         -> { index, start, end, duration, text, token_ids[] }
 *      EditStateResponse  -> { project_id, deleted_ids[], deletes[], total_deleted }
 *      DeleteSpanOut      -> { start, end, token_id, cause }
 *      ExportResponse     -> { project_id, output_duration, paths, token_count,
 *                              deleted_count, total_deleted }
 *      PreviewResponse    -> { project_id, format, content }
 *  - 时间单位一律为秒（float），见 docs/不变量.md 不变量 2。
 *  - baseURL 取自 import.meta.env.VITE_API_BASE，未配置时回落到 vite 代理
 *    可用的同源地址（见下）；对外导出常量 API_BASE 便于界面显示。
 *
 * 关于 baseURL 的双通道策略：
 *  - 默认直连 http://127.0.0.1:8000（后端 uvicorn 的默认监听地址）；
 *  - 若直连失败（例如后端未开 CORS，浏览器拦截跨域请求），自动回落到同源
 *    相对路径 /api/...，由 vite.config.js 里的 server.proxy 转发到后端。
 *  - 也可以在 frontend/.env.local 里写 VITE_API_BASE= 显式走代理。
 */

/** 后端默认地址（与 src/api.py 的本地调试入口一致）。 */
const DEFAULT_API_BASE = 'http://127.0.0.1:8000'

/** 实际使用的后端地址；环境变量优先，末尾斜杠会被去掉。 */
export const API_BASE = (
  import.meta.env.VITE_API_BASE ?? DEFAULT_API_BASE
).replace(/\/+$/, '')

/** 统一的接口错误对象：携带 HTTP 状态码与后端 detail。 */
export class ApiError extends Error {
  constructor(message, status = 0, detail = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** 把 FastAPI 的 detail（字符串 / 校验错误数组 / 对象）拼成人能看的一段话。 */
function formatDetail(detail) {
  if (detail === null || detail === undefined || detail === '') return ''
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === 'object') {
          const loc = Array.isArray(item.loc) ? item.loc.join('.') : ''
          return loc ? `${loc}: ${item.msg ?? JSON.stringify(item)}` : item.msg ?? JSON.stringify(item)
        }
        return String(item)
      })
      .join('；')
  }
  if (typeof detail === 'object') return detail.msg ?? JSON.stringify(detail)
  return String(detail)
}

/** 构造 fetch 的 options（FormData 不设置 Content-Type，交给浏览器加 boundary）。 */
function buildOptions({ method = 'GET', body }) {
  const options = { method, headers: {} }
  if (body === undefined || body === null) return options
  if (typeof FormData !== 'undefined' && body instanceof FormData) {
    options.body = body
    return options
  }
  options.headers['Content-Type'] = 'application/json'
  options.body = JSON.stringify(body)
  return options
}

/** 解析响应体：优先 JSON，其次纯文本。 */
async function readBody(response) {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

/** 核心请求函数：直连失败时自动回落同源代理。 */
async function request(path, options = {}) {
  const fetchOptions = buildOptions(options)
  let response = null

  try {
    response = await fetch(`${API_BASE}${path}`, fetchOptions)
  } catch (directError) {
    // 直连失败（后端未起 / 跨域被拦）：回落到同源相对路径走 vite 代理。
    if (!API_BASE) {
      throw new ApiError(`无法连接后端服务：${directError.message}`, 0, null)
    }
    try {
      response = await fetch(path, fetchOptions)
    } catch (proxyError) {
      throw new ApiError(
        `无法连接后端服务（${API_BASE} 与同源代理均不可用）：${proxyError.message}`,
        0,
        null,
      )
    }
  }

  const data = await readBody(response)
  if (!response.ok) {
    const detail = data && typeof data === 'object' ? data.detail ?? data : data
    const message = formatDetail(detail) || `请求失败（HTTP ${response.status}）`
    throw new ApiError(message, response.status, detail)
  }
  return data
}

/** 项目 id 编码到路径里。 */
function projectPath(projectId, suffix) {
  return `/api/projects/${encodeURIComponent(String(projectId))}${suffix}`
}

// ----------------------------------------------------------------------
// 接口封装
// ----------------------------------------------------------------------

/** GET /api/health -> { status, version, project_count, sample_rate } */
export function health() {
  return request('/api/health')
}

/** GET /api/projects -> ProjectSummary[] */
export function listProjects() {
  return request('/api/projects')
}

/**
 * POST /api/projects （multipart：file + name）
 * @param {{ file: File, name: string }} payload
 * @returns {Promise<{id: string, name: string, sample_rate: number, duration: number}>}
 */
export function createProject({ file, name }) {
  if (!file) throw new ApiError('未选择音频文件', 0, null)
  const form = new FormData()
  form.append('file', file, file.name || 'audio')
  form.append('name', name ?? '')
  return request('/api/projects', { method: 'POST', body: form })
}

/**
 * POST /api/projects/{pid}/transcribe -> TranscribeResponse
 * @param {string} projectId
 */
export function transcribeProject(projectId) {
  return request(projectPath(projectId, '/transcribe'), { method: 'POST' })
}

/**
 * GET /api/projects/{pid}/tokens -> TokenListResponse
 * @param {string} projectId
 */
export function fetchTokens(projectId) {
  return request(projectPath(projectId, '/tokens'))
}

/**
 * POST /api/projects/{pid}/delete -> EditStateResponse
 * @param {string} projectId
 * @param {string[]} tokenIds
 */
export function deleteTokens(projectId, tokenIds) {
  return request(projectPath(projectId, '/delete'), {
    method: 'POST',
    body: { token_ids: [...tokenIds] },
  })
}

/**
 * POST /api/projects/{pid}/restore -> EditStateResponse
 * @param {string} projectId
 * @param {string[]} tokenIds
 */
export function restoreTokens(projectId, tokenIds) {
  return request(projectPath(projectId, '/restore'), {
    method: 'POST',
    body: { token_ids: [...tokenIds] },
  })
}

/**
 * POST /api/projects/{pid}/export -> ExportResponse
 * @param {string} projectId
 */
/**
 * POST /api/projects/{pid}/export -> ExportResponse
 *
 * @param {string} projectId
 * @param {{ audio_format?: string, export_video?: boolean, video_mode?: string }} [options]
 *   与后端 ExportRequest 完全对齐；不传时后端使用默认值（wav / 不导出视频）。
 */
/**
 * POST /api/projects/{pid}/save -> SaveProjectResponse
 *
 * @param {string} projectId
 * @param {string} path 工程文件保存路径（.wcproj）
 */
export function saveProject(projectId, path) {
  return request(projectPath(projectId, '/save'), {
    method: 'POST',
    body: { path },
  })
}

/**
 * POST /api/projects/load -> LoadProjectResponse
 *
 * @param {string} path 工程文件路径（.wcproj）
 */
export function loadProject(path) {
  return request('/api/projects/load', {
    method: 'POST',
    body: { path },
  })
}

export function exportProject(projectId, options = {}) {
  const body = {
    audio_format: options.audio_format ?? 'wav',
    export_video: Boolean(options.export_video),
    video_mode: options.video_mode ?? 'precise',
  }
  return request(projectPath(projectId, '/export'), { method: 'POST', body })
}

/**
 * GET /api/projects/{pid}/preview?srt=true|false -> PreviewResponse
 * @param {string} projectId
 * @param {{ srt?: boolean }} [options]
 */
export function previewSubtitle(projectId, { srt = true } = {}) {
  const query = srt ? 'true' : 'false'
  return request(projectPath(projectId, `/preview?srt=${query}`))
}

export default {
  API_BASE,
  ApiError,
  health,
  listProjects,
  createProject,
  transcribeProject,
  fetchTokens,
  deleteTokens,
  restoreTokens,
  exportProject,
  previewSubtitle,
}

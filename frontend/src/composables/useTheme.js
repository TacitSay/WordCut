/**
 * 主题切换（浅色 / 深色）。
 *
 * 存两处：
 *   - `localStorage`：浏览器刷新与下次打开都记得（用户偏好）
 *   - 后端设置（可选）：做成"跨设备/跨浏览器"的一致体验
 *
 * 为什么主题要有后端设置：这个项目是本机跑的，换浏览器打开很常见；
 * 只存 localStorage 会导致"在主浏览器设成深色，换个浏览器又变浅色"。
 * 但后端不可达时绝不能因此没有主题——所以 localStorage 是权威回退。
 *
 * 落地方式：在 `<html>` 上写 `data-theme="light|dark"`，
 * CSS 里 `:root[data-theme='dark']` 覆盖整套 token。
 */

/** 存储键。 */
const STORAGE_KEY = 'wordcut.theme'

/** 可选主题。 */
export const THEMES = Object.freeze(['light', 'dark'])

/** 默认主题：浅色。 */
export const DEFAULT_THEME = 'light'

/**
 * 取当前生效主题。
 *
 * 优先 localStorage，其次 `<html data-theme>`（服务端渲染/首屏内联时写入），
 * 最后回落到默认浅色。
 *
 * @returns {'light'|'dark'}
 */
export function getTheme() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch {
    /* 隐私模式下 localStorage 可能抛异常，忽略 */
  }
  const attr = document.documentElement.getAttribute('data-theme')
  return attr === 'dark' ? 'dark' : DEFAULT_THEME
}

/**
 * 应用主题：写 `<html data-theme>` 并持久化到 localStorage。
 *
 * @param {'light'|'dark'} theme
 * @param {{ persist?: boolean }} [options] persist=false 时只改不存（用于首屏回填）
 */
export function applyTheme(theme, { persist = true } = {}) {
  const next = theme === 'dark' ? 'dark' : 'light'
  document.documentElement.setAttribute('data-theme', next)
  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      /* 存不了就算了，本次会话依然生效 */
    }
  }
  return next
}

/**
 * 在另一个主题间切换。
 * @returns {'light'|'dark'} 切换后的主题
 */
export function toggleTheme() {
  return applyTheme(getTheme() === 'dark' ? 'light' : 'dark')
}

/**
 * 首屏初始化：在 Vue 挂载前调用，避免"先闪一下浅色再变深色"。
 *
 * 逻辑：localStorage 有值就用它；没有就尝试问后端设置；都没有就用浅色。
 * 后端请求是异步的，所以先用本地值渲染，拿到后再纠正。
 */
export function initTheme() {
  const local = getTheme()
  applyTheme(local, { persist: false })
  return local
}

/**
 * 从后端设置同步主题（可选步骤，失败静默）。
 *
 * @param {(theme: string) => void} [onChange]
 * @returns {Promise<'light'|'dark'|null>} 后端给出的主题（没有则 null）
 */
export async function syncThemeFromServer(fetchSettings, onChange) {
  try {
    const data = await fetchSettings()
    const remote = String(data?.theme || '').trim()
    if (remote !== 'light' && remote !== 'dark') return null
    const local = getTheme()
    // 本地已经明确选过，就以本地为准（本地是用户最近一次操作的直接结果）
    if (local === remote) return remote
    const applied = applyTheme(remote)
    onChange?.(applied)
    return applied
  } catch {
    return null
  }
}

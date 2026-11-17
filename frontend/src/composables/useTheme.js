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
 *
 * 带圆形扩散动画（View Transitions API）：从按钮位置向外铺开新主题。
 * 浏览器不支持、或用户开了"减少动态效果"时退化为瞬时切换——功能不受影响。
 *
 * @param {HTMLElement|null} [originEl] 动画圆心元素（通常是按钮本身）
 * @returns {'light'|'dark'} 切换后的主题
 */
export function toggleTheme(originEl = null) {
  const next = getTheme() === 'dark' ? 'light' : 'dark'
  return switchTheme(next, originEl)
}

/** 是否应播放动画（API 可用且用户未要求减少动效）。 */
function canAnimate() {
  if (typeof document === 'undefined') return false
  // 第一版动画基于 View Transitions；不支持时直接瞬时切换
  if (typeof document.startViewTransition !== 'function') return false
  try {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return false
  } catch {
    return false
  }
  return true
}

/**
 * 带过渡地切换到指定主题。
 *
 * 方向：切到深色 = 圆形从按钮处扩散到全屏（墨滴落下）；
 *       切到浅色 = 圆形从全屏收拢回按钮（墨迹退去）。
 *
 * @param {'light'|'dark'} theme
 * @param {HTMLElement|null} [originEl]
 * @returns {'light'|'dark'}
 */
/**
 * 带过渡地切换到指定主题。
 *
 * 视觉语义（与需求一致）：
 *   - **变浅**：浅色从按钮处以圆形向外扩散，把深色盖掉；
 *   - **变深**：浅色层从全屏向按钮处收拢，深色由外向内取代它。
 *
 * 为什么不用 View Transitions：它只能拿到"新旧两层的快照"，变浅时让新层
 * （浅色）收缩，下面露出的却是旧深色快照，于是出现"白色收缩 → 全黑 → 拆
 * 快照后闪现白"。现在自己盖一层"浅色遮罩"，只移动这一层，底下始终是
 * 即将生效的那个主题，任何一帧都不会出现错误颜色。
 *
 * @param {'light'|'dark'} theme
 * @param {HTMLElement|null} [originEl]
 * @returns {'light'|'dark'}
 */
export function switchTheme(theme, originEl = null) {
  if (!canAnimate()) return applyTheme(theme)

  // 圆心：按钮中心；拿不到元素就退回视口中心
  let cx = window.innerWidth / 2
  let cy = window.innerHeight / 2
  if (originEl && typeof originEl.getBoundingClientRect === 'function') {
    const rect = originEl.getBoundingClientRect()
    cx = rect.left + rect.width / 2
    cy = rect.top + rect.height / 2
  }

  // 覆盖全屏所需半径：圆心到四角的最远距离
  const radius = Math.hypot(
    Math.max(cx, window.innerWidth - cx),
    Math.max(cy, window.innerHeight - cy),
  )

  const collapsed = `circle(0px at ${cx}px ${cy}px)`
  const expanded = `circle(${radius}px at ${cx}px ${cy}px)`

  // ------------------------------------------------------------------
  // 第一版的思路：用 View Transitions 拿到"新主题那一层"，让它从按钮处
  // 圆形扩散到全屏。新层扩散时，它下面的旧层被一点点盖住 —— 眼睛看到的是
  // 新主题边扩边显现，没有整屏停顿。
  //
  // 第一版唯一的 bug：变浅时写成了 [expanded, collapsed]（让新层收缩）。
  // 新层是浅色，一缩就露出底下的旧深色快照，于是出现
  // 「白色收缩 → 全黑 → 拆快照后闪现白」。
  //
  // 修正：**两个方向都让新层从 0 扩到全屏**。方向差异本来就由"新层是什么
  // 颜色"体现，不需要再反转 clip-path。
  // ------------------------------------------------------------------
  const transition = document.startViewTransition(() => {
    applyTheme(theme)
  })

  transition.ready
    .then(() => {
      document.documentElement.animate(
        { clipPath: [collapsed, expanded] },
        {
          duration: 520,
          easing: 'cubic-bezier(0.4, 0, 0.2, 1)',
          pseudoElement: '::view-transition-new(root)',
        },
      )
    })
    .catch(() => {
      /* 动画失败不影响"主题已切换"这个事实 */
    })

  return theme
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

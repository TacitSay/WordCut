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
  if (typeof document === 'undefined' || typeof window === 'undefined') return false
  // 依赖 Web Animations API（Element.animate）驱动遮罩的 clip-path
  if (typeof Element === 'undefined' || typeof Element.prototype.animate !== 'function') {
    return false
  }
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
 * 读取某个主题的"底色"（用于绘制过渡遮罩）。
 *
 * 做法：临时给 `<html>` 打上目标主题、读 CSS 变量、立刻还原。
 * 因为整个过程不发生布局刷新（同步读、同步还原），浏览器不会绘制中间态。
 *
 * @param {'light'|'dark'} theme
 * @returns {string} 可直接用于 background 的颜色值
 */
function surfaceColor(theme) {
  const root = document.documentElement
  const prev = root.getAttribute('data-theme')
  root.setAttribute('data-theme', theme)
  let color = ''
  try {
    color = getComputedStyle(root).getPropertyValue('--bg-reading').trim()
  } catch {
    color = ''
  }
  if (prev === null) root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', prev)
  // CSS 变量读不到时回落到当前两套主题的实际值（与 style.css 保持一致）
  return color || (theme === 'dark' ? '#282d34' : '#dde2ea')
}

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

  // 旧主题必须在 applyTheme 之前取：遮罩要用它的底色
  const oldTheme = getTheme()

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
  // 正确模型：**样式立刻切到新主题**，用旧色遮罩挖一个洞，让洞从小扩大。
  //
  // 洞之外是旧色（盖住新样式），洞之内已经是新样式。洞每扩大一点，
  // 就多显现一点新主题的组件——所以是"边铺开边显现"，不是"铺满后一起显现"。
  //
  // 走过的三条弯路（都写在这里，别再改回去）：
  //
  // 1. View Transitions 收缩"新层"（浅色）→ 底下露旧深色快照 →
  //    「白收缩 → 全黑 → 闪白」。
  // 2. 变深时先铺满浅色遮罩再收缩 → 整屏纯白的一帧 + 样式重算压在同一帧 →
  //    「白屏顿一下」。
  // 3. 遮罩 = 目标色、且**动画结束才换主题** → 铺开期间底下仍是旧主题，
  //    组件要等遮罩收完才换脸 → 「铺满之后才显现」。
  //
  // 关键区别：遮罩颜色必须是**旧主题**色（它演的是"正在被取代的旧面"），
  // 并且换主题要发生在动画**开始之前**。
  // ------------------------------------------------------------------
  const overlay = document.createElement('div')
  overlay.className = 'theme-wipe'
  overlay.setAttribute('aria-hidden', 'true')
  overlay.style.cssText = [
    'position:fixed',
    'inset:0',
    'z-index:2147483000',
    'pointer-events:none',
    // 旧主题底色：洞外面看到的"还没被换掉的部分"
    `background:${surfaceColor(oldTheme)}`,
    `clip-path:${collapsed}`,
  ].join(';')
  document.body.appendChild(overlay)

  // 强制一次样式计算，确保初始 clip（半径 0，整屏旧色）已经生效，
  // 否则可能先渲染出一帧"完全新主题"
  void overlay.offsetWidth

  // 先把新主题应用上去：此刻它被整屏旧色遮罩完全盖住，看不到跳变，
  // 但接下来洞一扩大，露出的就是新样式。
  applyTheme(theme)

  const animation = overlay.animate([{ clipPath: collapsed }, { clipPath: expanded }], {
    duration: 520,
    easing: 'cubic-bezier(0.4, 0, 0.2, 1)',
    fill: 'forwards',
  })

  // 洞扩到覆盖全屏时遮罩已无意义，直接移除（无需再等一帧换主题）
  const finish = () => overlay.remove()
  animation.finished.then(finish).catch(finish)

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

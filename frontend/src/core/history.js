/**
 * 通用撤销 / 重做命令栈（History）。
 *
 * 背景：App.vue 里原先的 undoStack 只能记录 { kind, ids } 两种操作，
 * 既没有 redo，也无法承载「其他编辑」（插入字幕文本、调整剪辑点、批量静音…）。
 * 本模块把「编辑操作」抽象成一条可逆记录（entry），任何编辑都能接入：
 *
 *   entry = {
 *     label:      string | ((entry) => string),  // 菜单 / 状态栏展示用
 *     undo:       async (this: entry) => any,    // 反向操作，返回后端结果供调用方应用
 *     redo:       async (this: entry) => any,    // 正向操作（重做）
 *     mergeKey?:  string,                        // 相同 mergeKey 且窗口内 → 合并成一条
 *     ids?:       string[],                      // Token id 列表（token 类记录用）
 *     meta?:      object,                        // 任意附加信息（不参与合并判断）
 *   }
 *
 * 设计约束（与 docs/不变量.md、docs/术语表.md 对齐）：
 *  - 时间单位一律秒（float），本模块本身不碰时间戳，只透传后端返回；
 *  - Token id 只增删引用、绝不重排（不变量 4），因此历史记录只保存 id 集合；
 *  - 纯 ES module，除 Vue 的 reactive（响应式状态）外不依赖任何东西；
 *  - undo / redo 内部出错时**绝不**破坏栈状态：只有 await 成功后才移动栈。
 *
 * 典型用法（替换 App.vue 里的 undoStack）：
 *
 *   import { createHistory, createTokenEditEntry } from './core/history.js'
 *   import { deleteTokens, restoreTokens } from './api/client.js'
 *
 *   const history = createHistory()                 // maxSize=100, mergeWindow=800
 *   const tokenApi = { deleteTokens, restoreTokens, projectId: () => project.value?.id }
 *
 *   // 执行一次删除：先调接口拿到最新编辑状态，再压入历史记录
 *   const state = await deleteTokens(project.value.id, ids)
 *   applyEditState(state)
 *   history.push(createTokenEditEntry(ids, 'delete', tokenApi))
 *
 *   // Ctrl+Z / Ctrl+Shift+Z
 *   if (history.canUndo) applyEditState(await history.undo())
 *   if (history.canRedo) applyEditState(await history.redo())
 *
 * 菜单可用性（core/commands.js 的 commandCtx）直接读 getter 即可，
 * 因为 canUndo / canRedo 内部读到的是 reactive 状态，Vue 会自动追踪：
 *
 *   get canUndo() { return history.canUndo }
 *   get canRedo() { return history.canRedo }
 */

import { reactive } from 'vue'

/** 默认历史深度上限。 */
export const DEFAULT_MAX_SIZE = 100

/** 默认合并窗口（毫秒）：窗口内连续同类操作合并成一条记录。 */
export const DEFAULT_MERGE_WINDOW = 800

/** Token 编辑动作的中文名（label 默认文案用）。 */
const KIND_LABELS = {
  delete: '删除',
  restore: '恢复',
}

// ----------------------------------------------------------------------
// 内部工具
// ----------------------------------------------------------------------
function now() {
  return Date.now()
}

/** 取正整数，非法值回落到 fallback。 */
function toPositiveInt(value, fallback) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return fallback
  const int = Math.floor(parsed)
  return int > 0 ? int : fallback
}

/** 取非负整数，非法值回落到 fallback（mergeWindow 允许 0）。 */
function toNonNegativeInt(value, fallback) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return fallback
  const int = Math.floor(parsed)
  return int >= 0 ? int : fallback
}

/** 去重并保持首次出现顺序。 */
function dedupeIds(list) {
  const seen = new Set()
  const out = []
  for (const id of list) {
    if (typeof id !== 'string' || !id) continue
    if (seen.has(id)) continue
    seen.add(id)
    out.push(id)
  }
  return out
}

/** 两个 id 列表求并集（保持顺序，去重）。 */
function unionIds(left, right) {
  return dedupeIds([...(Array.isArray(left) ? left : []), ...(Array.isArray(right) ? right : [])])
}

/** 合并 meta（后者覆盖前者）；任一侧不是普通对象时退化为「取存在的那个」。 */
function mergeMeta(left, right) {
  const isPlain = (value) =>
    Boolean(value) && typeof value === 'object' && !Array.isArray(value)
  if (!isPlain(left)) return right ?? null
  if (!isPlain(right)) return left ?? null
  return { ...left, ...right }
}

/**
 * 解析 label：字符串直接用；函数则以记录本身为参数求值。
 *
 * 之所以允许函数：合并后 ids 数量会变（连续删 5 次 → 一条记录），
 * 静态字符串会显示成过期数量，函数能拿到合并后的最终 ids。
 *
 * @param {object|null|undefined} entry
 * @returns {string}
 */
export function resolveLabel(entry) {
  if (!entry) return ''
  const label = entry.label
  if (typeof label === 'function') {
    try {
      const text = label(entry)
      return text === null || text === undefined ? '' : String(text)
    } catch {
      return ''
    }
  }
  return label === null || label === undefined ? '' : String(label)
}

/**
 * 校验并规范化一条历史记录。
 * @param {object} entry
 * @returns {object} 规范化后的新对象（不改动入参）
 */
function normalizeEntry(entry) {
  if (!entry || typeof entry !== 'object') {
    throw new Error('[history] push/entry 必须是对象')
  }
  if (typeof entry.undo !== 'function') {
    throw new Error('[history] 记录必须提供 undo() 函数')
  }
  if (typeof entry.redo !== 'function') {
    throw new Error('[history] 记录必须提供 redo() 函数')
  }
  return {
    ...entry,
    label: entry.label ?? '',
    // 空字符串 / null / undefined 一律视为「不参与合并」
    mergeKey: entry.mergeKey === null || entry.mergeKey === undefined ? '' : String(entry.mergeKey),
    ids: Array.isArray(entry.ids) ? dedupeIds(entry.ids) : [],
  }
}

// ----------------------------------------------------------------------
// History
// ----------------------------------------------------------------------
export class History {
  /**
   * @param {{ maxSize?: number, mergeWindow?: number }} [options]
   *   maxSize：历史深度上限（默认 100），超出丢弃最旧的记录；
   *   mergeWindow：合并时间窗（毫秒，默认 800），窗口内同 mergeKey 合并成一条。
   */
  constructor(options = {}) {
    /** 历史深度上限 */
    this.maxSize = toPositiveInt(options.maxSize, DEFAULT_MAX_SIZE)
    /** 合并时间窗（毫秒） */
    this.mergeWindow = toNonNegativeInt(options.mergeWindow, DEFAULT_MERGE_WINDOW)
    /**
     * 响应式状态。数组本身是 reactive，因此 push / splice 后
     * 依赖 canUndo 的组件渲染与 computed 会自动更新。
     *
     * 注意：记录里带函数，Vue 不会代理函数，只会代理记录这个普通对象；
     * 这不影响调用（proxy.undo() 的 this 仍是该记录）。
     */
    this.state = reactive({
      /** 撤销栈：数组尾部为最近一次操作 */
      undoStack: [],
      /** 重做栈：数组尾部为最近一次被撤销的操作 */
      redoStack: [],
      /** 是否正在执行 undo / redo（异步互斥，避免连点造成栈错乱） */
      busy: false,
    })
  }

  // ---------------- 只读视图 ----------------
  /** 撤销栈（响应式数组本身，勿直接改动） */
  get undoStack() {
    return this.state.undoStack
  }

  /** 重做栈（响应式数组本身，勿直接改动） */
  get redoStack() {
    return this.state.redoStack
  }

  /** 正在执行 undo / redo */
  get busy() {
    return this.state.busy
  }

  /** 可撤销（执行中为 false，防止连点重入） */
  get canUndo() {
    return !this.state.busy && this.state.undoStack.length > 0
  }

  /** 可重做（执行中为 false） */
  get canRedo() {
    return !this.state.busy && this.state.redoStack.length > 0
  }

  /** 下一条将被撤销的记录 */
  get peekUndo() {
    return this.state.undoStack[this.state.undoStack.length - 1] ?? null
  }

  /** 下一条将被重做的记录 */
  get peekRedo() {
    return this.state.redoStack[this.state.redoStack.length - 1] ?? null
  }

  /**
   * 下一条将被撤销的记录的 label（描述该记录的正向动作，如「删除 3 个 Token」），
   * UI 自行拼成「撤销·删除 3 个 Token」；空栈或执行中返回空串。
   */
  get undoLabel() {
    return this.state.busy ? '' : resolveLabel(this.peekUndo)
  }

  /** 下一条将被重做的记录的 label（同上，由 UI 拼「重做·…」），空栈返回空串 */
  get redoLabel() {
    return this.state.busy ? '' : resolveLabel(this.peekRedo)
  }

  /** 当前可撤销的记录条数 */
  get size() {
    return this.state.undoStack.length
  }

  // ---------------- 写入 ----------------
  /**
   * 压入一条新记录，并清空 redo 栈（标准行为：新分支产生后旧的重做失效）。
   *
   * 合并规则：若栈顶记录与新记录的 mergeKey 相同（非空），且距栈顶最后一次
   * 追加的时间不超过 mergeWindow，则合并成一条：
   *   - undo 保留**最早**那条（撤销要回到本组操作开始前的状态）；
   *   - redo 更新为**最新**那条（重做要落在本组操作结束后的状态）；
   *   - ids 取并集（合并后的 undo / redo 会作用于全体 id）；
   *   - 时间戳刷新为当前时刻（滚动窗口：手不停，就一直合并成一条）。
   *
   * @param {object} rawEntry 见文件头 entry 结构说明
   * @returns {object} 实际入栈的记录
   */
  push(rawEntry) {
    if (this.state.busy) {
      throw new Error('[history] 正在执行撤销 / 重做，暂时不能压入新记录')
    }

    const entry = normalizeEntry(rawEntry)
    const timestamp = now()
    const stack = this.state.undoStack
    const top = stack[stack.length - 1]

    // 1. 任何新记录都会让原有的重做失效
    this.state.redoStack.splice(0)

    // 2. 尝试与栈顶合并
    if (this._canMerge(top, entry, timestamp)) {
      const merged = {
        ...top,
        // 保留最早的 undo：撤销后回到整组操作开始前
        undo: top.undo,
        // 更新最新的 redo：重做后到达整组操作结束后
        redo: entry.redo,
        ids: unionIds(top.ids, entry.ids),
        mergeKey: entry.mergeKey,
        timestamp,
        mergedCount: (Number(top.mergedCount) || 1) + 1,
        meta: mergeMeta(top.meta, entry.meta),
      }
      // 用 splice 替换栈顶，而不是原地改记录：保证响应式且不污染旧引用
      stack.splice(stack.length - 1, 1, merged)
      this._prune(stack)
      return merged
    }

    // 3. 正常入栈
    const stored = { ...entry, timestamp, mergedCount: 1 }
    stack.push(stored)
    this._prune(stack)
    return stored
  }

  /**
   * 撤销栈顶一步。
   *
   * 语义保证：
   *   - 只有 entry.undo() 正常返回后，才把记录从 undo 栈移到 redo 栈；
   *   - entry.undo() 抛错时栈保持原样（不丢记录、不误移），错误原样抛给 UI 提示；
   *   - 成功时把 entry.undo() 的返回值（如后端 EditStateResponse）透传出去，
   *     由调用方 applyEditState() 应用。
   *
   * @returns {Promise<any>} 空栈时返回 undefined；否则返回 entry.undo() 的结果
   */
  async undo() {
    if (this.state.busy) {
      throw new Error('[history] 上一次撤销 / 重做尚未完成，请稍候')
    }
    const entry = this.peekUndo
    if (!entry) return undefined

    this.state.busy = true
    try {
      // 以 entry.undo() 形式调用：方法的 this 指向记录本身（含合并后的 ids 并集）
      const result = await entry.undo()
      this._move(this.state.undoStack, this.state.redoStack, entry)
      return result
    } finally {
      // 无论成功还是抛错都解除互斥；栈的移动只发生在上面成功分支里
      this.state.busy = false
    }
  }

  /**
   * 重做（把 redo 栈顶移回 undo 栈）。语义与 undo 完全对称。
   * @returns {Promise<any>} 空栈时返回 undefined
   */
  async redo() {
    if (this.state.busy) {
      throw new Error('[history] 上一次撤销 / 重做尚未完成，请稍候')
    }
    const entry = this.peekRedo
    if (!entry) return undefined

    this.state.busy = true
    try {
      const result = await entry.redo()
      this._move(this.state.redoStack, this.state.undoStack, entry)
      return result
    } finally {
      this.state.busy = false
    }
  }

  /** 清空全部历史（切换 / 新建项目、重新转写后调用） */
  clear() {
    this.state.undoStack.splice(0)
    this.state.redoStack.splice(0)
  }

  // ---------------- 内部 ----------------
  /**
   * 是否可以与栈顶合并。
   * @param {object|undefined} top
   * @param {object} entry
   * @param {number} timestamp
   */
  _canMerge(top, entry, timestamp) {
    if (!top) return false
    if (!entry.mergeKey) return false
    if (top.mergeKey !== entry.mergeKey) return false
    const last = Number(top.timestamp)
    if (!Number.isFinite(last)) return false
    return timestamp - last <= this.mergeWindow
  }

  /**
   * 把记录从 from 栈移到 to 栈（先在 from 中找到才移动，避免重复入栈）。
   * @param {object[]} from
   * @param {object[]} to
   * @param {object} entry
   */
  _move(from, to, entry) {
    const index = from.lastIndexOf(entry)
    if (index < 0) return
    from.splice(index, 1)
    to.push(entry)
    this._prune(to)
  }

  /**
   * 裁剪栈：超过 maxSize 时丢弃最旧的记录（数组头部）。
   * @param {object[]} stack
   */
  _prune(stack) {
    const extra = stack.length - this.maxSize
    if (extra > 0) stack.splice(0, extra)
  }
}

/**
 * 工厂函数（等价于 new History(options)）。
 * @param {{ maxSize?: number, mergeWindow?: number }} [options]
 * @returns {History}
 */
export function createHistory(options = {}) {
  return new History(options)
}

// ----------------------------------------------------------------------
// Token 删除 / 恢复记录
// ----------------------------------------------------------------------
/**
 * 解析项目 id：优先显式传入，其次 api.getProjectId()，最后 api.projectId。
 * 传函数时**每次调用时求值**，避免记录里存下已切换掉的旧项目 id。
 * @param {object} api
 * @param {string|Function|undefined} explicit options.projectId
 */
function resolveProjectId(api, explicit) {
  if (explicit !== undefined && explicit !== null && explicit !== '') {
    return typeof explicit === 'function' ? explicit() : explicit
  }
  if (typeof api.getProjectId === 'function') return api.getProjectId()
  const raw = api.projectId
  return typeof raw === 'function' ? raw() : raw
}

/**
 * 生成一条「删除 / 恢复 Token」的标准历史记录。
 *
 * undo 与 redo 分别调用对应的后端接口（src/api.py 的
 * POST /api/projects/{pid}/delete 与 /restore），并把返回的
 * EditStateResponse（{ project_id, deleted_ids, deletes, total_deleted }）
 * 原样返回，供调用方 applyEditState() 应用。
 *
 * @param {string[]|string} ids  受影响的 Token id（会自动去重）
 * @param {'delete'|'restore'} kind 这次操作的性质：delete=删除，restore=恢复
 * @param {object} api 后端接口集合，支持以下字段（任选其一，按顺序探测）：
 *   - deleteTokens(projectId, ids) / delete / removeTokens   （必需，删除接口）
 *   - restoreTokens(projectId, ids) / restore / addTokens    （必需，恢复接口）
 *   - projectId：string 或 () => string（也可用 getProjectId()）
 * @param {{ label?: string|Function, mergeKey?: string, projectId?: string|Function, meta?: object }} [options]
 *   label：自定义文案（默认「删除 N 个 Token / 恢复 N 个 Token」，N 取合并后 ids 数量）；
 *   mergeKey：合并键，默认 `token.<kind>` —— 连续同类操作（窗口内）合并成一条；
 *             传 null / '' 可关闭该记录的合并；
 *   projectId：覆盖 api 上的项目 id 解析；
 *   meta：附加信息，原样挂在记录上。
 * @returns {object} 可直接交给 history.push() 的记录
 */
export function createTokenEditEntry(ids, kind, api, options = {}) {
  if (kind !== 'delete' && kind !== 'restore') {
    throw new Error(`createTokenEditEntry: kind 只能是 'delete' 或 'restore'，收到 ${String(kind)}`)
  }
  const cleanIds = dedupeIds(Array.isArray(ids) ? ids : [ids])
  if (!cleanIds.length) {
    throw new Error('createTokenEditEntry: ids 不能为空')
  }
  if (!api || typeof api !== 'object') {
    throw new Error('createTokenEditEntry: 需要传入 api 对象（含 deleteTokens / restoreTokens）')
  }

  const deleteFn = api.deleteTokens ?? api.delete ?? api.removeTokens
  const restoreFn = api.restoreTokens ?? api.restore ?? api.addTokens
  if (typeof deleteFn !== 'function' || typeof restoreFn !== 'function') {
    throw new Error(
      'createTokenEditEntry: api 必须同时提供 deleteTokens 与 restoreTokens 函数',
    )
  }

  /**
   * 执行一次动作。
   *
   * 关键点：ids 从 `target`（调用时的记录本身）读取，而不是闭包里的 cleanIds。
   * History 合并记录时会生成 `{ ...top, redo: 最新redo, ids: 并集 }` 的新对象，
   * 并以 `entry.undo()` / `entry.redo()` 形式调用，因此这里的 `this` 就是合并后
   * 的记录 —— undo / redo 自然作用于全部 id 的并集。
   *
   * @param {object} target 记录（this）
   * @param {'delete'|'restore'} op
   */
  const run = (target, op) => {
    const source = Array.isArray(target?.ids) && target.ids.length ? target.ids : cleanIds
    const targetIds = dedupeIds(source)
    const projectId = resolveProjectId(api, options.projectId)
    if (!projectId) {
      throw new Error('[history] 缺少项目 id：请为 api 提供 projectId / getProjectId()')
    }
    return op === 'delete' ? deleteFn(projectId, targetIds) : restoreFn(projectId, targetIds)
  }

  const labelFor = (entry) =>
    `${KIND_LABELS[kind]} ${Array.isArray(entry?.ids) ? entry.ids.length : cleanIds.length} 个 Token`

  return {
    kind,
    // 默认用函数形式：合并后 ids 变多，文案自动跟着变成「删除 3 个 Token」
    label: options.label ?? labelFor,
    mergeKey: options.mergeKey === undefined ? `token.${kind}` : options.mergeKey,
    ids: cleanIds,
    meta: options.meta ?? { kind },

    // 注意：这里必须是普通方法（非箭头函数），this 才能指向「合并后的记录」
    undo() {
      return run(this, kind === 'delete' ? 'restore' : 'delete')
    },
    redo() {
      return run(this, kind)
    },
  }
}

export default {
  History,
  createHistory,
  createTokenEditEntry,
  resolveLabel,
  DEFAULT_MAX_SIZE,
  DEFAULT_MERGE_WINDOW,
}

/**
 * 命令注册表 —— 插件化的核心。
 *
 * 设计要点（见 docs/架构预留设计.md 第 4 节）：
 *  - 菜单栏、快捷键、右键菜单统一从这张表渲染；
 *  - 插件只需调用 registerCommand()，即可把能力挂进所有入口，无需改 UI 代码。
 *
 * 命令对象结构：
 *   {
 *     id:       'edit.delete',        // 全局唯一，插件以此为准
 *     title:    '删除选中',            // 菜单显示文本
 *     menu:     'edit',               // 归属菜单（file/edit/view/tools/playback）
 *     shortcut: 'Delete',             // 快捷键（可选，形如 'Ctrl+O' / 'Space'）
 *     handler:  (ctx) => void,        // 执行体，ctx 由 App 注入
 *     enabled:  (ctx) => boolean,     // 是否可用（可选，默认 true）
 *     plugin:   null,                 // 插件 id（内置命令为 null）
 *   }
 *
 * 不变量：本模块只做注册与查询，不持有业务状态。
 */

import { reactive } from 'vue'

/** 已注册命令：id -> command */
const registry = reactive(new Map())

/** 插件注册的命令 id，便于卸载 */
const pluginCommands = new Map()

/**
 * 注册命令。重复 id 会覆盖（便于插件热更新）。
 * @param {object} command
 * @returns {object} 注册后的命令
 */
export function registerCommand(command) {
  if (!command || typeof command.id !== 'string' || !command.id) {
    throw new Error('registerCommand: id 必须是非空字符串')
  }
  if (typeof command.handler !== 'function') {
    throw new Error(`registerCommand: 命令 ${command.id} 必须提供 handler 函数`)
  }
  const normalized = {
    menu: 'tools',
    shortcut: '',
    enabled: () => true,
    plugin: null,
    ...command,
  }
  registry.set(normalized.id, normalized)
  return normalized
}

/**
 * 批量注册。
 * @param {object[]} commands
 */
export function registerCommands(commands) {
  for (const command of commands) registerCommand(command)
}

/**
 * 注销命令。
 * @param {string} id
 */
export function unregisterCommand(id) {
  registry.delete(id)
}

/**
 * 按插件 id 注册一组命令（便于整体卸载）。
 * @param {string} pluginId
 * @param {object[]} commands
 */
export function registerPluginCommands(pluginId, commands) {
  const ids = []
  for (const command of commands) {
    registerCommand({ ...command, plugin: pluginId })
    ids.push(command.id)
  }
  pluginCommands.set(pluginId, ids)
  return ids
}

/**
 * 卸载某插件的全部命令。
 * @param {string} pluginId
 */
export function unregisterPluginCommands(pluginId) {
  const ids = pluginCommands.get(pluginId) || []
  for (const id of ids) registry.delete(id)
  pluginCommands.delete(pluginId)
}

/**
 * 取某菜单下的命令（按注册顺序）。
 * @param {string} menu
 * @returns {object[]}
 */
export function commandsOf(menu) {
  return [...registry.values()].filter((command) => command.menu === menu)
}

/** 全部命令（响应式）。 */
export function allCommands() {
  return [...registry.values()]
}

/**
 * 执行命令。
 * @param {string} id
 * @param {object} ctx 上下文（App 注入）
 */
export function runCommand(id, ctx) {
  const command = registry.get(id)
  if (!command) {
    console.warn(`[commands] 未注册的命令：${id}`)
    return
  }
  if (command.enabled && !command.enabled(ctx)) return
  try {
    command.handler(ctx)
  } catch (error) {
    console.error(`[commands] 命令 ${id} 执行失败`, error)
  }
}

/**
 * 快捷键映射表（key -> commandId）。同一快捷键后注册者覆盖先注册者。
 * @returns {Map<string, string>}
 */
export function shortcutMap() {
  const map = new Map()
  for (const command of registry.values()) {
    if (command.shortcut) map.set(normalizeShortcut(command.shortcut), command.id)
  }
  return map
}

/**
 * 规范化快捷键字符串：Ctrl+O / ctrl+o -> 'ctrl+o'。
 * @param {string} shortcut
 */
export function normalizeShortcut(shortcut) {
  return String(shortcut)
    .toLowerCase()
    .split('+')
    .map((part) => part.trim())
    .join('+')
}

/**
 * 由键盘事件生成规范化快捷键字符串。
 * @param {KeyboardEvent} event
 * @returns {string}
 */
export function shortcutFromEvent(event) {
  const parts = []
  if (event.ctrlKey || event.metaKey) parts.push('ctrl')
  if (event.altKey) parts.push('alt')
  if (event.shiftKey) parts.push('shift')
  const key = event.key
  if (!['Control', 'Alt', 'Shift', 'Meta'].includes(key)) {
    parts.push(key === ' ' ? 'space' : key.toLowerCase())
  }
  return parts.join('+')
}

/** 菜单定义（顺序即显示顺序）。插件可注册新菜单（menu 值不在表中时会追加）。 */
export const MENU_DEFS = [
  { id: 'file', title: '文件(F)' },
  { id: 'edit', title: '编辑(E)' },
  { id: 'view', title: '视图(V)' },
  { id: 'tools', title: '工具(T)' },
  { id: 'playback', title: '播放(P)' },
  { id: 'plugin', title: '插件', dynamic: true },
  { id: 'help', title: '帮助(H)' },
]

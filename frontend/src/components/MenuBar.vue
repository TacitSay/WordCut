<script setup>
/**
 * 菜单栏 —— 从命令注册表动态渲染。
 *
 * 结构：菜单栏标题（文件/编辑/…）→ 下拉项（来自 commandsOf(menu)）。
 * 插件只需 registerCommand({ menu: 'tools', ... }) 即可在这出现。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { MENU_DEFS, commandsOf } from '../core/commands.js'

const props = defineProps({
  /** 执行上下文，透传给命令的 enabled(ctx) */
  ctx: { type: Object, default: () => ({}) },
})

const emit = defineEmits(['run'])

const openMenu = ref('')
const rootRef = ref(null)

/** 每个菜单的命令列表（响应式：注册表变化会重算）。 */
const menus = computed(() =>
  MENU_DEFS.map((def) => ({
    ...def,
    items: commandsOf(def.id),
  })),
)

/** 插件菜单：显示已注册的插件命令（不含内置的空菜单项）。 */
const pluginCount = computed(
  () => commandsOf('plugin').filter((command) => command.plugin).length,
)

function toggle(menuId) {
  openMenu.value = openMenu.value === menuId ? '' : menuId
}

function hover(menuId) {
  if (openMenu.value && openMenu.value !== menuId) openMenu.value = menuId
}

function run(command) {
  if (command.enabled && !command.enabled(props.ctx)) return
  openMenu.value = ''
  emit('run', command.id)
}

function isEnabled(command) {
  return command.enabled ? Boolean(command.enabled(props.ctx)) : true
}

function closeOnOutside(event) {
  if (rootRef.value && !rootRef.value.contains(event.target)) openMenu.value = ''
}

function closeOnEscape(event) {
  if (event.key === 'Escape') openMenu.value = ''
}

onMounted(() => {
  document.addEventListener('click', closeOnOutside)
  document.addEventListener('keydown', closeOnEscape)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', closeOnOutside)
  document.removeEventListener('keydown', closeOnEscape)
})
</script>

<template>
  <nav ref="rootRef" class="menubar">
    <div
      v-for="menu in menus"
      :key="menu.id"
      class="menu-root"
      :class="{ open: openMenu === menu.id }"
    >
      <button
        class="menu-title"
        type="button"
        @click.stop="toggle(menu.id)"
        @mouseenter="hover(menu.id)"
      >
        {{ menu.title }}
        <span v-if="menu.id === 'plugin' && pluginCount" class="menu-badge">{{ pluginCount }}</span>
      </button>

      <div v-if="openMenu === menu.id" class="menu-dropdown" @click.stop>
        <template v-if="menu.items.length">
          <button
            v-for="item in menu.items"
            :key="item.id"
            class="menu-item"
            type="button"
            :disabled="!isEnabled(item)"
            @click="run(item)"
          >
            <span class="menu-item-title">{{ item.title }}</span>
            <span v-if="item.shortcut" class="menu-item-shortcut">{{ item.shortcut }}</span>
          </button>
        </template>
        <p v-else class="menu-empty">（暂无可用命令）</p>
      </div>
    </div>

    <slot name="extra" />
  </nav>
</template>

<style scoped>
.menubar {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: 0 0 auto;
  padding: 0 8px;
  height: 30px;
  background: var(--bg-toolbar);
  border-bottom: 1px solid var(--border);
  user-select: none;
}

.menu-root {
  position: relative;
}

.menu-title {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 24px;
  padding: 0 9px;
  font-family: inherit;
  font-size: 12.5px;
  color: var(--text);
  background: transparent;
  border: none;
  border-radius: 3px;
  cursor: pointer;
}

.menu-title:hover,
.menu-root.open .menu-title {
  background: #3a3a40;
  color: var(--text-strong);
}

.menu-badge {
  padding: 0 4px;
  font-size: 10px;
  line-height: 14px;
  color: #fff;
  background: var(--accent);
  border-radius: 7px;
}

.menu-dropdown {
  position: absolute;
  top: 100%;
  left: 0;
  z-index: 100;
  min-width: 200px;
  margin-top: 2px;
  padding: 4px;
  background: #252526;
  border: 1px solid var(--border-strong);
  border-radius: 4px;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.45);
}

.menu-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  width: 100%;
  padding: 5px 10px;
  font-family: inherit;
  font-size: 12.5px;
  color: var(--text);
  text-align: left;
  background: transparent;
  border: none;
  border-radius: 3px;
  cursor: pointer;
  white-space: nowrap;
}

.menu-item:hover:not(:disabled) {
  background: var(--accent);
  color: #fff;
}

.menu-item:disabled {
  color: var(--text-faint);
  cursor: not-allowed;
}

.menu-item-shortcut {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-faint);
}

.menu-item:hover:not(:disabled) .menu-item-shortcut {
  color: rgba(255, 255, 255, 0.75);
}

.menu-empty {
  margin: 0;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--text-faint);
}
</style>

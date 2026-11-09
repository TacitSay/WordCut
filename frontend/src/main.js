import { createApp } from 'vue'
import './style.css'
import App from './App.vue'
import { initTheme } from './composables/useTheme.js'

// 首屏主题在 index.html 的内联脚本里已经应用（避免闪烁），
// 这里再调一次用于兜底（比如内联脚本被 CSP 拦截的场景）。
initTheme()

createApp(App).mount('#app')

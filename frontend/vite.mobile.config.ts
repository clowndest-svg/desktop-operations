import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 手机版的构建配置：入口是 `mobile.html`，产物是 `dist-mobile`。
 *
 * 为什么不复用桌面那份产物：桌面上每一个面板都调 `window.pywebview`，手机上这个对象
 * 永远不存在——同一份 bundle 要么在手机上满屏"桥没准备好"，要么在桌面上多背一套手机
 * 代码。两份产物共用同一个 `src/`，只有入口不同，代价是一个 html 文件。
 */
export default defineConfig({
  // 入口 root=mobile，产物才叫 index.html —— Capacitor 的 WebView 只加载这个名字。
  root: 'mobile',
  publicDir: false,
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  // Capacitor 把页面装在 https://localhost 下，相对路径同样成立，且和桌面保持一致写法。
  base: './',
  build: {
    outDir: '../dist-mobile',
    emptyOutDir: true,
  },
})

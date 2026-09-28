import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// The desktop shell loads the bundle from jarvis/ui/web, so the build output
// goes straight there instead of a `dist/` someone has to copy by hand.
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  base: './',
  build: {
    outDir: '../jarvis/ui/web',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
  },
})

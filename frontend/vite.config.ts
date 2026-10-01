import { fileURLToPath, URL } from 'node:url'
import { defineConfig, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * Remove the `crossorigin` attribute Vite adds to every emitted script and link.
 *
 * The desktop shell loads this bundle from a `file://` URL (pywebview points the
 * window at `jarvis/ui/web/index.html`). With `crossorigin` present the browser
 * treats the fetch as a CORS request, the origin of a `file://` page is `null`,
 * and a local file carries no `Access-Control-Allow-Origin` — so **every** script
 * and stylesheet is blocked and the window renders as an empty near-black page.
 *
 * That failure is invisible from the Python side: pywebview reports a successful
 * window, the log says "opening desktop window", and nothing is logged because
 * the error exists only inside the webview's console. It shipped once. The
 * regression test in `tests/test_ui_bundle.py` and the check in
 * `scripts/build_desktop.py` exist so it cannot ship again.
 *
 * A `generateBundle` hook rather than `transformIndexHtml`: this runs on the
 * final emitted HTML, after every other plugin has had its turn, so there is no
 * ordering question about who added the attribute.
 */
function stripCrossorigin(): Plugin {
  return {
    name: 'jarvis-strip-crossorigin',
    enforce: 'post',
    generateBundle(_options, bundle) {
      for (const output of Object.values(bundle)) {
        if (output.type === 'asset' && output.fileName.endsWith('.html')) {
          output.source = String(output.source).replace(/\s+crossorigin(?:="[^"]*")?/g, '')
        }
      }
    },
  }
}

// The desktop shell loads the bundle from jarvis/ui/web, so the build output
// goes straight there instead of a `dist/` someone has to copy by hand.
export default defineConfig({
  plugins: [vue(), stripCrossorigin()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  // Relative asset URLs: an absolute `/assets/...` would resolve against the
  // filesystem root once the page is opened over `file://`.
  base: './',
  build: {
    outDir: '../jarvis/ui/web',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
  },
})

/** Acceptance of the actual packaged WebView2, using an isolated JARVIS_HOME. */
import fs from 'node:fs'
import path from 'node:path'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
const { chromium } = createRequire(import.meta.url)('playwright')
const port = process.argv[2] || '9336'
const output = path.resolve(process.argv[3] || 'reports/brand-r124/frozen')
fs.mkdirSync(output, { recursive: true })
const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`)
const page = browser.contexts().flatMap(context => context.pages()).find(page => !page.url().includes('mode=pet') && page.url().includes('127.0.0.1'))
assert.ok(page, 'packaged desktop page exists')
const checks = [], errors = []
page.on('pageerror', error => errors.push(error.message))
const api = (method, ...args) => page.evaluate(async ({ method, args }) => window.pywebview.api[method](...args), { method, args })
try {
  await page.waitForFunction(() => window.pywebview?.api && document.querySelector('.app__portrait canvas')?.dataset.mouth !== undefined)
  const info = await api('app_info')
  assert.equal(info.engine, 'pywebview')
  assert.ok(String(info.built).includes('2026-10-25'))
  assert.equal(await page.locator('.app__mock').count(), 0)
  assert.ok(await page.locator('.bar__mark').evaluate(image => image.complete && image.naturalWidth > 0))
  await page.screenshot({ path: path.join(output, 'home.png') })
  checks.push('actual packaged exe renders new logo, portrait and uncluttered home')
  await page.getByRole('button', { name: '功能', exact: true }).click()
  await page.getByRole('textbox', { name: '搜索功能' }).fill('系统')
  await page.locator('.feature-menu__groups button').first().click()
  await page.locator('.app__diagnostics canvas').first().waitFor()
  await page.screenshot({ path: path.join(output, 'systems.png') })
  await page.getByRole('button', { name: '关闭', exact: true }).click()
  checks.push('system diagnostics opens from unified menu in packaged app')
  await page.getByRole('button', { name: '功能', exact: true }).click()
  await page.getByRole('textbox', { name: '搜索功能' }).fill('语音')
  await page.locator('.feature-menu__groups button').first().click()
  const preview = api('tts_preview', 'zh-CN-XiaoxiaoNeural')
  await page.waitForFunction(() => Number(document.querySelector('.app__portrait canvas')?.dataset.mouth) > 0.1, null, { timeout: 30000 })
  await page.screenshot({ path: path.join(output, 'real-voice.png') })
  await page.getByRole('button', { name: '停下', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.app__portrait canvas')?.dataset.mouth === '0.000')
  const spoken = await preview
  assert.ok(spoken.ok, JSON.stringify(spoken))
  checks.push('packaged Python TTS -> bridge -> speaker analyser -> portrait, interruption closes mouth')
  await page.getByRole('button', { name: '对话选项', exact: true }).click()
  await page.getByRole('button', { name: '放大', exact: true }).click()
  const expanded = await page.locator('.chat--max').boundingBox()
  assert.ok(expanded.width > 900)
  await page.locator('.chat__close').click()
  checks.push('expanded chat remains operable in actual WebView2')
  assert.deepEqual(errors, [])
} finally {
  fs.writeFileSync(path.join(output, 'ui.json'), JSON.stringify({ checks, errors }, null, 2))
  console.log(JSON.stringify({ checks, errors }))
  await browser.close()
}

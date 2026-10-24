/** Real Chromium acceptance of branding, immersive layout and audio-driven mouth. */
import fs from 'node:fs'
import path from 'node:path'
import http from 'node:http'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

// Resolve Playwright from the environment (NODE_PATH may point at Codex's runtime).
const { chromium } = createRequire(import.meta.url)('playwright')
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = path.resolve(process.argv[2] || path.join(root, 'reports/brand-r124'))
fs.mkdirSync(output, { recursive: true })
const checks = []
const errors = []
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost')
  const mobile = url.pathname.startsWith('/mobile/')
  const base = path.join(root, mobile ? 'frontend/dist-mobile' : 'jarvis/ui/web')
  const relative = mobile ? url.pathname.slice(7) : url.pathname
  const file = path.resolve(base, '.' + (relative === '/' ? '/index.html' : relative))
  if (!file.startsWith(base + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return res.writeHead(404).end()
  res.setHeader('Content-Type', ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.webp': 'image/webp', '.png': 'image/png' })[path.extname(file)] || 'application/octet-stream')
  fs.createReadStream(file).pipe(res)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const url = `http://127.0.0.1:${server.address().port}`
const browser = await chromium.launch({ executablePath: process.env.XIAOYE_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--autoplay-policy=no-user-gesture-required'] })
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(url)
  const face = page.locator('.app__portrait canvas')
  await page.waitForFunction(() => document.querySelector('.app__portrait canvas')?.dataset.mouth !== undefined)
  assert.equal(await page.locator('html').getAttribute('data-skin'), 'moon')
  assert.ok(await page.locator('.bar__mark').evaluate(image => image.complete && image.naturalWidth > 0))
  assert.equal(await face.getAttribute('data-mouth'), '0.000')
  checks.push('new logo loaded, moon theme and silent closed mouth')
  await page.screenshot({ path: path.join(output, 'desktop.png') })
  assert.ok(await page.locator('.bar__actions > button').count() <= 3)
  assert.equal(await page.locator('.chat__options').isVisible(), false)
  await page.getByRole('button', { name: '功能', exact: true }).click()
  await page.getByRole('textbox', { name: '搜索功能' }).fill('语音')
  await page.getByRole('button', { name: '语音控制', exact: false }).click()
  await page.getByRole('button', { name: '试听', exact: true }).click()
  await page.waitForFunction(() => Number(document.querySelector('.app__portrait canvas')?.dataset.mouth) > 0.1)
  assert.ok(await page.evaluate(() => window.__jarvisAudioDebug().bytes_received > 0))
  await page.screenshot({ path: path.join(output, 'speaking.png') })
  await page.getByRole('button', { name: '停下', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.app__portrait canvas')?.dataset.mouth === '0.000')
  checks.push('actual PCM analyser opens mouth; stop closes it')
  await page.getByRole('button', { name: '关闭语音控制', exact: true }).click()
  await page.getByRole('button', { name: '功能', exact: true }).click()
  await page.getByRole('textbox', { name: '搜索功能' }).fill('系统')
  await page.getByRole('button', { name: '系统与工具', exact: false }).click()
  await page.locator('.app__diagnostics canvas').first().waitFor()
  await page.screenshot({ path: path.join(output, 'systems.png') })
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('.app__diagnostics').count(), 0)
  checks.push('system tools expand and close with Escape')
  await page.getByRole('button', { name: '对话选项', exact: true }).click()
  await page.getByRole('button', { name: '放大', exact: true }).click()
  const expanded = await page.locator('.chat--max').boundingBox()
  assert.ok(expanded.width > 1300)
  await page.screenshot({ path: path.join(output, 'expanded-chat.png') })
  await page.locator('.chat__close').click()
  checks.push('expanded chat still covers viewport and closes')
  await page.setViewportSize({ width: 900, height: 650 })
  await page.screenshot({ path: path.join(output, 'desktop-small.png') })
  const input = await page.locator('.chat__input').boundingBox()
  assert.ok(input.y >= 0 && input.y + input.height <= 650)
  checks.push('minimum desktop viewport keeps input reachable')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.getByRole('button', { name: '功能', exact: true }).click()
  await page.getByRole('button', { name: '语音控制', exact: false }).click()
  await page.getByRole('button', { name: '试听', exact: true }).click()
  await page.waitForFunction(() => Number(document.querySelector('.app__portrait canvas')?.dataset.mouth) > 0.1)
  checks.push('reduced motion preserves audio mouth')
  await page.getByRole('button', { name: '功能', exact: true }).click()
  await page.getByRole('textbox', { name: '搜索功能' }).fill('画图')
  await page.getByRole('button', { name: '画图', exact: false }).click()
  assert.ok(await page.locator('[role="dialog"]').first().isVisible())
  checks.push('searchable consolidated menu opens image creation')
  await page.locator('[role="dialog"]').getByRole('button', { name: '关闭', exact: true }).click()
  const destinations = [
    ['做视频', '视频生成'], ['提醒与助手', '助手能力'], ['连接手机', '手机接入'],
    ['手机投屏', '手机投屏'], ['电脑操作权限', '电脑控制权限'], ['外观', '皮肤'],
    ['扩展', '扩展'], ['操作记录', '最近动作'], ['提醒与告警', '告警'],
  ]
  for (const [label, dialogName] of destinations) {
    await page.getByRole('button', { name: '功能', exact: true }).click()
    await page.getByRole('textbox', { name: '搜索功能' }).fill(label)
    await page.locator('.feature-menu__groups button').first().click()
    const dialog = page.getByRole('dialog', { name: dialogName, exact: true })
    await dialog.waitFor()
    await dialog.getByRole('button', { name: '关闭', exact: true }).first().click()
    await dialog.waitFor({ state: 'hidden' })
  }
  checks.push('all consolidated feature destinations open and close')
  const realAudio = path.join(root, 'build/probe-audio.json')
  if (fs.existsSync(realAudio)) {
    await page.reload()
    await page.waitForFunction(() => document.querySelector('.app__portrait canvas')?.dataset.mouth !== undefined)
    await page.locator('.chat__input').click()
    const recording = JSON.parse(fs.readFileSync(realAudio, 'utf8'))
    await page.evaluate(messages => { for (const message of messages) window.__jarvisAudio(message) }, recording.messages)
    await page.waitForFunction(() => Number(document.querySelector('.app__portrait canvas')?.dataset.mouth) > 0.1)
    const debug = await page.evaluate(() => window.__jarvisAudioDebug())
    assert.ok(debug.bytes_received >= 400000)
    await page.screenshot({ path: path.join(output, 'real-tts-speaking.png') })
    await page.evaluate(() => window.__jarvisAudio({ flush: true }))
    await page.waitForFunction(() => document.querySelector('.app__portrait canvas')?.dataset.mouth === '0.000')
    checks.push('real Edge-TTS speech drives mouth; bridge interruption closes it')
  }
  const phone = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })
  phone.on('pageerror', error => errors.push(error.message))
  await phone.goto(url + '/mobile/')
  await phone.waitForFunction(() => document.querySelector('.mobile-portrait-backdrop canvas')?.dataset.mouth !== undefined)
  assert.ok(await phone.locator('.top__logo').evaluate(image => image.complete && image.naturalWidth > 0))
  await phone.screenshot({ path: path.join(output, 'mobile.png') })
  await phone.getByRole('button', { name: '会话', exact: true }).click()
  await phone.screenshot({ path: path.join(output, 'mobile-drawer.png') })
  checks.push('mobile logo, portrait and session drawer render')
  assert.deepEqual(errors, [])
} finally {
  await browser.close()
  server.close()
  fs.writeFileSync(path.join(output, 'ui.json'), JSON.stringify({ checks, errors }, null, 2))
  console.log(JSON.stringify({ checks, errors }))
}

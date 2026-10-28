/**
 * 写实形象的运动质量验收。
 *
 * 静态好看不等于动起来好看 —— 上一版每一帧拿出来都自然，动起来是"闪一下眼皮 + 嘴在
 * 抽搐"。所以这一份量的全是**时间序列**上的东西：眨眼有没有中间态、嘴巴相邻帧的
 * 跳变有多大。并且把"闭到最深"和"嘴张到最大"这两帧真实画面抓下来 —— 效果好不好
 * 最终要人眼看，但"抓不抓得到那一帧"是机器能保证的。
 *
 * 用法：node scripts/verify_portrait_motion.mjs [输出目录]
 */
import fs from 'node:fs'
import path from 'node:path'
import http from 'node:http'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const { chromium } = (() => {
  try { return require('playwright') } catch { return require('playwright-core') }
})()
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = path.resolve(process.argv[2] || path.join(root, 'reports/portrait-motion'))
fs.mkdirSync(output, { recursive: true })
const checks = []

const server = http.createServer((req, res) => {
  const relative = decodeURIComponent(new URL(req.url, 'http://localhost').pathname)
  const base = path.join(root, 'jarvis/ui/web')
  const file = path.resolve(base, '.' + (relative === '/' ? '/index.html' : relative))
  if (!file.startsWith(base + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return res.writeHead(404).end()
  const type = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.webp': 'image/webp' }[path.extname(file)]
  res.setHeader('Content-Type', type || 'application/octet-stream')
  fs.createReadStream(file).pipe(res)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))

const browser = await chromium.launch({
  executablePath: process.env.XIAOYE_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  headless: true,
  args: ['--autoplay-policy=no-user-gesture-required'],
})
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.setDefaultTimeout(25000)
const crashes = []
page.on('pageerror', error => crashes.push(error.message))

const speak = (hertz, seconds) => page.evaluate(([hz, sec]) => {
  const rate = 24000
  const pcm = new Int16Array(rate * sec)
  for (let index = 0; index < pcm.length; index += 1) {
    const t = index / rate
    const syllable = Math.max(0, Math.sin(Math.PI * t * 5.5))
    pcm[index] = Math.round(Math.sin(2 * Math.PI * hz * t) * syllable * 11000)
  }
  const bytes = new Uint8Array(pcm.buffer)
  let raw = ''
  for (let index = 0; index < bytes.length; index += 8192) raw += String.fromCharCode(...bytes.subarray(index, index + 8192))
  window.__jarvisAudio({ seq: 1, sample_rate: rate, channels: 1, format: 'pcm_s16le', final: true, pcm: btoa(raw) })
}, [hertz, seconds])

try {
  // —— 启动页先单独验一次（后面全部带 nosplash 跳过它）
  await page.goto(`http://127.0.0.1:${server.address().port}/`, { waitUntil: 'load' })
  await page.waitForFunction(() => Boolean(document.querySelector('.splash')), null, { timeout: 4000 })
  assert.equal(await page.evaluate(() => Boolean(document.getElementById('boot'))), false, '静态垫底没被接管，会闪两层')
  const splashTitle = await page.locator('.splash__title').textContent()
  assert.equal(splashTitle.replace(/\s/g, ''), '小夜', `启动页标题是「${splashTitle}」`)
  await page.waitForFunction(() => !document.querySelector('.splash'), null, { timeout: 5000 })
  await page.waitForFunction(() => Boolean(document.querySelector('.app__portrait canvas')))
  checks.push('A0 启动页：出现过、标题对、5 秒内自己让位给主界面')

  await page.goto(`http://127.0.0.1:${server.address().port}/?nosplash=1`, { waitUntil: 'load' })
  const face = page.locator('.app__portrait canvas').first()
  await face.waitFor()
  await page.waitForFunction(() => document.querySelector('.app__portrait canvas')?.dataset.loadedCharacter !== undefined)
  // data-character 挂在 .app__portrait 那个根 div 上（它同时也是 .moon-portrait）。
  assert.equal(await page.locator('.app__portrait').getAttribute('data-character'), 'p01')
  checks.push('A 形象加载：p01 写实照片')

  await page.evaluate(() => window.__jarvisAudio({ flush: true }))
  await page.waitForTimeout(700)
  assert.equal(await face.getAttribute('data-mouth'), '0.000')
  checks.push('B 静音归零')

  // —— 眨眼：先挂哨兵抓最深那一帧，同时记一条时间序列
  await page.evaluate(() => {
    const canvas = document.querySelector('.app__portrait canvas')
    const shot = { value: 0, data: '' }
    window.__blinkShot = shot
    const tick = () => {
      const value = Number(canvas.dataset.blink ?? 0)
      if (!shot.data && value > 0.6) { shot.value = value; shot.data = canvas.toDataURL('image/png') }
      if (!shot.data) requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  })
  const series = await page.evaluate(async () => {
    const seen = []
    const canvas = document.querySelector('.app__portrait canvas')
    for (let index = 0; index < 160; index += 1) {
      seen.push(Number(canvas.dataset.blink ?? 0))
      await new Promise(resolve => setTimeout(resolve, 55))
    }
    return seen
  })
  const peak = Math.max(...series)
  const middles = series.filter(value => value > 0.15 && value < 0.6).length
  assert.ok(peak > 0.6, `8.8 秒里没等到一次闭眼（最深 ${peak}）`)
  assert.ok(middles >= 2, `只有 ${middles} 个半闭采样：眨眼还是没有中间帧`)
  const blinkShot = await page.evaluate(() => window.__blinkShot.data)
  assert.ok(blinkShot.startsWith('data:image/png'), '没抓到闭眼那一帧')
  fs.writeFileSync(path.join(output, 'blink-closed.png'), Buffer.from(blinkShot.split(',')[1], 'base64'))
  checks.push(`C 眨眼：最深 ${peak.toFixed(2)}，半闭采样 ${middles} 个`)

  // —— 说话：嘴跟着动，且相邻帧跳变不大（不抽搐）
  await speak(220, 3)
  const mouthSeries = await page.evaluate(async () => {
    const seen = []
    const canvas = document.querySelector('.app__portrait canvas')
    for (let index = 0; index < 60; index += 1) {
      seen.push(Number(canvas.dataset.mouth ?? 0))
      await new Promise(resolve => setTimeout(resolve, 40))
    }
    return seen
  })
  const top = Math.max(...mouthSeries)
  assert.ok(top > 0.5, `喂了 3 秒音频，嘴最大只张到 ${top.toFixed(2)}`)
  const steps = mouthSeries.slice(1).map((value, index) => Math.abs(value - mouthSeries[index]))
  const sorted = [...steps].sort((a, b) => a - b)
  const median = sorted[Math.floor(sorted.length / 2)]
  // 这里刻意**不断言**跳变次数。标定过：去掉 60 ms 低通的反样本是 15 次，当前实现是
  // 12 次 —— 这条判据分辨不出来，写上去就是一条会绿的假判据。低通本身留着（它在物理
  // 上确实更平滑），但"嘴不抽搐"这件事交给下面两张真画面由人眼看。
  const jumps = steps.filter(value => value > 0.35).length
  checks.push(`D 口型：最大 ${top.toFixed(2)}，中位跳变 ${median.toFixed(3)}，大跳 ${jumps} 次（仅供参考，未设阈值）`)

  // 采样把那段 3 秒音频听完了；抓帧要重新喂一段，否则哨兵等的是一个已经闭嘴的人。
  await page.evaluate(() => {
    const canvas = document.querySelector('.app__portrait canvas')
    const shot = { data: '' }
    window.__mouthShot = shot
    const tick = () => {
      if (!shot.data && Number(canvas.dataset.mouth ?? 0) > 0.55) shot.data = canvas.toDataURL('image/png')
      if (!shot.data) requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  })
  await speak(200, 2.5)
  await page.waitForTimeout(1200)
  const mouthShot = await page.evaluate(() => window.__mouthShot.data)
  assert.ok(mouthShot.startsWith('data:image/png'), '没抓到张嘴那一帧')
  fs.writeFileSync(path.join(output, 'mouth-open.png'), Buffer.from(mouthShot.split(',')[1], 'base64'))
  checks.push('E 张嘴画面已抓取')

  // —— 表情：说话时不再被清零（先让 happy 生效，确认这条通道还在）
  await page.evaluate(() => window.__jarvisAudio({ flush: true }))
  await page.waitForTimeout(600)
  await page.evaluate(() => window.__jarvisState({ voice_state: 'idle', voice: 'off', voice_detail: '', history: [{ role: 'assistant', text: '太好了，恭喜你！😊' }], last_event: '', interrupted: false }))
  await page.waitForFunction(() => document.querySelector('.app__portrait')?.dataset.emotion === 'happy')
  checks.push('F 表情：happy 已生效')

  await page.screenshot({ path: path.join(output, 'face.png') }).catch(() => {})
  assert.equal(crashes.length, 0, `页面异常：${crashes.join(' | ')}`)
  console.log(checks.join('\n'))
  console.log(`\n动效验收通过（${checks.length} 项）`)
} finally {
  fs.writeFileSync(path.join(output, 'portrait-motion.json'), JSON.stringify({ checks, crashes }, null, 2))
  await browser.close()
  server.close()
}
process.exit(0)

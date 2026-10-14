// Exercise a running packaged WebView2 over its local debugging port. No browser download.
import fs from 'node:fs'
import path from 'node:path'

const port = process.argv[2] || '9228'
const output = process.argv[3] || 'D:/AI/XiaoYe/qa-ui'
fs.mkdirSync(output, { recursive: true })
const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
const page = pages.find(p => p.type === 'page' && !p.url.startsWith('devtools:'))
if (!page) throw new Error('No application page')
const socket = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true })
  socket.addEventListener('error', reject, { once: true })
})
let next = 0
const waiting = new Map()
socket.addEventListener('message', event => {
  const result = JSON.parse(event.data)
  if (waiting.has(result.id)) {
    const { resolve, reject, timer } = waiting.get(result.id)
    clearTimeout(timer); waiting.delete(result.id)
    if (result.error) reject(new Error(JSON.stringify(result.error)))
    else resolve(result.result)
  }
})
function send(method, params = {}) {
  const id = ++next
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Timeout ${method}`)), 15000)
    waiting.set(id, { resolve, reject, timer })
    socket.send(JSON.stringify({ id, method, params }))
  })
}
async function evaluate(expression) {
  const result = await send('Runtime.evaluate', { expression, returnByValue: true })
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails))
  return result.result?.value
}
async function waitFor(expression, timeout = 400000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return
    await new Promise(resolve => setTimeout(resolve, 1000))
  }
  throw new Error(`UI did not finish: ${expression}`)
}
async function screenshot(name) {
  const result = await send('Page.captureScreenshot', { format: 'png' })
  fs.writeFileSync(path.join(output, name), Buffer.from(result.data, 'base64'))
}
try {
  await waitFor('Boolean(window.pywebview?.api)', 30000)
  await evaluate("[...document.querySelectorAll('.bar__entry')].find(b => b.textContent.trim() === '生图').click()")
  await waitFor("Boolean(document.querySelector('.im__prompt'))", 10000)
  await evaluate("(() => { const p=document.querySelector('.im__prompt');p.value='一只戴着蓝色围巾的橘猫在雪地里跑步';p.dispatchEvent(new Event('input',{bubbles:true}));const s=document.querySelectorAll('.im select')[2];s.value='1';s.dispatchEvent(new Event('change',{bubbles:true}));})()")
  await new Promise(resolve => setTimeout(resolve, 600))
  await evaluate("document.querySelector('.im .hud-btn--primary').click()")
  await waitFor("Boolean(document.querySelector('.im__pic')) || Boolean(document.querySelector('.im__error')?.textContent)")
  const image = await evaluate("({english:document.querySelector('.im__sent')?.textContent,error:document.querySelector('.im__error')?.textContent,picture:Boolean(document.querySelector('.im__pic'))})")
  if (!image.picture || image.error) throw new Error(JSON.stringify(image))
  await screenshot('image-popup.png')
  await evaluate("[...document.querySelectorAll('.im__head button')].find(b => b.textContent.trim() === '关闭').click()")
  await evaluate("[...document.querySelectorAll('.bar__entry')].find(b => b.textContent.trim() === '视频').click()")
  await waitFor("Boolean(document.querySelector('#video-prompt'))", 10000)
  await evaluate("(() => { const p=document.querySelector('#video-prompt');p.value='Un chat orange marche dans un jardin, style animation';p.dispatchEvent(new Event('input',{bubbles:true}));})()")
  await evaluate("document.querySelector('.vp .hud-btn--primary').click()")
  await waitFor("Boolean(document.querySelector('.vp video')) || Boolean(document.querySelector('.vp__error')?.textContent)", 800000)
  const video = await evaluate("({text:document.querySelector('.vp__box').innerText,error:document.querySelector('.vp__error')?.textContent,ready:document.querySelector('.vp video')?.readyState})")
  if (video.error) throw new Error(JSON.stringify(video))
  await waitFor("document.querySelector('.vp video')?.readyState >= 2", 10000)
  await screenshot('video-popup.png')
  const report = { ok: true, image, video }
  fs.writeFileSync(path.join(output, 'ui-results.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report))
} finally { socket.close() }

/** Browser acceptance for productivity features, using isolated content and no model account. */
import fs from 'node:fs'
import path from 'node:path'
import http from 'node:http'
import { spawn } from 'node:child_process'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = path.resolve(process.argv[2] || 'D:/AI/XiaoYe/qa/mobile-productivity-0.5.0')
fs.mkdirSync(output, { recursive: true })
const profile = fs.mkdtempSync(path.join(output, 'edge-'))
const bundle = path.join(root, 'frontend/dist-mobile')
const checks = [], errors = []
const server = http.createServer((req, res) => {
  const file = path.resolve(bundle, '.' + (new URL(req.url, 'http://localhost').pathname === '/' ? '/index.html' : new URL(req.url, 'http://localhost').pathname))
  if (!file.startsWith(bundle + path.sep) || !fs.existsSync(file)) return res.writeHead(404).end()
  res.setHeader('Content-Type', ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[path.extname(file)] || 'application/octet-stream')
  fs.createReadStream(file).pipe(res)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const address = `http://127.0.0.1:${server.address().port}`
const edge = spawn('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { windowsHide: true, stdio: 'ignore' })
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
let socket
async function until(work, label, timeout = 15000) { const end = Date.now() + timeout; while (Date.now() < end) { if (await work()) return; await pause(100) } throw new Error(`Timeout: ${label}`) }
try {
  const portFile = path.join(profile, 'DevToolsActivePort')
  await until(() => fs.existsSync(portFile), 'Edge port')
  const port = fs.readFileSync(portFile, 'utf8').split('\n')[0]
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
  socket = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl)
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }) })
  let counter = 0
  const pending = new Map()
  socket.addEventListener('message', event => {
    const data = JSON.parse(event.data)
    if (data.method === 'Runtime.exceptionThrown') errors.push(data.params.exceptionDetails)
    const item = pending.get(data.id); if (!item) return
    clearTimeout(item.timer); pending.delete(data.id)
    if (data.error) item.reject(new Error(JSON.stringify(data.error))); else item.resolve(data.result)
  })
  function send(method, params = {}) { const id = ++counter; return new Promise((resolve, reject) => { const timer = setTimeout(() => { pending.delete(id); reject(new Error(method)) }, 15000); pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params })) }) }
  async function ev(code) { const r = await send('Runtime.evaluate', { expression: `(async()=>{${code}})()`, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails)); return r.result?.value }
  async function click(text, scope = 'document') {
    const point = await ev(`const b=[...${scope}.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(text)});if(!b||b.disabled)return null;if(!b.checkVisibility()&&b.classList.contains('acts__extra'))b.closest('.turn').querySelector('.more').click();await new Promise(r=>setTimeout(r,80));b.scrollIntoView({block:'nearest',inline:'nearest'});const r=b.getBoundingClientRect();if(!b.checkVisibility()||!r.width||!r.height)return null;return {x:r.x+r.width/2,y:r.y+r.height/2}`)
    assert(point, `visible enabled button: ${text}`)
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', button: 'left', clickCount: 1, ...point })
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', button: 'left', clickCount: 1, ...point })
    await pause(120)
  }
  async function input(selector, value) { await ev(`const e=document.querySelector(${JSON.stringify(selector)}); if(!e)throw new Error('input missing'); e.value=${JSON.stringify(value)}; e.dispatchEvent(new Event('input',{bubbles:true}));return true`); await pause(80) }
  async function store() { return ev(`return JSON.parse(localStorage.getItem('CapacitorStorage.xiaoye.workspace.v1'))`) }
  async function workbench() {
    // Wait for a closing session drawer before aiming at the tray behind it.
    await pause(320)
    await ev(`if(!document.querySelector('.dock__tray'))document.querySelector('.dock__plus').click()`)
    await pause(320)
    await click('✦ AI 工作台', `document.querySelector('.dock')`)
    await until(() => ev(`return !!document.querySelector('.workspace nav')`), 'workspace opened')
  }
  async function tab(name) { await click(name, `document.querySelector('.workspace nav')`) }
  async function check(name, work) { await work(); checks.push({ name, ok: true }); console.log(`PASS ${name}`) }
  await send('Runtime.enable'); await send('Page.enable')
  await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true })
  await send('Page.addScriptToEvaluateOnNewDocument', { source: `if (!localStorage.getItem('qa.seeded')) { localStorage.setItem('qa.seeded','1');localStorage.setItem('CapacitorStorage.xiaoye.history',JSON.stringify([{role:'user',text:'请解释苹果'},{role:'assistant',text:'苹果是水果。<script>window.injected=true</script>'},{role:'user',text:'苹果如何保存'},{role:'assistant',text:'放在阴凉处保存。'}]));localStorage.setItem('CapacitorStorage.xiaoye.voice',JSON.stringify({speakBack:false,handsFree:false}));}` })
  await send('Page.navigate', { url: address })
  await until(() => ev(`return document.querySelectorAll('.turn').length===4`), 'mobile startup')
  await check('16 templates, category search and variable form prepare a draft without a model call', async () => {
    await workbench(); await tab('提示词')
    assert.equal(await ev(`return document.querySelectorAll('.productivity article').length`), 16)
    await input('[aria-label="搜索提示词"]', '自然润色'); await click('使用模板')
    await input('[aria-label="语气"]', '友好'); await input('[aria-label="原文"]', '明天交给我。')
    await click('填好，放入输入框')
    assert((await ev(`return document.querySelector('.dock__input').value`)).includes('明天交给我。'))
    assert.equal(await ev(`return document.querySelectorAll('.turn').length`), 4)
  })
  await check('custom templates persist, can be found, edited and filled', async () => {
    await workbench(); await tab('提示词'); await click('＋ 自建提示词')
    await input('.productivity form input', '我的摘要'); await input('.productivity form textarea', '总结 {{资料}}')
    await click('保存提示词'); assert.equal((await store()).prompts.length, 1)
    await input('[aria-label="搜索提示词"]', '我的摘要'); await click('编辑')
    await input('.productivity form textarea', '简短总结 {{资料}}'); await click('保存提示词')
    assert.equal((await store()).prompts[0].body, '简短总结 {{资料}}')
    await click('使用模板'); await input('[aria-label="资料"]', '原文资料'); await click('填好，放入输入框')
    assert.equal(await ev(`return document.querySelector('.dock__input').value`), '简短总结 原文资料')
  })
  await check('draft survives creating and switching conversations and a browser restart', async () => {
    await input('.dock__input', '尚未发送的草稿')
    await ev(`document.querySelector('[aria-label="会话"]').click()`); await click('开一段新的')
    assert.equal(await ev(`return document.querySelector('.dock__input').value`), '')
    await input('.dock__input', '第二段草稿'); await ev(`document.querySelector('[aria-label="会话"]').click()`)
    await ev(`document.querySelectorAll('.local-session > button')[1].click()`); await pause(200)
    assert.equal(await ev(`return document.querySelector('.dock__input').value`), '尚未发送的草稿')
    await pause(450); await send('Page.reload'); await until(() => ev(`return document.querySelectorAll('.turn').length===4`), 'reload')
    assert.equal(await ev(`return document.querySelector('.dock__input').value`), '尚未发送的草稿')
  })
  await check('offline notes and tasks support tags, completion, filtering and attachment', async () => {
    await workbench(); await tab('笔记待办'); await click('＋ 新建笔记')
    await input('.productivity form input', '工作灵感'); await input('.productivity form textarea', '方案编号 42'); await click('保存笔记')
    await click('＋ 新建待办'); await input('.productivity form input', '读完资料'); await click('保存笔记')
    assert.equal((await store()).notes.length, 2)
    await click('标记完成'); assert((await store()).notes.find(n => n.kind === 'task').done)
    await click('已完成', `document.querySelector('.filters')`); assert.equal(await ev(`return document.querySelectorAll('.productivity article').length`), 1)
    await click('全部', `document.querySelector('.filters')`)
    await input('[aria-label="搜索笔记"]', '工作灵感'); await click('置顶'); await click('交给 AI 阅读')
    assert((await ev(`return document.querySelector('.document-attach').textContent`)).includes('工作灵感'))
    await ev(`document.querySelector('.document-attach button').click()`)
  })
  await check('reading preferences change actual chat style and persist', async () => {
    await workbench(); await tab('偏好统计')
    await ev(`const selects=document.querySelectorAll('.productivity select');selects[0].value='large';selects[0].dispatchEvent(new Event('change',{bubbles:true}));selects[1].value='10';selects[1].dispatchEvent(new Event('change',{bubbles:true}));document.querySelectorAll('.productivity input[type=checkbox]')[1].click()`)
    await pause(200); assert.deepEqual((await store()).reading, { fontSize: 'large', compact: false, context: 10, tools: false })
    await click('关闭'); assert.equal(await ev(`return getComputedStyle(document.querySelector('.bubble')).fontSize`), '19px')
  })
  await check('chat search navigates actual message positions and treats HTML as text', async () => {
    await ev(`document.querySelector('[aria-label="会话"]').click()`); await pause(320); await click('搜索本段聊天')
    await until(() => ev(`return !!document.querySelector('[aria-label="聊天关键词"]')`), 'chat search input')
    await input('[aria-label="聊天关键词"]', '苹果'); assert.equal(await ev(`return document.querySelector('.chat-search span').textContent`), '1/3')
    await ev(`document.querySelector('[aria-label="下一个结果"]').click()`); await pause(100)
    assert.equal(await ev(`return document.querySelector('.search-match').dataset.messageIndex`), '1')
    assert.equal(await ev(`return !!window.injected`), false)
    await ev(`document.querySelector('[aria-label="关闭聊天搜索"]').click()`)
  })
  await check('quote, summary, checklist and save-note do not alter original messages', async () => {
    const scope = `document.querySelector('.turn--assistant')`
    await click('引用', scope); assert((await ev(`return document.querySelector('.dock__input').value`)).includes('我的问题'))
    await click('精简', scope); assert((await ev(`return document.querySelector('.dock__input').value`)).startsWith('请用三句话'))
    await click('转清单', scope); assert((await ev(`return document.querySelector('.dock__input').value`)).includes('行动清单'))
    await click('存笔记', scope); assert.equal((await store()).notes.length, 3)
    assert.equal(await ev(`return document.querySelectorAll('.turn').length`), 4)
  })
  await check('branching copies only selected prefix and preserves original conversation', async () => {
    const before = (await store()).sessions.find(s => s.turns.length === 4)
    await click('从这里分支', `document.querySelector('.turn--assistant')`)
    const after = await store(), branch = after.sessions.find(s => s.id === after.active)
    assert.equal(branch.turns.length, 2); assert.notEqual(branch.id, before.id)
    assert.deepEqual(after.sessions.find(s => s.id === before.id).turns, before.turns)
  })
  await check('archive hides conversations from recent list and can restore them', async () => {
    await ev(`document.querySelector('[aria-label="会话"]').click()`)
    const total = await ev(`return document.querySelectorAll('.local-session').length`)
    await click('归档'); assert.equal(await ev(`return document.querySelectorAll('.local-session').length`), total - 1)
    await click('归档 1'); assert.equal(await ev(`return document.querySelectorAll('.local-session').length`), 1)
    await click('移出归档'); assert.equal((await store()).sessions.filter(s => s.archived).length, 0)
    await ev(`document.querySelector('.drawer__head [aria-label="关闭"]').click()`)
  })
  await check('narrow-screen workbench, template form and notebook render without horizontal overflow', async () => {
    await workbench()
    for (const width of [320, 390, 430]) {
      await send('Emulation.setDeviceMetricsOverride', { width, height: 844, deviceScaleFactor: 1, mobile: true })
      for (const name of ['提示词', '笔记待办', '偏好统计']) { await tab(name); assert(await ev(`return document.querySelector('.workspace').scrollWidth<=innerWidth`)) }
    }
    await tab('提示词'); await pause(2800)
    const image = await send('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(path.join(output, 'prompt-library.png'), Buffer.from(image.data, 'base64'))
    await tab('笔记待办'); const notes = await send('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(path.join(output, 'notebook.png'), Buffer.from(notes.data, 'base64'))
    assert.equal(errors.length, 0, JSON.stringify(errors))
  })
  fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify({ ok: true, checks, errors, device_tested: false, network: 'No model calls; isolated browser content only' }, null, 2))
  console.log(`${checks.length} productivity browser checks passed`)
} catch (error) { fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify({ ok: false, checks, errors, error: String(error) }, null, 2)); throw error }
finally { socket?.close(); edge.kill(); server.close() }

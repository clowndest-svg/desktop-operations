/** Isolated browser regression against the built mobile bundle. No real model or user data. */
import fs from 'node:fs'
import path from 'node:path'
import http from 'node:http'
import { spawn } from 'node:child_process'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = path.resolve(process.argv[2] || 'D:/AI/XiaoYe/qa/mobile-0.5.0')
fs.mkdirSync(output, { recursive: true })
const profile = fs.mkdtempSync(path.join(output, 'edge-'))
const bundle = path.join(root, 'frontend/dist-mobile')
const checks = [], errors = []
const server = http.createServer((req, res) => {
  const name = decodeURIComponent(new URL(req.url, 'http://localhost').pathname)
  const target = path.resolve(bundle, '.' + (name === '/' ? '/index.html' : name))
  if (!target.startsWith(bundle + path.sep) || !fs.existsSync(target)) { res.writeHead(404).end(); return }
  res.setHeader('Content-Type', ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[path.extname(target)] || 'application/octet-stream')
  fs.createReadStream(target).pipe(res)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const address = `http://127.0.0.1:${server.address().port}`
const edge = spawn('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { windowsHide: true, stdio: 'ignore' })
let socket
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(work, label, timeout = 20000) {
  const end = Date.now() + timeout
  let last
  while (Date.now() < end) { try { last = await work(); if (last) return last } catch {} await pause(100) }
  throw new Error(`Timeout: ${label}; last=${JSON.stringify(last)}`)
}
try {
  const portFile = path.join(profile, 'DevToolsActivePort')
  await until(() => fs.existsSync(portFile), 'Edge debug port')
  const port = fs.readFileSync(portFile, 'utf8').split('\n')[0]
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
  const page = pages.find(p => p.type === 'page')
  socket = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }) })
  let id = 0
  const waiting = new Map()
  socket.addEventListener('message', e => {
    const event = JSON.parse(e.data)
    if (event.method === 'Runtime.exceptionThrown') errors.push(event.params.exceptionDetails)
    const pending = waiting.get(event.id)
    if (!pending) return
    waiting.delete(event.id); clearTimeout(pending.timer)
    if (event.error) pending.reject(new Error(JSON.stringify(event.error)))
    else pending.resolve(event.result)
  })
  function send(method, params = {}) {
    const key = ++id
    return new Promise((resolve, reject) => { const timer = setTimeout(() => { waiting.delete(key); reject(new Error(`CDP timeout ${method}`)) }, 20000); waiting.set(key, { resolve, reject, timer }); socket.send(JSON.stringify({ id: key, method, params })) })
  }
  async function ev(body) {
    const r = await send('Runtime.evaluate', { expression: `(async()=>{${body}})()`, returnByValue: true, awaitPromise: true })
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails))
    return r.result?.value
  }
  async function click(text, scope = 'document') {
    const result = await ev(`const b=[...${scope}.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(text)}); if(!b||b.disabled)return false; if(getComputedStyle(b).display==='none')b.closest('.turn')?.querySelector('.more')?.click();b.click();return true`)
    assert(result, `button ${text} must be enabled`); await pause(100)
  }
  async function input(selector, value) { await ev(`const e=document.querySelector(${JSON.stringify(selector)});e.value=${JSON.stringify(value)};e.dispatchEvent(new Event('input',{bubbles:true}));return true`); await pause(80) }
  async function check(name, work) { await work(); checks.push({ name, ok: true }); console.log(`PASS ${name}`) }
  async function store() { return ev(`return JSON.parse(localStorage.getItem('CapacitorStorage.xiaoye.workspace.v1'))`) }
  async function ask(question) {
    const previous = await ev('return window.__requests.length')
    await input('.dock__input', question)
    await ev(`document.querySelector('.dock__send').click()`)
    await until(() => ev(`return window.__requests.length > ${previous} && !document.querySelector('.dock__stop')`), 'answer')
    await pause(150)
  }
  async function openWorkbench() {
    await ev(`document.querySelector('.dock__plus').click()`)
    await click('✦ AI 工作台')
  }
  async function upload(selector, file) {
    const { root: doc } = await send('DOM.getDocument')
    const { nodeId } = await send('DOM.querySelector', { nodeId: doc.nodeId, selector })
    await send('DOM.setFileInputFiles', { nodeId, files: [file] }); await pause(200)
  }
  await send('Runtime.enable')
  await send('Page.enable')
  await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true })
  await send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: output })
  await send('Page.addScriptToEvaluateOnNewDocument', { source: `
    if (!localStorage.getItem('qa.seeded')) {
      localStorage.setItem('qa.seeded','1');
      localStorage.setItem('CapacitorStorage.xiaoye.history',JSON.stringify([{role:'user',text:'旧会话问题'},{role:'assistant',text:'旧会话回答'}]));
      localStorage.setItem('CapacitorStorage.xiaoye.model',JSON.stringify({baseUrl:'https://model.example.test/v1',apiKey:'ui-test-only',model:'qa',pin:''}));
      localStorage.setItem('CapacitorStorage.xiaoye.voice',JSON.stringify({speakBack:false,handsFree:false}));
    }
    window.__requests=[];
    const original=window.fetch.bind(window);
    window.fetch=async(url,options)=>{
      if(String(url).startsWith('https://pc.example.test:8737/')) {
        const method=String(url).split('/').at(-1);
        const data=method==='chat_sessions'?{sessions:[{id:'pc-only',title:'电脑独有会话',turns:2}],current:'pc-only'}:method==='chat_messages'?{messages:[{role:'user',content:'电脑独有问题',at:'2026-10-16T00:00:00Z'},{role:'assistant',content:'电脑独有回答',at:'2026-10-16T00:00:01Z'}]}:{};
        return new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}});
      }
      if(String(url).startsWith('https://model.example.test/')) {
        window.__requests.push(JSON.parse(options.body));
        return new Response(JSON.stringify({choices:[{message:{role:'assistant',content:'这是测试模型返回的回答 '+window.__requests.length+'。\\n\\n可收藏、导出与追问。'}}]}),{status:200,headers:{'Content-Type':'application/json'}});
      }
      return original(url,options);
    };
  ` })
  await send('Page.navigate', { url: address })
  await until(() => ev(`return document.querySelectorAll('.turn').length===2`), 'legacy migration')
  await check('legacy history migrated without loss', async () => { assert.equal((await store()).sessions[0].turns[1].text, '旧会话回答') })
  await check('eight assistant presets and writer create separate conversation', async () => {
    await openWorkbench()
    assert.equal(await ev(`return document.querySelectorAll('.presets button').length`), 8)
    await ev(`document.querySelectorAll('.presets button')[1].click()`); await pause(200)
    assert.equal((await store()).sessions.length, 2)
    await ask('请润色这句话')
    const request = await ev('return window.__requests.at(-1)')
    assert(request.messages.some(m => String(m.content).includes('写作编辑')))
    assert.equal(request.messages.filter(m => m.role === 'user').length, 1)
  })
  await check('custom instructions reach request', async () => {
    await openWorkbench(); await input('.workspace textarea', '每次先给结论')
    await click('保存当前会话指令'); await click('关闭')
    await ask('解释一下')
    assert((await ev('return window.__requests.at(-1).messages')).some(m => String(m.content).includes('每次先给结论')))
  })
  await check('file attachment sends actual file text to model', async () => {
    const file = path.join(output, '资料.txt'); fs.writeFileSync(file, '订单总数为 42，请据此回答。')
    await upload('.hidden-file', file)
    assert(await ev(`return document.querySelector('.document-attach').textContent.includes('资料.txt')`))
    await ask('订单有多少？')
    assert((await ev('return window.__requests.at(-1).messages.at(-1).content')).includes('订单总数为 42'))
  })
  await check('save answer, follow up, and edit question preserve original turns', async () => {
    await click('收藏', `document.querySelectorAll('.turn--assistant')[2]`)
    assert.equal((await store()).saved.length, 1)
    const count = await ev(`return document.querySelectorAll('.turn').length`)
    await click('继续追问', `document.querySelectorAll('.turn--assistant')[2]`)
    assert((await ev(`return document.querySelector('.dock__input').value`)).includes('进一步解释'))
    assert.equal(await ev(`return document.querySelectorAll('.turn').length`), count)
    await click('修改提问', `document.querySelector('.turn--user')`)
    assert.equal(await ev(`return document.querySelector('.dock__input').value`), '请润色这句话')
  })
  await check('saved search and Markdown export work', async () => {
    await openWorkbench(); await click('收藏', `document.querySelector('.workspace nav')`)
    await input('.workspace input[type=search]', '不存在'); assert.equal(await ev(`return document.querySelectorAll('.workspace .saved').length`), 0)
    await input('.workspace input[type=search]', '测试模型'); assert.equal(await ev(`return document.querySelectorAll('.workspace .saved').length`), 1)
    await click('导出'); await until(() => fs.existsSync(path.join(output, '小夜-收藏.md')), 'Markdown download')
    assert(fs.readFileSync(path.join(output, '小夜-收藏.md'), 'utf8').includes('测试模型'))
  })
  await check('backup excludes credentials and validates before restore', async () => {
    await click('资料备份'); await click('保存 JSON 备份')
    const backup = path.join(output, '小夜-手机备份.json')
    await until(() => fs.existsSync(backup), 'backup download')
    const raw = fs.readFileSync(backup, 'utf8'); assert(!raw.includes('ui-test-only'))
    const before = await store()
    const invalid = path.join(output, 'invalid.json'); fs.writeFileSync(invalid, '{"version":99}')
    await upload('.workspace input[type=file]', invalid); assert.deepEqual(await store(), before)
    await upload('.workspace input[type=file]', backup)
    assert(await ev(`return document.querySelector('.gate__ask').textContent.includes('恢复将替换')`))
    await click('做', `document.querySelector('.gate')`)
    assert.deepEqual(await store(), before)
  })
  await check('workbench fits 390 and 320 pixel screens', async () => {
    for (const width of [390, 320]) {
      await send('Emulation.setDeviceMetricsOverride', { width, height: 844, deviceScaleFactor: 1, mobile: true })
      assert(await ev(`return document.querySelector('.workspace').scrollWidth<=innerWidth`))
    }
    await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true })
    await click('助手', `document.querySelector('.workspace nav')`)
    await pause(2600)
    const shot = await send('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(path.join(output, 'workbench-dark.png'), Buffer.from(shot.data, 'base64'))
    await click('关闭')
  })
  await check('rename, pin, search, cancel deletion and confirmed deletion', async () => {
    await ev(`document.querySelector('[aria-label="会话"]').click()`); await pause(100)
    await click('重命名'); await input('[aria-label="会话名称"]', '我的写作项目'); await click('保存名称')
    await click('置顶'); assert((await store()).sessions.some(s => s.title === '我的写作项目' && s.pinned))
    await input('.local-search', '旧会话回答'); assert.equal(await ev(`return document.querySelectorAll('.local-session').length`), 1)
    await click('删除', `document.querySelector('.local-session__tools')`); await click('取消', `document.querySelector('.gate')`)
    assert.equal((await store()).sessions.length, 2)
    await click('删除', `document.querySelector('.local-session__tools')`); await click('做', `document.querySelector('.gate')`)
    assert.equal((await store()).sessions.length, 1)
    await input('.local-search', '')
    await click('切到浅色'); await ev(`document.querySelector('[aria-label="关闭"]').click()`)
    await ev(`document.querySelectorAll('.turn--expanded .more').forEach(b=>b.click())`)
    await pause(500)
    const shot = await send('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(path.join(output, 'chat-light.png'), Buffer.from(shot.data, 'base64'))
  })
  await check('reloading preserves active session, instruction and saved content', async () => {
    const before = await store()
    await send('Page.reload')
    await until(() => ev(`return document.querySelectorAll('.turn').length===6`), 'reload history')
    assert.deepEqual(await store(), before)
    assert.equal(errors.length, 0, JSON.stringify(errors))
  })
  await check('sixteen templates fill variables without sending a request', async () => {
    await openWorkbench(); await click('提示词')
    assert.equal(await ev(`return document.querySelectorAll('.productivity article.card').length`), 16)
    await click('使用模板', `document.querySelector('.productivity article.card')`)
    await input('.template-form [aria-label="语气"]', '友好')
    await input('.template-form [aria-label="原文"]', '请确认时间')
    const previous = await ev('return window.__requests.length')
    await click('填好，放入输入框')
    assert((await ev(`return document.querySelector('.dock__input').value`)).includes('友好'))
    assert.equal(await ev('return window.__requests.length'), previous)
  })
  await check('custom prompt and offline note/task creation, completion and attachment', async () => {
    await openWorkbench(); await click('提示词'); await click('＋ 自建提示词')
    await input('.productivity form input', '我的模板')
    await input('.productivity form textarea', '请解释 {{主题}}')
    await click('保存提示词'); assert.equal((await store()).prompts.length, 1)
    await click('笔记待办'); await click('＋ 新建笔记')
    await input('.productivity form input', '项目想法')
    await input('.productivity form textarea', '离线记录的正文')
    await click('保存笔记'); assert.equal((await store()).notes.length, 1)
    await click('＋ 新建待办'); await input('.productivity form input', '明天整理资料'); await click('保存笔记')
    assert.equal((await store()).notes.length, 2)
    await click('标记完成'); assert((await store()).notes.some(n => n.kind === 'task' && n.done))
    await click('交给 AI 阅读'); assert(await ev(`return Boolean(document.querySelector('.document-attach'))`))
    assert.equal(await ev('return window.__requests.length'), 0)
    await ev(`document.querySelector('.document-attach button').click()`)
  })
  await check('reading preferences apply and tools switch changes the real request', async () => {
    await openWorkbench(); await click('偏好统计')
    await ev(`const s=document.querySelector('.productivity select');s.value='large';s.dispatchEvent(new Event('change',{bubbles:true}));const boxes=document.querySelectorAll('.productivity input[type=checkbox]');boxes[0].checked=true;boxes[0].dispatchEvent(new Event('change',{bubbles:true}));boxes[1].checked=false;boxes[1].dispatchEvent(new Event('change',{bubbles:true}));`)
    await pause(200); assert.equal((await store()).reading.tools, false)
    await click('关闭'); assert(await ev(`return document.querySelector('.app').classList.contains('app--large')`))
    await ask('只聊天，不动手机')
    assert.equal(await ev(`return window.__requests.at(-1).tools`), undefined)
  })
  await check('drafts persist across reload and branching keeps original messages', async () => {
    await input('.dock__input', '尚未发送的草稿'); await pause(500)
    const before = await store(), original = before.sessions.find(s => s.id === before.active)
    await send('Page.reload')
    await until(() => ev(`return document.querySelector('.dock__input')?.value==='尚未发送的草稿'`), 'draft reload')
    await click('从这里分支', `document.querySelectorAll('.turn--assistant')[0]`)
    const after = await store(); assert.equal(after.sessions.length, before.sessions.length + 1)
    assert.deepEqual(after.sessions.find(s => s.id === original.id).turns, original.turns)
    assert.equal(after.sessions.find(s => s.id === after.active).turns.length, 2)
  })
  await check('archive and message search keep content retrievable', async () => {
    await ev(`document.querySelector('[aria-label="会话"]').click()`); await pause(150)
    await click('归档', `document.querySelector('.local-session__tools')`)
    assert((await store()).sessions.some(s => s.archived))
    await click('归档 1'); assert.equal(await ev(`return document.querySelectorAll('.local-session').length`), 1)
    await click('移出归档'); await click('最近会话')
    await click('搜索本段聊天')
    await input('.chat-search input', '测试模型')
    assert(await ev(`return document.querySelectorAll('.search-match').length===1`))
    assert.equal(errors.length, 0, JSON.stringify(errors))
  })
  await check('storage failure stays visible instead of claiming a successful save', async () => {
    await openWorkbench()
    await ev(`window.__originalSetItem=Storage.prototype.setItem;Storage.prototype.setItem=function(key,value){if(key==='CapacitorStorage.xiaoye.workspace.v1')throw new Error('QA quota exceeded');return window.__originalSetItem.call(this,key,value)}`)
    await input('.workspace textarea', '不能假称保存成功')
    await click('保存当前会话指令')
    assert(await ev(`return document.querySelector('.toast')?.textContent.includes('未保存')`))
    await ev('Storage.prototype.setItem=window.__originalSetItem')
    await click('保存当前会话指令'); await click('关闭')
  })
  await check('PC mode reads PC history while standalone workspace stays independent', async () => {
    const before = await store()
    await ev(`localStorage.setItem('CapacitorStorage.xiaoye.pc',JSON.stringify({host:'pc.example.test',port:8737,pin:'sha256:'+'a'.repeat(64),token:'pc-test-token',name:'测试电脑',methods:[]}))`)
    await send('Page.reload')
    await until(() => ev(`return document.querySelector('.stream')?.textContent.includes('电脑独有回答')`), 'PC history')
    assert.deepEqual(await store(), before)
    await ev(`localStorage.removeItem('CapacitorStorage.xiaoye.pc')`)
    await send('Page.reload')
    await until(() => ev(`return document.querySelector('.stream')?.textContent.includes('请润色这句话')`), 'standalone history after PC mode')
    assert(!(await ev(`return document.querySelector('.stream').textContent`)).includes('电脑独有'))
    assert.deepEqual(await store(), before)
    assert.equal(errors.length, 0, JSON.stringify(errors))
  })
  fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify({ ok: true, model: 'mock HTTP response; no real model used', checks, errors, device_tested: false }, null, 2))
  console.log(`${checks.length} browser checks passed; ${output}`)
} catch (error) {
  fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify({ ok: false, checks, errors, error: String(error) }, null, 2))
  throw error
} finally {
  socket?.close(); edge.kill(); server.close()
}

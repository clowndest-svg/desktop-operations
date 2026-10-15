// r119 新增能力在**打包版**里的界面判据：右击改提问那条链、桥面那扇新门、视频流式那扇旧门。
//
// 为什么单独写一份：verify_media_ui.mjs 验的是生图/生视频两条重链路，要占显卡；
// 这一份只验"这一轮新加的入口在冻结产物里到底点不点得动"，不碰模型也不碰 GPU。
// 冻结产物里 pywebview 按 **Python 形参名**生成 JS stub，形参名对不上就是
// "源码有、界面上永远调不动" —— 那种 bug pytest 抓不到，必须拿真 exe 调一次。
//
// 两个坑（都是这一轮真踩到的）：
//   1) 桌面版有**两扇 WebView2**：`/`（HUD）和 `/?mode=pet`（宠物）。按 type==='page'
//      随便挑一个会挑到宠物窗，那上面一个 .chat 都没有。
//   2) Runtime.evaluate 交一个**没调用的箭头函数**进去，拿到的是函数本身，
//      序列化成 `{}`、判真值恒为真 —— 于是"✓ 用户气泡已落屏"是假的。
//      所以下面一律用 IIFE 真正调用，并且每条判据都回一个可打印的值。
//
// 用法：先起打包版
//   WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9228 小夜.exe --desktop
// 再
//   node scripts/verify_r119_ui.mjs 9228 D:/AI/XiaoYe/qa-ui-r119
import fs from 'node:fs'
import path from 'node:path'

const port = process.argv[2] || '9228'
const output = process.argv[3] || 'D:/AI/XiaoYe/qa-ui-r119'
fs.mkdirSync(output, { recursive: true })

const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
const hud = pages.filter(p => p.type === 'page' && !p.url.startsWith('devtools:'))
const page = hud.find(p => !p.url.includes('mode=pet'))
if (!page) throw new Error(`没有 HUD 页面（一共 ${hud.length} 页：${hud.map(p => p.url).join(', ')}）`)
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
    clearTimeout(timer)
    waiting.delete(result.id)
    if (result.error) reject(new Error(JSON.stringify(result.error)))
    else resolve(result.result)
  }
})
function send(method, params = {}) {
  const id = ++next
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Timeout ${method}`)), 20000)
    waiting.set(id, { resolve, reject, timer })
    socket.send(JSON.stringify({ id, method, params }))
  })
}
/** 交一段**函数体**进去；用 IIFE 真调用，别把箭头函数本体当结果交出去。 */
async function ev(body) {
  const result = await send('Runtime.evaluate', {
    expression: `(async () => { ${body} })()`,
    returnByValue: true,
    awaitPromise: true,
  })
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails).slice(0, 300))
  return result.result?.value
}
async function waitFor(body, label, timeout = 60000) {
  const deadline = Date.now() + timeout
  let last = null
  while (Date.now() < deadline) {
    last = await ev(body)
    if (last) return last
    await new Promise(resolve => setTimeout(resolve, 400))
  }
  throw new Error(`等不到：${label}（最后一次读到 ${JSON.stringify(last)}）`)
}
const checks = []
function note(name, value, pass) {
  checks.push({ name, value, pass: Boolean(pass) })
  console.log(`${pass ? '✓' : '✗'} ${name}: ${JSON.stringify(value).slice(0, 220)}`)
}

try {
  await waitFor('return Boolean(window.pywebview?.api)', 'pywebview 桥没接上', 40000)
  note('连的是 HUD 那一扇', page.url, !page.url.includes('mode=pet'))

  // 1) 新那扇门在冻结产物里调得动，且回的是**具体那一句**拒绝。
  //    回 "undefined is not a function" 和回 "原提问已不存在" 是两回事：前者是门没生成。
  const door = await ev(`
    try {
      return await window.pywebview.api.chat_edit_question('no-such-conversation', 999999, '改一下')
    } catch (error) { return { thrown: String(error) } }
  `)
  note(
    '桥面 chat_edit_question 回的是那格具体拒绝',
    door,
    String(door?.error || door?.thrown || '').includes('原提问已不存在'),
  )

  // 2) 发一句话，让对话区真出现一个用户气泡（回答要不要等出来是另一回事，这里不等）
  const question = '今晚的月亮是什么形状'
  const form = await ev(`
    const input = document.querySelector('.chat__input')
    if (!input) return { missing: '没有 .chat__input，对话面板没在前台' }
    input.value = ${JSON.stringify(question)}
    input.dispatchEvent(new Event('input', { bubbles: true }))
    const f = document.querySelector('.chat__form')
    if (!f) return { missing: '没有 .chat__form' }
    f.requestSubmit()
    return { sent: true }
  `)
  note('按发送键那一下', form, form.sent === true)
  const bubble = await waitFor(
    `return [...document.querySelectorAll('.chat__turn--user')].some(t => t.textContent.includes(${JSON.stringify(
      question,
    )}))`,
    '发出去的那句话没落到对话区',
  )
  note('用户气泡已落屏', { bubble }, bubble === true)

  // 3) 右击那一句 —— 判据自证"那一下真点到了"：菜单出现 + 条目里有那颗
  const menu = await ev(`
    const turn = [...document.querySelectorAll('.chat__turn--user')].find(t =>
      t.textContent.includes(${JSON.stringify(question)}))
    const target = turn.querySelector('.chat__text') || turn.querySelector('.chat__bubble')
    const box = target.getBoundingClientRect()
    target.dispatchEvent(new MouseEvent('contextmenu', {
      bubbles: true, cancelable: true, view: window,
      clientX: box.left + 8, clientY: box.top + 8, button: 2,
    }))
    await new Promise(r => setTimeout(r, 700))
    return {
      hitTarget: target.className,
      ctxCount: document.querySelectorAll('.ctx').length,
      items: [...document.querySelectorAll('.ctx__item')].map(b => b.textContent.trim()),
    }
  `)
  note('右击弹出来的菜单条目', menu.items, menu.ctxCount === 1 && menu.items.includes('修改并重新提问'))

  // 4) 点「修改并重新提问」：原话要填回输入框，那行"正在修改历史提问"要出现
  await ev(`
    [...document.querySelectorAll('.ctx__item')]
      .find(b => b.textContent.trim() === '修改并重新提问').click()
    return true
  `)
  const edited = await waitFor(
    `return document.querySelector('.chat__input')?.value === ${JSON.stringify(question)}`,
    '点完没把原话填回输入框',
  )
  const bar = await ev(
    `return [...document.querySelectorAll('p.hud-label')]
      .map(p => p.textContent.trim()).filter(t => t.includes('正在修改历史提问'))`,
  )
  note('输入框被填回原话 + 修改态那一行', { edited, bar: bar.length }, edited && bar.length === 1)

  // 5) 取消之后那一行必须消失（不然"我在改历史"这个状态赖在屏上）
  await ev(`
    [...document.querySelectorAll('button')]
      .find(b => b.textContent.trim() === '取消修改').click()
    return true
  `)
  const cleared = await waitFor(
    `return ![...document.querySelectorAll('p.hud-label')]
      .some(p => p.textContent.includes('正在修改历史提问'))`,
    '点了取消那一行还在',
  )
  note('取消后状态清干净', cleared, cleared === true)

  // 6) 大视频那扇门：不是软件产物的路径必须被**具体那句拒绝**挡掉。
  //    判据不能写成"抛异常也算过" —— 方法名写错时它同样抛异常，那就成了假绿。
  const stream = await ev(`
    try {
      return await window.pywebview.api.artifact_video('C:/Windows/win.ini')
    } catch (error) { return { thrown: String(error) } }
  `)
  note(
    '视频那扇门只放行软件产物',
    stream,
    stream?.ok === false && String(stream.error).includes('不是软件生成的视频'),
  )

  // 7) 这一轮改过的翻译话术，界面上要说"自带模型"而不是"联网"
  const wording = await ev(`
    const texts = [...document.querySelectorAll('p, span, label, button')]
      .map(e => e.textContent.trim()).filter(t => t && t.length < 90)
    return {
      还写着联网翻译: texts.filter(t => t.includes('联网翻译')).slice(0, 3),
      说了自带模型: texts.filter(t => /自带|本地模型|无需 ?Key/i.test(t)).length,
    }
  `)
  note('界面文案没有停在"需要联网翻译"', wording, wording.还写着联网翻译.length === 0)

  const shot = await send('Page.captureScreenshot', { format: 'png' })
  fs.writeFileSync(path.join(output, 'r119-chat-edit.png'), Buffer.from(shot.data, 'base64'))

  const report = { ok: checks.every(c => c.pass), checks, page: page.url }
  fs.writeFileSync(path.join(output, 'r119-ui.json'), JSON.stringify(report, null, 2))
  console.log(
    JSON.stringify({ ok: report.ok, failed: checks.filter(c => !c.pass).map(c => c.name) }),
  )
  process.exitCode = report.ok ? 0 : 1
} finally {
  socket.close()
}

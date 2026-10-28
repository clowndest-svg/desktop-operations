/**
 * 逐弹窗量几何：溢出、文字截断、按钮重叠、列错位、底部留白。
 *
 * 这不是"看一眼觉得对不对"。排版判据必须落在数字上 —— 上一轮用户指着右上角说
 * "关闭.." 那两个点，源码里没有任何一处写着"重叠"，只有 rect 相交能把它抓出来。
 *
 * 用法：node scripts/audit_layout.mjs [输出目录]
 *      node scripts/audit_layout.mjs --narrow   再按 1180x760 跑一遍小窗口
 *
 * 它读的是 `npm run build` 之后的产物（jarvis/ui/web），所以量到的就是发出去的那份。
 */
import fs from 'node:fs'
import path from 'node:path'
import http from 'node:http'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

// 这台机器上只有 qa-tools 里那份 playwright-core；launch 时显式给浏览器路径就够了。
const require = createRequire(import.meta.url)
const { chromium } = (() => {
  try { return require('playwright') } catch { return require('playwright-core') }
})()

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = path.resolve(process.argv[2] || path.join(root, 'reports/layout-audit'))
fs.mkdirSync(output, { recursive: true })

const server = http.createServer((req, res) => {
  // 中文文件名（avatar/小夜.vrm）在 pathname 里是百分号编码的，不解码就是假 404。
  const relative = decodeURIComponent(new URL(req.url, 'http://localhost').pathname)
  const file = path.resolve(root, 'jarvis/ui/web', '.' + (relative === '/' ? '/index.html' : relative))
  if (!file.startsWith(path.join(root, 'jarvis/ui/web')) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
    return res.writeHead(404).end()
  }
  const type = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.webp': 'image/webp', '.png': 'image/png', '.json': 'application/json', '.vrm': 'application/octet-stream' }[path.extname(file)]
  res.setHeader('Content-Type', type || 'application/octet-stream')
  fs.createReadStream(file).pipe(res)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const origin = `http://127.0.0.1:${server.address().port}`

/** 弹窗本体：绝大多数弹窗的 role="dialog" 挂在那层全屏遮罩上，盒子解析在尺子里做。 */
const POPUP = '[role="dialog"]:not([aria-label="功能"])'
/** 顶栏「功能」里每一张能打开的界面；action 那三颗（宠物/最大化/托盘）会动窗口，不进列表。 */
const ENTRIES = [
  ['画图', POPUP], ['做视频', POPUP], ['提醒与助手', POPUP],
  ['长期记忆', POPUP], ['知识库', POPUP], ['自动化', POPUP],
  ['连接手机', POPUP], ['手机投屏', POPUP], ['电脑操作权限', POPUP],
  ['系统与工具', '.app__diagnostics'],
  ['形象与声音', POPUP], ['语音控制', '.app__voice'], ['外观', POPUP], ['扩展', POPUP],
  ['操作记录', POPUP], ['对话历史', POPUP], ['用量', POPUP], ['提醒与告警', POPUP],
  ['设置', POPUP],
]

/**
 * 在页面里跑的那把尺子。
 *
 * 必须是真函数而不是字符串表达式：`evaluate('(() => ...)()')` 拿到的是函数本身，
 * 结果是一个 undefined，看起来像"什么都没测到"而不是"测法错了"。
 *
 * 只报"看得见的东西"：display:none 和零面积的都不算，否则一个隐藏的模板会把
 * 底部留白算成一片空白。
 */
function auditScope(node) {
  // 弹窗的 role="dialog" 挂在 `position:fixed; inset:0` 那层遮罩上，量它等于量整个
  // 视口 —— 于是"底部留白 40%"是假的（盒子居中，四周当然有空）。所以：**只在当前这层
  // 铺满视口时往里走一步**，走到那个带边框的盒子就停。
  //
  // 只走一步是刻意的。连续下钻（"谁装着 90% 的控件就进谁"）会一路钻进页签条或标题行，
  // 于是「用量」那张被量成 900x121，它真正的截断问题就在这一步里被漏掉了。
  let scope = node
  const outer = scope.getBoundingClientRect()
  if (outer.width >= window.innerWidth - 2 && outer.height >= window.innerHeight - 2) {
    const shell = [...scope.children].find(child => (
      !/scrim|overlay|mask/i.test(String(child.className))
      && child.querySelectorAll('button, input, select, a').length > 0
    ))
    if (shell) scope = shell
  }
  const shown = (el) => {
    const box = el.getBoundingClientRect()
    if (!box.width || !box.height) return false
    const style = getComputedStyle(el)
    return style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity) > 0.01
  }
  const walk = (el, depth, out) => {
    for (const child of el.children) {
      if (!shown(child)) continue
      out.push({ el: child, depth })
      walk(child, depth + 1, out)
    }
    return out
  }
  const label = (el) => String(el.getAttribute('aria-label') || el.className || el.tagName).slice(0, 46)
  const box = scope.getBoundingClientRect()
  const items = walk(scope, 0, [])
  const problems = []

  // 1) 横向溢出：容器被内容撑破，用户得横向滚动，或者看不见被裁的那一列。
  if (scope.scrollWidth > scope.clientWidth + 1) problems.push(`横向溢出 ${scope.scrollWidth - scope.clientWidth}px`)

  for (const { el } of items) {
    const style = getComputedStyle(el)
    if (el.children.length || !el.textContent.trim()) continue
    // 2) 文字被截断：确实带 ellipsis 的才算。
    //
    // 带 title 的省略号是设计的一部分（悬停能读全文），不该报；只有"根本没有全文可看"
    // 或者"截掉一大半"才算事故。模型名那两处差 2~8 px，带上 title 之后就是正常的。
    if (style.textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth + 1) {
      const missing = (el.scrollWidth - el.clientWidth) / Math.max(1, el.scrollWidth)
      const hinted = el.getAttribute('title') || el.closest('[title]')
      if (!hinted || missing > 0.4) {
        problems.push(`文字截断「${el.textContent.trim().slice(0, 18)}」差 ${el.scrollWidth - el.clientWidth}px`)
      }
    }
  }
  for (const { el } of items) {
    const style = getComputedStyle(el)
    // 3) 按钮里的中文被挤断行 —— 这是"排版烂"最常见的形状。
    // 数的是文本自己的 client rects：一颗卡片按钮有两行（标题 + 说明）是设计，
    // 文字在按钮内部被折断才是事故。按高度判会把「外观 / 选择界面配色」和方形图标
    // 按钮全报成假阳性 —— 第一版就是这么误报了三个。
    if (/^(BUTTON|A)$/.test(el.tagName) && !el.children.length && el.textContent.trim()) {
      const range = document.createRange()
      range.selectNodeContents(el)
      if (range.getClientRects().length > 1) problems.push(`按钮文字折断「${el.textContent.trim().slice(0, 12)}」`)
    }
    // 4) 内容被 overflow:hidden 裁掉而不是滚动 —— 用户看不见就等于丢了。
    if (style.overflowY === 'hidden' && el.scrollHeight > el.clientHeight + 4 && el.clientHeight > 40) {
      problems.push(`内容被裁切 ${el.scrollHeight - el.clientHeight}px（${label(el)}）`)
    }
  }

  // 5) 同层兄弟重叠：两个都点得着，但屏幕上是一坨。
  const groups = new Map()
  for (const { el } of items) {
    if (!/^(BUTTON|A|INPUT|SELECT)$/.test(el.tagName)) continue
    const key = el.parentElement
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(el)
  }
  for (const group of groups.values()) {
    for (let a = 0; a < group.length; a += 1) {
      for (let b = a + 1; b < group.length; b += 1) {
        const x = group[a].getBoundingClientRect(), y = group[b].getBoundingClientRect()
        const left = Math.max(x.left, y.left), right = Math.min(x.right, y.right)
        const top = Math.max(x.top, y.top), bottom = Math.min(x.bottom, y.bottom)
        if (right - left > 2 && bottom - top > 2) {
          problems.push(`控件重叠「${(group[a].textContent.trim() || label(group[a])).slice(0, 8)}」×「${(group[b].textContent.trim() || label(group[b])).slice(0, 8)}」`)
        }
      }
    }
  }

  // 6) 底部留白：内容只占上半截，下面一大片空。
  let deepest = box.top
  for (const { el } of items) {
    const rect = el.getBoundingClientRect()
    const style = getComputedStyle(el)
    if (style.position === 'absolute' || style.position === 'fixed') continue
    if (rect.bottom > deepest && rect.bottom <= box.bottom + 2) deepest = rect.bottom
  }
  const pad = parseFloat(getComputedStyle(scope).paddingBottom) || 0
  const gap = Math.round(box.bottom - pad - deepest)
  const ratio = box.height ? gap / box.height : 0
  if (gap > 56 && ratio > 0.14) problems.push(`底部留白 ${gap}px（${Math.round(ratio * 100)}%）`)

  return {
    box: { width: Math.round(box.width), height: Math.round(box.height) },
    problems: [...new Set(problems)],
  }
}

const browser = await chromium.launch({
  executablePath: process.env.XIAOYE_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  headless: true,
  args: ['--autoplay-policy=no-user-gesture-required', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
})
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.setDefaultTimeout(8000)
const crashes = []
page.on('pageerror', error => crashes.push(error.message))

const results = []

async function openEntry(label) {
  await page.goto(origin + '?nosplash=1', { waitUntil: 'load' })
  if (label === '设置') {
    // 「设置」在顶栏，功能菜单开着时那层 scrim 会把手指挡在外面 —— 所以走它自己那颗。
    await page.getByRole('button', { name: '设置', exact: true }).click()
  } else {
    await page.getByRole('button', { name: /^功能/ }).click()
    await page.locator('.feature-menu').waitFor()
    await page.locator('.feature-menu__group button', { hasText: label }).first().click()
  }
  await page.waitForTimeout(900)
}

async function look(label, suffix = '') {
  const entry = ENTRIES.find(row => row[0] === label)
  try {
    await openEntry(label)
    await measureScope(entry ? entry[1] : POPUP, label + suffix)
  } catch (error) {
    results.push({ name: label + suffix, ok: false, problems: [`没能打开：${String(error).split('\n')[0].slice(0, 90)}`] })
  }
}

async function measureScope(selector, name) {
  const handle = page.locator(selector).first()
  if (!(await handle.count())) {
    results.push({ name, ok: false, problems: ['没找到这一层'] })
    return
  }
  const answer = await handle.evaluate(auditScope)
  results.push({ name, ok: !answer.problems.length, ...answer })
}

// —— 命令面板本身
await page.goto(origin + '?nosplash=1', { waitUntil: 'load' })
await page.getByRole('button', { name: /^功能/ }).click()
await page.locator('.feature-menu').waitFor()
await measureScope('.feature-menu', '命令面板')
const columns = await page.evaluate(() => {
  const list = [...document.querySelectorAll('.feature-menu__column')]
  const edges = list.map(el => el.getBoundingClientRect())
  return edges.length >= 2
    ? { left: Math.round(edges[0].height), right: Math.round(edges[1].height), diff: Math.round(Math.abs(edges[0].height - edges[1].height)) }
    : { left: 0, right: 0, diff: -1 }
})
results.push({ name: '命令面板两列高差', ok: columns.diff >= 0 && columns.diff <= 90, ...columns })

for (const [label] of ENTRIES) await look(label)

// —— 对话区右上角那一排：放大态才会多出「关闭」，那一排才可能挤
await page.goto(origin + '?nosplash=1', { waitUntil: 'load' })
await page.locator('.chat__toolbar').first().waitFor()
await page.getByRole('button', { name: '对话选项', exact: true }).click()
await page.locator('#chat-options .chat__expand').click({ timeout: 8000 }).catch(() => {})
await page.waitForTimeout(500)
const toolbar = await page.locator('.chat__toolbar').first().evaluate(auditScope)
results.push({ name: '对话区顶栏（放大态）', ok: !toolbar.problems.length, ...toolbar })
const overlap = await page.evaluate(() => {
  const buttons = [...document.querySelectorAll('.chat__toolbar > button')].filter(el => el.offsetParent)
  const rects = buttons.map(el => ({ text: el.textContent.trim().slice(0, 6), ...el.getBoundingClientRect().toJSON() }))
  let worst = 0
  let pair = ''
  for (let a = 0; a < rects.length; a += 1) {
    for (let b = a + 1; b < rects.length; b += 1) {
      const x = Math.min(rects[a].right, rects[b].right) - Math.max(rects[a].left, rects[b].left)
      const y = Math.min(rects[a].bottom, rects[b].bottom) - Math.max(rects[a].top, rects[b].top)
      if (x > 2 && y > 2 && x > worst) { worst = x; pair = `${rects[a].text}×${rects[b].text}` }
    }
  }
  return { count: rects.length, overlap: Math.round(worst), pair, expanded: rects.some(el => el.text.includes('关闭')) }
})
results.push({ name: '对话区顶栏按钮重叠', ok: overlap.overlap === 0 && overlap.expanded, ...overlap })

if (process.argv.includes('--narrow')) {
  await page.setViewportSize({ width: 1180, height: 760 })
  await look('形象与声音', ' @1180x760')
  await look('知识库', ' @1180x760')
  await look('扩展', ' @1180x760')
}

const bad = results.filter(entry => !entry.ok)
fs.writeFileSync(path.join(output, 'layout-audit.json'), JSON.stringify({ at: new Date().toISOString(), results, crashes }, null, 2))
for (const entry of results) {
  const size = entry.box ? `${entry.box.width}x${entry.box.height}` : entry.diff !== undefined ? `${entry.left}/${entry.right}px` : ''
  console.log(`${entry.ok ? 'PASS' : 'FAIL'} ${entry.name} ${size} ${(entry.problems || entry.pair || '').toString()}`)
}
if (crashes.length) console.log('页面异常：', crashes.join(' | '))
console.log(`\n${results.length} 项检查，${bad.length} 项有问题`)
await page.screenshot({ path: path.join(output, 'audit.png') }).catch(() => {})
await browser.close()
server.close()
process.exit(bad.length || crashes.length ? 1 : 0)

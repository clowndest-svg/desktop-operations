/** Run only on seed_history_qa.py's isolated home: modifies synthetic QA sessions. */
import fs from 'node:fs'
import path from 'node:path'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
const require = createRequire(process.env.XIAOYE_QA_NODE_PACKAGE || import.meta.url)
const { chromium } = require('playwright-core')
const port = process.argv[2] || '9337'
const output = path.resolve(process.argv[3] || 'reports/audit-r129/ui')
const mode = process.argv[4] || 'initial'
fs.mkdirSync(output, { recursive: true })
const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`)
const page = browser.contexts().flatMap(c => c.pages()).find(p => p.url().includes('127.0.0.1') && !p.url().includes('mode=pet'))
assert.ok(page)
page.setDefaultTimeout(15000)
const report = { ok: false, mode, checks: [], errors: [] }
page.on('pageerror', error => report.errors.push(error.message))
const api = (method, ...args) => page.evaluate(async ({ method, args }) => window.pywebview.api[method](...args), { method, args })
const check = (name, value) => { assert.ok(value, name); report.checks.push(name); console.log('PASS ' + name) }
try {
  await page.waitForFunction(() => window.pywebview?.api && document.querySelector('.chat'))
  check('synthetic isolated history is present', (await api('chat_sessions', 'QA 完整长对话', 0, 50, false)).sessions.some(row => row.id === 'qa-history-long'))
  if (mode === 'restart') {
    const pinned = await api('chat_sessions', '', 0, 50, true)
    check('pin and renamed title survive process restart', pinned.sessions.some(row => row.id === 'qa-history-long' && row.title === 'QA 完整长对话 已改名'))
    const exported = await api('chat_export', 'qa-history-long')
    check('full export remains complete after restart', exported.ok && exported.count === 262)
  } else {
    await api('chat_pin', 'qa-history-long', false)
    await api('chat_rename', 'qa-history-long', 'QA 完整长对话')
    await page.getByRole('button', { name: '历史', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '对话历史', exact: true })
    await page.waitForFunction(() => document.querySelectorAll('.ss__row').length === 50)
    await dialog.getByRole('button', { name: '加载更多', exact: true }).click()
    await page.waitForFunction(() => document.querySelectorAll('.ss__row').length >= 66)
    check('older sessions are accessible by loading another page', await dialog.locator('.ss__row').count() >= 66)
    await dialog.getByRole('button', { name: '全选已加载', exact: true }).click()
    const search = dialog.getByRole('searchbox', { name: '搜索对话标题或内容' })
    await search.fill("100%_O'Reilly")
    await page.waitForFunction(() => document.querySelectorAll('.ss__row').length === 1)
    check('body search finds an old session and shows the matching excerpt', (await dialog.locator('.ss__excerpt').innerText()).includes("100%_O'Reilly"))
    check('changing filters clears hidden deletion selections', await dialog.getByRole('button', { name: '删除所选', exact: true }).isDisabled())
    await dialog.locator('.ss__row').getByRole('button', { name: '置顶', exact: true }).click()
    await dialog.getByRole('button', { name: '取消置顶', exact: true }).waitFor()
    await dialog.getByRole('button', { name: '改名', exact: true }).click()
    await dialog.getByRole('textbox', { name: '新的对话名称' }).fill('QA 完整长对话 已改名')
    await dialog.getByRole('button', { name: '保存名称', exact: true }).click()
    await dialog.locator('strong').filter({ hasText: '已改名' }).waitFor()
    await dialog.getByRole('button', { name: '导出', exact: true }).click()
    await dialog.getByRole('status').filter({ hasText: '已导出 262 条消息' }).waitFor()
    const exported = await api('chat_export', 'qa-history-long')
    const text = fs.readFileSync(exported.path, 'utf8')
    check('actual bridge export includes first and last message and correct title', exported.ok && text.includes('验收消息 0000') && text.includes('验收消息 0261') && text.startsWith('# QA 完整长对话 已改名'))
    await search.fill('')
    await dialog.getByLabel('只看置顶').check()
    await page.waitForFunction(() => document.querySelectorAll('.ss__row').length === 1)
    check('pinned-only filter reaches the database', (await api('chat_sessions', '', 0, 50, true)).sessions.some(row => row.id === 'qa-history-long'))
    await page.screenshot({ path: path.join(output, 'history-desktop.png') })
    const cdp = await page.context().newCDPSession(page)
    await cdp.send('Emulation.setDeviceMetricsOverride', { width: 420, height: 820, deviceScaleFactor: 1, mobile: false })
    check('narrow layout keeps actions inside the dialog', await dialog.evaluate(node => node.scrollWidth <= node.clientWidth + 1))
    await page.screenshot({ path: path.join(output, 'history-narrow.png') })
    await cdp.send('Emulation.clearDeviceMetricsOverride')
    await cdp.detach()
    await dialog.getByLabel('只看置顶').uncheck()
    await search.fill('QA 项目 00')
    await page.waitForFunction(() => document.querySelectorAll('.ss__row').length === 1 && document.querySelector('.ss__row strong').textContent.includes('项目 00'))
    await dialog.getByRole('button', { name: '删除', exact: true }).click()
    await dialog.locator('.ss__confirm').getByRole('button', { name: '取消', exact: true }).click()
    check('cancel deletion retains the real record', (await api('chat_sessions', 'QA 项目 00')).sessions.length === 1)
    await dialog.getByRole('button', { name: '删除', exact: true }).click()
    await dialog.locator('.ss__confirm').getByRole('button', { name: '确认删除', exact: true }).click()
    await page.waitForFunction(() => document.querySelectorAll('.ss__row').length === 0)
    check('explicit deletion removes only the chosen synthetic record', (await api('chat_sessions', 'QA 项目 00')).sessions.length === 0 && (await api('chat_sessions', 'QA 项目 01')).sessions.length === 1)
    // Delay only the old reply to reproduce response inversion on the real frontend.
    await page.evaluate(() => {
      const original = window.pywebview.api.chat_sessions
      window.__historyOriginal = original
      window.pywebview.api.chat_sessions = async (...args) => {
        const reply = await original(...args)
        if (args[0] === 'QA 项目 01') { window.__historySlowStarted = true; await new Promise(resolve => setTimeout(resolve, 1200)) }
        return reply
      }
    })
    await search.fill('QA 项目 01')
    await page.waitForFunction(() => window.__historySlowStarted)
    await search.fill('QA 项目 02')
    await page.waitForFunction(() => document.querySelector('.ss__row strong')?.textContent.includes('项目 02'))
    await page.waitForTimeout(1300)
    check('slow obsolete search response cannot replace the latest result', (await dialog.locator('strong').innerText()).includes('项目 02'))
    await page.evaluate(() => { window.pywebview.api.chat_sessions = window.__historyOriginal })
    await page.evaluate(() => { window.__historySwitch = window.pywebview.api.chat_switch; window.pywebview.api.chat_switch = async () => ({ error: 'QA 打开失败', current: '', sessions: [] }) })
    await dialog.getByRole('button', { name: '打开', exact: true }).click()
    await dialog.getByRole('alert').filter({ hasText: 'QA 打开失败' }).waitFor()
    check('failed open stays visible and reports the error', await dialog.isVisible())
    await page.evaluate(() => { window.pywebview.api.chat_switch = window.__historySwitch })
    await dialog.getByRole('button', { name: '关闭', exact: true }).click()
  }
  check('no JavaScript runtime errors', report.errors.length === 0)
  report.ok = true
} catch (error) { report.error = String(error); await page.screenshot({ path: path.join(output, 'failure.png') }).catch(() => {}); throw error }
finally { fs.writeFileSync(path.join(output, `history-${mode}.json`), JSON.stringify(report, null, 2)); await browser.close() }

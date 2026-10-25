/** Real packaged local-model search: success needs sources; failure stays explicit. */
import fs from 'node:fs'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
const { chromium } = createRequire(import.meta.url)('playwright')
const browser = await chromium.connectOverCDP(`http://127.0.0.1:${process.argv[2] || '9336'}`)
const page = browser.contexts().flatMap(c => c.pages()).find(p => p.url().includes('127.0.0.1') && !p.url().includes('mode=pet'))
const output = process.argv[3] || 'reports/brand-r124/search-result.json'
assert.ok(page)
const api = (method, ...args) => page.evaluate(async ({ method, args }) => window.pywebview.api[method](...args), { method, args })
try {
  const settings = await api('settings_apply', { local_model: 'Qwen3-0.6B-Q8_0.gguf' })
  assert.equal(Object.keys(settings.problems || {}).length, 0)
  await api('chat_pick', 'local', 'qwen3-0.6b')
  await api('chat_tuning', 'off', 5, true)
  const fresh = await api('chat_new')
  const task = await api('chat_send', 'Please search the web: 美国大学排名和榜单年份', [], fresh.conversation)
  const deadline = Date.now() + 90000
  let result
  while (Date.now() < deadline) {
    result = (await api('chat_tasks')).tasks.find(row => row.task_id === task.task_id)
    if (result && ['done', 'failed', 'cancelled'].includes(result.state)) break
    await new Promise(resolve => setTimeout(resolve, 500))
  }
  assert.equal(result?.state, 'done')
  assert.ok(result.tools_used.includes('search_web'))
  const answer = (await api('chat_messages', fresh.conversation)).messages.filter(row => row.role === 'assistant').at(-1)?.content || ''
  assert.ok(answer.includes('未取得可核对的资料') || /https?:\/\//.test(answer), answer)
  assert.notEqual(answer.trim(), '我查过了。')
  fs.writeFileSync(output, JSON.stringify({ ok: true, state: result.state, tools: result.tools_used, answer, note: 'Real upstream search may have no hits; no account credentials or microphone used.' }, null, 2))
  console.log('PASS packaged search returns actual sources or an explicit failure receipt')
} finally { await browser.close() }

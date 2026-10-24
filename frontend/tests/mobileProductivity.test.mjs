import assert from 'node:assert/strict'
import test from 'node:test'
import { PROMPTS, promptFields, fillPrompt, matchingMessages } from '../src/mobile/productivity.ts'
import { emptyWorkspace, parseWorkspace, backupWorkspace, forkSession } from '../src/mobile/workspace.ts'
import { requestHistory } from '../src/mobile/conversation.ts'

test('each of the 16 built-in templates can be completed and has a unique identifier', () => {
  assert.equal(PROMPTS.length, 16)
  assert.equal(new Set(PROMPTS.map(p => p.id)).size, 16)
  for (const p of PROMPTS) {
    const values = Object.fromEntries(promptFields(p.body).map(field => [field, '测试内容']))
    assert(!fillPrompt(p.body, values).includes('{{'))
  }
})
test('template variables are unique, whitespace normalized, required and inserted literally', () => {
  assert.deepEqual(promptFields('{{ 原文 }} {{原文}} {{目标}}'), ['原文', '目标'])
  assert.equal(fillPrompt('{{原文}} {{ 原文 }}', { 原文: '$& {{literal}} <script>' }), '$& {{literal}} <script> $& {{literal}} <script>')
  assert.throws(() => fillPrompt('{{原文}}', {}), /全部/)
  assert.throws(() => fillPrompt('{{原文}}', { 原文: ' ' }), /全部/)
  assert.throws(() => fillPrompt('{{原文}}', { 原文: 'x'.repeat(40001) }), /40000/)
})
test('old v1 backups migrate with default drafts, archive, notes, prompts and preferences', () => {
  const w = emptyWorkspace([{ role: 'user', text: '原文' }])
  delete w.notes; delete w.prompts; delete w.reading
  delete w.sessions[0].archived; delete w.sessions[0].draft
  const restored = parseWorkspace(JSON.stringify(w))
  assert.deepEqual(restored.notes, []); assert.deepEqual(restored.prompts, [])
  assert.equal(restored.sessions[0].draft, ''); assert.equal(restored.sessions[0].archived, false)
  assert.deepEqual(restored.reading, { fontSize: 'normal', compact: false, context: 30, tools: true })
})
test('backup restores full notebooks, tasks, templates, drafts and reading preferences', () => {
  const w = emptyWorkspace()
  w.sessions[0].draft = '尚未发送'; w.sessions[0].archived = true
  w.notes = [{ id: 'n', title: '待办', text: '测试', tags: '工作', kind: 'task', done: true, pinned: true, updated: 12 }]
  w.prompts = [{ id: 'p', title: '模板', category: '自建', body: '{{原文}}' }]
  w.reading = { fontSize: 'large', compact: true, context: 10, tools: false }
  assert.deepEqual(parseWorkspace(backupWorkspace(w)), w)
})
test('content validation rejects malformed notes, duplicate IDs and invalid conversation preferences', () => {
  for (const change of [
    w => { w.notes = [{ id: 'n', title: 'n', text: '', tags: '', kind: 'injected', done: false, pinned: false, updated: 1 }] },
    w => { w.prompts = [{ id: 'p', title: 'p', category: 'a', body: 'b' }, { id: 'p', title: 'p', category: 'a', body: 'b' }] },
    w => { w.sessions[0].draft = 'x'.repeat(40001) },
    w => { w.sessions[0].archived = 'true' },
    w => { w.reading.context = '30' },
    w => { w.reading.tools = 'false' },
    w => { w.reading.fontSize = '<script>' },
    w => { w.notes = Array(101).fill({}) },
    w => { w.prompts = Array(51).fill({}) },
  ]) { const w = emptyWorkspace(); change(w); assert.throws(() => parseWorkspace(JSON.stringify(w))) }
})
test('unknown nested fields are excluded from exported notebooks and prompts', () => {
  const w = emptyWorkspace()
  w.notes = [{ id: 'n', title: 'n', text: 't', tags: '', kind: 'note', done: false, pinned: false, updated: 12, apiKey: 'NOTE_SECRET' }]
  w.prompts = [{ id: 'p', title: 'p', category: 'a', body: 'b', token: 'PROMPT_TOKEN' }]
  w.reading.token = 'READING_TOKEN'
  const raw = backupWorkspace(w)
  for (const value of ['NOTE_SECRET', 'PROMPT_TOKEN', 'READING_TOKEN']) assert(!raw.includes(value))
})
test('conversation branches preserve originals and attachment context without sharing objects', () => {
  const w = emptyWorkspace([{ role: 'user', text: '问题', context: '资料原文' }, { role: 'assistant', text: '回答' }, { role: 'user', text: '后续' }])
  const source = w.sessions[0]; source.instruction = '自定义指令'; source.preset = 'reader'
  const branch = forkSession(source, 1)
  assert.equal(branch.turns.length, 2); assert.notEqual(branch.id, source.id)
  assert.equal(branch.preset, 'reader'); assert.equal(branch.instruction, '自定义指令')
  assert.equal(branch.turns[0].context, '资料原文')
  branch.turns[0].text = '修改'
  assert.equal(source.turns[0].text, '问题'); assert.equal(source.turns.length, 3)
  for (const index of [-1, 3, 0.5, NaN]) assert.throws(() => forkSession(source, index))
})
test('search returns exact message indices without treating user input as regex or HTML', () => {
  const turns = [{ text: '苹果 apple' }, { text: 'APPLE' }, { text: '<script> [x]' }]
  assert.deepEqual(matchingMessages(turns, ' APPLE '), [0, 1])
  assert.deepEqual(matchingMessages(turns, '[x]'), [2]); assert.deepEqual(matchingMessages(turns, ''), [])
  assert.deepEqual(matchingMessages(turns, '<script>'), [2])
})
test('context settings bound actual messages and keep the newest question once', () => {
  const turns = Array.from({ length: 60 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', text: String(i) }))
  for (const limit of [10, 30, 60]) {
    const result = requestHistory(turns, '新问题', limit)
    assert(result.length <= limit); assert.equal(result[0].role, 'user')
    assert.equal(result.at(-1).text, '新问题'); assert.equal(result.filter(t => t.text === '新问题').length, 1)
  }
})

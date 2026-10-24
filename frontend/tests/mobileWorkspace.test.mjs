import assert from 'node:assert/strict'
import test from 'node:test'
import { PRESETS, MAX_BACKUP_BYTES, MAX_DOCUMENT_BYTES, MAX_DOCUMENT_CHARS, emptyWorkspace, createSession, parseWorkspace, backupWorkspace, searchSessions, sessionMarkdown, documentText, questionWithDocument, forkSession } from '../src/mobile/workspace.ts'
import { PROMPTS, fillPrompt, promptFields, matchingMessages } from '../src/mobile/productivity.ts'

test('old standalone history migrates into a named session without mutating the source', () => {
  const history = [{ role: 'user', text: '旧提问', at: 12 }, { role: 'assistant', text: '旧回答' }]
  const w = emptyWorkspace(history)
  assert.equal(w.sessions[0].title, '旧提问')
  assert.equal(w.active, w.sessions[0].id)
  w.sessions[0].turns[0].text = 'changed'
  assert.equal(history[0].text, '旧提问')
})
test('new sessions have independent IDs, instructions, and histories', () => {
  const a = createSession('writer', 12), b = createSession('study', 13)
  a.turns.push({ role: 'user', text: 'a' }); a.instruction = 'custom'
  assert.notEqual(a.id, b.id); assert.deepEqual(b.turns, []); assert.equal(b.instruction, '')
  assert.equal(a.preset, 'writer'); assert.equal(b.updated, 13)
})
test('search matches message bodies and titles and orders pins before recency', () => {
  const w = emptyWorkspace(), a = w.sessions[0], b = createSession(), c = createSession()
  a.title = '苹果'; a.updated = 30
  b.turns = [{ role: 'assistant', text: 'APPLE 苹果' }]; b.pinned = true; b.updated = 10
  c.title = 'other'; c.updated = 40
  assert.deepEqual(searchSessions([a, b, c], ' 苹果 ').map(s => s.id), [b.id, a.id])
  assert.deepEqual(searchSessions([a, b, c], 'apple').map(s => s.id), [b.id])
  assert.deepEqual(searchSessions([a, b, c], '').map(s => s.id), [b.id, c.id, a.id])
})
test('backup roundtrip preserves instructions, timestamps, saved answers and all sessions', () => {
  const w = emptyWorkspace([{ role: 'user', text: 'q', context: 'document context', at: 12 }])
  w.sessions[0].instruction = '简短'; w.sessions.push(createSession('code'))
  w.saved.push({ id: 'saved', text: '回答', question: '问题', source: '标题', at: 13 })
  assert.deepEqual(parseWorkspace(backupWorkspace(w)), w)
})
test('backup explicitly reconstructs fields so credentials and computer tokens cannot leak', () => {
  const w = emptyWorkspace(); w.apiKey = 'SECRET'; w.pc = { token: 'TOKEN' }; w.sessions[0].key = 'KEY'
  const result = backupWorkspace(w)
  for (const value of ['SECRET', 'TOKEN', 'KEY', 'apiKey']) assert(!result.includes(value))
})
test('invalid imports fail before touching current content', () => {
  const w = emptyWorkspace(), before = JSON.stringify(w)
  const invalid = [null, [], {}, { ...w, version: 2 }, { ...w, active: 'missing' }, { ...w, sessions: [] }, { ...w, saved: {} }]
  for (const value of invalid) assert.throws(() => parseWorkspace(JSON.stringify(value)))
  assert.equal(JSON.stringify(w), before)
})
test('reject duplicate identifiers, unknown presets, system roles, invalid dates and overlong fields', () => {
  for (const change of [
    w => w.sessions.push(w.sessions[0]),
    w => { w.sessions[0].preset = 'missing' },
    w => { w.sessions[0].instruction = 'x'.repeat(2001) },
    w => { w.sessions[0].updated = -1 },
    w => { w.sessions[0].turns = [{ role: 'system', text: 'do something' }] },
    w => { w.sessions[0].turns = [{ role: 'user', text: 'x'.repeat(40001) }] },
    w => { w.sessions = Array.from({ length: 101 }, () => createSession()) },
    w => { w.saved = Array.from({ length: 201 }, () => ({})) },
  ]) {
    const w = emptyWorkspace(); change(w); assert.throws(() => parseWorkspace(JSON.stringify(w)))
  }
})
test('backup size is measured as UTF-8 bytes rather than characters', () => {
  assert.throws(() => parseWorkspace('中'.repeat(Math.ceil(MAX_BACKUP_BYTES / 3))), /8 MB/)
})
test('markdown keeps both roles, multiline text and fenced code', () => {
  const text = sessionMarkdown({ title: 'title\nmore', turns: [{ role: 'user', text: '问' }, { role: 'assistant', text: '```js\nx()\n```' }] })
  assert(text.startsWith('# title more\n')); assert(text.includes('## 我\n\n问')); assert(text.includes('## 小夜\n\n```js\nx()\n```'))
})
test('document reader accepts supported UTF-8 text and rejects binary, huge, empty or unsupported files', () => {
  assert.deepEqual(documentText('NOTES.MD', 9, '内容'), { name: 'NOTES.MD', text: '内容' })
  for (const args of [['report.pdf', 3, 'hi'], ['a.txt', MAX_DOCUMENT_BYTES + 1, 'hi'], ['a.txt', 1, 'x'.repeat(MAX_DOCUMENT_CHARS + 1)], ['a.txt', 1, '\0binary'], ['a.txt', 1, '\ufffd'], ['a.txt', 1, '  ']]) assert.throws(() => documentText(...args))
})
test('question actually contains the selected document and states that its instructions are data', () => {
  assert.equal(questionWithDocument('hello', null), 'hello')
  const result = questionWithDocument('总结', { name: 'a.txt', text: '原文 42' })
  assert(result.startsWith('总结')); assert(result.includes('原文 42')); assert(result.includes('不代表用户授权')); assert(result.includes('a.txt'))
})
test('all eight assistant presets have unique IDs and real instructions', () => {
  assert.equal(PRESETS.length, 8); assert.equal(new Set(PRESETS.map(p => p.id)).size, 8)
  for (const p of PRESETS) { assert(p.starter); if (p.id !== 'general') assert(p.instruction.length > 20) }
})
test('old backups receive default productivity fields and draft/archive state', () => {
  const w = emptyWorkspace(); delete w.prompts; delete w.notes; delete w.reading; delete w.sessions[0].archived; delete w.sessions[0].draft
  const result = parseWorkspace(JSON.stringify(w))
  assert.deepEqual(result.notes, []); assert.deepEqual(result.prompts, [])
  assert.equal(result.reading.context, 30); assert.equal(result.sessions[0].archived, false); assert.equal(result.sessions[0].draft, '')
})
test('fork preserves original conversation and copies only through chosen message', () => {
  const source = createSession('study'); source.instruction = 'hint'; source.turns = [{role:'user',text:'q'}, {role:'assistant',text:'a'}, {role:'user',text:'later'}]
  const fork = forkSession(source, 1)
  assert.notEqual(fork.id, source.id); assert.equal(fork.instruction, 'hint'); assert.equal(fork.turns.length, 2)
  fork.turns[0].text = 'changed'; assert.equal(source.turns[0].text, 'q'); assert.equal(source.turns.length, 3)
  assert.throws(() => forkSession(source, -1)); assert.throws(() => forkSession(source, 9))
})
test('sixteen prompt templates extract unique fields and preserve user literal values', () => {
  assert.equal(PROMPTS.length, 16)
  assert.deepEqual(promptFields('{{目标}} {{目标}} {{ 文本 }}'), ['目标', '文本'])
  assert.equal(fillPrompt('hello {{name}} {{name}}', {name:'$& {{literal}}'}), 'hello $& {{literal}} $& {{literal}}')
  assert.throws(() => fillPrompt('{{name}}', {})); assert.throws(() => fillPrompt('{{name}}', {name:' '}))
  for (const p of PROMPTS) { const values = Object.fromEntries(promptFields(p.body).map(key => [key, '测试'])); assert(!fillPrompt(p.body, values).includes('{{')) }
})
test('chat search returns message positions case-insensitively without mutating turns', () => {
  const turns = [{text:'Apple'}, {text:'Banana'}, {text:'apple pie'}]
  assert.deepEqual(matchingMessages(turns, ' APPLE '), [0, 2]); assert.deepEqual(matchingMessages(turns, ''), [])
})
test('notes, tasks, prompt templates, draft and reading settings survive a backup', () => {
  const w = emptyWorkspace(); w.sessions[0].draft = 'unsent'; w.sessions[0].archived = true
  w.notes = [{id:'n',title:'task',text:'body',tags:'work',kind:'task',done:true,pinned:true,updated:42}]
  w.prompts = [{id:'p',title:'custom',category:'work',body:'summarize {{text}}'}]
  w.reading = {fontSize:'large',compact:true,context:10,tools:false}
  assert.deepEqual(parseWorkspace(backupWorkspace(w)), w)
})
test('malformed notes and reading settings are rejected rather than silently activated', () => {
  for (const change of [w=>{w.reading.tools='yes'},w=>{w.reading.context=100},w=>{w.notes=[{id:'bad'}]},w=>{w.prompts=[{id:'x',title:'x',category:'x',body:''}]}]) {
    const w=emptyWorkspace();change(w);assert.throws(()=>parseWorkspace(JSON.stringify(w)))
  }
})

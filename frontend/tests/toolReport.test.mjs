import assert from 'node:assert/strict'
import test from 'node:test'
import { describeToolReport } from '../src/chat/toolReport.ts'

test('reopened history has no tool metadata and must remain renderable', () => {
  for (const value of [{}, undefined, null, { used: null }, { used: 'search_web' }]) {
    assert.equal(describeToolReport(value), '这条历史未记录工具调用信息')
  }
})

test('absence of metadata does not claim that no tools were carried', () => {
  assert.equal(describeToolReport({ used: [] }), '这条历史未记录工具调用信息')
  assert.equal(describeToolReport({ used: [], count: 0 }), '这一轮没有携带工具')
})

test('real calls retain the tool names and selected capability pack', () => {
  assert.equal(
    describeToolReport({ used: ['search_web', 'fetch_url'], count: 4, pack: 'network' }),
    '本轮调用了 search_web、fetch_url（能力包 network）',
  )
})

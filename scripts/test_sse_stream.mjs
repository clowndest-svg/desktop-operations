/**
 * 流式解析的单元测试：直接在 Node 里跑 `frontend/src/mobile/sse.ts`。
 *
 * 为什么要单独跑：这些边界（半行、`[DONE]` 前还有字、tool_calls 的参数一个字符
 * 一个字符漏出来、端点被要求流式却整块返回）在手机上的表现是"答案少了半句"，
 * **不报错、不崩溃**，装机时没人会发现。能在 Node 里判死的就别留给真机。
 *
 * 跑法：node --experimental-strip-types scripts/test_sse_stream.mjs
 * （Node 24 自带 TypeScript 类型剥离，不需要先构建前端。）
 */

import { createAccumulator } from '../frontend/src/mobile/sse.ts'

let failed = 0
let passed = 0

function check(label, actual, expected) {
  const left = JSON.stringify(actual)
  const right = JSON.stringify(expected)
  if (left === right) {
    passed += 1
    console.log(`[过] ${label}`)
    return
  }
  failed += 1
  console.log(`[不过] ${label}\n      期望 ${right}\n      实际 ${left}`)
}

function frame(obj) {
  return `data: ${JSON.stringify(obj)}\n\n`
}

function feed(chunks) {
  const acc = createAccumulator()
  const seen = []
  for (const chunk of chunks) {
    const delta = acc.push(chunk)
    if (delta.content) seen.push(delta.content)
  }
  const done = acc.finish()
  return { ...done, shown: seen.join('') }
}

// 1. 正常一段话：每帧一个字
const plain = feed(
  ['你', '好', '呀'].map((word) => frame({ choices: [{ delta: { content: word } }] })),
)
check('逐字帧拼成整句', plain.content, '你好呀')
check('上屏顺序和到达顺序一致', plain.shown, '你好呀')
check('认得出这是流式', plain.streamed, true)

// 2. `data:` 行被网络切成两半（最常见的一种）
const split = feed([
  'data: {"choi',
  'ces":[{"delta":{"content":"分段"}}]}\n\nda',
  'ta: {"choices":[{"delta":{"content":"也拼得回"},"finish_reason":"stop"}]}\n\n',
  'data: [DONE]\n\n',
])
check('半行能拼回来', split.content, '分段也拼得回')
check('finish_reason 带得回来', split.finishReason, 'stop')

// 3. `[DONE]` 和最后一帧挤在同一次到达里
const tail = feed([frame({ choices: [{ delta: { content: '结尾' } }] }) + 'data: [DONE]\n\n'])
check('[DONE] 不吞掉同一批里的最后一帧', tail.content, '结尾')

// 4. 注释行、event 行、空 data 都不该产出内容
const noisy = feed([
  ': ping\n\n',
  'event: message\ndata: {"choices":[{"delta":{"content":"只有这句"}}]}\n\n',
  'data: \n\n',
  'data: [DONE]\n\n',
])
check('SSE 的注释/事件行被丢掉', noisy.content, '只有这句')

// 5. 坏帧不带走整条回答
const broken = feed([
  frame({ choices: [{ delta: { content: '前半' } }] }),
  'data: {这行不是 JSON}\n\n',
  frame({ choices: [{ delta: { content: '后半' } }] }),
])
check('坏帧只丢那一帧', broken.content, '前半后半')

// 6. 推理链（各家字段名不同，这里认 reasoning_content）
const thought = feed([
  frame({ choices: [{ delta: { reasoning_content: '先想想' } }] }),
  frame({ choices: [{ delta: { reasoning_content: '再想想', content: '答案' } }] }),
])
check('思考链单独累加', thought.reasoning, '先想想再想想')
check('正文不被思考链污染', thought.content, '答案')

// 7. tool_calls 的名字和参数都是一帧一帧漏出来的
const tool = feed([
  frame({
    choices: [
      {
        delta: {
          tool_calls: [{ index: 0, id: 'call_1', function: { name: 'set_', arguments: '{"pe' } }],
        },
      },
    ],
  }),
  frame({ choices: [{ delta: { tool_calls: [{ index: 0, function: { name: 'volume', arguments: 'rcent":4' } }] } }] }),
  frame({ choices: [{ delta: { tool_calls: [{ index: 0, function: { arguments: '0}' } }] }, finish_reason: 'tool_calls' }] }),
])
check('工具名跨帧拼回来', tool.toolCalls[0]?.name, 'set_volume')
check('参数按到达顺序拼成完整 JSON', tool.toolCalls[0]?.arguments, '{"percent":40}')
check('id 只在第一帧也有', tool.toolCalls[0]?.id, 'call_1')

// 8. 两个工具并行时按 index 归位，不按到达顺序
const two = feed([
  frame({ choices: [{ delta: { tool_calls: [{ index: 1, id: 'b', function: { name: 'torch', arguments: '{}' } }] } }] }),
  frame({ choices: [{ delta: { tool_calls: [{ index: 0, id: 'a', function: { name: 'battery', arguments: '{}' } }] } }] }),
])
check('按 index 排序而不是到达顺序', two.toolCalls.map((row) => row.name), ['battery', 'torch'])

// 9. 端点被要求 stream 却整块返回 JSON —— 不能显示空白
const whole = feed([JSON.stringify({ choices: [{ message: { content: '整块返回' } }] })])
check('非流式回答兜得住', whole.content, '整块返回')
check('这种要标成没流式', whole.streamed, false)

// 10. 空回答不抛异常
const nothing = feed(['data: [DONE]\n\n'])
check('什么都没有时是空串而不是崩', nothing.content, '')

console.log(`\n${passed} 条通过，${failed} 条不过`)
process.exit(failed ? 1 : 0)

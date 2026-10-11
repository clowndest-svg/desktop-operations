/**
 * 设置页校验规则的单元测试：直接在 Node 里跑 `frontend/src/mobile/settings.ts`。
 *
 * 为什么值得单独跑：这几条规则拦住的都是**屏幕上看不出区别**的错——
 * Base URL 少写一层 `/v1` 得到 404，多写一层得到 404，Key 打错一个字母也是 404。
 * 装机试的话只会得到一句"连不上模型"，然后开始一栏一栏地猜。
 *
 * 跑法：node --experimental-strip-types scripts/test_settings.mjs
 */

import { checkModel, checkPort, explainStatus, normalizePin } from '../frontend/src/mobile/settings.ts'

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

/** 只挑出有哪几栏不合格，不比对文案（文案改一个字不该让测试红）。 */
function fieldsOf(input) {
  return checkModel(input).map((issue) => issue.field)
}

const GOOD = {
  baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  model: 'qwen-plus',
  pin: '',
  apiKey: 'sk-abc',
}

// 1. 一份合法配置不该被挑出任何毛病
check('合法配置零问题', fieldsOf(GOOD), [])

// 2. 自己机器上的 Ollama 不要 Key：Key 空着也必须能存
check('没有 Key 不算错（自建端点）', fieldsOf({ ...GOOD, apiKey: '' }), [])

// 3. 地址空着
check('地址空着要拦', fieldsOf({ ...GOOD, baseUrl: '  ' }), ['baseUrl'])

// 4. 少了协议
check('地址没协议要拦', fieldsOf({ ...GOOD, baseUrl: 'dashscope.aliyuncs.com/v1' }), ['baseUrl'])

// 5. 复制的时候带进一个空格
check('地址里有空格要拦', fieldsOf({ ...GOOD, baseUrl: 'https://a.com/ v1' }), ['baseUrl'])

// 6. 把整条端点抄进来了：拼出来会变成 .../chat/completions/chat/completions，
//    而这个 404 和"地址错一层"在屏幕上长得一模一样
check(
  '地址写到 /chat/completions 要拦',
  fieldsOf({ ...GOOD, baseUrl: 'https://a.com/v1/chat/completions' }),
  ['baseUrl'],
)

// 7. 尾巴带 query
check('地址带 ? 要拦', fieldsOf({ ...GOOD, baseUrl: 'https://a.com/v1?key=1' }), ['baseUrl'])

// 8. 模型名空着
check('模型名空着要拦', fieldsOf({ ...GOOD, model: '' }), ['model'])

// 9. 指纹：完整格式
check('完整指纹算合法', fieldsOf({ ...GOOD, pin: `sha256:${'a'.repeat(64)}` }), [])
check('指纹大小写归一', normalizePin(`SHA256:${'A'.repeat(64)}`), `sha256:${'a'.repeat(64)}`)

// 10. 指纹：从 openssl 输出里复制出来的那种，没有前缀
check('裸 64 位补上前缀', normalizePin('B'.repeat(64)), `sha256:${'b'.repeat(64)}`)

// 11. 指纹：留空是合法的（= 只认正规 CA）
check('指纹留空合法', normalizePin(''), '')

// 12. 指纹：少一位就是另一串证书，必须拦
check('63 位不算指纹', normalizePin('a'.repeat(63)), null)
check('65 位不算指纹', normalizePin('a'.repeat(65)), null)
check('指纹不合法要拦', fieldsOf({ ...GOOD, pin: 'a'.repeat(63) }), ['pin'])

// 13. 指纹：不是十六进制
check('带非十六进制字符不算指纹', normalizePin(`sha256:${'z'.repeat(64)}`), null)

// 14. 一次能报出多栏的毛病（不是修一栏才知道下一栏也错）
check(
  '两栏都错就报两条',
  fieldsOf({ baseUrl: 'a.com', model: '', pin: '' }),
  ['baseUrl', 'model'],
)

// 15. 端口
check('端口 8737 合法', checkPort(8737, 8737), null)
check('端口字符串合法', checkPort('8737', 8737), null)
check('端口留空按默认算', checkPort('', 8737), null)
check('端口 0 不合法', checkPort(0, 8737)?.field, 'port')
check('端口 65536 不合法', checkPort(65536, 8737)?.field, 'port')
check('端口不是整数不合法', checkPort('8737.5', 8737)?.field, 'port')
check('端口是文字不合法', checkPort('八七三七', 8737)?.field, 'port')

// 16. 三种失败要能分开说
check('401 说的是 Key', explainStatus(401, '').includes('Key'), true)
check('404 说的是地址或模型名', explainStatus(404, '').includes('404'), true)
check('422 说的是请求被拒', explainStatus(422, '').includes('422'), true)
check('429 说的是限流', explainStatus(429, '').includes('限流'), true)
check('别的状态码也要带上号码', explainStatus(503, '').includes('503'), true)

// 17. 端点回的原话要带出来，但不能整页糊上去
check('带上原话', explainStatus(404, '{"error":"model not found"}').includes('model not found'), true)
check('换行压成一行', explainStatus(500, 'a\n\nb').includes('\n'), false)
check('长正文截断', explainStatus(500, 'x'.repeat(4000)).length < 260, true)

console.log(`\n${passed} 条通过，${failed} 条不过`)
process.exit(failed ? 1 : 0)

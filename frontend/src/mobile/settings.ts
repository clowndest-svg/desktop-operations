/**
 * 设置页那几栏的校验规则。
 *
 * 这个文件**刻意不 import 任何插件**：判断全是纯字符串的活，放在能被 Node 直接跑的
 * 模块里才有办法在没手机的情况下判死它（`scripts/test_settings.mjs`）。放在 .vue 里的
 * 校验只能靠装机试出来，而"填了个连不上的地址、保存还说已保存"正是最难查的那类。
 *
 * 规则本身的取向：**拦得住的一定拦，拦不住的老实说**。比如指纹填错一位，
 * 存下去的后果是"连不上"而不是"随便谁都信"，所以这一栏可以宽松；
 * 而地址少写一层 `/v1` 会得到一个 404，用户在屏幕上看不出区别，所以要在保存前就说。
 */

export type FieldName = 'baseUrl' | 'model' | 'pin' | 'port'

export interface FieldIssue {
  field: FieldName
  text: string
}

const PIN_FULL = /^sha256:([0-9a-fA-F]{64})$/i
const PIN_BARE = /^([0-9a-fA-F]{64})$/

/**
 * 把指纹归一化成 `sha256:<64 位小写十六进制>`。
 *
 * 也认不带前缀的裸 64 位——那串多半是从 `openssl x509 -fingerprint` 的输出里复制的，
 * 逼人自己补前缀只会多一种填错的方式。返回 `null` 表示这一栏填得不像指纹。
 */
export function normalizePin(raw: string): string | null {
  const value = (raw ?? '').trim()
  if (!value) return ''
  const full = PIN_FULL.exec(value)
  if (full) return `sha256:${full[1].toLowerCase()}`
  const bare = PIN_BARE.exec(value)
  if (bare) return `sha256:${bare[1].toLowerCase()}`
  return null
}

export interface ModelDraft {
  baseUrl: string
  model: string
  pin: string
}

/** 逐栏挑毛病。返回空表就是可以存。 */
export function checkModel(input: ModelDraft): FieldIssue[] {
  const issues: FieldIssue[] = []
  const base = (input.baseUrl ?? '').trim()
  if (!base) {
    issues.push({ field: 'baseUrl', text: '地址不能空着：她要靠这个地址找到模型' })
  } else if (!/^https:\/\//i.test(base)) {
    issues.push({ field: 'baseUrl', text: '手机版只连接 https:// 地址；自建服务请启用 TLS 并填写指纹，或使用电脑遥控模式' })
  } else if (/\s/.test(base)) {
    issues.push({ field: 'baseUrl', text: '地址里有空格，多半是复制的时候带进来的' })
  } else if (/[?#]/.test(base)) {
    issues.push({ field: 'baseUrl', text: '地址写到 /v1 这一层为止，后面的 ? 和 # 都不要带' })
  } else if (/\/chat\/completions$/i.test(base)) {
    // 端点是这一层自己拼的，用户把整条抄进来就会变成 .../chat/completions/chat/completions。
    issues.push({ field: 'baseUrl', text: '不用写到 /chat/completions，这一层自己会加' })
  }
  if (base && !issues.some(issue => issue.field === 'baseUrl')) {
    try {
      const url = new URL(base)
      if (!url.hostname || url.username || url.password) throw new Error('invalid endpoint')
    } catch {
      issues.push({ field: 'baseUrl', text: '地址不是完整网址，或夹带了用户名/密码；Key 请填到单独一栏' })
    }
  }
  if (!(input.model ?? '').trim()) {
    issues.push({ field: 'model', text: '模型名不能空着：端点要靠它挑模型' })
  }
  if (normalizePin(input.pin ?? '') === null) {
    issues.push({
      field: 'pin',
      text: '指纹要长成 sha256: 加 64 位十六进制。不填就是只认正规 CA 签发的地址',
    })
  }
  return issues
}

/** 端口。空着按默认算，别的都得是 1 到 65535 的整数。 */
export function checkPort(raw: number | string, fallback: number): FieldIssue | null {
  const text = String(raw ?? '').trim()
  if (!text) return null
  const port = Number(text)
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    return { field: 'port', text: `端口是 1 到 65535 之间的整数（默认 ${fallback}）` }
  }
  return null
}

/**
 * 把一次探测的结果翻成人话。
 *
 * 404 / 401 / 422 这三种失败在屏幕上长得一模一样（都是"连不上模型"），
 * 而修法完全不同：地址错一层、Key 不对、模型名不存在。分不开的话，
 * 用户只能一栏一栏地试。
 */
export function explainStatus(status: number, body: string): string {
  const detail = (body ?? '').replace(/\s+/g, ' ').trim().slice(0, 160)
  const tail = detail ? `（原话：${detail}）` : ''
  switch (status) {
    case 401:
    case 403:
      return `Key 不对，或者这个 Key 没有用那个模型的权限（${status}）${tail}`
    case 404:
      return `地址上没有这个东西（404）：要么 Base URL 多写/少写了一层，要么模型名不存在${tail}`
    case 422:
      return `地址对了，请求被拒（422）：多半是模型名不对，或者这个端点不收 max_tokens${tail}`
    case 429:
      return `被限流了（429）：等一会儿再试${tail}`
    default:
      return `模型返回 ${status}${tail}`
  }
}


/** No dummy bearer token: key-free compatible servers receive no Authorization header. */
export function modelHeaders(apiKey: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const key = (apiKey ?? '').trim()
  if (key) headers.Authorization = `Bearer ${key}`
  return headers
}

export function modelProbeResult(status: number, body: string): { ok: boolean; text: string } {
  if (status < 200 || status >= 300) return { ok: false, text: explainStatus(status, body) }
  try {
    const parsed = JSON.parse(body)
    const message = parsed?.choices?.[0]?.message
    if (!parsed.error && message && typeof message === 'object' &&
        (typeof message.content === 'string' || typeof message.reasoning_content === 'string' ||
         message.role === 'assistant' || Array.isArray(message.tool_calls))) {
      return { ok: true, text: `通了（${status}），收到有效的模型响应` }
    }
  } catch { /* A login page or reverse-proxy HTML is not an LLM response. */ }
  return { ok: false, text: `地址返回 ${status}，但不是有效的聊天模型响应；请检查 Base URL 和网关` }
}

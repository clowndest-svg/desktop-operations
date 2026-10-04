/**
 * 手机侧的三条外路：自己的模型 key、这台电脑、以及本地历史记录。
 *
 * 为什么模型请求也走原生层而不是 `fetch`：页面装在 `https://localhost` 这个源里，
 * 而绝大多数 OpenAI 兼容端点不发 CORS 头，`fetch` 会在**预检**阶段就死掉，
 * 报回来的还是一句没有信息量的 "Failed to fetch"。`XyNet` 把请求交给 Kotlin 发，
 * 没有跨域这一关，状态码和响应片段也能带回来。
 *
 * 电脑端点是**自签证书**，系统信任链不会认。所以那条路多带一个 `pin`：
 * 由 Kotlin 把对端证书的 SHA-256 和配对时人在电脑上念给你那串比一下——
 * 指纹固定替代 CA，而不是把校验关掉。
 */
import { Preferences } from '@capacitor/preferences'
import { Capacitor, registerPlugin, type PluginListenerHandle } from '@capacitor/core'
import { createAccumulator, type Delta } from './sse'
import type { ActionRow } from './types'

const MODEL_KEY = 'xiaoye.model'
const HISTORY_KEY = 'xiaoye.history'
const PC_KEY = 'xiaoye.pc'
const VOICE_KEY = 'xiaoye.voice'
const LEDGER_KEY = 'xiaoye.actions'
const HISTORY_CAP = 60
const LEDGER_CAP = 40

/**
 * 一张图的 data URL 最多多少字符才发得出去。
 *
 * 电脑侧 `MAX_IMAGE_DATA_CHARS` 是 400_000，这里留 20K 余量给 JSON 别的字段。
 * 拍照和翻相册**共用这一个数**：分两处写的话迟早只有一条路守得住，
 * 另一条在遥控模式下吃 413，而屏幕上只写得下"她没看见这张图"。
 */
export const MAX_DATA_CHARS = 380_000

export interface ModelConfig {
  baseUrl: string
  apiKey: string
  model: string
  /**
   * 自建/内网模型的证书指纹（`sha256:...`），留空就是走系统信任链。
   *
   * 为什么要有这一栏：手机打模型那条路默认只认正经 CA 签发的证书，
   * 于是**公司内网的网关、自己机器上的 Ollama/vLLM 全都连不上**——
   * 报回来的还是一句没有信息量的证书错误。这一栏不是给普通用户准备的，
   * 是给"我有一台自己的模型服务器"的人的出口，和配对时那栏同理：
   * **指纹替代 CA，而不是关掉校验**。填错了连不上，不会退化成不校验。
   */
  pin: string
}

export interface Turn {
  role: 'user' | 'assistant'
  text: string
  /**
   * 这一轮发生的时间（毫秒）。老记录里没有这个字段，所以是可选的：
   * 缺了就不显示时间，而不是拿 `Date.now()` 补一个假的时间戳 —— 屏幕上写着
   * "03:12"，实际是三个月前说的话，那种错误没人会去核对。
   */
  at?: number
}

/** 一张随问题一起发出去的图片。`data` 是 data URL，和桌面那侧同一个形状。 */
export interface Attachment {
  name: string
  kind: 'image'
  mime: string
  data: string
}

export interface PcLink {
  host: string
  port: number
  pin: string
  token: string
  name: string
  methods: string[]
}

export interface NetReply {
  status: number
  body: string
  error: string
}

export interface NetRequest {
  url: string
  method: string
  body?: string
  headers?: Record<string, string>
  /** `sha256:<hex>`：这串来自电脑屏幕，不是来自这次连接。 */
  pin?: string
  timeoutMs?: number
}

/** 原生层流式吐回来的每一帧。`kind` 之外没有别的约定：文本原样带，不解析。 */
export interface NetEvent {
  kind: 'chunk' | 'end'
  text?: string
  stopped?: boolean
  error?: string
}

interface XyNetPlugin {
  request(options: NetRequest): Promise<NetReply>
  /** 同 request，但正文一段一段用 `xyNet` 事件吐出来，最后 resolve 状态码。 */
  stream(options: NetRequest): Promise<NetReply>
  cancelStream(): Promise<{ ok: boolean }>
  addListener(eventName: 'xyNet', listener: (event: NetEvent) => void): Promise<PluginListenerHandle>
}

/**
 * Web 降级：真的用 `fetch` 发出去，让浏览器自己去撞证书校验那面墙。
 * 撞墙报错比假装成功有用——在桌面浏览器里调试手机页面时，这条错误就是答案本身。
 *
 * `stream` 在浏览器里**不做流式**，直接回一句"这条只有真机有"：
 * 调试页上看到"没有逐字"和看到"逐字坏了"是两件不同的事，含糊不得。
 */
const XyNet = registerPlugin<XyNetPlugin>('XyNet', {
  web: {
    async request(options: NetRequest) {
      const { url, method, body, headers } = options
      try {
        const response = await fetch(url, {
          method,
          body,
          headers: new Headers(headers ?? {}),
        })
        return { status: response.status, body: await response.text(), error: '' }
      } catch (err) {
        return { status: 0, body: '', error: err instanceof Error ? err.message : String(err) }
      }
    },
    async stream() {
      return { status: 0, body: '', error: '浏览器里不做流式：这条只有真机有' }
    },
    async cancelStream() {
      return { ok: false }
    },
    async addListener() {
      // 浏览器这条路不做流式，所以也没有可听的：给一个空句柄，
      // 让 `finally { handle.remove() }` 那段代码不用为调试页特判。
      return { eventId: 'none', remove: async () => {} } as PluginListenerHandle
    },
  },
})

/** 这一版 App 的版本。必须和 `android/app/build.gradle` 的 versionName 一致，
 * 由 `tests/test_mobile_frontend.py` 盯着：手机上出问题第一句就是"你装的是哪一版"，
 * 而 APK 的图标底下不写这个。 */
export const APP_VERSION = '0.4.0'

export const DEFAULT_MODEL: ModelConfig = {
  baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  apiKey: '',
  model: 'qwen-plus',
  pin: '',
}

export async function loadModel(): Promise<ModelConfig> {
  const { value } = await Preferences.get({ key: MODEL_KEY })
  if (!value) return { ...DEFAULT_MODEL }
  try {
    return { ...DEFAULT_MODEL, ...(JSON.parse(value) as Partial<ModelConfig>) }
  } catch {
    return { ...DEFAULT_MODEL }
  }
}

export async function saveModel(next: ModelConfig): Promise<void> {
  await Preferences.set({ key: MODEL_KEY, value: JSON.stringify(next) })
}

export async function loadHistory(): Promise<Turn[]> {
  const { value } = await Preferences.get({ key: HISTORY_KEY })
  if (!value) return []
  try {
    const rows = JSON.parse(value)
    return Array.isArray(rows) ? (rows as Turn[]).slice(-HISTORY_CAP) : []
  } catch {
    return []
  }
}

export async function appendHistory(turns: Turn[]): Promise<void> {
  await Preferences.set({
    key: HISTORY_KEY,
    value: JSON.stringify(turns.slice(-HISTORY_CAP)),
  })
}

export interface VoiceFlags {
  speakBack: boolean
  handsFree: boolean
}

/**
 * 朗读 / 免提这两个开关。
 *
 * 免提记的是"上次用户开过"，不是"下次自己开麦"：它只在**她答完话之后**接着听一轮，
 * 启动 App、亮屏、后台回来都不碰麦克风。这条区别是免提功能能不能见人的一部分。
 */
export async function loadVoiceFlags(): Promise<VoiceFlags> {
  const { value } = await Preferences.get({ key: VOICE_KEY })
  if (!value) return { speakBack: true, handsFree: false }
  try {
    const parsed = JSON.parse(value) as Partial<VoiceFlags>
    return { speakBack: parsed.speakBack ?? true, handsFree: parsed.handsFree ?? false }
  } catch {
    return { speakBack: true, handsFree: false }
  }
}

export async function saveVoiceFlags(flags: VoiceFlags): Promise<void> {
  await Preferences.set({ key: VOICE_KEY, value: JSON.stringify(flags) })
}

/**
 * 她在手机上做过什么。存在这台手机上，跟着 App 走。
 *
 * 为什么手机上也留一本：电脑那本记的是**电脑上**她做过的事，管不到这台设备的
 * 手电筒和闹钟。而"她刚才是不是动了我的手机"这个问题，必须有一个地方能答。
 */
export async function loadLedger(): Promise<ActionRow[]> {
  const { value } = await Preferences.get({ key: LEDGER_KEY })
  if (!value) return []
  try {
    const rows = JSON.parse(value)
    return Array.isArray(rows) ? (rows as ActionRow[]).slice(0, LEDGER_CAP) : []
  } catch {
    return []
  }
}

export async function appendLedger(rows: ActionRow[]): Promise<void> {
  await Preferences.set({ key: LEDGER_KEY, value: JSON.stringify(rows.slice(0, LEDGER_CAP)) })
}

/**
 * 独立模式的身份说明。
 *
 * 两句都要留着：一句是**她不能做什么**（碰电脑），一句是**她能做什么**（这台手机自己）。
 * 只写前一句的话，模型会照着"只有对话能力"去拒绝真实的手机动作，那个功能就等于没做；
 * 只写后一句的话，她会开始答应操作电脑。两边都不是我们要的。
 */
export const MOBILE_SYSTEM = [
  '你是「小夜」，运行在手机上的语音助手，说话简短、直接、口语化，别用 Markdown 标题和表格。',
  '现在这台是**手机版（独立模式）**。',
  '你不能操作任何电脑，不能读写电脑文件，不能清理磁盘，不能执行电脑上的命令，也不知道用户电脑上发生了什么。',
  '这类要求直接说明"这需要连上开着『手机接入』的电脑"，不要假装自己做了，也不要编造结果。',
  '但这台手机本身她能动：定闹钟、调音量、开手电筒、拨号、写短信草稿、加日程、开应用和设置页。',
  '要做这些事就**调工具**，别只用嘴说"我帮你开了"；工具回什么你就照实说什么。',
  '需要人点头的动作，系统会先问一句，人没点你就说没做——这条不许绕。',
].join('\n')

function endpoint(baseUrl: string): string {
  const trimmed = baseUrl.replace(/\/+$/, '')
  return `${trimmed}/chat/completions`
}

/** 一轮模型回答：正文，或者它想调的那几个工具。 */
export interface AssistantTurn {
  content: string
  toolCalls: { id: string; name: string; arguments: string }[]
  /** 人按了"不听了"。工具环看到这一条就该收住，别拿半截参数去动手机。 */
  stopped: boolean
}

export interface ChatMessage {
  role: string
  content: unknown
  tool_calls?: unknown
  tool_call_id?: string
}

/**
 * 发一次对话请求，拿回**没加工过**的那一条回答。
 *
 * 工具环要的是 `tool_calls`，而 `askModel` 只回正文——把两件事塞进一个函数里，
 * 迟早出现"工具调了但答案被吞了"。所以这里只负责发和收，判断留给上层。
 */
export interface ChatOptions {
  /** 给了就流式（真机上）：每来一段就调一次，界面直接往气泡里追加。 */
  onDelta?: (delta: Delta) => void
}

/** 中途不听了。返回的是"连接断没断"，不是"她想没想完"——见 `stopped` 那条注释。 */
export async function cancelChat(): Promise<boolean> {
  if (!Capacitor.isNativePlatform()) return false
  stopRequested = true
  const reply = await XyNet.cancelStream()
  return Boolean(reply.ok)
}

/**
 * 这一趟流式有没有被人叫停。
 *
 * 为什么放在模块里而不是从响应里读：连接是被我们这边掐掉的，
 * 服务器不会留一个"这是客户端取消的"标记，Java 层回来的只是"读完了、没错误"。
 * 一次只跑一条流（这 App 就这样），所以一个布尔够用。
 */
let stopRequested = false

function failureOf(reply: NetReply, messages: ChatMessage[]): Error {
  const detail = `模型返回 ${reply.status}：${reply.body.slice(0, 200)}`
  const hasImage = JSON.stringify(messages).includes('image_url')
  // 带图的问题撞上纯文本模型，回的就是这么一句没头没尾的 4xx。
  // 不点出来的话，用户看到的是"拍照这个功能是假的"，而真相是模型选错了。
  return new Error(hasImage ? `${detail}\n（这个问题带着一张图：换一个能看图的模型再问）` : detail)
}

/** 流式那一趟：逐帧喂累加器，最后交回一条完整回答。 */
async function streamed(
  body: Record<string, unknown>,
  headers: Record<string, string>,
  url: string,
  pin: string,
  messages: ChatMessage[],
  onDelta: (delta: Delta) => void,
): Promise<AssistantTurn> {
  const acc = createAccumulator()
  stopRequested = false
  const handle = await XyNet.addListener('xyNet', (event) => {
    if (event.kind !== 'chunk') return
    const delta = acc.push(event.text ?? '')
    if (delta.content || delta.reasoning) onDelta(delta)
  })
  let reply: NetReply
  try {
    reply = await XyNet.stream({ url, method: 'POST', headers, body: JSON.stringify(body), pin })
  } finally {
    await handle.remove()
  }
  if (reply.error) throw new Error(`连不上模型：${reply.error}`)
  if (reply.status < 200 || reply.status >= 300) throw failureOf(reply, messages)
  const done = acc.finish()
  return { content: done.content, toolCalls: done.toolCalls, stopped: stopRequested }
}

export async function rawChat(
  messages: ChatMessage[],
  cfg: ModelConfig,
  tools: object[] = [],
  options: ChatOptions = {},
): Promise<AssistantTurn> {
  if (!cfg.apiKey) throw new Error('还没填模型 Key：点右上角「设置」填一次就好')
  const body: Record<string, unknown> = {
    model: cfg.model,
    messages: [{ role: 'system', content: MOBILE_SYSTEM }, ...messages],
    stream: false,
  }
  if (tools.length) body.tools = tools
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${cfg.apiKey}`,
  }
  const url = endpoint(cfg.baseUrl)
  // 老配置里没有 `pin` 这一栏（存过 Preferences 的手机上会是 undefined），
  // 合并默认值也只在读的时候生效，这里再兜一次，免得给原生层传个 undefined 进去。
  const pin = (cfg.pin ?? '').trim().toLowerCase()
  // 只有**真机 + 有人要看增量**才走流式。浏览器里那条路直接回一句"不做流式"，
  // 所以这里宁可退回整块返回，也不要在调试页上显示一个永远不动的气泡。
  if (options.onDelta && Capacitor.isNativePlatform()) {
    return streamed({ ...body, stream: true }, headers, url, pin, messages, options.onDelta)
  }
  const reply = await XyNet.request({
    url,
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    pin,
  })
  if (reply.error) throw new Error(`连不上模型：${reply.error}`)
  if (reply.status < 200 || reply.status >= 300) throw failureOf(reply, messages)
  let parsed: Record<string, unknown>
  try {
    parsed = JSON.parse(reply.body) as Record<string, unknown>
  } catch {
    throw new Error(`模型返回的不是 JSON：${reply.body.slice(0, 160)}`)
  }
  const choices = parsed.choices as { message?: { content?: string; tool_calls?: unknown } }[] | undefined
  const message = choices?.[0]?.message ?? {}
  const calls = Array.isArray(message.tool_calls) ? message.tool_calls : []
  return {
    content: typeof message.content === 'string' ? message.content : '',
    toolCalls: calls.map((item, index) => {
      const cast = item as { id?: string; function?: { name?: string; arguments?: string } }
      return {
        id: cast.id ?? `call-${index}`,
        name: cast.function?.name ?? '',
        arguments: cast.function?.arguments ?? '{}',
      }
    }),
    stopped: false,
  }
}

// -- 电脑（遥控模式） ------------------------------------------------------

export async function loadPc(): Promise<PcLink | null> {
  const { value } = await Preferences.get({ key: PC_KEY })
  if (!value) return null
  try {
    return JSON.parse(value) as PcLink
  } catch {
    return null
  }
}

export async function savePc(link: PcLink | null): Promise<void> {
  if (link === null) {
    await Preferences.remove({ key: PC_KEY })
    return
  }
  await Preferences.set({ key: PC_KEY, value: JSON.stringify(link) })
}

function pcUrl(link: PcLink, path: string): string {
  return `https://${link.host}:${link.port}${path}`
}

async function pcRequest(link: PcLink, method: string, path: string, body?: object): Promise<NetReply> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (link.token) headers.Authorization = `Bearer ${link.token}`
  return XyNet.request({
    url: pcUrl(link, path),
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    pin: link.pin,
  })
}

async function jsonOf<T>(reply: NetReply): Promise<T> {
  if (reply.error) throw new Error(`电脑没答上：${reply.error}`)
  let parsed: unknown
  try {
    parsed = JSON.parse(reply.body || '{}')
  } catch {
    throw new Error(`电脑返回的不是 JSON（HTTP ${reply.status}）`)
  }
  return parsed as T
}

export interface HelloReply {
  name: string
  version: string
  pairing_open: boolean
}

/**
 * 第一跳：拿到这台机器的名字，顺便确认地址、端口和指纹写得对。
 *
 * 这一步**也**要带指纹。上一版没带，于是"先探一下"对着自签证书走系统信任链，
 * 在手机上永远回一句 `Trust anchor for certification path not found` ——
 * 一个永远失败的按钮比没有这个按钮更糟（装机第一次点就发现了）。
 */
export async function pcHello(host: string, port: number, pin: string): Promise<HelloReply> {
  const clean = pin.trim().toLowerCase()
  if (!clean) throw new Error('先把电脑上那串证书指纹填进来：自签证书没有 CA 可验，没指纹连不上')
  const reply = await XyNet.request({ url: `https://${host}:${port}/hello`, method: 'GET', pin: clean })
  const parsed = await jsonOf<HelloReply>(reply)
  if (!parsed.name) throw new Error('对面不像是小夜')
  return parsed
}

/**
 * 用 6 位配对码换 token。
 *
 * 指纹在这一步**第一次**建立：人必须在电脑上看着那串 `sha256:...` 念给手机，
 * 所以填码的表单里"指纹"是一个必填框，不是一个自动带回来的字段。
 * 自动带回来就没有意义了——攻击者本来就能改回答。
 */
export async function pcPair(input: {
  host: string
  port: number
  pin: string
  code: string
  device: string
}): Promise<PcLink> {
  const seed: PcLink = {
    host: input.host,
    port: input.port,
    pin: input.pin.trim().toLowerCase(),
    token: '',
    name: '',
    methods: [],
  }
  const reply = await pcRequest(seed, 'POST', '/pair', { code: input.code, device: input.device })
  if (reply.status === 403) throw new Error('配对码不对、已过期或已经用过了')
  const parsed = await jsonOf<{
    paired?: boolean
    token?: string
    name?: string
    methods?: string[]
    error?: string
  }>(reply)
  if (!parsed.paired || !parsed.token) throw new Error(parsed.error || '配对失败')
  return {
    host: input.host,
    port: input.port,
    pin: seed.pin,
    token: parsed.token,
    name: parsed.name || '小夜',
    methods: parsed.methods ?? [],
  }
}

export async function pcCall<T>(
  link: PcLink,
  method: string,
  args: Record<string, unknown> = {},
): Promise<T> {
  const reply = await pcRequest(link, 'POST', `/rpc/${method}`, args)
  const parsed = await jsonOf<T>(reply)
  if (reply.status === 401) {
    // 电脑端撤销过这台设备：留着 token 只会让每一次请求都失败，界面上要说清是哪一边动的手。
    throw new Error('这台设备已被电脑撤销，需要重新配对')
  }
  return parsed
}

// -- 通话与音色（电脑那边的嗓子） -------------------------------------------

/** 电脑上一个可选音色。`kind` 区分内置和录制的，`engine` 说它归哪个引擎。 */
export interface VoiceChoice {
  id: string
  label: string
  kind: 'builtin' | 'clone'
  engine: string
  current: boolean
  name?: string
  duration_ms?: number
}

/** 录音的接受范围。这些数是**电脑那边量出来的**，不是这里估的。 */
export interface CloneRules {
  available: boolean
  reason: string
  min_ms: number
  comfortable_ms: number
  max_ms: number
  max_voices: number
}

export interface VoiceBoard {
  error: string
  engine: string
  current: string
  choices: VoiceChoice[]
  cloning: CloneRules
  speed: number
  volume: number
  speed_min: number
  speed_max: number
  volume_min: number
  volume_max: number
}

/** 一轮通话的结果。成功和失败是**同一个形状**，少一个键和空一个键在屏幕上长得一样。 */
export interface CallReply {
  error: string
  heard: string
  answer: string
  spoken: string
  sample_rate: number
  truncated: boolean
  /** base64 的 s16le PCM；合成失败时是空串，但 `answer` 里的话还在。 */
  audio: string
}

export interface CloneBoard {
  error: string
  voices: VoiceChoice[]
  cloning: CloneRules
}

export function pcVoices(link: PcLink): Promise<VoiceBoard> {
  return pcCall<VoiceBoard>(link, 'tts_voices')
}

/** 换音色。**下一次说话**生效，不用重启。 */
export function pcPickVoice(link: PcLink, voice: string): Promise<VoiceBoard> {
  return pcCall<VoiceBoard>(link, 'tts_pick', { voice })
}

/**
 * 试听一段。拿回来的是字节，手机自己播 ——
 * 电脑那边没有"从手机扬声器出声"的能力，那头播的话是从电脑的喇叭出来。
 */
export function pcPreviewVoice(
  link: PcLink,
  voice: string,
): Promise<{ ok: boolean; error: string; voice: string; pcm?: string; sample_rate?: number }> {
  // 走 `tts_preview_pcm` 而不是 `tts_preview`：后者把音频推到**电脑**的扬声器，
  // 而用户正拿着手机 —— 从手机按下试听却在电脑上响，是一个说不通的按钮。
  return pcCall(link, 'tts_preview_pcm', { voice, text: PREVIEW_LINE })
}

/**
 * 试听用的那句话。
 *
 * 长度是有讲究的：太短听不出音色的差别（"你好"两个字，谁念都差不多），
 * 太长每点一次要等好几秒。这一句包含开口音、闭口音和一个问句语调。
 */
export const PREVIEW_LINE = '你好，我是小夜。这一段就是选中的声音读出来的样子。'

export function pcCloneVoices(link: PcLink): Promise<CloneBoard> {
  return pcCall<CloneBoard>(link, 'voice_clone_list')
}

/** 存一段录音当音色。`pcm` 是 base64，格式必须是 16kHz 单声道 s16le。 */
export function pcAddClone(
  link: PcLink,
  input: { name: string; promptText: string; pcm: string; sampleRate: number },
): Promise<CloneBoard> {
  return pcCall<CloneBoard>(link, 'voice_clone_add', {
    name: input.name,
    prompt_text: input.promptText,
    pcm: input.pcm,
    sample_rate: input.sampleRate,
  })
}

export function pcRemoveClone(link: PcLink, voiceId: string): Promise<CloneBoard> {
  return pcCall<CloneBoard>(link, 'voice_clone_remove', { voice_id: voiceId })
}

/** 通话开始前问一次：她能不能听见、能不能说话。 */
export interface CallReadiness {
  error: string
  ready: boolean
  asr: string
  tts: string
  asr_loaded: boolean
  asr_error: string
  chat: boolean
  cloning: boolean
}

export function pcCallReadiness(link: PcLink): Promise<CallReadiness> {
  return pcCall<CallReadiness>(link, 'call_readiness')
}

/**
 * 让她现在就开始加载耳朵。
 *
 * 识别模型有几百兆，第一次要几十秒。通话界面一打开就调这个，让等待落在
 * 用户还在读屏幕的时候，而不是落在他已经说完第一句话之后。
 */
export function pcCallWarmup(link: PcLink): Promise<CallReadiness> {
  return pcCall<CallReadiness>(link, 'call_warmup')
}

/** 一轮语音对话：把一段 16kHz 的原声交给电脑上的她。 */
export function pcCallTurn(link: PcLink, pcm: string, sampleRate: number): Promise<CallReply> {
  return pcCall<CallReply>(link, 'call_turn', { pcm, sample_rate: sampleRate })
}

/** 把屏幕上已有的一句话念出来，**不**再问一遍智能体。 */
export function pcCallSpeak(link: PcLink, text: string): Promise<CallReply> {
  return pcCall<CallReply>(link, 'call_speak', { text })
}

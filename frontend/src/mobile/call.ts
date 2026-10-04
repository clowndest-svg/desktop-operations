/**
 * 手机当"电话听筒"：录一段 16kHz 的话，播一段电脑合成好的 PCM。
 *
 * 为什么这条路不经过原生插件：`XySpeech` 走的是 Android 的**系统识别**，
 * 它给回来的是一段文字，中间那段音频 JS 拿不到 —— 而通话要的是"把用户的原声交给
 * 电脑上的她"，让她用自己的耳朵（SenseVoice）听。这条链路上任何一环换成系统的，
 * 都会变成"手机用一个脑子理解、电脑用另一个脑子回答"的两套智能。
 *
 * Capacitor 这边已经具备两个前提，所以可以纯 TS 做：
 *   * `setMediaPlaybackRequiresUserGesture(false)` —— 收到音频就能直接播，不用先点一下；
 *   * `BridgeWebChromeClient.onPermissionRequest` 会自动同意 `AUDIO_CAPTURE` ——
 *     `getUserMedia` 不会卡在一个没人应答的授权框上。
 *
 * 两个**必须自己算**的量，写在这里而不是交给浏览器：
 *   * **采样率**。管线统一 16 kHz，而 `AudioContext` 的采样率是设备定的（常见 48 kHz，
 *     也有 44.1k 的）。直接把 48k 的字节当 16k 发过去，她听到的是慢放三倍的怪声，
 *     而且**没有任何报错** —— 识别只是"没听清"。所以这里必须重采样。
 *   * **时长**。一次最多 30 秒（电脑侧 `MAX_HEARD_MS` 同数），到了就由调用方掐断。
 *     让用户对着麦克风念三分钟然后收到一句"太长了"，是这里最不该发生的事。
 */

/** 管线的采样率，不是设备采样率。两边必须是同一个数。 */
export const TARGET_RATE = 16000

/** 和电脑侧 `VoiceCall.MAX_HEARD_MS` 保持一致。 */
export const MAX_RECORD_MS = 30000

/**
 * 采集用的 worklet。
 *
 * 用 `AudioWorklet` 而不是 `createScriptProcessor`：后者已废弃，而且它跑在主线程上，
 * 一帧卡顿就是一段录音的空洞 —— 在手机上表现为"她偶尔漏掉半句话"。
 * worklet 在自己的实时线程里，把每一帧的浮点采样拷一份发过来。
 *
 * 用 blob URL 而不是单独一个文件：这个 App 的页面是从 `assets/public` 里
 * 以 `https://localhost` 载入的，多一个文件就要多一条打包期的路径假设，
 * 而这个模块只有二十行。
 */
const WORKLET_SOURCE = `
class XyTap extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0]
    if (channel && channel.length) this.port.postMessage(new Float32Array(channel))
    return true
  }
}
registerProcessor('xy-tap', XyTap)
`

/** 录音前先问一下有没有麦克风。没有就是没有，不假装在录。 */
export function recorderAvailable(): { ok: boolean; reason: string } {
  if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
    return { ok: false, reason: '这个环境拿不到麦克风（浏览器要 https，App 里才有）' }
  }
  return { ok: true, reason: '' }
}

/**
 * 一段录好的话，已经是管线格式：s16le / 单声道 / 16 kHz。
 *
 * 重采样做在**录完之后**而不是实时做：一段 15 秒的话在内存里只有 480 KB，
 * 而实时重采样要在每一帧里保持跨帧的滤波器状态，一旦写错就是每隔几毫秒一声轻响
 * —— 那种 bug 在真机上听起来像"她耳朵有问题"。
 */
export interface Recording {
  pcm: Uint8Array
  /** 时长（毫秒），用来做"太短了"的提示。 */
  ms: number
}

class CallAudio {
  private context: AudioContext | null = null
  private stream: MediaStream | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private worklet: AudioWorkletNode | null = null
  private legacyless = false
  private meter: AnalyserNode | null = null
  private meterData = new Uint8Array(0)
  private chunks: Float32Array[] = []
  private inputRate = 0
  private recording = false
  private playSource: AudioBufferSourceNode | null = null

  private async ensure(out: boolean): Promise<AudioContext> {
    if (!this.context) {
      const Created =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (!Created) throw new Error('这个环境没有 AudioContext，放不出声音')
      this.context = new Created()
    }
    if (out && this.context.state !== 'running') {
      // 不设超时：这里的 resume 是在用户按了「开始通话」之后调的，属于手势上下文里，
      // 正常会立刻成功。真的被拦了，下面的播放会报出来。
      await this.context.resume()
    }
    return this.context
  }

  /**
   * 开始录。
   *
   * 关掉回声消除以外的处理都打开，但 `echoCancellation` 这一条是**必须**的：
   * 通话时她正在外放（用户听到的回答从同一个手机的扬声器出来），
   * 不消除回声的话录进去的是**她自己的声音**，然后她就会回答自己 ——
   * 这是实时语音里最有名的那个死循环。
   */
  async start(): Promise<void> {
    const context = await this.ensure(false)
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    })
    this.inputRate = context.sampleRate
    this.chunks = []
    this.recording = true

    this.source = context.createMediaStreamSource(this.stream)
    // 电平表是独立的 Analyser：worklet 只负责把采样搬过来，
    // 把两件事混在一条线上会让"波形在动但录到的是空的"这种错误没法区分。
    this.meter = context.createAnalyser()
    this.meter.fftSize = 512
    this.meter.smoothingTimeConstant = 0.6
    this.meterData = new Uint8Array(this.meter.fftSize)
    this.source.connect(this.meter)
    // 不接 destination：接上去就是把自己说的话再外放一遍，等于对着麦克风开功放。

    await this.attachTap(context)
  }

  private async attachTap(context: AudioContext): Promise<void> {
    try {
      const url = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: 'text/javascript' }))
      try {
        await context.audioWorklet.addModule(url)
      } finally {
        URL.revokeObjectURL(url)
      }
      const node = new AudioWorkletNode(context, 'xy-tap', { numberOfOutputs: 0 })
      node.port.onmessage = (event: MessageEvent<Float32Array>) => {
        if (this.recording) this.chunks.push(event.data)
      }
      this.source?.connect(node)
      this.worklet = node
      return
    } catch {
      // 老 WebView 可能没有 audioWorklet。退回 ScriptProcessor：慢一点、
      // 会有一声废弃警告，但比"录不到"好。
      this.legacyless = true
    }
    const processor = context.createScriptProcessor(4096, 1, 1)
    processor.onaudioprocess = (event) => {
      if (this.recording) this.chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)))
    }
    this.source?.connect(processor)
    this.worklet = processor as unknown as AudioWorkletNode
  }

  /** 当前的响度，0..1。画那颗跳动的点用；没在录时是 0。 */
  level(): number {
    if (!this.meter || !this.recording) return 0
    this.meter.getByteTimeDomainData(this.meterData)
    let sum = 0
    for (let index = 0; index < this.meterData.length; index += 1) {
      const deviation = (this.meterData[index] - 128) / 128
      sum += deviation * deviation
    }
    return Math.min(1, Math.sqrt(sum / this.meterData.length) * 3)
  }

  get isRecording(): boolean {
    return this.recording
  }

  /**
   * 已经录到多少毫秒。
   *
   * 按**采样数**算而不是按墙上时钟：界面上那个秒数要和最后真正发出去的长度一致，
   * 否则用户看到"12 秒"、电脑收到"9 秒"，然后不明白为什么"太短了"。
   */
  recordingMs(): number {
    if (!this.inputRate) return 0
    let count = 0
    for (const chunk of this.chunks) count += chunk.length
    return Math.round((count / this.inputRate) * 1000)
  }

  /** 停录并把已经收到的采样转成 16 kHz s16le。 */
  async stop(): Promise<Recording> {
    this.recording = false
    const chunks = this.chunks
    const rate = this.inputRate
    this.chunks = []
    this.releaseInput()
    if (!chunks.length) return { pcm: new Uint8Array(0), ms: 0 }
    const total = chunks.reduce((count, chunk) => count + chunk.length, 0)
    const joined = new Float32Array(total)
    let at = 0
    for (const chunk of chunks) {
      joined.set(chunk, at)
      at += chunk.length
    }
    return { pcm: to16k(joined, rate), ms: Math.round((joined.length / rate) * 1000) }
  }

  /** 放弃这一段。按了「取消」时用。 */
  abort(): void {
    this.recording = false
    this.chunks = []
    this.releaseInput()
  }

  private releaseInput(): void {
    if (this.worklet) {
      this.worklet.port.onmessage = null
      try {
        this.worklet.disconnect()
      } catch {
        // 已经断开过了。
      }
      if (this.legacyless) {
        ;(this.worklet as unknown as ScriptProcessorNode).onaudioprocess = null
      }
      this.worklet = null
    }
    this.source?.disconnect()
    this.meter?.disconnect()
    this.source = null
    this.meter = null
    for (const track of this.stream?.getTracks() ?? []) track.stop()
    this.stream = null
  }

  /**
   * 播一段 PCM。
   *
   * 返回一个在**播放真正结束**时 resolve 的 Promise，而不是"排进队列就返回"：
   * 通话的下一轮要等她说完才能开麦，否则麦克风录到的是她的尾音。
   */
  async play(pcm: Uint8Array, sampleRate: number): Promise<void> {
    if (!pcm.byteLength) return
    const context = await this.ensure(true)
    const samples = Math.floor(pcm.byteLength / 2)
    if (samples <= 0) return
    const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength)
    const buffer = context.createBuffer(1, samples, sampleRate)
    const channel = buffer.getChannelData(0)
    for (let index = 0; index < samples; index += 1) {
      // Int16 → [-1, 1)。显式小端：电脑写的是 s16le，平台的原生字节序不是可以依赖的东西。
      channel[index] = view.getInt16(index * 2, true) / 0x8000
    }
    const source = context.createBufferSource()
    source.buffer = buffer
    source.connect(context.destination)
    this.playSource = source
    await new Promise<void>((resolve) => {
      source.onended = () => {
        if (this.playSource === source) this.playSource = null
        resolve()
      }
      source.start()
    })
  }

  /** 打断。用户按「打断」，或一轮说完要插话时用。 */
  hush(): void {
    try {
      this.playSource?.stop()
    } catch {
      // 已经播完了。每一帧都记一条日志没有意义。
    }
    this.playSource = null
  }

  get speaking(): boolean {
    return this.playSource !== null
  }
}

/**
 * 线性重采样到 16 kHz。
 *
 * 线性插值在"人声 48k → 16k"这个比例上够用（比例正好是整数三时就是三点平均，
 * 等价于一个粗糙的抗混叠滤波器），而且**没有状态**，所以不会出现跨帧的怪声。
 * 采样率本来就是 16k 时原样返回，不做任何计算。
 */
function to16k(samples: Float32Array, inputRate: number): Uint8Array {
  if (inputRate === TARGET_RATE) return toInt16(samples)
  const ratio = inputRate / TARGET_RATE
  const outLength = Math.floor(samples.length / ratio)
  const out = new Uint8Array(outLength * 2)
  const view = new DataView(out.buffer)
  for (let index = 0; index < outLength; index += 1) {
    // 落在一个输出采样区间里的输入采样取平均：整数比例时是三点平均，
    // 非整数比例时区间长度在 2~4 之间变化，误差上限是半个输入采样，听不出来。
    const from = index * ratio
    const to = Math.min(from + ratio, samples.length)
    let sum = 0
    let count = 0
    for (let at = Math.floor(from); at < to; at += 1) {
      sum += samples[at]
      count += 1
    }
    view.setInt16(index * 2, toInt(count > 0 ? sum / count : 0), true)
  }
  return out
}

function toInt16(samples: Float32Array): Uint8Array {
  const out = new Uint8Array(samples.length * 2)
  const view = new DataView(out.buffer)
  for (let index = 0; index < samples.length; index += 1) {
    view.setInt16(index * 2, toInt(samples[index]), true)
  }
  return out
}

function toInt(value: number): number {
  const clamped = Math.max(-1, Math.min(1, value))
  // 负半轴到 -32768，正半轴到 32767：只乘 32767 会让 -1 差一格，
  // 大音量时表现为极轻微的削顶，听不出来但没必要留着。
  return clamped < 0 ? Math.round(clamped * 0x8000) : Math.round(clamped * 0x7fff)
}

/** `Uint8Array` → base64。JSON 装不下裸字节，这是唯一的过路方式。 */
export function toBase64(bytes: Uint8Array): string {
  let binary = ''
  const step = 0x8000
  // 分块拼接：一次 `String.fromCharCode(...bytes)` 在 2 MB 的音频上会抛
  // "Maximum call stack size exceeded"，而那看起来像"录音坏了"。
  for (let at = 0; at < bytes.length; at += step) {
    binary += String.fromCharCode(...bytes.subarray(at, at + step))
  }
  return btoa(binary)
}

/** One instance per page: two would fight over the microphone. */
export const callAudio = new CallAudio()
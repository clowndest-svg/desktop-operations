/**
 * 手机侧的"能不能听到/说出来"。
 *
 * Android 的识别和朗读都是**系统服务**，JS 摸不到，必须走原生插件。这里只写契约，
 * 并且给 Web 一个**诚实的**降级实现：在浏览器里点麦克风会得到"这个环境没有系统语音"，
 * 而不是转圈或假波形——一个看起来能用其实不能用的按钮，比没有这个按钮更糟。
 */
import { registerPlugin } from '@capacitor/core'

export interface ListenResult {
  ok: boolean
  transcript: string
  error: string
}

export interface SpeakResult {
  ok: boolean
  error: string
}

export interface SpeechCapability {
  native: boolean
  /** 为什么不能用。空字符串代表能用。 */
  reason: string
  /** 系统认不认识中文（RecognitionSupport != SUPPORTED 时是 false）。 */
  chinese: boolean
}

interface XySpeechPlugin {
  capability(): Promise<SpeechCapability>
  /** 按下说话键：立刻开始录音，别等松手。 */
  listenStart(options: { language?: string; maxMs?: number }): Promise<{ ok: boolean; error: string }>
  /** 松手：结束这次识别，拿回文字。 */
  listenStop(): Promise<ListenResult>
  speak(options: { text: string; language?: string }): Promise<SpeakResult>
  stop(): Promise<SpeakResult>
}

const unavailable: SpeechCapability = {
  native: false,
  reason: '这不是 App，是浏览器：Android 的系统语音只在装好的 APK 里有',
  chinese: false,
}

export const XySpeech = registerPlugin<XySpeechPlugin>('XySpeech', {
  web: {
    async capability() {
      return unavailable
    },
    async listenStart() {
      return { ok: false, error: unavailable.reason }
    },
    async listenStop() {
      return { ok: false, transcript: '', error: unavailable.reason }
    },
    async speak() {
      return { ok: false, error: unavailable.reason }
    },
    async stop() {
      return { ok: false, error: unavailable.reason }
    },
  },
})

/** 朗读前的清洗：和她说话一样，只念字和数字，标点念出来是噪音。 */
export function speakable(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, '（代码略）')
    .replace(/[*_#`>|]/g, '')
    .replace(/[，。、；：！？,.;:!?"'“”()（）\[\]{}]/g, ' ')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

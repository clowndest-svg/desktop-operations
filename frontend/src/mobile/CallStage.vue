<template>
  <!--
    通话页：一个反复出现的圆 + 一句状态 + 两个按钮。

    为什么整页只有这三样：通话是**一只手拿着、眼睛可能不在屏幕上**的场景。
    豆包那个界面的克制不是设计品味，是可用性 —— 说话时用户只能看一个东西，
    所以屏幕上只放一个东西：她现在的状态。详情（听到了什么、她答了什么）
    在下面滚动，但也要能滚到，因为"她答错了"必须能回看。
  -->
  <section class="call" :class="{ 'call--live': phase === 'listening' }">
    <header class="call__top">
      <span class="call__who">
        <strong>{{ link.name || '小夜' }}</strong>
        <em>{{ status }}</em>
      </span>
      <button class="call__x" type="button" aria-label="挂断" @click="hangup">
        <svg viewBox="0 0 24 24" width="18" height="18">
          <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" />
        </svg>
      </button>
    </header>

    <div class="call__stage">
      <div class="call__orb" :class="`call__orb--${phase}`">
        <span class="call__core" :style="coreStyle" />
        <span class="call__ring call__ring--1" />
        <span class="call__ring call__ring--2" />
        <span class="call__ring call__ring--3" />
      </div>
      <p class="call__hint">{{ aside || hint }}</p>
    </div>

    <div ref="log" class="call__log">
      <p v-if="!turns.length" class="call__empty">
        按下中间的圆开始说话。她听到的、答的，都会按顺序留在这里。
      </p>
      <div v-for="(turn, index) in turns" :key="index" class="call__line" :class="`call__line--${turn.role}`">
        <span class="call__tag">{{ turn.role === 'user' ? '你说' : '她说' }}</span>
        <p class="call__text">{{ turn.text }}</p>
        <button v-if="turn.role === 'assistant'" class="call__again" type="button" @click="replay(index)">
          再念一遍
        </button>
      </div>
    </div>

    <footer class="call__dock">
      <button
        class="call__round call__round--side"
        type="button"
        :disabled="busy || phase === 'starting'"
        @click="toggleHandsFree"
        :class="{ 'is-on': handsFree }"
      >
        <svg viewBox="0 0 24 24" width="20" height="20">
          <path
            d="M4 11a8 8 0 0116 0M7 11a5 5 0 0110 0M10.5 11a1.5 1.5 0 013 0"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
          />
        </svg>
        <span>免提</span>
      </button>

      <button
        class="call__round call__round--talk"
        :class="{ 'is-on': phase === 'listening' }"
        type="button"
        :disabled="!canTalk"
        :aria-label="talkLabel"
        @pointerdown.prevent="talkStart"
        @pointerup.prevent="talkStop"
        @pointercancel="talkCancel"
        @pointerleave="talkCancel"
      >
        <svg v-if="phase !== 'listening'" viewBox="0 0 24 24" width="30" height="30">
          <rect x="9.2" y="3" width="5.6" height="11" rx="2.8" fill="none" stroke="currentColor" stroke-width="1.7" />
          <path d="M5.5 11.5a6.5 6.5 0 0013 0M12 18v3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" />
        </svg>
        <svg v-else viewBox="0 0 24 24" width="26" height="26">
          <rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" />
        </svg>
        <span>{{ phase === 'listening' ? '松手' : '按住说' }}</span>
      </button>

      <button
        class="call__round call__round--side"
        type="button"
        :disabled="!turns.length"
        @click="clear"
      >
        <svg viewBox="0 0 24 24" width="20" height="20">
          <path d="M5 7h14M9 7V5h6v2M8 7l1 12h6l1-12" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <span>清空</span>
      </button>
    </footer>
  </section>
</template>

<script setup lang="ts">
/**
 * 手机上的实时语音通话。
 *
 * 三件事让这个界面成立，缺一个就变成"看着像能通话但是不行"：
 *
 * 1. **耳朵是电脑的。** 录到的原声（16kHz s16le）整个交给电脑上的
 *    `VoiceCall.turn`，由电脑的 SenseVoice 识别、电脑的 ChatService 回答。
 *    手机不本地识别 —— 本地识别意味着"手机用系统语音理解一遍、电脑再理解一遍"，
 *    同一个问题问两种方式会得到两种能力，这正是不该发生的事。
 *
 * 2. **嘴也是电脑的。** 回答的音频是电脑合成好、base64 传回来的 PCM。
 *    不用手机的系统 TTS：那会让手机上的音色和你刚在电脑上选的不一样，
 *    而"选音色"这件事的意义就是无论从哪个屏幕听都是同一个声音。
 *
 * 3. **状态是推出来的，不是编的。** 圆圈的动静来自真实的录音电平和播放状态，
 *    没有音频的时候它是静的。一个永远在脉动的圆是在骗人 —— 用户会对着
 *    一个没在听的麦克风说一分钟。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  pcCallReadiness,
  pcCallSpeak,
  pcCallTurn,
  pcCallWarmup,
  pcPreviewVoice,
  pcVoices,
  type CallReadiness,
  type PcLink,
  type VoiceBoard,
} from './api'
import { MAX_RECORD_MS, TARGET_RATE, callAudio, recorderAvailable, toBase64 } from './call'

const props = defineProps<{ link: PcLink }>()
const emit = defineEmits<{ (event: 'close'): void; (event: 'voice'): void }>()

type Phase = 'starting' | 'idle' | 'listening' | 'thinking' | 'speaking' | 'blocked'

const phase = ref<Phase>('starting')
const turns = ref<{ role: 'user' | 'assistant'; text: string }[]>([])
/**
 * 一句盖过状态的话，比如「这一下太短了」。
 *
 * 和 `hint` 分开是必要的：`hint` 是**从状态推出来的**（正在听 / 正在想），
 * 而这是一次性的、状态推不出来的信息。混成一个变量就会出现
 * "状态变了但提示还是上一轮的"这种既难复现又难解释的界面。
 */
const aside = ref('')
const busy = ref(false)
const level = ref(0)
const handsFree = ref(false)
const readiness = ref<CallReadiness | null>(null)
const board = ref<VoiceBoard | null>(null)
const log = ref<HTMLElement | null>(null)

let frame = 0
let watchdog = 0
let alive = true

const currentVoice = computed(() => {
  const found = board.value?.choices.find((choice) => choice.current)
  return found?.label ?? board.value?.current ?? ''
})

const status = computed(() => {
  switch (phase.value) {
    case 'starting':
      return '连接中'
    case 'listening':
      return '在听'
    case 'thinking':
      return '在想'
    case 'speaking':
      return '在说'
    case 'blocked':
      return '用不了'
    default:
      return currentVoice.value ? `待机 · ${currentVoice.value}` : '待机'
  }
})

const hint = computed(() => {
  switch (phase.value) {
    case 'starting':
      return '她在准备耳朵，第一次要几十秒'
    case 'listening':
      return '说吧，松手就发出去'
    case 'thinking':
      return '她在想'
    case 'speaking':
      return '她在说'
    case 'blocked':
      return readiness.value?.asr_error || '这台电脑现在听不了'
    default:
      return handsFree.value ? '按住说，松手之后她会连着听下一句' : '按住中间的圆说话'
  }
})

/**
 * 状态变化时清掉那句一次性提示。
 *
 * 否则「这一下太短了」会一直挂在屏幕上，而用户早就开始说下一句了。
 */
watch(phase, () => {
  aside.value = ''
})

const canTalk = computed(
  () => !busy.value && phase.value !== 'starting' && phase.value !== 'blocked',
)

const talkLabel = computed(() => (phase.value === 'listening' ? '松手发送' : '按住说话'))

/**
 * 圆圈的亮度。
 *
 * 说话时用**播放**的电平而不是录音的：用户看着屏幕想知道的是"她还在说吗"，
 * 而不是"麦克风多响"。听的时候反过来。
 */
const coreStyle = computed(() => {
  const glow = 0.35 + level.value * 0.65
  return { '--call-glow': String(glow), '--call-scale': String(1 + level.value * 0.12) }
})

function note(text: string): void {
  turns.value = [...turns.value, { role: 'assistant', text }]
}

async function scroll(): Promise<void> {
  await nextTick()
  const node = log.value
  if (node) node.scrollTop = node.scrollHeight
}

onMounted(async () => {
  const mic = recorderAvailable()
  if (!mic.ok) {
    phase.value = 'blocked'
    aside.value = mic.reason
    return
  }
  // 音色列表顺带拿一次：待机时显示当前音色，用户不用退回设置页才知道她在用什么声音。
  void pcVoices(props.link)
    .then((payload) => {
      if (alive) board.value = payload
    })
    .catch(() => undefined)
  try {
    readiness.value = await pcCallReadiness(props.link)
    if (readiness.value.error) {
      phase.value = 'blocked'
      aside.value = readiness.value.error
      return
    }
    phase.value = 'idle'
    // 不 await：这是"提前开始加载"，不是"等它加载完"。
    void pcCallWarmup(props.link).then((payload) => {
      if (!alive) return
      readiness.value = payload
      if (payload.asr_error) {
        phase.value = 'blocked'
        aside.value = payload.asr_error
      }
    })
  } catch (err) {
    phase.value = 'blocked'
    aside.value = err instanceof Error ? err.message : String(err)
  }
})

onBeforeUnmount(() => {
  alive = false
  window.clearInterval(frame)
  window.clearInterval(watchdog)
  callAudio.hush()
  callAudio.abort()
})

watch(phase, (now) => {
  window.clearInterval(frame)
  if (now === 'listening') {
    frame = window.setInterval(() => {
      level.value = callAudio.level()
    }, 60)
  } else {
    level.value = now === 'speaking' ? 0.5 : 0
  }
})

async function talkStart(): Promise<void> {
  if (!canTalk.value) return
  if (callAudio.speaking) {
    // 她在说话时按下来说话键 = 打断。这是语音对话里唯一不需要解释的手势。
    callAudio.hush()
    await waitIdle()
  }
  try {
    await callAudio.start()
    phase.value = 'listening'
    // 到点自动掐断：让用户念满三分钟再收到"太长了"是最不该发生的失败。
    watchdog = window.setTimeout(() => {
      void talkStop()
    }, MAX_RECORD_MS)
  } catch (err) {
    note(`麦克风打不开：${err instanceof Error ? err.message : String(err)}`)
    phase.value = 'idle'
  }
}

async function talkStop(): Promise<void> {
  window.clearTimeout(watchdog)
  if (!callAudio.isRecording) return
  const recorded = await callAudio.stop()
  if (recorded.pcm.byteLength === 0) {
    phase.value = 'idle'
    return
  }
  if (recorded.ms < 300) {
    // 半秒以内几乎都是一个误触。发出去只会得到"没听清"，还多一轮。
    phase.value = 'idle'
    aside.value = '这一下太短了，按住再说'
    return
  }
  await send(toBase64(recorded.pcm))
}

function talkCancel(): void {
  window.clearTimeout(watchdog)
  if (callAudio.isRecording) {
    callAudio.abort()
    phase.value = 'idle'
    note('这一段没发出去。')
  }
}

async function send(pcm: string): Promise<void> {
  busy.value = true
  phase.value = 'thinking'
  try {
    const reply = await pcCallTurn(props.link, pcm, TARGET_RATE)
    if (reply.heard) turns.value = [...turns.value, { role: 'user', text: reply.heard }]
    else if (reply.error) turns.value = [...turns.value, { role: 'assistant', text: reply.error }]
    if (reply.answer) turns.value = [...turns.value, { role: 'assistant', text: reply.answer }]
    if (reply.truncated) {
      turns.value = [...turns.value, { role: 'assistant', text: '（这一条太长，只念了开头，全文在屏幕上）' }]
    }
    await scroll()
    await play(reply.audio, reply.sample_rate)
  } catch (err) {
    turns.value = [...turns.value, { role: 'assistant', text: `这一轮没走通：${err instanceof Error ? err.message : String(err)}` }]
    await scroll()
  } finally {
    busy.value = false
    settle()
  }
  // 免提：她说完之后提示留在屏幕上，但**不自动开麦** ——
  // 自动开麦会让她开始自言自语地回答自己的回声。
  if (handsFree.value && alive) aside.value = '免提开着：按住说下一句'
}

async function play(encoded: string, rate: number): Promise<void> {
  if (!encoded) return
  phase.value = 'speaking'
  const bytes = decode(encoded)
  try {
    await callAudio.play(bytes, rate)
  } catch (err) {
    note(`放不出来：${err instanceof Error ? err.message : String(err)}`)
  } finally {
    if (phase.value === 'speaking') phase.value = 'idle'
  }
}

async function replay(index: number): Promise<void> {
  const turn = turns.value[index]
  if (!turn || turn.role !== 'assistant') return
  try {
    // 走 call_speak 而不是再问一遍：屏幕上已经有这句话了，
    // 重新问一次会改掉对话内容，而用户按的只是"再念一遍"。
    const reply = await pcCallSpeak(props.link, turn.text)
    if (reply.error) {
      note(reply.error)
      return
    }
    await play(reply.audio, reply.sample_rate)
  } catch (err) {
    note(err instanceof Error ? err.message : String(err))
  }
}

/** 试听当前音色。让"她在用什么声音"是可听的，而不只是一个名字。 */
async function audition(): Promise<void> {
  const voice = board.value?.current
  if (!voice) return
  try {
    const reply = await pcPreviewVoice(props.link, voice)
    if (reply.error || !reply.pcm) {
      note(reply.error || '试听没有拿到音频')
      return
    }
    await play(reply.pcm, reply.sample_rate ?? 24_000)
  } catch (err) {
    note(err instanceof Error ? err.message : String(err))
  }
}

function toggleHandsFree(): void {
  handsFree.value = !handsFree.value
  note(handsFree.value ? '免提开了：她说完接着听下一句。' : '免提关了。')
}

function clear(): void {
  turns.value = []
}

function hangup(): void {
  callAudio.hush()
  callAudio.abort()
  emit('close')
}

/**
 * 回到待机，除非这一次通话已经确定用不了。
 *
 * 用「读一次再判断」而不是内联的 `if (phase.value !== 'blocked')`：后者在
 * `finally` 里写出来时，TypeScript 只看到本函数里刚赋过的 `'thinking'`，
 * 于是把整个判断当成永真并报错 —— 而真正的事实是，`await play()` 里
 * 可能已经把状态改成 `'blocked'` 了，那个收窄是错的。
 */
function settle(): void {
  const now: Phase = phase.value
  if (now !== 'blocked') phase.value = 'idle'
}

function waitIdle(): Promise<void> {
  return new Promise((resolve) => {
    const check = window.setInterval(() => {
      if (!callAudio.speaking) {
        window.clearInterval(check)
        resolve()
      }
    }, 40)
  })
}

function decode(encoded: string): Uint8Array {
  const binary = atob(encoded)
  const out = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) out[index] = binary.charCodeAt(index)
  return out
}

defineExpose({ audition })
</script>

<style scoped>
.call {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: flex;
  flex-direction: column;
  background: var(--xy-bg);
  /* 通话页盖住整个 App，包括键盘让位那一层：这里没有输入框。 */
}

.call__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: calc(env(safe-area-inset-top) + 14px) 16px 10px;
}

.call__who {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.call__who strong {
  font-size: 15px;
}

.call__who em {
  font-size: 12px;
  font-style: normal;
  color: var(--xy-text-2);
}

.call__x {
  width: var(--xy-tap);
  height: var(--xy-tap);
  border-radius: var(--xy-pill);
  border: 1px solid var(--xy-line-2);
  background: var(--xy-card);
  color: var(--xy-text-2);
  display: grid;
  place-items: center;
}

.call__stage {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 26px;
  min-height: 0;
}

.call__orb {
  position: relative;
  width: 190px;
  height: 190px;
  display: grid;
  place-items: center;
}

.call__core {
  width: 108px;
  height: 108px;
  border-radius: 50%;
  background: radial-gradient(circle at 34% 30%, var(--xy-accent), color-mix(in srgb, var(--xy-accent) 40%, #05101c));
  box-shadow: 0 0 48px var(--xy-accent-line);
  transform: scale(var(--call-scale, 1));
  opacity: var(--call-glow, 0.5);
  transition: transform 90ms linear, opacity 90ms linear;
}

.call__ring {
  position: absolute;
  inset: 0;
  border-radius: 50%;
  border: 1px solid var(--xy-accent-line);
  opacity: 0;
}

/* 只在真的有人说话/她在说的时候扩散。静止的脉动是假的。 */
.call--live .call__ring--1 {
  animation: call-pulse 1.8s ease-out infinite;
}
.call--live .call__ring--2 {
  animation: call-pulse 1.8s ease-out 0.6s infinite;
}
.call--live .call__ring--3 {
  animation: call-pulse 1.8s ease-out 1.2s infinite;
}

@keyframes call-pulse {
  0% {
    transform: scale(0.62);
    opacity: 0.55;
  }
  100% {
    transform: scale(1.05);
    opacity: 0;
  }
}

.call__orb--thinking .call__core {
  animation: call-breathe 1.4s ease-in-out infinite;
}

@keyframes call-breathe {
  0%,
  100% {
    transform: scale(0.94);
  }
  50% {
    transform: scale(1.04);
  }
}

.call__orb--blocked .call__core {
  background: var(--xy-card-2);
  box-shadow: none;
  opacity: 0.5;
}

.call__hint {
  margin: 0;
  max-width: 300px;
  text-align: center;
  font-size: 13px;
  line-height: 1.6;
  color: var(--xy-text-2);
}

.call__log {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  -webkit-overflow-scrolling: touch;
}

.call__empty {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--xy-text-3);
}

.call__line {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-width: 84%;
}

.call__line--assistant {
  align-self: flex-end;
  align-items: flex-end;
}

.call__tag {
  font-size: 11px;
  color: var(--xy-text-3);
}

.call__text {
  margin: 0;
  padding: 10px 13px;
  border-radius: var(--xy-r-md);
  background: var(--xy-card);
  font-size: 14px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.call__line--user .call__text {
  background: var(--xy-accent-soft);
}

.call__again {
  border: none;
  background: none;
  color: var(--xy-text-3);
  font-size: 12px;
  padding: 2px 0;
}

.call__dock {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 22px calc(env(safe-area-inset-bottom) + 18px);
}

.call__round {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  border: none;
  background: none;
  color: var(--xy-text-2);
  font-size: 11px;
}

.call__round--side {
  width: 62px;
}

.call__round--side.is-on {
  color: var(--xy-accent);
}

.call__round--side svg {
  width: 45px;
  height: 45px;
  padding: 12px;
  border-radius: var(--xy-pill);
  background: var(--xy-card);
  border: 1px solid var(--xy-line);
}

.call__round--side:disabled {
  opacity: 0.4;
}

.call__round--talk {
  width: 96px;
  height: 96px;
  border-radius: 50%;
  background: var(--xy-accent);
  color: var(--xy-on-accent);
  justify-content: center;
  gap: 2px;
  box-shadow: 0 10px 30px var(--xy-accent-line);
}

.call__round--talk.is-on {
  background: var(--xy-danger);
  color: #fff;
}

.call__round--talk:disabled {
  background: var(--xy-card-2);
  color: var(--xy-text-3);
  box-shadow: none;
}

.call__round--talk span {
  font-size: 11px;
  font-weight: 600;
}
</style>
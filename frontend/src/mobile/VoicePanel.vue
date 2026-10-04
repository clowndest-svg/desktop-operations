<template>
  <section class="vn">
    <header class="vn__head">
      <h2>音色</h2>
      <button class="vn__x" type="button" aria-label="关闭" @click="emit('close')">
        <svg viewBox="0 0 24 24" width="18" height="18">
          <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
        </svg>
      </button>
    </header>

    <div class="vn__body">
      <p v-if="error" class="vn__bad">{{ error }}</p>
      <p v-else-if="note" class="vn__ok">{{ note }}</p>

      <!-- 录自己的声音 -->
      <div class="vn__card">
        <h3>录一段我的声音</h3>
        <p class="vn__why">
          {{
            rules.available
              ? '她照着这段录音学你的音色。念一句平时会说的话，3 到 15 秒最好。'
              : rules.reason || '这台电脑还不能用录制的音色。'
          }}
        </p>

        <template v-if="rules.available">
          <label class="vn__field">
            <span>名字</span>
            <input v-model="draft.name" type="text" maxlength="24" placeholder="比如：我自己" />
          </label>
          <label class="vn__field">
            <span>你要念的那句话</span>
            <input
              v-model="draft.prompt"
              type="text"
              maxlength="200"
              placeholder="把下面要念的内容原样写在这里"
            />
          </label>

          <div class="vn__rec">
            <button
              class="vn__dot"
              type="button"
              :class="{ 'is-on': recording }"
              :disabled="saving"
              @click="toggleRec"
            >
              <span class="vn__blink" :style="{ opacity: String(0.3 + breath * 0.7) }" />
              {{ recording ? '停止录音' : '开始录音' }}
            </button>
            <span class="vn__time">{{ elapsed }}s / {{ Math.round(rules.max_ms / 1000) }}s</span>
          </div>

          <div class="vn__track" aria-hidden="true">
            <span class="vn__track-fill" :style="{ width: `${barPercent}%` }" />
          </div>
          <p class="vn__meter-note">{{ meterNote }}</p>

          <button class="vn__save" type="button" :disabled="!canSave || saving" @click="save">
            {{ saving ? '正在交给电脑…' : '存成我的音色' }}
          </button>
        </template>
      </div>

      <!-- 已录制的 -->
      <div v-if="clones.voices.length" class="vn__card">
        <h3>我录的音色</h3>
        <ul class="vn__list">
          <li v-for="voice in clones.voices" :key="voice.id">
            <button
              class="vn__row"
              type="button"
              :class="{ 'is-on': voice.id === board.current }"
              @click="pick(voice.id)"
            >
              <span class="vn__name">{{ voice.name || voice.label }}</span>
              <span class="vn__meta">{{ (voice.duration_ms ?? 0) / 1000 >= 10 ? '录音' : '' }} {{ ((voice.duration_ms ?? 0) / 1000).toFixed(1) }}s</span>
            </button>
            <button class="vn__del" type="button" :disabled="busy" @click="drop(voice.id)">删</button>
          </li>
        </ul>
        <p class="vn__why">同一个 Wi-Fi 下，手机上删掉的录音电脑上也就没了。</p>
      </div>

      <!-- 内置音色 -->
      <div v-if="board.choices.length" class="vn__card">
        <h3>现成的声音</h3>
        <ul class="vn__list">
          <li v-for="voice in board.choices" :key="voice.id">
            <button
              class="vn__row"
              type="button"
              :class="{ 'is-on': voice.id === board.current }"
              @click="pick(voice.id)"
            >
              <span class="vn__name">{{ voice.label }}</span>
            </button>
            <button class="vn__hear" type="button" :disabled="busy" @click="hear(voice.id)">试听</button>
          </li>
        </ul>
        <p class="vn__why">试听不会改掉当前音色 —— 听另一个不该等于选它。</p>
      </div>

      <p v-if="!board.choices.length && !clones.voices.length" class="vn__why">
        正在从电脑上读音色列表…
      </p>
    </div>
  </section>
</template>

<script setup lang="ts">
/**
 * 音色面板：选一个现成的，或者录一个自己的。
 *
 * 两个判断写在这个文件的注释里，因为它们决定了界面的形状：
 *
 * * **试听不改选中。** "让我听听另一个"变成"为什么她换声音了"是选音色功能
 *   最容易犯的错，所以试听和选中是两个按钮，而不是一个按钮的两种后果。
 * * **录制有下限也有上限，而且是电脑说了算。** 2 秒以下不够学一个人，
 *   30 秒以上每句话都要重新拷贝一遍这段音频。这些数字来自
 *   `voice_library` 的常量，通过 `cloning` 块送上来，界面照它显示 ——
 *   在界面上另写一个数，两边迟早不一致。
 *
 * 录音用「开始/停止」而不是按住说：录自己的音色时用户还要看着稿子念，
 * 一只手按着屏幕是做不到的。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import {
  pcAddClone,
  pcCloneVoices,
  pcPreviewVoice,
  pcPickVoice,
  pcRemoveClone,
  pcVoices,
  type CloneBoard,
  type CloneRules,
  type PcLink,
  type VoiceBoard,
} from './api'
import { MAX_RECORD_MS, TARGET_RATE, callAudio, recorderAvailable, toBase64 } from './call'

const props = defineProps<{ link: PcLink }>()
const emit = defineEmits<{ (event: 'close'): void; (event: 'changed'): void }>()

const emptyRules: CloneRules = {
  available: false,
  reason: '正在问电脑要录音规则…',
  min_ms: 2000,
  comfortable_ms: 15000,
  max_ms: 30000,
  max_voices: 20,
}

const board = reactive<VoiceBoard>({
  error: '',
  engine: '',
  current: '',
  choices: [],
  cloning: emptyRules,
  speed: 1,
  volume: 1,
  speed_min: 0.5,
  speed_max: 1.5,
  volume_min: 0,
  volume_max: 1,
})

/** 录好的那些音色，单独存而不是并进 `board`：它们来自另一个 RPC
 * （`voice_clone_list`），合并成一个对象就得决定"重名时谁赢"这种没有答案的问题。 */
const clones = reactive<CloneBoard>({ error: '', voices: [], cloning: emptyRules })
const draft = reactive({ name: '', prompt: '' })
const error = ref('')
const note = ref('')
const busy = ref(false)
const saving = ref(false)
const recording = ref(false)
const elapsed = ref(0)
const breath = ref(0)

let tick = 0
let cap = 0
let alive = true

const rules = computed<CloneRules>(() => clones.cloning ?? emptyRules)

/** 录制进度条：按时间画，这样用户知道什么时候会说"太长了"。 */
const barPercent = computed(() => Math.min(100, (elapsed.value / (rules.value.max_ms / 1000)) * 100))

const meterNote = computed(() => {
  if (!recording.value) return ''
  const best = Math.round(rules.value.comfortable_ms / 1000)
  if (elapsed.value < 1) return '在听…说点什么'
  return `最好停在 ${best} 秒左右。到 ${Math.round(rules.value.max_ms / 1000)} 秒会自动停。`
})

const recorded = ref<Uint8Array | null>(null)

const canSave = computed(() => {
  return (
    !recording.value &&
    draft.name.trim().length > 0 &&
    draft.prompt.trim().length > 0 &&
    recorded.value !== null &&
    elapsed.value * 1000 >= rules.value.min_ms
  )
})

async function load(): Promise<void> {
  try {
    const [voices, own] = await Promise.all([pcVoices(props.link), pcCloneVoices(props.link)])
    if (!alive) return
    Object.assign(board, voices)
    Object.assign(clones, own)
    if (own.error || voices.error) error.value = own.error || voices.error
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

onMounted(() => {
  const mic = recorderAvailable()
  if (!mic.ok) note.value = mic.reason
  void load()
})

onBeforeUnmount(() => {
  alive = false
  window.clearInterval(tick)
  window.clearTimeout(cap)
  if (callAudio.isRecording) callAudio.abort()
})

/**
 * 开始 / 停止录音。
 *
 * `speakable` 那种清洗**不**用在这里：用户念的是自己写的那句话，
 * 标点也是这句话的一部分（参考文本要和音频逐字对应，多一个逗号
 * 都会让模型分不清音色和内容）。
 */
async function toggleRec(): Promise<void> {
  if (recording.value) {
    await finishRec()
    return
  }
  error.value = ''
  note.value = ''
  recorded.value = null
  try {
    await callAudio.start()
  } catch (err) {
    error.value = `录音打不开：${err instanceof Error ? err.message : String(err)}`
    return
  }
  recording.value = true
  elapsed.value = 0
  tick = window.setInterval(() => {
    elapsed.value = Math.round(callAudio.recordingMs() / 1000)
    breath.value = callAudio.level()
  }, 100)
  // 上限用 min(电脑说的, 手机这边的常量)：两边任何一边改了，取更严的那个，
  // 不会出现"手机上还在录、电脑已经拒收"的窗口。
  const limit = Math.min(rules.value.max_ms, MAX_RECORD_MS)
  cap = window.setTimeout(() => {
    void finishRec()
  }, limit)
}

async function finishRec(): Promise<void> {
  window.clearInterval(tick)
  window.clearTimeout(cap)
  if (!callAudio.isRecording) return
  const taken = await callAudio.stop()
  recording.value = false
  breath.value = 0
  if (taken.pcm.byteLength === 0) {
    error.value = '这段没录上，再试一次'
    return
  }
  recorded.value = taken.pcm
  elapsed.value = Math.round(taken.ms / 1000)
  if (taken.ms < rules.value.min_ms) {
    error.value = `太短了（${(taken.ms / 1000).toFixed(1)} 秒），至少要说 ${rules.value.min_ms / 1000} 秒`
  }
}

async function save(): Promise<void> {
  const taken = recorded.value
  if (!taken) return
  saving.value = true
  error.value = ''
  try {
    const next = await pcAddClone(props.link, {
      name: draft.name.trim(),
      promptText: draft.prompt.trim(),
      pcm: toBase64(taken),
      sampleRate: TARGET_RATE,
    })
    Object.assign(clones, next)
    if (next.error) {
      error.value = next.error
      return
    }
    note.value = `已存成「${draft.name.trim()}」。选中它之后她就用这个声音说话。`
    draft.name = ''
    draft.prompt = ''
    recorded.value = null
    elapsed.value = 0
    emit('changed')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    saving.value = false
  }
}

async function pick(voiceId: string): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    const next = await pcPickVoice(props.link, voiceId)
    Object.assign(board, next)
    if (next.error) error.value = next.error
    else note.value = '换好了，下一句就用这个声音。'
    emit('changed')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

async function hear(voiceId: string): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    const reply = await pcPreviewVoice(props.link, voiceId)
    if (reply.error || !reply.pcm) {
      error.value = reply.error || '试听没有拿到音频'
      return
    }
    await callAudio.play(decode(reply.pcm), reply.sample_rate ?? 24_000)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

async function drop(voiceId: string): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    const next = await pcRemoveClone(props.link, voiceId)
    Object.assign(clones, next)
    if (next.error) error.value = next.error
    else note.value = '删掉了。'
    emit('changed')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

function decode(encoded: string): Uint8Array {
  const binary = atob(encoded)
  const out = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) out[index] = binary.charCodeAt(index)
  return out
}
</script>

<style scoped>
.vn {
  position: fixed;
  inset: 0;
  z-index: 45;
  display: flex;
  flex-direction: column;
  background: var(--xy-bg);
}

.vn__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: calc(env(safe-area-inset-top) + 14px) 16px 8px;
}

.vn__head h2 {
  margin: 0;
  font-size: 17px;
}

.vn__x {
  width: var(--xy-tap);
  height: var(--xy-tap);
  border-radius: var(--xy-pill);
  border: 1px solid var(--xy-line-2);
  background: var(--xy-card);
  color: var(--xy-text-2);
  display: grid;
  place-items: center;
}

.vn__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 16px calc(env(safe-area-inset-bottom) + 24px);
  display: flex;
  flex-direction: column;
  gap: 14px;
  -webkit-overflow-scrolling: touch;
}

.vn__card {
  background: var(--xy-card);
  border-radius: var(--xy-r-lg);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.vn__card h3 {
  margin: 0;
  font-size: 14px;
}

.vn__why {
  margin: 0;
  font-size: 12px;
  line-height: 1.65;
  color: var(--xy-text-3);
}

.vn__field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.vn__field span {
  font-size: 12px;
  color: var(--xy-text-2);
}

.vn__field input {
  border: 1px solid var(--xy-line-2);
  background: var(--xy-card-2);
  border-radius: var(--xy-r-sm);
  padding: 11px 12px;
  color: var(--xy-text);
  font-size: 16px;
}

.vn__rec {
  display: flex;
  align-items: center;
  gap: 12px;
}

.vn__dot {
  display: flex;
  align-items: center;
  gap: 8px;
  border: none;
  border-radius: var(--xy-pill);
  padding: 11px 18px;
  background: var(--xy-accent);
  color: var(--xy-on-accent);
  font-size: 14px;
  font-weight: 600;
}

.vn__dot.is-on {
  background: var(--xy-danger);
  color: #fff;
}

.vn__dot:disabled {
  opacity: 0.5;
}

.vn__blink {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: currentColor;
}

.vn__time {
  font-size: 13px;
  color: var(--xy-text-2);
  font-variant-numeric: tabular-nums;
}

.vn__track {
  height: 6px;
  border-radius: var(--xy-pill);
  background: var(--xy-card-2);
  overflow: hidden;
}

.vn__track-fill {
  display: block;
  height: 100%;
  background: var(--xy-accent);
  transition: width 120ms linear;
}

.vn__meter-note {
  margin: 0;
  font-size: 12px;
  color: var(--xy-text-3);
  min-height: 16px;
}

.vn__save {
  border: none;
  border-radius: var(--xy-r-sm);
  padding: 13px;
  background: var(--xy-accent);
  color: var(--xy-on-accent);
  font-size: 15px;
  font-weight: 600;
}

.vn__save:disabled {
  background: var(--xy-card-2);
  color: var(--xy-text-3);
}

.vn__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.vn__list li {
  display: flex;
  align-items: center;
  gap: 8px;
}

.vn__row {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border: 1px solid var(--xy-line);
  border-radius: var(--xy-r-sm);
  background: var(--xy-card-2);
  padding: 12px 13px;
  color: var(--xy-text);
  font-size: 14px;
  text-align: left;
}

.vn__row.is-on {
  border-color: var(--xy-accent-line);
  background: var(--xy-accent-soft);
}

.vn__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.vn__meta {
  font-size: 12px;
  color: var(--xy-text-3);
  flex-shrink: 0;
}

.vn__del,
.vn__hear {
  flex-shrink: 0;
  border: 1px solid var(--xy-line-2);
  border-radius: var(--xy-r-sm);
  background: none;
  color: var(--xy-text-2);
  font-size: 13px;
  padding: 11px 14px;
}

.vn__del {
  color: var(--xy-danger);
  border-color: var(--xy-danger-soft);
}

.vn__del:disabled,
.vn__hear:disabled {
  opacity: 0.45;
}

.vn__bad,
.vn__ok {
  margin: 0;
  border-radius: var(--xy-r-sm);
  padding: 11px 13px;
  font-size: 13px;
  line-height: 1.6;
}

.vn__bad {
  background: var(--xy-danger-soft);
  color: var(--xy-danger);
}

.vn__ok {
  background: var(--xy-accent-soft);
  color: var(--xy-accent);
}
</style>
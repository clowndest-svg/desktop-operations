<template>
  <div v-if="open" class="vp" role="dialog" aria-modal="true" aria-label="语音音色">
    <div class="vp__scrim" @click="emit('close')"></div>

    <section class="vp__box">
      <header class="vp__head">
        <h2 class="hud-title">音色 · VOICE</h2>
        <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
      </header>

      <!--
        Rate and volume live here rather than in 设置 because they are the other two
        halves of "how does she sound": choosing a voice and then hearing it at the
        wrong speed is half a decision. Both apply to the next sentence -- including
        the 试听 below, which is the only way to judge them.
      -->
      <section class="vp__style" aria-label="语速与音量">
        <label class="vp__slider">
          <span class="vp__slider-name">语速 <b class="hud-num">{{ speed.toFixed(2) }}×</b></span>
          <input
            v-model.number="speed"
            type="range"
            :min="speedMin"
            :max="speedMax"
            step="0.05"
            :disabled="busy"
            @change="applyStyle()"
          />
        </label>
        <label class="vp__slider">
          <span class="vp__slider-name">音量 <b class="hud-num">{{ Math.round(volume * 100) }}%</b></span>
          <input
            v-model.number="volume"
            type="range"
            :min="volumeMin"
            :max="volumeMax"
            step="0.05"
            :disabled="busy"
            @change="applyStyle()"
          />
        </label>
        <button
          class="hud-btn vp__try"
          type="button"
          :disabled="busy || current === ''"
          title="用当前音色、当前语速和音量读一遍"
          @click="preview(current)"
        >
          {{ previewing !== '' ? '合成中…' : '试听这一档' }}
        </button>
      </section>

      <p v-if="error" class="vp__error">{{ error }}</p>
      <p v-else-if="loading" class="vp__wait">读取音色列表…</p>
      <p v-else-if="choices.length === 0" class="vp__wait">这个引擎没有可选音色。</p>

      <ul v-else class="vp__list">
        <li v-for="choice in choices" :key="choice.id" class="vp__row" :class="{ 'vp__row--on': choice.id === current }">
          <button
            class="vp__pick"
            type="button"
            :disabled="picking !== ''"
            :title="choice.id === current ? '正在用的就是这个' : '换成这个声音'"
            @click="pick(choice.id)"
          >
            {{ choice.id === current ? '● 在用' : '○ 选用' }}
          </button>
          <span class="vp__label">{{ choice.label }}</span>
          <button
            class="hud-btn vp__try"
            type="button"
            :disabled="previewing === choice.id || busy"
            @click="preview(choice.id)"
          >
            {{ previewing === choice.id ? '合成中…' : '试听' }}
          </button>
          <button
            v-if="choice.kind === 'clone'"
            class="hud-btn vp__try"
            type="button"
            :disabled="removing !== ''"
            title="删掉这段录音做出来的音色"
            @click="remove(choice.id)"
          >
            {{ removing === choice.id ? '删除中…' : '删' }}
          </button>
        </li>
      </ul>

      <!--
        Making a voice out of the operator's own speech.

        The recording is held by Python, not by this page: on this machine the microphone
        belongs to the process that answers the wake word, so a browser getUserMedia here
        would either be refused or quietly steal audio from it. What crosses the bridge is
        state and two short strings -- never the audio.
      -->
      <section class="vp__clone">
        <h3 class="hud-label">录一段我的声音</h3>

        <p v-if="!rules" class="vp__why">读取中…</p>
        <p v-else-if="!rules.available" class="vp__why vp__why--bad">
          这台机器上做不出复刻音色：{{ rules.reason || '条件不满足' }}
        </p>
        <template v-else>
          <p class="vp__why">录音时请念下面这一句 —— 不用自己编，录完也不用填：</p>
          <p class="vp__readline">
            「{{ readLine }}」
            <button class="hud-btn" type="button" title="换一句念" @click="nextReadLine">
              换一句
            </button>
          </p>
          <p class="vp__why">
            对着麦克风念它，{{ seconds(rules.min_ms) }}–{{ seconds(rules.max_ms) }} 秒，
            {{ seconds(rules.comfortable_ms) }} 秒上下最稳。存下来之后它就出现在上面那张表里。
          </p>

          <div class="vp__rec">
            <button
              v-if="phase !== 'recording'"
              class="hud-btn hud-btn--primary"
              type="button"
              @click="start"
            >
              {{ phase === 'captured' ? '再录一段' : '开始录音' }}
            </button>
            <button v-else class="hud-btn" type="button" @click="stop">
              停止 · 已录 {{ (ms / 1000).toFixed(1) }} 秒
            </button>
            <span
              v-if="phase === 'recording'"
              class="vp__level"
              :title="'输入电平 ' + peak + ' / 32767'"
              ><i :style="{ width: levelPercent + '%' }"></i
            ></span>
          </div>
          <p v-if="phase === 'recording' && ms > 0 && peak === 0" class="vp__why vp__why--bad">
            到现在还没收到声音 —— 检查一下输入设备，别白录一段。
          </p>
          <p
            v-if="phase === 'recording' && targetMs > 0 && ms >= targetMs"
            class="vp__why"
            title="到点只是提示，不停下来：把你说到一半的那句掐掉比多说几秒贵得多"
          >
            够了，随时可以停。（不会自动掐你）
          </p>

          <template v-if="phase === 'captured'">
            <label class="vp__field">
              <span class="hud-label">给这个音色起个名</span>
              <input v-model.trim="name" type="text" maxlength="24" placeholder="如：我的声音" />
            </label>
            <p class="vp__why">
              对齐用的就是上面那句「{{ readLine }}」—— 复刻拿录音和这句话对齐，
              所以念的时候别加词别改口。
            </p>
            <label class="vp__upload">
              <input v-model="uploadCloud" type="checkbox" />
              <span>把这段录音传到厂商做云端复刻</span>
            </label>
            <p class="vp__why">
              不勾就只存在这台机器上 —— 本机跑得动离线复刻模型才说得出你的声音。
              勾上才立刻能用，但这段录音会离开这台机器（要 DASHSCOPE_API_KEY）。
            </p>
            <div class="vp__rec-ops">
              <button
                class="hud-btn hud-btn--primary"
                type="button"
                :disabled="saving || !canSave"
                :title="canSave ? '把这段录音存成一个音色' : '这段还不够长或太安静，存了也不会像你'"
                @click="save"
              >
                {{ saving ? '存入中…' : '存成音色' }}
              </button>
              <button class="hud-btn" type="button" @click="discard">放弃这段</button>
            </div>
          </template>
        </template>
        <p v-if="sampleError" class="vp__note-line vp__note-line--bad">{{ sampleError }}</p>
      </section>

      <p v-if="note" class="vp__note-line" :class="{ 'vp__note-line--bad': noteBad }">{{ note }}</p>

      <!--
        Two promises the panel has to keep in writing: picking takes effect on the
        *next* sentence (the engine is asked per utterance, not rebuilt), and
        previewing never selects -- hearing the alternative is not committing to it.
      -->
      <p class="vp__note">
        选用后从下一句朗读开始生效；试听只播给你听，不会改变当前选择。
        语速和音量同样从下一句开始生效，「试听这一档」听的就是它们。
      </p>
    </section>
  </div>
</template>

<script setup lang="ts">
/**
 * The voice picker: which of the synthesis voices answers, heard before chosen.
 *
 * Choosing a voice by reading its id is choosing blind; the 试听 column exists so
 * the decision is made with ears. Previews travel the same audio channel as real
 * answers, so what you hear in the popup is what you will hear in conversation --
 * not a second, slightly different playback path.
 */
import { computed, ref, watch } from 'vue'
import {
  discardVoiceSample,
  fetchVoices,
  pickVoice,
  previewVoice,
  removeVoiceClone,
  saveVoiceSample,
  setVoiceStyle,
  startVoiceSample,
  stopVoiceSample,
  voiceSampleStatus,
  type CloningState,
  type SampleStatus,
  type VoiceChoice,
} from '@/api/bridge'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const choices = ref<VoiceChoice[]>([])
const current = ref('')
const error = ref('')
const note = ref('')
const noteBad = ref(false)
const loading = ref(false)
const picking = ref('')
const previewing = ref('')

// Rate and volume, with the ranges the backend offers. Kept in one place so a slider
// cannot promise a value the engine will clamp.
const speed = ref(1)
const volume = ref(1)
const speedMin = ref(0.5)
const speedMax = ref(1.5)
const volumeMin = ref(0)
const volumeMax = ref(1)

const busy = ref(false)

// The recording half: what the shell allows, and where this take stands right now.
const rules = ref<CloningState | null>(null)
const phase = ref('idle')
const ms = ref(0)
const peak = ref(0)
const canSave = ref(false)
const targetMs = ref(0)
const uploadCloud = ref(false)
const sampleError = ref('')
const name = ref('')
/**
 * The sentences a recording may read. Fixed on purpose: asking people to type what
 * they just said produced wrong transcripts, and a wrong transcript is not a note --
 * the clone aligns the audio against it, so the voice came out not-quite-theirs.
 */
const READ_LINES = [
  '今天天气不错，我们一起去公园走走吧。',
  '我把那本书放在桌子上了，你看见了吗？',
  '晚上想吃点热的，汤面或者粥都行。',
]
const readLineIndex = ref(0)
const readLine = computed(() => READ_LINES[readLineIndex.value % READ_LINES.length])
function nextReadLine(): void {
  readLineIndex.value = (readLineIndex.value + 1) % READ_LINES.length
}
const saving = ref(false)
const removing = ref('')
let poll: ReturnType<typeof setInterval> | undefined

/** 3000 -> "3 秒". The rules arrive in milliseconds; the sentence is read aloud. */
function seconds(value: number): string {
  return (value / 1000).toFixed(value % 1000 === 0 ? 0 : 1)
}

/** How much of the bar the level has filled. A number, not a colour, so it works
 *  on any skin and does not depend on anybody noticing a subtle hue change. */
const levelPercent = computed(() => {
  if (!peak.value) return 0
  return Math.max(4, Math.min(100, Math.round((peak.value / 6000) * 100)))
})

function stopPolling(): void {
  if (poll !== undefined) {
    window.clearInterval(poll)
    poll = undefined
  }
}

/** Copy one sampler state onto the page. Every verb answers with the whole state, so the
 *  rules about what the buttons may do live in one place instead of five handlers. */
function applyState(state: SampleStatus): void {
  phase.value = state.phase
  ms.value = state.ms
  peak.value = state.peak
  canSave.value = state.can_save === true
  if (state.target_ms !== undefined) targetMs.value = state.target_ms
  if (state.error) sampleError.value = state.error
}

async function refreshSample(): Promise<void> {
  try {
    const state = await voiceSampleStatus()
    applyState(state)
    if (state.phase !== 'recording') stopPolling()
  } catch (err) {
    stopPolling()
    sampleError.value = err instanceof Error ? err.message : String(err)
  }
}

async function start(): Promise<void> {
  sampleError.value = ''
  try {
    const state = await startVoiceSample()
    applyState(state)
    if (state.phase === 'recording') {
      stopPolling()
      poll = window.setInterval(() => void refreshSample(), 200)
    }
  } catch (err) {
    sampleError.value = err instanceof Error ? err.message : String(err)
  }
}

async function stop(): Promise<void> {
  stopPolling()
  try {
    applyState(await stopVoiceSample())
  } catch (err) {
    sampleError.value = err instanceof Error ? err.message : String(err)
  }
}

async function discard(): Promise<void> {
  stopPolling()
  name.value = ''
  uploadCloud.value = false
  sampleError.value = ''
  try {
    applyState(await discardVoiceSample())
  } catch (err) {
    sampleError.value = err instanceof Error ? err.message : String(err)
  }
}

async function save(): Promise<void> {
  saving.value = true
  sampleError.value = ''
  const wantsCloud = uploadCloud.value
  try {
    const state = await saveVoiceSample(name.value, readLine.value, wantsCloud)
    if (!state.ok) {
      applyState(state)
      if (!state.error) sampleError.value = '没能存下这段录音'
      return
    }
    applyState(state)
    name.value = ''
      uploadCloud.value = false
    if (state.uploaded) {
      note.value = '已存成音色，并传到云端 —— 下一句朗读开始用你的声音'
      noteBad.value = false
    } else if (state.cloud_error) {
      // The recording is safe on this machine; only the disclosure half failed.
      note.value = '录音已经存在本机，云端那步没成：' + state.cloud_error
      noteBad.value = true
    } else {
      note.value = '已经存成你的音色（只在这台机器上），在上面那张表里选它'
      noteBad.value = false
    }
    await load()
  } catch (err) {
    sampleError.value = err instanceof Error ? err.message : String(err)
  } finally {
    saving.value = false
  }
}

async function remove(voice: string): Promise<void> {
  if (!window.confirm('删掉这个用你的录音做出来的音色？音频文件会一起删。')) return
  removing.value = voice
  try {
    const answer = await removeVoiceClone(voice)
    if (answer.error) {
      note.value = answer.error
      noteBad.value = true
    } else {
      note.value = '已删掉那个音色（包括它的录音文件）'
      noteBad.value = false
    }
    await load()
  } catch (err) {
    note.value = err instanceof Error ? err.message : String(err)
    noteBad.value = true
  } finally {
    removing.value = ''
  }
}

function absorb(list: {
  choices?: VoiceChoice[]
  current?: string
  speed?: number
  volume?: number
  speed_min?: number
  speed_max?: number
  volume_min?: number
  volume_max?: number
  cloning?: CloningState
}): void {
  if (list.choices) choices.value = list.choices
  if (list.current !== undefined) current.value = list.current
  if (list.speed !== undefined) speed.value = list.speed
  if (list.volume !== undefined) volume.value = list.volume
  if (list.speed_min !== undefined) speedMin.value = list.speed_min
  if (list.speed_max !== undefined) speedMax.value = list.speed_max
  if (list.volume_min !== undefined) volumeMin.value = list.volume_min
  if (list.volume_max !== undefined) volumeMax.value = list.volume_max
  if (list.cloning) rules.value = list.cloning
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const list = await fetchVoices()
    absorb(list)
    error.value = list.error
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function pick(voice: string): Promise<void> {
  picking.value = voice
  note.value = ''
  try {
    const list = await pickVoice(voice)
    absorb(list)
    note.value = list.error || `已切到 ${voice}，下一句朗读开始用它`
    noteBad.value = Boolean(list.error)
  } catch (err) {
    note.value = err instanceof Error ? err.message : String(err)
    noteBad.value = true
  } finally {
    picking.value = ''
  }
}

const PREVIEW_WATCHDOG_MS = 75_000

async function preview(voice: string): Promise<void> {
  if (voice === '') return
  previewing.value = voice
  note.value = ''
  // The bridge call has its own ceiling on the Python side, but a bridge that never
  // answers would pin this button at 合成中 forever -- which is exactly what the
  // 2026-10-05 screenshot showed. The watchdog gives the operator their button back
  // and says so, instead of letting them wait out a fifteen-minute timeout.
  const watchdog = window.setTimeout(() => {
    if (previewing.value === voice) {
      previewing.value = ''
      noteBad.value = true
      note.value = '试听 75 秒没回音，先不等了；后台若还在算，稍后再点一次试听'
    }
  }, PREVIEW_WATCHDOG_MS)
  try {
    const result = await previewVoice(voice)
    if (!result.ok) {
      note.value = result.error
      noteBad.value = true
    }
  } catch (err) {
    note.value = err instanceof Error ? err.message : String(err)
    noteBad.value = true
  } finally {
    window.clearTimeout(watchdog)
    previewing.value = ''
  }
}

async function applyStyle(): Promise<void> {
  busy.value = true
  note.value = ''
  try {
    const list = await setVoiceStyle(speed.value, volume.value)
    absorb(list)
    note.value = list.error || '已保存：下一句朗读开始用这个语速和音量'
    noteBad.value = Boolean(list.error)
  } catch (err) {
    note.value = err instanceof Error ? err.message : String(err)
    noteBad.value = true
  } finally {
    busy.value = false
  }
}

watch(
  () => props.open,
  (shown) => {
    if (shown) {
      void load()
      // A take in flight survives closing the dialog -- Python is still holding it -- so
      // reopen by asking where it got to rather than pretending it never started.
      void refreshSample()
    } else {
      // Polling a closed dialog would keep crossing the bridge for a page that is not
      // looking, and a timer behind a hidden window is how this project has already been
      // billed a phantom animation.
      stopPolling()
    }
  },
)
</script>

<style scoped>
.vp {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
}

.vp__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.vp__box {
  position: relative;
  width: min(560px, 94vw);
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  padding: 16px 20px 14px;
  border: 1px solid var(--hud-line);
  border-top: 1px solid rgba(77, 216, 255, 0.45);
  border-radius: var(--hud-radius);
  background: linear-gradient(180deg, rgba(10, 24, 40, 0.96), rgba(4, 8, 14, 0.98));
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}

.vp__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.vp__error,
.vp__wait {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.7;
}

.vp__error {
  color: var(--hud-red);
}

.vp__wait {
  color: var(--hud-dim);
}

.vp__list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.vp__style {
  display: grid;
  grid-template-columns: 1fr 1fr auto;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--hud-line);
}

.vp__slider {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 11px;
  color: var(--hud-dim);
}

.vp__slider-name {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
}

.vp__slider input[type='range'] {
  width: 100%;
  accent-color: var(--hud-cyan);
  cursor: pointer;
}

.vp__slider input[type='range']:disabled {
  cursor: default;
  opacity: 0.5;
}

.vp__row {
  display: grid;
  grid-template-columns: 64px 1fr 64px;
  align-items: center;
  gap: 10px;
  padding: 5px 8px;
  border: 1px solid transparent;
  border-radius: var(--hud-radius);
  font-size: 12px;
}

.vp__row--on {
  border-color: rgba(77, 216, 255, 0.4);
  background: rgba(77, 216, 255, 0.07);
}

.vp__pick {
  padding: 2px 6px;
  border: none;
  background: transparent;
  color: var(--hud-dim);
  font-size: 11px;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
}

.vp__row--on .vp__pick {
  color: var(--hud-cyan);
}

.vp__pick:hover {
  color: var(--hud-cyan);
}

.vp__label {
  color: var(--hud-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.vp__try {
  padding: 2px 8px;
  font-size: 11px;
}

.vp__note-line {
  margin: 8px 0 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--hud-green);
}

.vp__note-line--bad {
  color: var(--hud-red);
}

.vp__note {
  margin: 8px 0 0;
  padding-top: 8px;
  border-top: 1px solid var(--hud-line);
  font-size: 10px;
  line-height: 1.6;
  color: var(--hud-dim);
}

.vp__readline {
  margin: 6px 0 2px;
  font-size: 15px;
  color: var(--hud-text, #dcefff);
}

.vp__readline .hud-btn {
  margin-left: 8px;
  vertical-align: 2px;
}

/* --- 录一段我的声音 --- */
.vp__clone {
  margin-top: 14px;
  padding-top: 10px;
  border-top: 1px dashed rgba(120, 205, 245, 0.22);
}

.vp__why {
  margin: 6px 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: rgba(196, 226, 244, 0.72);
}

.vp__why--bad {
  color: rgba(255, 138, 138, 0.95);
}

.vp__rec {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}

/* The level is a bar, not a colour: it has to read the same on every skin. It is a track
   the fill lives inside, because a bar whose own width is the percentage sat next to a
   180px button and ran 66px past the edge of this box in the first browser walk. */
.vp__level {
  flex: 1 1 auto;
  min-width: 0;
  height: 6px;
  border-radius: 3px;
  overflow: hidden;
  background: rgba(77, 216, 255, 0.12);
}

.vp__level i {
  display: block;
  height: 100%;
  min-width: 2px;
  border-radius: 3px;
  background: linear-gradient(90deg, rgba(77, 216, 255, 0.85), rgba(77, 216, 255, 0.25));
  transition: width 180ms linear;
}

.vp__rec > button {
  flex: 0 0 auto;
}

.vp__field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 8px;
}

.vp__field input {
  height: var(--hud-control-h);
  padding: 0 10px;
}

.vp__rec-ops {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

/* The disclosure is a checkbox the operator has to look at, not a switch remembered from
   the last recording: it is off every time, and the sentence next to it says what ticking
   it sends away. */
.vp__upload {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  font-size: 11.5px;
  color: rgba(196, 226, 244, 0.86);
  cursor: pointer;
}

.vp__upload input {
  width: 14px;
  height: 14px;
  accent-color: var(--hud-cyan);
}
</style>

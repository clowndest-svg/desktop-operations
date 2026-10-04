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
        </li>
      </ul>

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
import { ref, watch } from 'vue'
import {
  fetchVoices,
  pickVoice,
  previewVoice,
  setVoiceStyle,
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

function absorb(list: {
  choices?: VoiceChoice[]
  current?: string
  speed?: number
  volume?: number
  speed_min?: number
  speed_max?: number
  volume_min?: number
  volume_max?: number
}): void {
  if (list.choices) choices.value = list.choices
  if (list.current !== undefined) current.value = list.current
  if (list.speed !== undefined) speed.value = list.speed
  if (list.volume !== undefined) volume.value = list.volume
  if (list.speed_min !== undefined) speedMin.value = list.speed_min
  if (list.speed_max !== undefined) speedMax.value = list.speed_max
  if (list.volume_min !== undefined) volumeMin.value = list.volume_min
  if (list.volume_max !== undefined) volumeMax.value = list.volume_max
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

async function preview(voice: string): Promise<void> {
  if (voice === '') return
  previewing.value = voice
  note.value = ''
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
    if (shown) void load()
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
</style>

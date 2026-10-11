<template>
  <Transition name="im-anim">
    <div v-if="open" class="im" role="dialog" aria-modal="true" aria-label="生图">
      <div class="im__scrim" @click="emit('close')"></div>

      <section class="im__box">
        <header class="im__head">
          <h2 class="hud-title">生图 · 本地 ComfyUI 或云端 API</h2>
          <span class="im__phase" :class="phaseClass">{{ phaseWord }}</span>
          <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
        </header>

        <div class="im__body">
          <p v-if="state.detail" class="im__note" :class="{ 'im__note--bad': isBad }">{{ state.detail }}</p>
          <!-- 后端那句「没能照着做」必须画出来：只回在返回值里，1.5 秒后就被下一次轮询擦掉，
               用户看到的就是一个转完圈然后什么都没发生的框。 -->
          <p v-if="state.error" class="im__error">{{ state.error }}</p>

          <!-- 引擎：这一格决定去哪张卡上画 -->
          <div class="im__section">
            <span class="hud-label">用哪条引擎</span>
            <select v-model="engineChoice" class="hud-field" :disabled="busy" @change="tune">
              <option v-for="row in state.engines" :key="row.id" :value="row.id">{{ row.label }}</option>
            </select>
            <p class="im__why">{{ engineNote }}</p>
          </div>

          <!-- 画什么 -->
          <div class="im__section">
            <label class="im__row">
              <span class="hud-label">画什么</span>
              <textarea v-model="prompt" class="hud-field im__prompt" rows="3" placeholder="一只在月光下的橘猫，插画风，柔光"></textarea>
            </label>
            <div class="im__row im__row--inline">
              <span class="hud-label">尺寸</span>
              <select v-model="sizeChoice" class="hud-field" :disabled="busy" @change="tune">
                <option v-for="row in state.sizes" :key="row.size" :value="row.size">{{ row.size }}</option>
              </select>
              <span class="hud-label">步数</span>
              <input v-model.number="steps" class="hud-field im__steps" type="number" min="4" max="60" :disabled="busy" @change="tune" />
            </div>
            <p class="im__why">{{ sizeNote }}</p>
            <p class="im__why">步数是采样次数，多一步就慢一点；本机那张卡实测 512x512 二十步约 22 秒。</p>
            <div class="im__actions">
              <button class="hud-btn hud-btn--primary" type="button" :disabled="busy || !prompt.trim()" @click="generate">
                {{ busy ? '正在画…' : '画一张' }}
              </button>
              <button class="hud-btn" type="button" :disabled="busy" @click="reprobe">检测本机引擎</button>
            </div>
            <p v-if="busy" class="im__note">本机画图是真的要等几十秒，别关这个框 —— 画完会自己出现在下面。</p>
          </div>

          <!-- 上一次那张 -->
          <div class="im__section">
            <span class="hud-label">刚画的那张</span>
            <div v-if="lastImage" class="im__result">
              <img class="im__pic" :src="lastImage" alt="刚生成的图" />
              <p class="im__file">{{ lastPath }}</p>
              <p class="im__why">{{ lastWords }}</p>
            </div>
            <p v-else class="im__empty">还没画过。上面那句写完按「画一张」。</p>
          </div>

          <!-- 引擎的来历 -->
          <div class="im__section">
            <span class="hud-label">本机引擎</span>
            <p v-if="state.local" class="im__why" :class="{ 'im__error': !state.local.ok }">
              {{ state.local.ok ? state.local.detail : state.local.detail || '没找到' }}
            </p>
            <p v-else class="im__why">还没检测过。按上面「检测本机引擎」问一次 —— 它只在那时候发网络请求，平时这个框每 1.5 秒读的是内存里的状态。</p>
            <label class="im__row">
              <span class="hud-label">地址</span>
              <input v-model="comfyUrl" class="hud-field" type="text" placeholder="留空＝自动找 127.0.0.1:8188 这些端口" :disabled="busy" />
            </label>
            <label class="im__row">
              <span class="hud-label">ComfyUI 目录</span>
              <input v-model="comfyDir" class="hud-field" type="text" placeholder="留空＝本软件不起引擎，只连你开好的那个" :disabled="busy" />
            </label>
            <p class="im__why">
              填了目录，本机没开引擎时它才替你起一个。<strong>显存只有一份</strong>：那张 4 GB 的卡上已经有 ComfyUI 在跑时再起一个，
              第二个会崩在装模型那一步（这是实测过的，不是猜的），所以默认不替你起。
            </p>
          </div>

          <!-- 云端 -->
          <div class="im__section">
            <span class="hud-label">云端 API</span>
            <label class="im__row">
              <span class="hud-label">服务地址</span>
              <input v-model="cloudBase" class="hud-field" type="text" placeholder="https://…/v1（OpenAI 兼容的那个基础地址）" :disabled="busy" />
            </label>
            <label class="im__row">
              <span class="hud-label">模型名</span>
              <input v-model="cloudModel" class="hud-field" type="text" placeholder="留空＝用那家默认的" :disabled="busy" />
            </label>
            <div class="im__row im__row--pair">
              <input v-model="cloudKey" class="hud-field" type="password" autocomplete="off" placeholder="钥匙（只写进环境变量，界面不回显）" :disabled="busy" />
              <button class="hud-btn" type="button" :disabled="busy || !cloudKey.trim()" @click="saveKey">存这把</button>
            </div>
            <p class="im__why">
              钥匙状态：{{ state.cloud_key_set ? '已设置' : '未设置' }}。填了地址没填钥匙 = 云端这一条走不了。
              这一格不发图 —— 它只把钥匙交给设置那一层，配置文件里一个字节都不留。
            </p>
            <div class="im__actions">
              <button class="hud-btn" type="button" :disabled="busy" @click="saveSetup">存这些设置</button>
            </div>
          </div>

          <div v-if="state.tail.length" class="im__section">
            <span class="hud-label">引擎最近说的几行</span>
            <pre class="im__tail">{{ state.tail.slice(-6).join('\n') }}</pre>
          </div>
        </div>
      </section>
    </div>
  </Transition>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import {
  fetchImageState,
  generateImage,
  probeImageEngine,
  saveSettings,
  setupImage,
  tuneImage,
  type ImageState,
} from '@/api/bridge'

const empty: ImageState = {
  engine: 'auto',
  engines: [],
  sizes: [],
  size: '512x512',
  steps: 20,
  comfy_url: '',
  comfy_dir: '',
  cloud_base: '',
  cloud_model: '',
  cloud_key_set: false,
  can_start_local: false,
  vendored_engine: false,
  local: null,
  phase: 'off',
  detail: '',
  last_path: '',
  last_seconds: 0,
  tail: [],
  error: '',
}

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'changed', label: string): void
}>()

const state = reactive<ImageState>({ ...empty })
const prompt = ref('')
const busy = ref(false)
const engineChoice = ref('auto')
const sizeChoice = ref('512x512')
const steps = ref(20)
const comfyUrl = ref('')
const comfyDir = ref('')
const cloudBase = ref('')
const cloudModel = ref('')
const cloudKey = ref('')
const lastImage = ref('')
const lastPath = ref('')
const lastWords = ref('')

const WORDS: Record<string, string> = {
  off: '没在用',
  starting: '在起引擎',
  running: '引擎在画',
  idle: '就绪',
  failed: '失败',
}

const phaseWord = computed(() => WORDS[state.phase] ?? state.phase)
const isBad = computed(() => state.phase === 'failed' || !!state.error)
const phaseClass = computed(() => {
  if (state.phase === 'idle' || state.phase === 'running') return 'im__phase--on'
  if (state.phase === 'failed') return 'im__phase--bad'
  return ''
})
const engineNote = computed(() => state.engines.find((row) => row.id === engineChoice.value)?.note ?? '')
const sizeNote = computed(() => state.sizes.find((row) => row.size === sizeChoice.value)?.note ?? '')

function apply(next: ImageState): void {
  Object.assign(state, next)
  engineChoice.value = next.engine
  sizeChoice.value = next.size
  steps.value = next.steps
  comfyUrl.value = next.comfy_url
  comfyDir.value = next.comfy_dir
  cloudBase.value = next.cloud_base
  cloudModel.value = next.cloud_model
}

async function refresh(): Promise<void> {
  try {
    apply(await fetchImageState())
  } catch (err) {
    state.error = err instanceof Error ? err.message : String(err)
  }
}

async function tune(): Promise<void> {
  busy.value = true
  try {
    apply(await tuneImage(engineChoice.value, sizeChoice.value, Number(steps.value) || 20))
  } finally {
    busy.value = false
  }
}

async function reprobe(): Promise<void> {
  busy.value = true
  try {
    apply(await probeImageEngine())
    emit('changed', state.local?.ok ? '找到本机引擎' : '本机没有引擎，走云端')
  } finally {
    busy.value = false
  }
}

async function generate(): Promise<void> {
  const text = prompt.value.trim()
  if (!text) return
  busy.value = true
  try {
    const answer = await generateImage(text, sizeChoice.value)
    if (answer.ok) {
      lastImage.value = answer.preview
      lastPath.value = answer.path
      lastWords.value = `${answer.size} · ${answer.steps} 步 · ${answer.engine === 'local' ? '本机 ComfyUI' : '云端 API'} · ${answer.seconds} 秒 · 种子 ${answer.seed}`
      emit('changed', '画好了')
    } else {
      emit('changed', answer.error || '这一张没画成')
    }
    apply(await fetchImageState())
  } catch (err) {
    state.error = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

async function saveSetup(): Promise<void> {
  busy.value = true
  try {
    apply(
      await setupImage(comfyUrl.value, comfyDir.value, cloudBase.value, cloudModel.value),
    )
    emit('changed', state.error ? state.error : '引擎设置已存')
  } finally {
    busy.value = false
  }
}

async function saveKey(): Promise<void> {
  const value = cloudKey.value
  if (!value.trim()) return
  busy.value = true
  try {
    const result = await saveSettings({ api_key: value, key_for: 'image' })
    const problems = result.problems as Record<string, string> | undefined
    state.error = problems?.api_key ?? ''
    cloudKey.value = ''
    apply(await fetchImageState())
    emit('changed', state.error ? state.error : '钥匙已存进环境变量')
  } catch (err) {
    state.error = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

let poll: ReturnType<typeof setInterval> | undefined

watch(
  () => props.open,
  (opened) => {
    if (poll) {
      clearInterval(poll)
      poll = undefined
    }
    if (!opened) return
    void refresh()
    // 只读内存里那份状态：真去问引擎是「检测本机引擎」那颗按钮的事。
    poll = setInterval(() => void refresh(), 1500)
  },
)
</script>

<style scoped>
.im {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: flex;
  align-items: center;
  justify-content: center;
}

.im__scrim {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
}

.im__box {
  position: relative;
  width: min(760px, 92vw);
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--hud-line, #3a3f4b);
  border-radius: 14px;
  background: var(--hud-panel, #171a21);
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.45);
}

.im__head {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid var(--hud-line, #3a3f4b);
}

.im__head .hud-title {
  flex: 1;
  margin: 0;
  font-size: 14px;
}

.im__phase {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  background: rgba(255, 255, 255, 0.08);
}

.im__phase--on {
  background: rgba(74, 222, 128, 0.18);
}

.im__phase--bad {
  background: rgba(248, 113, 113, 0.2);
}

.im__body {
  padding: 14px 16px 18px;
  overflow-y: auto;
}

.im__section {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 0;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}

.im__row {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.im__row--inline {
  flex-direction: row;
  align-items: center;
  gap: 10px;
}

.im__row--pair {
  flex-direction: row;
  gap: 8px;
}

.im__prompt {
  resize: vertical;
}

.im__steps {
  width: 78px;
}

.im__actions {
  display: flex;
  gap: 8px;
}

.im__why {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--hud-muted, #9aa3b2);
}

.im__note {
  margin: 0;
  font-size: 12px;
  color: var(--hud-muted, #9aa3b2);
}

.im__note--bad {
  color: #f87171;
}

.im__error {
  margin: 0;
  font-size: 12px;
  color: #f87171;
}

.im__empty {
  margin: 0;
  font-size: 12px;
  color: var(--hud-muted, #9aa3b2);
}

.im__result {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.im__pic {
  max-width: 320px;
  border-radius: 10px;
}

.im__file {
  margin: 0;
  font-size: 12px;
  word-break: break-all;
  color: var(--hud-text, #e7ebf3);
}

.im__tail {
  margin: 0;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.35);
  font-size: 11px;
  white-space: pre-wrap;
}

.im-anim-enter-active,
.im-anim-leave-active {
  transition: opacity 0.16s ease;
}

.im-anim-enter-from,
.im-anim-leave-to {
  opacity: 0;
}
</style>

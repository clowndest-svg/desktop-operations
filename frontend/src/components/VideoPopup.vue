<template>
  <div v-if="open" class="vp" role="dialog" aria-modal="true" aria-label="视频生成">
    <div class="vp__scrim" @click="emit('close')"></div>
    <section class="vp__box">
      <header class="vp__head">
        <h2 class="hud-title">视频生成 · 本地 AnimateDiff / 云端 API</h2>
        <span class="vp__phase" :class="phaseClass">{{ phaseWord }}</span>
        <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
      </header>

      <div class="vp__body">
        <p v-if="error || state.error" class="vp__error" role="alert">{{ error || state.error }}</p>

        <div class="vp__section">
          <span class="hud-label">用哪条引擎</span>
          <div class="vp__engines">
            <button
              v-for="choice in engineChoices"
              :key="choice.id"
              class="hud-btn vp__engine"
              :class="{ 'vp__engine--on': engine === choice.id }"
              type="button"
              :disabled="busy"
              @click="pickEngine(choice.id)"
            >
              {{ choice.label }}
            </button>
          </div>
          <p class="vp__why">{{ engineNote }}</p>
          <p v-if="engine === 'cloud' && !cloudReady" class="vp__why vp__why--warn">云端还没配齐：下面「云端 API 配置」里要有建任务地址、视频地址取法，钥匙所在的环境变量 <code>{{ cloud.key_env || 'DASHSCOPE_API_KEY' }}</code> 也得设好。配好之前生成会拒绝，不会假装成功。</p>
        </div>

        <div class="vp__section">
          <label class="vp__row" for="video-prompt"><span class="hud-label">画什么</span></label>
          <textarea id="video-prompt" v-model="prompt" class="hud-field vp__prompt" rows="6" placeholder="一只戴着蓝色围巾的橘猫在雪地里跑步，镜头缓缓推近" :disabled="busy"></textarea>
          <p class="vp__why">
            {{ engine === 'cloud'
              ? '云端多半原生支持中文，这段原话会直接发给服务商、不翻成英文。'
              : '本地那条会整句翻成英文（SD1.5 只认英文标签）。翻译需要联网，视频在本机生成，和生图共用同一张显卡。' }}
          </p>
        </div>

        <div class="vp__section">
          <span class="hud-label">分辨率 / 帧率 / 时长 / 步数</span>
          <div class="vp__grid">
            <label class="vp__field"><span>分辨率</span>
              <select v-model="sizeChoice" class="hud-field" :disabled="busy" @change="tune">
                <option value="">自动（按本机）</option>
                <option v-for="s in options.sizes" :key="s" :value="s">{{ s }}</option>
              </select>
            </label>
            <label class="vp__field"><span>帧率</span>
              <select v-model.number="fpsChoice" class="hud-field" :disabled="busy" @change="tune">
                <option :value="0">自动（按本机）</option>
                <option v-for="f in options.fpss" :key="f" :value="f">{{ f }} 帧/秒</option>
              </select>
            </label>
            <label class="vp__field"><span>时长</span>
              <select v-model.number="secondsChoice" class="hud-field" :disabled="busy" @change="tune">
                <option :value="0">自动（短视频）</option>
                <option v-for="d in options.seconds" :key="d" :value="d">{{ d }} 秒</option>
              </select>
            </label>
            <label class="vp__field"><span>步数</span>
              <input v-model.number="stepsChoice" class="hud-field" type="number" min="12" max="30" :disabled="busy" @change="tune" />
            </label>
          </div>
          <p v-if="engine !== 'cloud'" class="vp__why">默认全部走「自动」—— 它按本机显卡显存给一档短视频（4 GB 卡＝256×256·8 帧/秒·2 秒，r113 实测形状）。想更长就把时长调大；超出显存能装下的帧数会被截到上限，并在下面写明截到了多少。</p>
          <p v-else class="vp__why">云端这几档按服务商自己的上限走，软件不按本机显存截它；能不能 Longer/更大取决于你填的那家。</p>
          <template v-if="engine !== 'cloud'">
            <p v-if="plan" class="vp__plan">
              这一炉实际会画：<strong>{{ plan.size }}</strong> · {{ plan.fps }} 帧/秒 · {{ plan.frames }} 帧（约 {{ secondsOf(plan) }} 秒）· {{ plan.steps }} 步
              <span v-if="!plan.explicit">（自动档）</span>
            </p>
            <p v-if="plan && plan.capped" class="vp__why vp__why--warn">{{ plan.note }}</p>
            <p v-else-if="plan && !plan.vram_total_mb" class="vp__why vp__why--warn">还没检测过本机引擎，上限按 4 GB 保守估。按「检测本机引擎」拿真实显存。</p>
          </template>
          <div class="vp__actions">
            <button class="hud-btn hud-btn--primary" type="button" :disabled="busy || !prompt.trim()" @click="generate">{{ busy ? `正在生成 · ${elapsed} 秒` : (engine === 'cloud' ? '云端生成视频' : '生成视频') }}</button>
            <button v-if="engine !== 'cloud'" class="hud-btn" type="button" :disabled="busy" @click="reprobe">检测本机引擎</button>
          </div>
          <p v-if="busy" class="vp__why">{{ state.detail || '正在提交…' }} 关闭弹窗后任务仍继续。</p>
        </div>

        <details class="vp__section vp__cloud">
          <summary class="hud-label">云端 API 配置（多家可自填 · 默认通义万相）</summary>
          <p class="vp__why">视频没有像生图那样的通用协议：可灵 / 即梦 / 通义万相 / Vidu 各家都是「提交任务 → 轮询 → 拿回下载地址」，字段名各不一样。</p>
          <p class="vp__why">这里把每个接口形状摊成能填的格子，默认值按阿里通义万相预置（复用项目里已有的 <code>DASHSCOPE_API_KEY</code>，是能直接跑通的一家）。换成别家，照着它的文档逐格改即可。</p>
          <p class="vp__why">钥匙的<strong>值</strong>永远只待在你设的环境变量里，软件只记「住哪个变量」这个名字，不落盘、不上传。</p>
          <p class="vp__why vp__why--warn">诚实边界：这条链路验到契约层（用离线假端点跑通了提交→轮询→下载全流程），但通义万相只是按它公开的异步契约预置成默认档，本轮没有可用的 DASHSCOPE 密钥、没拿真账号跑过一次真视频；配好钥匙请先自己生成一次确认，别家更是要你照它文档填。</p>

          <div class="vp__cloud-grid">
            <label class="vp__field vp__field--wide"><span>建任务地址（POST）</span>
              <input v-model="cloud.submit_url" class="hud-field" type="text" :placeholder="defaults.submit_url" :disabled="busy" />
            </label>
            <label class="vp__field"><span>钥匙环境变量名</span>
              <input v-model="cloud.key_env" class="hud-field" type="text" :placeholder="defaults.key_env" :disabled="busy" />
              <span class="vp__hint" :class="{ 'vp__hint--ok': cloud.key_set }">{{ cloud.key_set ? '这个变量已设' : '这个变量还没设' }}</span>
            </label>
            <label class="vp__field"><span v-pre>模型 id（填进 {{model}}）</span>
              <input v-model="cloud.model" class="hud-field" type="text" :placeholder="defaults.model" :disabled="busy" />
            </label>
            <label class="vp__field"><span>任务 id 取法</span>
              <input v-model="cloud.task_path" class="hud-field" type="text" :placeholder="defaults.task_path" :disabled="busy" />
            </label>
            <label class="vp__field vp__field--wide"><span v-pre>轮询地址（{{task_id}}；留空＝一次性返回）</span>
              <input v-model="cloud.poll_url" class="hud-field" type="text" :placeholder="defaults.poll_url" :disabled="busy" />
            </label>
            <label class="vp__field"><span>视频地址取法</span>
              <input v-model="cloud.video_path" class="hud-field" type="text" :placeholder="defaults.video_path" :disabled="busy" />
            </label>
            <label class="vp__field"><span>状态取法（可留空）</span>
              <input v-model="cloud.status_path" class="hud-field" type="text" :placeholder="defaults.status_path" :disabled="busy" />
            </label>
            <label class="vp__field"><span>成功值</span>
              <input v-model="cloud.succeeded" class="hud-field" type="text" :placeholder="defaults.succeeded" :disabled="busy" />
            </label>
            <label class="vp__field"><span>进行中值（逗号分隔）</span>
              <input v-model="cloud.running" class="hud-field" type="text" :placeholder="defaults.running" :disabled="busy" />
            </label>
            <label class="vp__field"><span>失败值（逗号分隔）</span>
              <input v-model="cloud.failed" class="hud-field" type="text" :placeholder="defaults.failed" :disabled="busy" />
            </label>
          </div>
          <label class="vp__field vp__field--wide"><span v-pre>请求体 JSON 模板（占位符按值插入，不要加引号：{{prompt}} {{model}} {{width}} {{height}} {{seconds}} {{fps}} {{frames}}）</span>
            <textarea v-model="cloud.body" class="hud-field vp__code" rows="3" :placeholder="defaults.body" :disabled="busy"></textarea>
          </label>
          <label class="vp__field vp__field--wide"><span>额外请求头（JSON；鉴权 Bearer 软件自动加）</span>
            <textarea v-model="cloud.headers" class="hud-field vp__code" rows="2" :placeholder="defaults.headers" :disabled="busy"></textarea>
          </label>
          <div class="vp__actions">
            <button class="hud-btn" type="button" :disabled="busy" @click="saveCloud">保存云端配置</button>
            <button class="hud-btn" type="button" :disabled="busy" @click="resetCloud">恢复出厂默认</button>
          </div>
        </details>

        <div v-if="answer && answer.ok" class="vp__section">
          <span class="hud-label">刚生成的这段（{{ answer.engine === 'cloud' ? '云端' : '本地' }}）</span>
          <video v-if="videoUrl" :src="videoUrl" controls loop playsinline class="vp__video"></video>
          <p class="vp__why">原文：{{ answer.prompt_input }}</p>
          <p v-if="answer.engine !== 'cloud'" class="vp__why">实际英文：{{ answer.prompt }}</p>
          <p v-if="answer.prompt_note" class="vp__why">{{ answer.prompt_note }}</p>
          <p class="vp__why">{{ answer.size }} · {{ answer.fps }} 帧/秒 · {{ answer.duration }} 秒 · 生成用了 {{ answer.seconds }} 秒。</p>
          <p v-if="answer.capped" class="vp__why vp__why--warn">{{ answer.note }}</p>
          <div class="vp__actions">
            <button class="hud-btn" type="button" @click="openFile('open')">打开视频</button>
            <button class="hud-btn" type="button" @click="openFile('folder')">所在文件夹</button>
          </div>
          <p class="vp__path">{{ answer.path }}</p>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import {
  generateVideo,
  fetchVideoState,
  tuneVideo,
  probeVideoEngine,
  setVideoEngine,
  saveVideoCloud,
  resetVideoCloud,
  artifactVideo,
  openArtifact,
  type VideoAnswer,
  type VideoCloudConfig,
  type VideoPlan,
  type VideoState,
} from '@/api/bridge'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const emptyState: VideoState = {
  phase: 'idle',
  detail: '',
  error: '',
  last_path: '',
  last_seconds: 0,
}

const emptyCloud: VideoCloudConfig = {
  submit_url: '',
  key_env: '',
  model: '',
  body: '',
  headers: '',
  task_path: '',
  poll_url: '',
  video_path: '',
  status_path: '',
  succeeded: '',
  running: '',
  failed: '',
}

const prompt = ref('')
const busy = ref(false)
const error = ref('')
const answer = ref<VideoAnswer | null>(null)
const state = reactive<VideoState>({ ...emptyState })
const videoUrl = ref('')
const elapsed = ref(0)
const sizeChoice = ref('')
const fpsChoice = ref(0)
const secondsChoice = ref(0)
const stepsChoice = ref(0)
const options = reactive({
  sizes: [] as string[],
  fpss: [] as number[],
  seconds: [] as number[],
  engines: ['auto', 'local', 'cloud'] as string[],
})
const plan = ref<VideoPlan | null>(null)
const engine = ref('local')
const cloud = reactive<VideoCloudConfig>({ ...emptyCloud })
const defaults = reactive<VideoCloudConfig>({ ...emptyCloud })
let timer: ReturnType<typeof setInterval> | undefined

const ENGINE_LABELS: Record<string, string> = {
  auto: '自动（本地优先）',
  local: '只用本地 AnimateDiff',
  cloud: '只用云端 API',
}
const ENGINE_NOTES: Record<string, string> = {
  auto: '本机引擎连得上就走本地，连不上且云端配好才走云端。',
  local: '只用本机 AnimateDiff，不碰任何云端，也不需要钥匙。',
  cloud: '只用你在下面配置的云端服务商；本机显卡不参与。',
}
const engineChoices = computed(() =>
  (options.engines.length ? options.engines : ['auto', 'local', 'cloud']).map((id) => ({
    id,
    label: ENGINE_LABELS[id] ?? id,
  })),
)
const engineNote = computed(() => ENGINE_NOTES[engine.value] ?? '')
const cloudReady = computed(
  () => Boolean(cloud.submit_url.trim() && cloud.video_path.trim() && cloud.key_set),
)

const WORDS: Record<string, string> = {
  idle: '就绪',
  translating: '翻译中',
  generating: '在生成',
  ready: '就绪',
  failed: '失败',
}
const phaseWord = computed(() => WORDS[state.phase] ?? state.phase)
const phaseClass = computed(() => {
  if (state.phase === 'generating' || state.phase === 'translating') return 'vp__phase--on'
  if (state.phase === 'failed') return 'vp__phase--bad'
  return ''
})

function secondsOf(p: VideoPlan): number {
  return Math.round((p.frames / p.fps) * 10) / 10
}

function absorb(next: VideoState): void {
  Object.assign(state, next)
  if (next.plan) plan.value = next.plan
  if (next.engine) engine.value = next.engine
  if (next.options) {
    options.sizes = next.options.sizes
    options.fpss = next.options.fpss
    options.seconds = next.options.seconds
    if (next.options.engines) options.engines = next.options.engines
  }
  if (next.choice) {
    sizeChoice.value = next.choice.size
    fpsChoice.value = next.choice.fps
    secondsChoice.value = next.choice.seconds
    stepsChoice.value = next.choice.steps
  }
  if (next.cloud) Object.assign(cloud, next.cloud)
  if (next.cloud_defaults) Object.assign(defaults, next.cloud_defaults)
}

async function refresh(): Promise<void> {
  try {
    absorb(await fetchVideoState())
  } catch {
    /* 桥可能还在起 */
  }
}

async function tune(): Promise<void> {
  busy.value = true
  try {
    absorb(
      await tuneVideo(
        sizeChoice.value || null,
        Number(fpsChoice.value) || null,
        Number(secondsChoice.value) || null,
        Number(stepsChoice.value) || null,
      ),
    )
  } finally {
    busy.value = false
  }
}

async function pickEngine(id: string): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    absorb(await setVideoEngine(id))
  } finally {
    busy.value = false
  }
}

async function saveCloud(): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    const payload: Record<string, string> = {}
    for (const key of Object.keys(emptyCloud) as (keyof VideoCloudConfig)[]) {
      if (key === 'key_set') continue
      payload[key] = String(cloud[key] ?? '')
    }
    absorb(await saveVideoCloud(payload))
  } finally {
    busy.value = false
  }
}

async function resetCloud(): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    absorb(await resetVideoCloud())
  } finally {
    busy.value = false
  }
}

async function reprobe(): Promise<void> {
  busy.value = true
  try {
    absorb(await probeVideoEngine())
  } finally {
    busy.value = false
  }
}

async function generate(): Promise<void> {
  busy.value = true
  error.value = ''
  elapsed.value = 0
  const started = Date.now()
  timer = setInterval(() => {
    elapsed.value = Math.floor((Date.now() - started) / 1000)
    fetchVideoState()
      .then((s) => Object.assign(state, s))
      .catch(() => {})
  }, 1500)
  try {
    const result = await generateVideo(prompt.value, {
      size: sizeChoice.value || null,
      fps: Number(fpsChoice.value) || null,
      seconds: Number(secondsChoice.value) || null,
      steps: Number(stepsChoice.value) || null,
    })
    answer.value = result
    if (!result.ok) {
      error.value = result.error
      return
    }
    const preview = await artifactVideo(result.path)
    videoUrl.value = preview.data_url
    if (!preview.ok) error.value = preview.error
  } catch (e) {
    error.value = String(e)
  } finally {
    busy.value = false
    clearInterval(timer)
    timer = undefined
    refresh()
  }
}

async function openFile(action: 'open' | 'folder'): Promise<void> {
  if (!answer.value?.path) return
  const result = await openArtifact(answer.value.path, action)
  if (!result.ok) error.value = result.error
}

watch(
  () => props.open,
  (opened) => {
    if (opened) void refresh()
  },
)
onBeforeUnmount(() => clearInterval(timer))
</script>

<style scoped>
.vp {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: grid;
  place-items: center;
}
.vp__scrim {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
}
.vp__box {
  position: relative;
  width: min(760px, 92vw);
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--hud-edge, #285264);
  border-radius: 14px;
  background: var(--hud-panel, #171c23);
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.45);
  color: var(--hud-text, #e7ebf3);
}
.vp__head {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid var(--hud-line, #285264);
}
.vp__head .hud-title {
  flex: 1;
  margin: 0;
  font-size: 14px;
}
.vp__phase {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  background: rgba(255, 255, 255, 0.08);
}
.vp__phase--on {
  background: rgba(74, 222, 128, 0.18);
}
.vp__phase--bad {
  background: rgba(248, 113, 113, 0.2);
}
.vp__body {
  padding: 14px 16px 18px;
  overflow-y: auto;
}
.vp__section {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 0;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}
.vp__section:first-child {
  border-top: none;
}
.vp__engines {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.vp__engine {
  opacity: 0.7;
}
.vp__engine--on {
  opacity: 1;
  border-color: var(--hud-accent, #46c8ff);
  color: var(--hud-accent, #46c8ff);
}
.vp__prompt {
  width: 100%;
  min-height: 132px;
  box-sizing: border-box;
  resize: vertical;
  line-height: 1.6;
}
.vp__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.vp__cloud-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.vp__field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--hud-dim, #9aa3b2);
}
.vp__field--wide {
  grid-column: 1 / -1;
}
.vp__field .hud-field {
  width: 100%;
}
.vp__hint {
  font-size: 11px;
  color: var(--hud-muted, #9aa3b2);
}
.vp__hint--ok {
  color: var(--hud-green, #4ade80);
}
.vp__code {
  width: 100%;
  box-sizing: border-box;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
  resize: vertical;
}
.vp__cloud summary {
  cursor: pointer;
}
.vp__actions {
  display: flex;
  gap: 8px;
}
.vp__plan {
  margin: 0;
  font-size: 13px;
  color: var(--hud-text, #e7ebf3);
}
.vp__why {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--hud-muted, #9aa3b2);
}
.vp__why--warn {
  color: var(--hud-amber, #ffb547);
}
.vp__why code {
  font-family: ui-monospace, Consolas, monospace;
  background: rgba(255, 255, 255, 0.06);
  padding: 0 4px;
  border-radius: 4px;
}
.vp__error {
  margin: 0;
  font-size: 12px;
  color: #ff8b8b;
}
.vp__video {
  display: block;
  width: min(100%, 420px);
}
.vp__path {
  font-size: 11px;
  color: var(--hud-muted, #9aa3b2);
  word-break: break-all;
}
</style>

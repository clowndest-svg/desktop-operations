<template>
  <Transition name="sm-anim">
    <div v-if="open" class="sm" role="dialog" aria-modal="true" aria-label="手机投屏">
      <div class="sm__scrim" @click="emit('close')"></div>

      <section class="sm__box">
        <header class="sm__head">
          <h2 class="hud-title">手机投屏 · SCRCPY</h2>
          <span class="sm__phase" :class="phaseClass">{{ phaseWord }}</span>
          <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
        </header>

        <div class="sm__body">
          <p v-if="!state.available" class="sm__error">
            这个包里没带 scrcpy 运行时（jarvis/vendor/scrcpy），投屏开不了。重新做一个交付包，或用 JARVIS_SCRCPY_DIR 指到一个装有 scrcpy.exe 和 adb.exe 的目录。
          </p>

          <p v-if="state.detail" class="sm__note" :class="{ 'sm__note--bad': isBad }">
            {{ state.detail }}
          </p>
          <!-- 每一格后端返回的「没能照着做」都从这里出来：铺满/全屏改不动、设置没写进磁盘，
               都只在 error 里说话，不画出来的话界面就只剩一个看着像成功的下拉框。 -->
          <p v-if="state.error" class="sm__error">{{ state.error }}</p>
          <p v-if="state.phase === 'running'" class="sm__running">
            正在投 {{ state.serial }} · 已经 {{ seconds }} 秒 · {{ tierLabel }}
          </p>

          <!-- 方式：这一格改的是"去哪找手机"和"找不到时怎么教你"，不改画质 -->
          <div class="sm__section">
            <span class="hud-label">连接方式</span>
            <select v-model="mode" class="hud-field" :disabled="busy" @change="applyMode">
              <option v-for="row in state.modes" :key="row.id" :value="row.id">
                {{ row.label }}
              </option>
            </select>
            <p class="sm__pros">好处：{{ currentMode.pros }}</p>
            <p class="sm__cons">代价：{{ currentMode.cons }}</p>
            <p class="sm__tip">
              选这一格不会改变画面质量，改的是「开始投屏」去哪找手机、以及找不到时怎么教你。两种手机同时连着时，只有这一档对应的那一种会被选中。
            </p>
          </div>

          <!-- 显示方式：铺满是对着开着的窗口做的，全屏要重开 -->
          <div class="sm__section">
            <span class="hud-label">显示方式</span>
            <select v-model="windowChoice" class="hud-field" :disabled="busy" @change="applyWindow">
              <option v-for="row in state.windows" :key="row.id" :value="row.id">
                {{ row.label }}
              </option>
            </select>
            <p class="sm__tip">{{ currentWindow.note }}</p>
            <label class="sm__check">
              <input
                type="checkbox"
                :checked="stretchChoice"
                :disabled="busy"
                @change="onStretch"
              />
              <span>画面拉伸填满窗口（没有黑边，但会变形成屏幕的比例）</span>
            </label>
            <p class="sm__tip">
              投屏窗口里按 F11 可以随时切全屏。滚轮是安卓的滚动事件：鼠标要停在画面上（黑边里不算），视频页这类本来没得滚的界面自然不动 —— 要滑动就用左键按住拖。
            </p>
          </div>

          <!-- 手机操作：这一下改的是手机上的东西，不是我们这边的状态 -->
          <div class="sm__section">
            <span class="hud-label">手机上这一下</span>
            <div class="sm__keys">
              <button
                v-for="row in state.keys"
                :key="row.id"
                class="hud-btn"
                type="button"
                :disabled="busy || !running"
                @click="pressKey(row.id)"
              >
                {{ row.label }}
              </button>
              <button
                v-for="row in state.swipes"
                :key="row.id"
                class="hud-btn"
                type="button"
                :disabled="busy || !running"
                @click="swipe(row.id)"
              >
                {{ row.label }}
              </button>
            </div>
            <button class="hud-btn" type="button" :disabled="busy || reading" @click="readScreen">
              {{ reading ? '正在读这一屏…' : '让她看这块屏' }}
            </button>
          </div>

          <!-- 读屏的回答：来源一条条摆出来，哪条没走、为什么没走，比一句"看不到"有用。 -->
          <div v-if="answer.sources.length || answer.text" class="sm__section">
            <span class="hud-label">这一屏读到的</span>
            <div v-if="answer.sources.length" class="sm__sources">
              <span v-for="row in answer.sources" :key="row" class="hud-chip">{{ row }}</span>
            </div>
            <pre v-if="answer.text" class="sm__answer">{{ answer.text }}</pre>
            <label class="sm__check">
              <input
                type="checkbox"
                :checked="answer.allow_vision"
                :disabled="busy"
                @change="onAllowVision"
              />
              <span>允许把这块屏发给能看图的模型（截图会离开这台机器，默认不勾）</span>
            </label>
            <p v-if="lastAction" class="sm__note">{{ lastAction }}</p>
            <p v-if="lastNote" class="sm__note">{{ lastNote }}</p>
            <p class="sm__tip">投屏没开着的时候这几颗是灰的 —— 不知道要发给哪台手机，瞎按会改到另一台。</p>
          </div>

          <!-- 设备 -->
          <div class="sm__section">
            <div class="sm__section-head">
              <span class="hud-label">连着这台电脑的手机</span>
              <button class="hud-btn" type="button" :disabled="busy" @click="refresh">
                {{ loading ? '读取中…' : '刷新设备' }}
              </button>
            </div>
            <p v-if="deviceError" class="sm__error">{{ deviceError }}</p>
            <ul v-else-if="devices.length" class="sm__devices">
              <li v-for="row in devices" :key="row.serial" class="sm__device">
                <span class="sm__device-name">{{ row.label }}</span>
                <span class="hud-chip" :class="row.ready ? 'hud-chip' : 'hud-chip--bad'">
                  {{ row.ready ? '可投屏' : row.state }}
                </span>
                <span class="sm__device-serial">{{ row.serial }}</span>
                <!--
                  adb 按通道列设备：插着一根线又开过两个 tcpip 端口的话，一台手机是三行。
                  服务端已经并成一行，这里把并掉的通道说清楚 —— 不说，用户会以为下面少了
                  两台设备；说了，他知道「投这一台」投的是哪一个。
                -->
                <p v-if="row.channels.length > 1" class="sm__hint">
                  这一台在 adb 里是 {{ row.channels.length }} 个通道（{{ row.channels.join('、') }}），按上面那一格挑了 {{ row.serial }}
                </p>
                <button
                  v-if="row.ready"
                  class="hud-btn"
                  type="button"
                  :disabled="busy"
                  @click="start(row.serial)"
                >
                  投这一台
                </button>
                <!--
                  Android 10 及以下没有「无线调试」可以配对，唯一的无线走法是插着线让它
                  改听 TCP 端口 —— 所以这一颗只在 WiFi 档、且这一行是插线的那台时出现。
                -->
                <button
                  v-if="row.ready && !row.wireless && state.mode !== 'usb'"
                  class="hud-btn sm__device-port"
                  type="button"
                  :disabled="busy"
                  @click="openPort(row.serial)"
                >
                  开无线端口
                </button>
                <p v-if="row.hint" class="sm__hint">{{ row.hint }}</p>
              </li>
            </ul>
            <p v-else class="sm__empty">
              这一档没找到能投的手机。USB 档要插一根能传数据的线并在手机上点「允许 USB 调试」；不想插线就切到「同一个 WiFi」按那里的配对说明走，或者切到「不开无线调试，自动找」那一档，插一次线开端口后点「自动找」。
            </p>
          </div>

          <!-- 开关 -->
          <div class="sm__section">
            <div class="sm__actions">
              <button
                class="hud-btn hud-btn--primary"
                type="button"
                :disabled="busy || running || !state.available"
                @click="start('')"
              >
                开始投屏
              </button>
              <button
                class="hud-btn"
                type="button"
                :disabled="busy || idle"
                @click="stop"
              >
                停止投屏
              </button>
            </div>
            <label class="sm__check">
              <input type="checkbox" :checked="state.audio" :disabled="busy" @change="onAudio" />
              <span>带上手机的声音（会占带宽，WiFi 上更容易卡，默认关）</span>
            </label>
            <label class="sm__check">
              <input
                type="checkbox"
                :checked="!state.keep_screen"
                :disabled="busy"
                @change="onKeepScreen"
              />
              <span>投屏时把手机自己的屏幕关掉（省手机电，弱机上更顺）</span>
            </label>
          </div>

          <!-- 画质 -->
          <div class="sm__section">
            <span class="hud-label">画质档位</span>
            <p class="sm__tip">
              卡了就往左退一档。这几档改的是 scrcpy 的分辨率上限、帧率上限、码率和编码器。
            </p>
            <ul class="sm__tiers">
              <li v-for="row in state.tiers" :key="row.id" class="sm__tier">
                <button
                  class="hud-btn"
                  type="button"
                  :disabled="busy || tuning"
                  @click="pick(row.id)"
                >
                  {{ row.id === state.tier ? '● 当前' : '○ 切到这一档' }}
                </button>
                <span class="sm__tier-label">{{ row.label }}</span>
                <p class="sm__tier-note">{{ row.note }}</p>
              </li>
            </ul>
          </div>

          <!-- 自动找：不开无线调试的那一档 -->
          <div v-if="state.mode === 'scan'" class="sm__section">
            <span class="hud-label">自动找（不开无线调试）</span>
            <p class="sm__tip">
              这一档不碰「无线调试」那个页面：手机插一次线、点下面那一行的「开无线端口」，端口就固定在 5555；之后每次点「自动找」，由电脑扫本网段把它连上。
            </p>
            <p class="sm__cons">
              第一次那根线省不掉 —— 不开 USB 调试也不开无线调试，安卓不会把屏幕交给任何软件。这一档还会扫一遍本网段的 5555 端口，公司或公共 WiFi 别用。
            </p>
            <button
              class="hud-btn hud-btn--primary"
              type="button"
              :disabled="busy"
              @click="discover"
            >
              {{ scanning ? '正在扫本网段…' : '自动找' }}
            </button>
          </div>

          <!-- 无线 -->
          <div v-if="state.mode === 'wifi'" class="sm__section">
            <span class="hud-label">无线调试（不用线）</span>
            <p class="sm__tip">
              手机「开发者选项 → 无线调试」打开后，先点「使用配对码配对设备」，把上面那行 IP:端口和 6 位配对码填进来配一次；之后用「无线调试」页面上显示的 IP:端口 点连接。手机和电脑要在同一个 WiFi。
            </p>
            <div class="sm__wireless">
              <input v-model="address" class="hud-field" placeholder="192.168.1.9:5555" />
              <input v-model="code" class="hud-field" placeholder="配对码 6 位" />
              <button class="hud-btn" type="button" :disabled="busy" @click="pair">配对</button>
              <button class="hud-btn" type="button" :disabled="busy" @click="connect">连接</button>
            </div>
          </div>

          <div v-if="state.tail.length" class="sm__section">
            <span class="hud-label">scrcpy 的最后几行</span>
            <pre class="sm__tail">{{ state.tail.join('\n') }}</pre>
          </div>
        </div>
      </section>
    </div>
  </Transition>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import {
  connectMirrorWireless,
  discoverMirrorPhones,
  displayMirrorWindow,
  fetchMirrorDevices,
  openMirrorWirelessPort,
  fetchMirrorState,
  pairMirrorWireless,
  pressMirrorKey,
  readMirrorScreen,
  setMirrorScreenVision,
  startMirror,
  stopMirror,
  swipeMirrorPhone,
  tuneMirror,
  type MirrorDevice,
  type MirrorScreenAnswer,
  type MirrorState,
} from '@/api/bridge'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'changed', label: string): void }>()

const empty: MirrorState = {
  phase: 'off',
  detail: '',
  serial: '',
  available: true,
  tier: 'balanced',
  tiers: [],
  mode: 'usb',
  modes: [],
  window: 'window',
  windows: [],
  stretch: false,
  keys: [],
  swipes: [],
  audio: false,
  keep_screen: true,
  seconds: 0,
  tail: [],
}

const state = reactive<MirrorState>({ ...empty })
const devices = ref<MirrorDevice[]>([])
const deviceError = ref('')
const address = ref('')
const code = ref('')
const busy = ref(false)
const loading = ref(false)
const tuning = ref(false)
const mode = ref('usb')
const windowChoice = ref('window')
const lastAction = ref('')
// 显示方式和拉伸各发一半，就会互相盖掉对方：下拉发的是服务器记着的那格拉伸，勾头发的是
// 服务器记着的那格显示方式，谁的后到的回包赢，屏上就出现"我明明选了铺满又弹回全屏"。
// 两个都改成本地这一格的当前值，发的永远是用户最新的那次组合。
const stretchChoice = ref(false)
const scanning = ref(false)
let poll: ReturnType<typeof setInterval> | undefined

const WORDS: Record<string, string> = {
  off: '没在投',
  starting: '正在启动',
  running: '正在投屏',
  stopping: '正在关闭',
  failed: '失败',
}

const phaseWord = computed(() => WORDS[state.phase] ?? state.phase)
const isBad = computed(() => state.phase === 'failed' || !!deviceError.value)
const running = computed(() => state.phase === 'running' || state.phase === 'starting')
const idle = computed(() => state.phase === 'off')
const seconds = computed(() => {
  // The panel keeps its own count between polls so the number does not jump by 2.
  if (state.phase !== 'running') return 0
  return state.seconds + Math.max(0, Math.round((Date.now() - seenAt) / 1000))
})
const tierLabel = computed(() => {
  const row = state.tiers.find((item) => item.id === state.tier)
  return row ? row.label : state.tier
})
const phaseClass = computed(() => {
  if (state.phase === 'running') return 'sm__phase--on'
  if (state.phase === 'failed') return 'sm__phase--bad'
  return ''
})

let seenAt = Date.now()

function apply(next: MirrorState): void {
  Object.assign(state, next)
  mode.value = state.mode
  windowChoice.value = state.window
  stretchChoice.value = state.stretch
  seenAt = Date.now()
}

async function refresh(): Promise<void> {
  loading.value = true
  try {
    const list = await fetchMirrorDevices()
    devices.value = list.devices
    deviceError.value = list.error
  } catch (err) {
    deviceError.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
  await tick()
}

async function tick(): Promise<void> {
  try {
    apply(await fetchMirrorState())
  } catch (err) {
    deviceError.value = err instanceof Error ? err.message : String(err)
  }
}

async function start(serial: string): Promise<void> {
  busy.value = true
  try {
    apply(await startMirror(serial))
    await refresh()
    emit('changed', state.phase === 'running' ? '投屏已打开' : state.detail)
  } finally {
    busy.value = false
  }
}

async function stop(): Promise<void> {
  busy.value = true
  try {
    apply(await stopMirror())
    emit('changed', '投屏已关闭')
  } finally {
    busy.value = false
  }
}

/** The dropdown's own two lines of copy, read from the engine so they cannot drift. */
const currentMode = computed(() => {
  const rows = state.modes.filter((row) => row.id === state.mode)
  return rows.length ? rows[0] : { id: state.mode, label: state.mode, pros: '', cons: '' }
})

async function write(): Promise<void> {
  apply(await tuneMirror(state.tier, state.audio, state.keep_screen, mode.value))
}

async function applyMode(): Promise<void> {
  busy.value = true
  try {
    await write()
  } finally {
    busy.value = false
  }
}

async function pick(tier: string): Promise<void> {
  tuning.value = true
  try {
    apply(await tuneMirror(tier, state.audio, state.keep_screen, mode.value))
  } finally {
    tuning.value = false
  }
}

async function onAudio(event: Event): Promise<void> {
  const on = (event.target as HTMLInputElement).checked
  apply(await tuneMirror(state.tier, on, state.keep_screen, mode.value))
}

async function onKeepScreen(event: Event): Promise<void> {
  const checked = (event.target as HTMLInputElement).checked
  // The checkbox reads "把手机屏幕关掉"; the stored key is the opposite.
  apply(await tuneMirror(state.tier, state.audio, !checked, mode.value))
}

/** The display dropdown's own copy, read from the engine so it cannot drift. */
const currentWindow = computed(() => {
  const rows = state.windows.filter((row) => row.id === state.window)
  return rows.length ? rows[0] : { id: state.window, label: state.window, note: '' }
})

async function applyWindow(): Promise<void> {
  busy.value = true
  try {
    apply(await displayMirrorWindow(windowChoice.value, stretchChoice.value))
  } finally {
    busy.value = false
  }
}

async function onStretch(event: Event): Promise<void> {
  const on = (event.target as HTMLInputElement).checked
  stretchChoice.value = on
  busy.value = true
  try {
    apply(await displayMirrorWindow(windowChoice.value, on))
  } finally {
    busy.value = false
  }
}

const KEYS_AND_SWIPES = computed(() => [...state.keys, ...state.swipes])
const lastActionId = ref('')

function actionLabel(id: string): string {
  const row = KEYS_AND_SWIPES.value.find((item) => item.id === id)
  return row ? row.label : id
}

// 底下那行说明跟着刚按的那颗走；一次没按过的时候说清这五个是什么。
const lastNote = computed(() => {
  const row = KEYS_AND_SWIPES.value.find((item) => item.id === lastActionId.value)
  if (row && row.note) return row.note
  return '返回 / 主页 / 多任务 就是手机上那三个键；往上滑是下一条（抖音那种整屏翻页），往下滑是上一条。'
})

async function pressKey(key: string): Promise<void> {
  busy.value = true
  lastActionId.value = key
  try {
    const next = await pressMirrorKey(key)
    apply(next)
    lastAction.value = next.error ? '' : `已经发给这台：${actionLabel(key)}`
  } finally {
    busy.value = false
  }
}

const EMPTY_ANSWER: MirrorScreenAnswer = { text: '', error: '', sources: [], allow_vision: false }
const answer = ref<MirrorScreenAnswer>({ ...EMPTY_ANSWER })
const reading = ref(false)

async function readScreen(): Promise<void> {
  reading.value = true
  busy.value = true
  try {
    answer.value = await readMirrorScreen('')
  } catch (err) {
    answer.value = { ...EMPTY_ANSWER, error: err instanceof Error ? err.message : String(err) }
  } finally {
    reading.value = false
    busy.value = false
  }
}

async function onAllowVision(event: Event): Promise<void> {
  const on = (event.target as HTMLInputElement).checked
  busy.value = true
  try {
    const next = await setMirrorScreenVision(on)
    // 这扇门不重新读屏，所以刚读出来的那段不能因为它而被擦掉。
    answer.value = {
      text: answer.value.text || next.text,
      error: next.error,
      sources: next.error ? [] : answer.value.sources,
      allow_vision: next.allow_vision,
    }
  } catch (err) {
    answer.value = { ...EMPTY_ANSWER, error: err instanceof Error ? err.message : String(err) }
  } finally {
    busy.value = false
  }
}

async function swipe(direction: string): Promise<void> {
  busy.value = true
  lastActionId.value = direction
  try {
    const next = await swipeMirrorPhone(direction)
    apply(next)
    lastAction.value = next.error ? '' : `已经发给这台：${actionLabel(direction)}`
  } finally {
    busy.value = false
  }
}

/** Sweep the local /24 for phones on port 5555 and connect whatever answers. */
async function discover(): Promise<void> {
  busy.value = true
  scanning.value = true
  try {
    apply(await discoverMirrorPhones())
    await refresh()
  } finally {
    scanning.value = false
    busy.value = false
  }
}

/** ``adb tcpip 5555`` + connect, for phones with no 无线调试 pairing dialog. */
async function openPort(serial: string): Promise<void> {
  busy.value = true
  try {
    apply(await openMirrorWirelessPort(serial))
    await refresh()
  } finally {
    busy.value = false
  }
}

async function connect(): Promise<void> {
  busy.value = true
  try {
    apply(await connectMirrorWireless(address.value))
    await refresh()
  } finally {
    busy.value = false
  }
}

async function pair(): Promise<void> {
  busy.value = true
  try {
    apply(await pairMirrorWireless(address.value, code.value))
  } finally {
    busy.value = false
  }
}

function stopPolling(): void {
  if (poll !== undefined) {
    window.clearInterval(poll)
    poll = undefined
  }
}

/**
 * The mirror's phase changes without anybody touching this panel -- scrcpy finishes
 * starting, or the operator closes its window by hand -- so while the dialog is open
 * it is read every 1.5 seconds and the timer stops the moment it is not.
 */
watch(
  () => props.open,
  (shown) => {
    if (shown) {
      void refresh()
      stopPolling()
      poll = window.setInterval(() => void tick(), 1500)
    } else {
      stopPolling()
    }
  },
)

onBeforeUnmount(stopPolling)
</script>

<style scoped>
.sm {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
}

.sm__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.sm__box {
  position: relative;
  width: min(620px, 94vw);
  max-height: 86vh;
  display: flex;
  flex-direction: column;
  padding: 16px 20px 14px;
  border: 1px solid var(--hud-line);
  border-top: 1px solid rgba(77, 216, 255, 0.45);
  border-radius: var(--hud-radius);
  background: linear-gradient(180deg, rgba(10, 24, 40, 0.96), rgba(4, 8, 14, 0.98));
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}

.sm__head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--hud-line);
}

/* 「关闭」贴右上角，和其余十二扇弹窗同一个位置；状态词仍紧跟标题。 */
.sm__head > :last-child {
  margin-left: auto;
}

.sm__head .hud-title {
  flex: 1 1 auto;
}

.sm__phase {
  padding: 2px 10px;
  border-radius: 999px;
  border: 1px solid var(--hud-line);
  font-size: 12px;
  color: var(--hud-dim);
}

.sm__phase--on {
  color: #5ff0b5;
  border-color: rgba(95, 240, 181, 0.5);
}

.sm__phase--bad {
  color: var(--hud-red);
  border-color: rgba(255, 92, 92, 0.5);
}

.sm__body {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  gap: 12px;
  padding-top: 12px;
}

.sm__section {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.sm__section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.sm__note {
  margin: 0;
  font-size: 12px;
  color: #7fe3c0;
  word-break: break-word;
}

.sm__note--bad {
  color: var(--hud-red);
}

.sm__running {
  margin: 0;
  font-size: 12px;
  color: var(--hud-dim);
}

.sm__error {
  margin: 0;
  font-size: 12px;
  color: var(--hud-red);
}

.sm__pros,
.sm__cons,
.sm__empty,
.sm__tip {
  margin: 0;
  font-size: 12px;
  color: var(--hud-dim);
}

.sm__pros {
  margin: 0;
  font-size: 12px;
  color: #7fe3c0;
}

.sm__cons {
  margin: 0;
  font-size: 12px;
  color: #ffc46b;
}

.sm__devices,
.sm__tiers {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.sm__device {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 4px 10px;
  align-items: center;
  padding: 8px 10px;
  border: 1px solid var(--hud-line);
  border-radius: 8px;
  background: rgba(6, 16, 28, 0.6);
}

.sm__device-name {
  font-size: 13px;
}

.sm__device-serial {
  font-size: 11px;
  color: var(--hud-dim);
}

.sm__device-port {
  grid-column: 2;
  justify-self: end;
}

.sm__hint {
  grid-column: 1 / -1;
  margin: 0;
  font-size: 11px;
  color: #ffc46b;
}

.sm__keys {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.sm__sources {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}

.sm__answer {
  margin: 0;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--hud-text, #e8eefc);
}

.sm__actions {
  display: flex;
  gap: 10px;
}

.sm__check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--hud-dim);
}

.sm__tier {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 2px 10px;
  align-items: center;
}

.sm__tier-label {
  font-size: 13px;
}

.sm__tier-note {
  grid-column: 2;
  margin: 0;
  font-size: 11px;
  color: var(--hud-dim);
}

.sm__wireless {
  display: grid;
  grid-template-columns: 1.2fr 1fr auto auto;
  gap: 8px;
}

.sm__tail {
  margin: 0;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--hud-line);
  background: rgba(2, 6, 12, 0.8);
  font-size: 11px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 140px;
  overflow-y: auto;
}

.sm-anim-enter-active,
.sm-anim-leave-active {
  transition: opacity 0.18s ease;
}

.sm-anim-enter-from,
.sm-anim-leave-to {
  opacity: 0;
}

@media (prefers-reduced-motion: reduce) {
  .sm-anim-enter-active,
  .sm-anim-leave-active {
    transition: none;
  }
}
</style>

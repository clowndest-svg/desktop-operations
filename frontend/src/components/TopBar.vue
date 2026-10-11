<template>
  <header class="bar">
    <div class="bar__brand">
      <span class="bar__mark">◈</span>
      <span class="bar__name">{{ info.name }}</span>
      <span class="hud-label bar__ver">v{{ info.version }} · {{ info.engine }}{{ info.built ? ' · 构建 ' + info.built : '' }}</span>
    </div>

    <div class="bar__states">
      <span class="hud-chip" :title="store.error || '数据链路正常'">
        <span class="hud-dot" :class="linkClass"></span>
        <span class="hud-label">{{ store.connected ? '遥测正常' : '遥测中断' }}</span>
      </span>

      <!--
        有没有网，单独一格：遥测中断说的是"读不到这台机器"，这一格说的是"这台机器连不上外面"。
        两件事的修法相反，画在同一个灯上就是让人去查错的那根线。

        title 里带着这一跳多旧（`store.netTitle`）。这一格读的是服务端缓存，每 10 秒问一次，
        所以探测线程不走了和"没网"在屏上长得一样 —— 把秒数写出来是唯一能分开它们的办法。
      -->
      <span
        class="hud-chip"
        :class="store.network.online === false ? 'hud-chip--bad' : ''"
        :title="store.netTitle"
      >
        <span class="hud-dot" :class="store.netDotClass"></span>
        <span class="hud-label">{{ store.netLabel }}</span>
      </span>

      <!--
        Two axes, one chip: ``phase`` says whether the microphone is available at
        all, ``turn`` says what it is doing. Showing "聆听中" after a failed start
        would be the kind of lie that costs a demo.
      -->
      <span class="hud-chip" :title="voice.hint || voice.label">
        <span class="hud-dot" :class="voice.dotClass"></span>
        <span class="hud-label">{{ voice.label }}</span>
      </span>
      <span v-if="voice.error" class="hud-chip hud-chip--warn" :title="voice.error">
        <span class="hud-label">语音调用失败</span>
      </span>
      <button
        v-if="!voice.enabled"
        class="hud-btn"
        type="button"
        :disabled="voice.loading"
        @click="voice.enable()"
      >
        {{ voice.loading ? '加载中…' : '启用语音' }}
      </button>
      <button v-else class="hud-btn" type="button" @click="voice.mute()">释放麦克风</button>

      <!--
        One number for both kinds of trouble, and the tooltip says which is which: this
        chip's job is "look over there", and two chips competing for that glance are two
        chips the eye learns to skip. A critical alert tints it red.

        它是个**按钮**：上一版这里是 <span>，只带一个 title —— 用户点了没反应，因为
        确实没有东西可点（r99 那版用户实拍报的）。红点的意思就是"现在给我看是哪几条"。
      -->
      <button
        v-if="alertLines.length"
        class="hud-chip al__chip"
        :class="anyCritical ? 'hud-chip--bad' : 'hud-chip--warn'"
        type="button"
        title="点开看是哪几条、现在多少、线在哪"
        @click="emit('alerts')"
      >
        <span class="hud-label">告警 {{ alertLines.length }}</span>
      </button>

      <span class="hud-chip">
        <span class="hud-label">运行</span>
        <span class="hud-num bar__uptime">{{ uptime }}</span>
      </span>

      <span class="hud-chip hud-num bar__clock">{{ clock }}</span>

      <!-- 齿轮放在最后：它是唯一一个会改东西的入口，不该混在一排只读指示灯里。 -->
      <!--
        The control level is on the bar, not buried in a dialog, because it is the
        one setting that answers "what is this thing allowed to do to my machine
        right now" -- and that question tends to arrive while something is moving.
      -->
      <button
        class="hud-btn bar__entry"
        type="button"
        :title="accessLabel ? `电脑控制权限：${accessLabel}` : '电脑控制权限'"
        @click="emit('access')"
      >
        控制{{ accessLabel ? ` · ${accessLabel}` : '' }}
      </button>
      <!--
        手机接入 is a listening port, so its state belongs on the bar next to 控制:
        "is something on the network able to reach my machine right now" is the same
        kind of question, and it should not cost a trip into a dialog to answer.
      -->
      <button
        class="hud-btn bar__entry"
        type="button"
        :title="phoneLabel ? `手机接入：${phoneLabel}` : '手机接入'"
        @click="emit('phone')"
      >
        手机{{ phoneLabel ? ` · ${phoneLabel}` : '' }}
      </button>
      <!--
        投屏 carries no live label on purpose. The bar's other entries read a socket
        or a switch that Python already knows; this one would have to run ``adb
        devices`` on the bar's timer to say 连着 1 台, and a subprocess on the poll
        path is how the window starts stuttering. The count lives inside the dialog.
      -->
      <button
        class="hud-btn bar__entry"
        type="button"
        title="把手机画面投到这台电脑（scrcpy）"
        @click="emit('mirror')"
      >
        投屏
      </button>
      <!--
        生图和投屏同一行：它是一个"要不要现在画一张"的决定，不是一个设置项 ——
        放在设置里就会变成第 38 轮那次一样的"用户以为没这个功能"。
      -->
      <button
        class="hud-btn bar__entry"
        type="button"
        title="一句话画一张图（本机 ComfyUI 或云端 API）"
        @click="emit('images')"
      >
        生图
      </button>
      <!--
        带一个当前色的圆点：一排按钮里，「皮肤」是唯一一个"现在是什么"看不出来的，
        而把当前皮肤的名字写进按钮会把这一行的宽度顶歪（见 bar__entry 的说明）。
      -->
      <button
        class="hud-btn bar__entry bar__skin"
        type="button"
        :title="`皮肤：${skinLabel}`"
        @click="emit('skin')"
      >
        <span class="bar__dot" :style="{ background: hex(currentSkin().line) }"></span>皮肤
      </button>
      <button class="hud-btn bar__entry" type="button" title="最近动作" aria-label="最近动作" @click="emit('activity')">
        动作
      </button>
      <button
        class="hud-btn bar__entry"
        type="button"
        :title="maximized ? '还原窗口大小' : '让窗口铺满屏幕'"
        @click="toggleMax"
      >
        {{ maximized ? '还原' : '铺满' }}
      </button>
      <button class="hud-btn bar__entry" type="button" title="提醒 / 记忆 / 知识库" @click="emit('assistant')">
        助手
      </button>
      <button
        class="hud-btn bar__entry"
        :class="{ 'hud-btn--primary': petShown }"
        type="button"
        :title="petShown ? '收起桌面宠物' : '在桌面上显示小夜：说唤醒词时她会从虫洞里出来'"
        @click="flipPet"
      >
        宠物
      </button>
      <!--
        Only offered when the tray is actually up. Without an icon the X is the only
        way out, and a button that hides the app somewhere unfindable would be a trap
        -- so the shell answers with tray=false and this disappears.
      -->
      <button
        v-if="info.tray"
        class="hud-btn bar__entry"
        type="button"
        title="收进右下角托盘：程序继续在后台等你说话，托盘图标里右键才是退出"
        @click="hideToTray"
      >
        收进托盘
      </button>
      <button class="hud-btn bar__entry" type="button" title="设置" aria-label="设置" @click="emit('settings')">
        设置
      </button>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  fetchAppInfo,
  fetchComputerLevels,
  fetchPetState,
  fetchWindowState,
  formatUptime,
  hideWindow,
  togglePet,
  toggleWindowMax,
  fetchMobileState,
  type AppInfo,
} from '@/api/bridge'
import { SKINS, currentSkin, hex, skin } from '@/theme'
import { useSystemStore } from '@/stores/system'
import { useVoiceStore } from '@/stores/voice'

const store = useSystemStore()

const emit = defineEmits<{
  (e: 'settings'): void
  (e: 'activity'): void
  (e: 'alerts'): void
  (e: 'access'): void
  (e: 'phone'): void
  (e: 'mirror'): void
  (e: 'images'): void
  (e: 'assistant'): void
  (e: 'skin'): void
}>()
const voice = useVoiceStore()

const info = ref<AppInfo>({ name: '小夜', version: '--', engine: '--' })
const petShown = ref(false)
const accessLabel = ref('')
const phoneLabel = ref('')
const maximized = ref(false)

/**
 * The window's own size, from the button that changes it. Asking Python rather than
 * tracking it in the page is the point: the operator can also maximise with the
 * title bar or Win+↑, and a label that only knew about its own clicks would lie.
 */
/**
 * The desktop figure is a second window, so the button asks Python rather than
 * tracking a click: it can also be switched from the tray menu, and a label that
 * only knew about its own clicks would disagree with the desktop.
 */
async function flipPet(): Promise<void> {
  petShown.value = (await togglePet()).shown
}

async function refreshPet(): Promise<void> {
  try {
    petShown.value = (await fetchPetState()).shown
  } catch {
    petShown.value = false
  }
}

async function toggleMax(): Promise<void> {
  const state = await toggleWindowMax()
  maximized.value = state.maximized
}

/**
 * Hide, not quit. The window goes away and the process keeps the microphone, which
 * is the whole point of the tray; the only way out is the icon's own 退出.
 */
function hideToTray(): void {
  void hideWindow()
}

const skinLabel = computed(() => SKINS.find((entry) => entry.id === skin.value)?.label ?? skin.value)
/** 选皮肤是一个带预览的选择，不是"盲点着换下一个"——按钮只负责打开那个选择器。 */

const clock = ref('')

const linkClass = computed(() => (store.connected ? 'live' : 'error'))
const uptime = computed(() => formatUptime(store.metrics.uptime_seconds ?? 0))
/** 告警（过了线的）在前，读数残缺（warnings）在后 —— 一起进那颗 chip。 */
const alertLines = computed(() => [
  ...store.alerts.map((row) => row.message),
  ...store.warnings,
])
const anyCritical = computed(() => store.alerts.some((row) => row.severity === 'critical'))

let timer: ReturnType<typeof setInterval> | undefined

function tickClock(): void {
  // A clock behind a hidden window has no audience. Cheap, but this timer runs
  // once a second for as long as the app is open, which is the whole point of
  // keeping it honest about when it does work.
  if (document.hidden) return
  clock.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
}

function onVisibility(): void {
  if (document.hidden) return
  tickClock()
  // The tray's 「手机接入」 usually moves the port while this window is hidden, and a
  // label that only knew about its own clicks would disagree with the socket.
  void refreshPhone()
}

/**
 * The page's own resize is the cheapest available signal that the frame changed,
 * and it fires when the operator maximises from the title bar or with Win+↑ — where
 * this button learned nothing from a click. The answer comes from Python, so the
 * label follows the window rather than following the last thing the page asked for.
 */
function onResize(): void {
  void refreshWindowState()
}

async function refreshWindowState(): Promise<void> {
  try {
    maximized.value = (await fetchWindowState()).maximized
  } catch {
    maximized.value = false
  }
}

/**
 * Whether the phone port is open, and how many phones it will answer.
 *
 * Asked of Python rather than remembered from a click, because the tray's
 * 「手机接入」 switches the very same listener.
 */
/**
 * The popup switched the port. The event carries a label, and it is ignored: the
 * reading comes from Python, so the bar cannot be told something the socket
 * disagrees with.
 */
function onPhoneSwitched(): void {
  void refreshPhone()
}

async function refreshPhone(): Promise<void> {
  try {
    const state = await fetchMobileState()
    phoneLabel.value = state.running ? `开 · ${state.devices.length} 台` : '关'
  } catch {
    phoneLabel.value = ''
  }
}

async function onSettingsSaved(): Promise<void> {
  try {
    info.value = await fetchAppInfo()
    document.title = info.value.name
  } catch {
    /* the previous name stays on screen; the next save or launch fixes it */
  }
}

onMounted(async () => {
  tickClock()
  timer = setInterval(tickClock, 1000)
  document.addEventListener('visibilitychange', onVisibility)
  window.addEventListener('resize', onResize)
  window.addEventListener('jarvis-phone-changed', onPhoneSwitched)
  // 「她叫什么」 lives on this bar too: after a save the brand has to be re-read,
  // or the panel says 保存成功 while the corner keeps calling her the old name.
  window.addEventListener('jarvis-settings-saved', onSettingsSaved)
  info.value = await fetchAppInfo()
  // The document title is what a browser tab shows and what the
  // webview may sync its caption from -- it must not stay on the shipped name.
  document.title = info.value.name
  void refreshWindowState()
  void refreshPet()
  void refreshPhone()
  try {
    accessLabel.value = (await fetchComputerLevels()).current.label
  } catch {
    accessLabel.value = ''
  }
})

onBeforeUnmount(() => {
  if (timer !== undefined) clearInterval(timer)
  document.removeEventListener('visibilitychange', onVisibility)
  window.removeEventListener('resize', onResize)
  window.removeEventListener('jarvis-phone-changed', onPhoneSwitched)
  window.removeEventListener('jarvis-settings-saved', onSettingsSaved)
})
</script>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  /* 两条警告灯同时亮时右侧状态组会挤过 900px 的最小窗宽。不换行的话它是"被裁掉"
     ——用户看到的是指示灯凭空消失，而不是"这里还有内容"。 */
  flex-wrap: wrap;
  row-gap: 8px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--hud-line);
  background: linear-gradient(180deg, rgba(9, 24, 40, 0.9), rgba(4, 7, 13, 0.5));
}

.bar__brand {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.bar__mark {
  color: var(--hud-cyan);
  font-size: 15px;
  text-shadow: 0 0 12px var(--hud-glow);
}

.bar__name {
  font-size: 17px;
  letter-spacing: 0.34em;
  color: var(--hud-cyan);
  text-shadow: 0 0 16px rgba(77, 216, 255, 0.5);
}

.bar__ver {
  letter-spacing: 0.04em;
}

.bar__states {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 6px 6px;
}

/*
 * Every entry on this row is now the same height as every other -- chips, the
 * microphone button and the four action buttons all resolve to --hud-control-h --
 * and the short labels share a width so 「皮肤」「动作」「设置」 read as a group
 * rather than four differently-shaped things.
 */
.bar__entry {
  min-width: 52px;
}

.bar__skin {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.bar__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  box-shadow: 0 0 6px currentColor;
}

.bar__uptime {
  font-size: 12px;
  color: var(--hud-text);
}

.bar__clock {
  font-size: 13px;
  color: var(--hud-cyan);
  letter-spacing: 0.06em;
  min-width: 88px;
  justify-content: center;
}

/*
  这颗 chip 是个按钮，所以两件事要补：``<button>`` 的 UA 字体/颜色会把 chip 的样子打掉
  （按回继承），以及**留一个点得动的证据** —— 一个和旁边指示灯长得一模一样、悬停也没
  任何反应的按钮，用户只会认为它跟指示灯一样是给人看的。上一版它就是这样的。
*/
.al__chip {
  font-family: inherit;
  cursor: pointer;
  transition: border-color 0.16s, box-shadow 0.16s, transform 0.08s;
}

.al__chip:hover {
  border-color: rgba(77, 216, 255, 0.5);
  box-shadow: 0 0 14px rgba(77, 216, 255, 0.18);
}

.al__chip:active {
  transform: translateY(1px);
}
</style>

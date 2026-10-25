<template>
  <div class="hud-backdrop"></div>

  <div class="app" @keydown.esc="systemsOpen = false">
    <TopBar
        @settings="settingsOpen = true"
        @activity="activityOpen = true"
        @alerts="alertsOpen = true"
        @access="accessOpen = true"
        @phone="phoneOpen = true"
        @mirror="mirrorOpen = true"
        @images="imagesOpen = true"
        @videos="videosOpen = true"
      @assistant="assistantOpen = true"
      @extensions="extensionsOpen = true"
      @skin="skinOpen = true"
      @systems="systemsOpen = true"
      @voice-controls="voicePanelOpen = true"
      @portraits="portraitsOpen = true"
      />

    <p v-if="store.error" class="app__error">{{ store.error }}</p>
    <p v-else-if="runningMocked" class="app__mock">浏览器预览模式 · 数据为模拟生成，非真实机器读数</p>

    <main class="app__scene">
      <DesktopPortrait class="app__portrait" />
      <div class="app__shade" aria-hidden="true" />
      <div class="app__intro"><span>小夜 · 与你此刻相伴</span><small>每一句，都认真听。</small></div>
      <ChatPanel class="app__chat" @usage="usageOpen = true" @voice="portraitsOpen = true" />
      <div v-show="voicePanelOpen" class="app__voice"><button class="app__character" type="button" @click="portraitsOpen = true">形象与声音 <span aria-hidden="true">›</span></button><VoiceCore /></div>
    </main>
    <section v-if="systemsOpen" id="system-drawer" class="app__diagnostics" aria-label="系统与工具">
      <header class="app__diagnostics-head"><strong>系统与工具</strong><button class="hud-btn" type="button" @click="systemsOpen = false">关闭</button></header>
      <div class="app__grid">
      <section class="hud-panel rings">

        <!--
          A gauge pinned at 0% reads as "the machine is idle", which is a different
          claim from "we could not read the machine". When telemetry fails, say the
          latter instead of drawing a confident empty dial.
        -->
        <div v-if="store.error" class="rings__row rings__row--empty">
          <span>无读数</span>
        </div>
        <div v-else class="rings__row">
          <div class="rings__stat">
            <!--
              psutil's cpu_percent has no previous sample to compare against on the
              very first read and returns 0.0. The trend chart refuses to plot that
              point; a 30px numeral has to do the same, or the first thing the user
              sees is a confident "0%" that means "not measured yet".
            -->
            <span class="hud-display">
              {{ store.cpuReady ? store.cpuPercent.toFixed(0) : '--' }}<span class="hud-unit">%</span>
            </span>
            <span class="hud-label">CPU</span>
          </div>
          <div class="rings__stat">
            <span class="hud-display">
              {{ store.metrics.memory ? store.memoryPercent.toFixed(0) : '--' }}<span class="hud-unit">%</span>
            </span>
            <span class="hud-label">内存</span>
          </div>
        </div>

        <!-- Live per-core bars: the detail that makes the panel feel like an instrument. -->
        <div v-if="store.metrics.cpu" class="rings__cores">
          <span
            v-for="(core, index) in store.metrics.cpu.per_core"
            :key="index"
            class="rings__core-bar"
            :title="`#${index + 1} ${core.toFixed(0)}%`"
            :style="{ transform: `scaleY(${Math.max(0.06, core / 100)})` }"
          ></span>
        </div>

        <div class="rings__facts">
          <div class="rings__meta">
            <span class="hud-label">逻辑核心</span>
            <span class="hud-num">{{ store.cores || '--' }}</span>
          </div>
          <div class="rings__meta">
            <span class="hud-label">内存占用</span>
            <span class="hud-num">{{ store.metrics.memory ? formatBytes(store.metrics.memory.used_bytes ?? 0) : '--' }}</span>
          </div>
          <!--
            Swap and the network rate are readings the collector already had and the
            page never showed. A machine at "内存 82%" that has not touched the page
            file is a different situation from one swapping, and the panel used to
            draw them identically.
          -->
          <div v-if="store.metrics.memory?.swap_percent !== undefined" class="rings__meta">
            <span class="hud-label">交换分区</span>
            <span class="hud-num">{{ store.metrics.memory.swap_percent.toFixed(0) }}%</span>
          </div>
          <div v-if="store.metrics.net" class="rings__meta">
            <span class="hud-label">网络 ↓ / ↑</span>
            <span class="hud-num">{{ rate(store.metrics.net.receive_bps) }} / {{ rate(store.metrics.net.send_bps) }}</span>
          </div>
        </div>
      </section>

      <TrendPanel class="app__trend" />
      <CleanPanel class="app__clean" />
      <DiskPanel />
      <ProcessPanel
        class="app__procs"
        @reminders="assistantOpen = true"
        @usage="usageOpen = true"
        @actions="activityOpen = true"
        @alerts="alertsOpen = true"
      />
      </div>
    </section>

    <SettingsPanel
      :open="settingsOpen"
      :voice-closed="voiceClosed"
      @close="settingsOpen = false"
      @saved="onSettingsSaved"
      @voice="portraitsOpen = true"
      @pet="petOpen = true"
      @alerts="alertsOpen = true"
      :pet-closed="petClosed"
    />
    <!--
      One voice picker for both entrances (对话区 and 设置), mounted right after the
      settings dialog so the same z-index layer resolves above it by document order.
      Mounted inside ChatPanel it sat *under* 设置: the dialog the operator could see
      but not reach is the failure this window has already been billed for once.
    -->
    <VoicePickerPopup :open="voiceOpen" @close="closeVoicePicker" />
    <PortraitPicker :open="portraitsOpen" @close="portraitsOpen = false" @advanced="portraitsOpen = false; voiceOpen = true" />
    <!--
      Same reason as the voice picker: this one is opened from 设置 too, so it has to
      be mounted after that dialog and not inside it.
    -->
    <PetForgePopup :open="petOpen" @close="closePetForge()" />
    <ActivityPopup :open="activityOpen" @close="activityOpen = false" />
    <!--
      告警弹窗挂在所有对话框之后：它有三个入口（顶栏那颗「告警 N」、概览卡告警格、
      设置 → 告警里那颗按钮），从设置开着它时弹窗必须压在设置面板上面 —— 挂在设置
      内部就是"看得见够不着"，这个项目为这件事付过一次账。
    -->
    <AlertsPopup :open="alertsOpen" @close="alertsOpen = false" />
    <ComputerAccessPopup :open="accessOpen" @close="accessOpen = false" />
    <MobileAccessPopup
      :open="phoneOpen"
      @close="phoneOpen = false"
      @changed="announcePhoneChange"
    />
    <!--
      投屏 sits next to 手机接入 because that is where the phone questions live. It is
      mounted at the top level rather than inside ChatPanel for the same reason the
      voice picker is: a dialog the operator can see but not reach is a billed bug.
    -->
    <MirrorPopup :open="mirrorOpen" @close="mirrorOpen = false" />
    <ImagePopup :open="imagesOpen" @close="imagesOpen = false" />
    <VideoPopup :open="videosOpen" @close="videosOpen = false" />
    <AssistantPopup :open="assistantOpen" @close="assistantOpen = false" />
    <!--
      扩展（插件市场）挂在助手之后、皮肤之前：它是从顶栏和设置都开得动的入口，而它自己里面
      还要「去设置改那三档」。同 z-index 层靠文档顺序决胜这一窗已经吃过两次亏（音色、告警），
      所以这里再钉一层保险 —— 组件里那张是 z-index: 46，稳稳压住设置面板的 40。
    -->
    <ExtensionsPopup :open="extensionsOpen" @close="extensionsOpen = false" />
    <SkinPopup :open="skinOpen" @close="skinOpen = false" />

    <UsagePopup :open="usageOpen" @close="usageOpen = false" />
    <!--
      断线那张卡挂在**所有**弹窗的最后。它和这里的每一张不一样：没人点它，是界面自己发现的，
      所以它必须压得住操作员正开着的那一张 —— 这一窗口的桌面弹窗全是 z-index: 40，同层靠
      文档顺序决胜，挂在对话区里面它就会沉在设置面板底下（音色 / 告警那两条同一条规矩）。
      只在"有网 → 没网"那一跳上出现，离线期间每一轮都不许再冒，所以 :open 是 store 里
      那个一次性开关，不是 network.online === false。
    -->
    <OfflineNotice
      :open="store.offlineNotice"
      :detail="store.network.detail"
      :seconds-ago="store.network.seconds_ago"
      @close="store.dismissOffline()"
    />
    <!--
      右键菜单在**所有**层的最后：它要能盖过正开着的任何一张弹窗（z-index 60 > 40），
      而打包版那扇 WebView2 把原生右键菜单关了 —— 不自己做一个，右键就是"鼠标没反应"。
    -->
    <ContextMenu />
  </div>
</template>

<script setup lang="ts">
import { defineAsyncComponent, onBeforeUnmount, onMounted, ref } from 'vue'
import { formatBytes, reportPetPalette, runningMocked, type SettingsSnapshot } from '@/api/bridge'

/**
 * A network speed, or the honest dash.
 *
 * ``null`` is "there was no previous sample to difference against", which on a fresh
 * launch is every first poll. Printing 0 there would be the same confident zero the
 * CPU readout used to show.
 */
function rate(bytesPerSecond: number | null | undefined): string {
  if (bytesPerSecond === null || bytesPerSecond === undefined) return '--'
  return `${formatBytes(bytesPerSecond)}/s`
}
import ChatPanel from '@/components/ChatPanel.vue'
const DiskPanel = defineAsyncComponent(() => import('@/components/DiskPanel.vue'))
import ActivityPopup from '@/components/ActivityPopup.vue'
import AlertsPopup from '@/components/AlertsPopup.vue'
import AssistantPopup from '@/components/AssistantPopup.vue'
import ExtensionsPopup from '@/components/ExtensionsPopup.vue'
import SkinPopup from '@/components/SkinPopup.vue'
const CleanPanel = defineAsyncComponent(() => import('@/components/CleanPanel.vue'))
import ComputerAccessPopup from '@/components/ComputerAccessPopup.vue'
import MobileAccessPopup from '@/components/MobileAccessPopup.vue'
import MirrorPopup from '@/components/MirrorPopup.vue'
import ImagePopup from '@/components/ImagePopup.vue'
import VideoPopup from '@/components/VideoPopup.vue'
import OfflineNotice from '@/components/OfflineNotice.vue'
import ContextMenu from '@/components/ContextMenu.vue'
const ProcessPanel = defineAsyncComponent(() => import('@/components/ProcessPanel.vue'))
import SettingsPanel from '@/components/SettingsPanel.vue'
import TopBar from '@/components/TopBar.vue'
import UsagePopup from '@/components/UsagePopup.vue'
import VoicePickerPopup from '@/components/VoicePickerPopup.vue'
import PortraitPicker from '@/components/PortraitPicker.vue'
import { portraitState, savePortraitPreferences } from '@/avatar/portraitState'
import { fetchVoices } from '@/api/bridge'
import PetForgePopup from '@/components/PetForgePopup.vue'
const TrendPanel = defineAsyncComponent(() => import('@/components/TrendPanel.vue'))
import VoiceCore from '@/components/VoiceCore.vue'
import { useSystemStore } from '@/stores/system'
import { useVoiceStore } from '@/stores/voice'
import DesktopPortrait from '@/avatar/DesktopPortrait.vue'
import { startAudioChannel, stopAudioChannel } from '@/audio/channel'
import { applyStoredSkin, currentSkin } from '@/theme'

const store = useSystemStore()
const voice = useVoiceStore()
const settingsOpen = ref(false)
const systemsOpen = ref(false)
const voicePanelOpen = ref(true)
const portraitsOpen = ref(false)

/**
 * A save must be visible without a restart, in the two places a restart used to be
 * the only way: the poll cadence, and the chat header's model dropdown (which now
 * lists whatever the settings panel just wrote).
 */
function onSettingsSaved(snapshot: SettingsSnapshot): void {
  store.setIntervalMs(snapshot.telemetry_interval_ms)
  window.dispatchEvent(new CustomEvent('jarvis-settings-saved'))
}

/**
 * The popup switched the listening port, so the bar reads it again.
 *
 * One event rather than a shared copy of the state: the label is a reading of a
 * socket, and a socket has exactly one owner -- Python.
 */
function announcePhoneChange(label: string): void {
  window.dispatchEvent(new CustomEvent('jarvis-phone-changed', { detail: label }))
}
const activityOpen = ref(false)
const alertsOpen = ref(false)
const accessOpen = ref(false)
const phoneOpen = ref(false)
const mirrorOpen = ref(false)
const imagesOpen = ref(false)
const videosOpen = ref(false)
const usageOpen = ref(false)
const voiceOpen = ref(false)
const petOpen = ref(false)
/** Bumped when the pet picker closes, so 设置 re-reads who is standing there. */
const petClosed = ref(0)
/** Bumped when the picker closes, so 设置 re-reads which voice is now in force. */
const voiceClosed = ref(0)

/** Closing the picker is also the signal that the in-force voice may have changed. */
async function closeVoicePicker(): Promise<void> {
  voiceOpen.value = false
  voiceClosed.value += 1
  try {
    const answer=await fetchVoices()
    if(!answer.error && answer.current) await savePortraitPreferences({...portraitState,bindings:{...portraitState.bindings,[portraitState.character]:answer.current}})
  } catch(error) { store.error='音色已更改，但形象绑定未能保存：'+String(error) }
}

/** Same bargain as the voice picker: the row in 设置 has to say what the panel changed. */
function closePetForge(): void {
  petOpen.value = false
  petClosed.value += 1
}
const assistantOpen = ref(false)
const extensionsOpen = ref(false)
const skinOpen = ref(false)

onMounted(() => {
  // Before the first frame: a flash of the wrong palette reads as a restart bug.
  applyStoredSkin()
  // And the other half of the same skin: her two desktop cards are painted by Python,
  // which has no copy of these numbers, so they would stay the default blue-black while
  // everything else recoloured. Fire-and-forget -- a build with no pet on the desktop
  // has nothing to recolour, and that is not an error worth a banner.
  void reportPetPalette(currentSkin()).catch(() => undefined)
  void store.start(1500)
  void voice.start()
  // Before any answer can arrive: this is what tells Python whether the page or the
  // speaker is going to be the one making sound.
  void startAudioChannel()
})
onBeforeUnmount(() => {
  store.stop()
  voice.stop()
  stopAudioChannel()
})
</script>

<style scoped>
.app {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100vh;
}

.app__error,
.app__mock {
  margin: 0;
  padding: 6px 16px;
  font-size: 12px;
  letter-spacing: 0.04em;
}

.app__error {
  color: var(--hud-red);
  background: rgba(255, 93, 93, 0.1);
  border-bottom: 1px solid rgba(255, 93, 93, 0.3);
}

.app__mock {
  color: var(--hud-amber);
  background: rgba(255, 181, 71, 0.08);
  border-bottom: 1px solid rgba(255, 181, 71, 0.25);
}

.app__grid {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr) 320px;
  /* 清理面板要能滚几百行，所以给它最大的一份；趋势图和对话各占一屏的零头。
     之前是 1.1/1/0.95，清理反而分到最小，590 项只露出 1-2 行。 */
  grid-template-rows: minmax(0, 1fr) minmax(0, 0.9fr) minmax(0, 1.5fr);
  gap: 12px;
  padding: 12px 16px 16px;
}

.rings {
  grid-row: span 3;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
  /* Without this the core's minimum height wins and paints over the readouts
     instead of being clipped, when the window is dragged to its smallest size. */
  overflow: hidden;
}

/*
 * The rail splits between the character and the core, and both are allowed to
 * collapse: at the window's minimum height the gauges and the status text matter
 * more than either picture, so neither image gets to push them out of the panel.
 */
.rings__avatar {
  flex: 1 1 54%;
  min-height: 140px;
  width: 100%;
}

/* The core owns the rest of the top of the rail and absorbs its slack. */
.rings__core {
  flex: 1 1 46%;
  min-height: 0;
  width: 100%;
}

.rings__row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.rings__stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}

.rings__row--empty {
  grid-template-columns: 1fr;
  align-items: center;
  justify-items: center;
  min-height: 64px;
  border: 1px dashed rgba(77, 216, 255, 0.25);
  border-radius: var(--hud-radius);
  color: var(--hud-dim);
  font-size: 12px;
  letter-spacing: 0.1em;
}

.rings__facts {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.rings__meta {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: 12px;
}

.rings__cores {
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: 46px;
  padding: 4px 0;
  border-block: 1px solid rgba(77, 216, 255, 0.1);
}

/*
 * Fixed height, animated with ``scaleY``. A percentage-``height`` transition is not
 * composited: twelve of them meant a layout pass on the main thread every 1.5s,
 * and that layout is exactly what the canvas used to have to re-measure around.
 */
.rings__core-bar {
  flex: 1;
  height: 100%;
  transform-origin: bottom;
  background: linear-gradient(180deg, var(--hud-cyan), rgba(77, 216, 255, 0.2));
  transition: transform 0.4s ease;
}

.app__trend {
  grid-column: 2;
  grid-row: 1;
}

/*
 * The trend chart used to own two rows. Chat needs one: a window that can show
 * telemetry but not answer a typed question is a dashboard, not an assistant.
 */
.app__chat {
  grid-column: 2;
  grid-row: 2;
  min-height: 0;
}

.app__clean {
  grid-column: 2;
  grid-row: 3;
}

.app__procs {
  grid-row: span 2;
}
</style>

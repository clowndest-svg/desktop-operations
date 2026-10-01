<template>
  <div class="hud-backdrop"></div>

  <div class="app">
    <TopBar
        @settings="settingsOpen = true"
        @activity="activityOpen = true"
        @access="accessOpen = true"
      @assistant="assistantOpen = true"
      />

    <p v-if="store.error" class="app__error">{{ store.error }}</p>
    <p v-else-if="runningMocked" class="app__mock">浏览器预览模式 · 数据为模拟生成，非真实机器读数</p>

    <main class="app__grid">
      <section class="hud-panel rings">
        <AvatarStage class="rings__avatar" />
        <VoiceCore class="rings__core" />

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
      <ChatPanel class="app__chat" @usage="usageOpen = true" />
      <CleanPanel class="app__clean" />
      <DiskPanel />
      <ProcessPanel class="app__procs" />
    </main>

    <SettingsPanel :open="settingsOpen" @close="settingsOpen = false" @saved="onSettingsSaved" />
    <ActivityPopup :open="activityOpen" @close="activityOpen = false" />
    <ComputerAccessPopup :open="accessOpen" @close="accessOpen = false" />
    <AssistantPopup :open="assistantOpen" @close="assistantOpen = false" />

    <UsagePopup :open="usageOpen" @close="usageOpen = false" />
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { formatBytes, runningMocked, type SettingsSnapshot } from '@/api/bridge'

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
import DiskPanel from '@/components/DiskPanel.vue'
import ActivityPopup from '@/components/ActivityPopup.vue'
import AssistantPopup from '@/components/AssistantPopup.vue'
import CleanPanel from '@/components/CleanPanel.vue'
import ComputerAccessPopup from '@/components/ComputerAccessPopup.vue'
import ProcessPanel from '@/components/ProcessPanel.vue'
import SettingsPanel from '@/components/SettingsPanel.vue'
import TopBar from '@/components/TopBar.vue'
import UsagePopup from '@/components/UsagePopup.vue'
import TrendPanel from '@/components/TrendPanel.vue'
import VoiceCore from '@/components/VoiceCore.vue'
import { useSystemStore } from '@/stores/system'
import { useVoiceStore } from '@/stores/voice'
import AvatarStage from '@/avatar/AvatarStage.vue'
import { startAudioChannel, stopAudioChannel } from '@/audio/channel'
import { applyStoredSkin } from '@/theme'

const store = useSystemStore()
const voice = useVoiceStore()
const settingsOpen = ref(false)

/**
 * A save must be visible without a restart, in the two places a restart used to be
 * the only way: the poll cadence, and the chat header's model dropdown (which now
 * lists whatever the settings panel just wrote).
 */
function onSettingsSaved(snapshot: SettingsSnapshot): void {
  store.setIntervalMs(snapshot.telemetry_interval_ms)
  window.dispatchEvent(new CustomEvent('jarvis-settings-saved'))
}
const activityOpen = ref(false)
const accessOpen = ref(false)
const usageOpen = ref(false)
const assistantOpen = ref(false)

onMounted(() => {
  // Before the first frame: a flash of the wrong palette reads as a restart bug.
  applyStoredSkin()
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

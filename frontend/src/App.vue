<template>
  <div class="hud-backdrop"></div>

  <div class="app">
    <TopBar />

    <p v-if="store.error" class="app__error">{{ store.error }}</p>
    <p v-else-if="runningMocked" class="app__mock">浏览器预览模式 · 数据为模拟生成，非真实机器读数</p>

    <main class="app__grid">
      <section class="hud-panel rings">
        <header class="hud-title">核心指标 · VITALS</header>
        <!--
          A gauge pinned at 0% reads as "the machine is idle", which is a different
          claim from "we could not read the machine". When telemetry fails, say the
          latter instead of drawing a confident empty dial.
        -->
        <div v-if="store.error" class="rings__row rings__row--empty">
          <span>无读数</span>
        </div>
        <div v-else class="rings__row">
          <HudGauge label="CPU" :value="store.cpuPercent" />
          <HudGauge label="内存" :value="store.memoryPercent" />
        </div>
        <div class="rings__meta">
          <span class="hud-label">逻辑核心</span>
          <span class="hud-num">{{ store.cores || '--' }}</span>
        </div>
        <div class="rings__cores">
          <span
            v-for="(core, index) in store.metrics.cpu?.per_core ?? []"
            :key="index"
            class="rings__core"
            :title="`#${index + 1} ${core.toFixed(0)}%`"
            :style="{ height: `${Math.max(6, core)}%` }"
          ></span>
        </div>
        <div class="rings__meta">
          <span class="hud-label">内存占用</span>
          <span class="hud-num">{{ store.metrics.memory ? formatBytes(store.metrics.memory.used_bytes ?? 0) : '--' }}</span>
        </div>
      </section>

      <TrendPanel class="app__trend" />
      <ChatPanel class="app__chat" />
      <CleanPanel class="app__clean" />
      <DiskPanel />
      <ProcessPanel class="app__procs" />
    </main>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue'
import { formatBytes, runningMocked } from '@/api/bridge'
import ChatPanel from '@/components/ChatPanel.vue'
import DiskPanel from '@/components/DiskPanel.vue'
import CleanPanel from '@/components/CleanPanel.vue'
import HudGauge from '@/components/HudGauge.vue'
import ProcessPanel from '@/components/ProcessPanel.vue'
import TopBar from '@/components/TopBar.vue'
import TrendPanel from '@/components/TrendPanel.vue'
import { useSystemStore } from '@/stores/system'
import { useVoiceStore } from '@/stores/voice'

const store = useSystemStore()
const voice = useVoiceStore()

onMounted(() => {
  void store.start(1500)
  void voice.start()
})
onBeforeUnmount(() => {
  store.stop()
  voice.stop()
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
  grid-template-rows: minmax(0, 1.1fr) minmax(0, 1fr) minmax(0, 0.95fr);
  gap: 12px;
  padding: 12px 16px 16px;
}

.rings {
  grid-row: span 3;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
}

.rings__row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}

.rings__row--empty {
  grid-template-columns: 1fr;
  align-items: center;
  justify-items: center;
  min-height: 96px;
  border: 1px dashed rgba(77, 216, 255, 0.25);
  color: var(--hud-dim);
  font-size: 12px;
  letter-spacing: 0.1em;
}

.rings__meta {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: 12px;
}

/* Live per-core bars: the detail that makes the panel feel like an instrument. */
.rings__cores {
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: 54px;
  padding: 4px 0;
  border-block: 1px solid rgba(77, 216, 255, 0.1);
}

.rings__core {
  flex: 1;
  min-height: 3px;
  background: linear-gradient(180deg, var(--hud-cyan), rgba(77, 216, 255, 0.25));
  transition: height 0.4s ease;
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

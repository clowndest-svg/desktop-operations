<template>
  <header class="bar">
    <div class="bar__brand">
      <span class="bar__mark">◈</span>
      <span class="bar__name">小夜</span>
      <span class="hud-label bar__ver">v{{ info.version }} · {{ info.engine }}</span>
    </div>

    <div class="bar__states">
      <span class="bar__chip" :title="store.error || '数据链路正常'">
        <span class="hud-dot" :class="linkClass"></span>
        <span class="hud-label">{{ store.connected ? '遥测正常' : '遥测中断' }}</span>
      </span>

      <!--
        Two axes, one chip: ``phase`` says whether the microphone is available at
        all, ``turn`` says what it is doing. Showing "聆听中" after a failed start
        would be the kind of lie that costs a demo.
      -->
      <span class="bar__chip" :title="voice.hint || voice.label">
        <span class="hud-dot" :class="voice.dotClass"></span>
        <span class="hud-label">{{ voice.label }}</span>
      </span>
      <span v-if="voice.error" class="bar__chip bar__chip--warn" :title="voice.error">
        <span class="hud-label">语音调用失败</span>
      </span>
      <button
        v-if="!voice.enabled"
        class="hud-btn bar__voice"
        type="button"
        :disabled="voice.loading"
        @click="voice.enable()"
      >
        {{ voice.loading ? '加载中…' : '启用语音' }}
      </button>
      <button v-else class="hud-btn bar__voice" type="button" @click="voice.mute()">释放麦克风</button>

      <span v-if="store.warnings.length" class="bar__chip bar__chip--warn" :title="store.warnings.join('\n')">
        <span class="hud-label">告警 {{ store.warnings.length }}</span>
      </span>

      <span class="bar__chip">
        <span class="hud-label">运行</span>
        <span class="hud-num bar__uptime">{{ uptime }}</span>
      </span>

      <span class="hud-num bar__clock">{{ clock }}</span>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { fetchAppInfo, formatUptime, type AppInfo } from '@/api/bridge'
import { useSystemStore } from '@/stores/system'
import { useVoiceStore } from '@/stores/voice'

const store = useSystemStore()
const voice = useVoiceStore()

const info = ref<AppInfo>({ name: '小夜', version: '--', engine: '--' })
const clock = ref('')

const linkClass = computed(() => (store.connected ? 'live' : 'error'))
const uptime = computed(() => formatUptime(store.metrics.uptime_seconds ?? 0))

let timer: ReturnType<typeof setInterval> | undefined

function tickClock(): void {
  clock.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
}

onMounted(async () => {
  tickClock()
  timer = setInterval(tickClock, 1000)
  info.value = await fetchAppInfo()
})

onBeforeUnmount(() => {
  if (timer !== undefined) clearInterval(timer)
})
</script>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-bottom: 1px solid var(--hud-line);
  background: linear-gradient(180deg, rgba(9, 24, 40, 0.9), rgba(4, 7, 13, 0.5));
}

.bar__brand {
  display: flex;
  align-items: baseline;
  gap: 8px;
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
  gap: 14px;
}

.bar__chip {
  display: flex;
  align-items: center;
  gap: 6px;
}

.bar__chip--warn .hud-label {
  color: var(--hud-amber);
}

.bar__voice {
  padding: 2px 10px;
  font-size: 11px;
}

.bar__uptime {
  font-size: 12px;
  color: var(--hud-text);
}

.bar__clock {
  font-size: 13px;
  color: var(--hud-cyan);
  letter-spacing: 0.06em;
  min-width: 74px;
  text-align: right;
}
</style>

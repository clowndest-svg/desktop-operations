<template>
  <section class="hud-panel disk">
    <header class="hud-title">
      存储 · STORAGE
      <span class="disk__hint hud-label">{{ store.disks.length }} 个分区</span>
    </header>

    <ul v-if="store.disks.length" class="disk__list">
      <li v-for="disk in store.disks" :key="disk.mount" class="disk__row">
        <div class="disk__head">
          <span class="hud-num disk__mount">{{ disk.mount }}</span>
          <span class="hud-label">{{ disk.fstype || '—' }}</span>
          <span class="hud-num disk__pct" :style="{ color: tone(disk.percent) }">
            {{ disk.percent.toFixed(0) }}%
          </span>
        </div>
        <div class="disk__bar">
          <div class="disk__fill" :style="{ width: `${disk.percent}%`, background: tone(disk.percent) }"></div>
        </div>
        <div class="hud-label disk__meta">
          可用 {{ formatBytes(disk.free_bytes) }} / 共 {{ formatBytes(disk.total_bytes) }}
        </div>
      </li>
    </ul>
    <p v-else class="hud-label disk__empty">未读取到分区信息</p>
  </section>
</template>

<script setup lang="ts">
import { formatBytes } from '@/api/bridge'
import { useSystemStore } from '@/stores/system'

const store = useSystemStore()

function tone(percent: number): string {
  if (percent >= 90) return 'var(--hud-red)'
  if (percent >= 75) return 'var(--hud-amber)'
  return 'var(--hud-cyan)'
}
</script>

<style scoped>
.disk {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
}

.disk__hint {
  margin-left: auto;
  letter-spacing: 0;
}

.disk__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.disk__head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.disk__mount {
  font-size: 13px;
  color: var(--hud-text);
}

.disk__pct {
  margin-left: auto;
  font-size: 13px;
}

.disk__bar {
  height: 4px;
  margin: 5px 0 3px;
  background: rgba(77, 216, 255, 0.12);
}

.disk__fill {
  height: 100%;
  box-shadow: 0 0 8px currentColor;
  transition: width 0.5s;
}

.disk__meta {
  letter-spacing: 0.02em;
}

.disk__empty {
  padding: 8px 0;
}
</style>

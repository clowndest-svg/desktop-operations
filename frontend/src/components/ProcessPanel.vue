<template>
  <section class="hud-panel procs">
    <header class="hud-title">
      占用排行 · TOP PROCESSES
      <span class="hud-label procs__hint">按内存</span>
    </header>

    <table v-if="store.processes.length" class="procs__table">
      <thead>
        <tr>
          <th>进程</th>
          <th class="procs__num">内存</th>
          <th class="procs__num">CPU</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="proc in store.processes" :key="proc.pid">
          <td class="procs__name" :title="`PID ${proc.pid}`">{{ truncate(proc.name) }}</td>
          <td class="procs__num hud-num">{{ formatBytes(proc.memory_bytes) }}</td>
          <td class="procs__num hud-num" :style="{ color: cpuTone(proc.cpu_percent) }">
            {{ proc.cpu_percent.toFixed(1) }}%
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="hud-label procs__empty">未读取到进程</p>
  </section>
</template>

<script setup lang="ts">
import { formatBytes } from '@/api/bridge'
import { useSystemStore } from '@/stores/system'

const store = useSystemStore()

function truncate(name: string): string {
  return name.length > 22 ? `${name.slice(0, 21)}…` : name
}

function cpuTone(percent: number): string {
  if (percent >= 50) return 'var(--hud-red)'
  if (percent >= 20) return 'var(--hud-amber)'
  return 'var(--hud-text)'
}
</script>

<style scoped>
.procs {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  min-height: 0;
}

.procs__hint {
  margin-left: auto;
  letter-spacing: 0;
}

.procs__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.procs__table th {
  text-align: left;
  font-weight: 400;
  font-size: 10px;
  letter-spacing: 0.1em;
  color: var(--hud-dim);
  padding-bottom: 5px;
  border-bottom: 1px solid var(--hud-line);
}

.procs__table td {
  padding: 4px 0;
  border-bottom: 1px solid rgba(77, 216, 255, 0.06);
}

.procs__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 0;
  width: 58%;
}

.procs__num {
  text-align: right;
}

.procs__empty {
  padding: 6px 0;
}
</style>

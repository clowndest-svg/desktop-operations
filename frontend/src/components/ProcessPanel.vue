<template>
  <section class="hud-panel procs">
    <header class="hud-title">
      占用排行 · TOP
      <span class="procs__tools">
        <button
          class="hud-btn"
          type="button"
          :disabled="busy || pickedList.length === 0"
          :title="pickedList.length ? `结束这 ${pickedList.length} 个进程` : '先勾选左边的行'"
          @click="ask"
        >
          结束进程
        </button>
      </span>
    </header>

    <table v-if="store.processes.length" class="procs__table">
      <thead>
        <tr>
          <th class="procs__pick"></th>
          <th>进程</th>
          <th class="procs__num">内存</th>
          <th class="procs__num">CPU</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="proc in store.processes" :key="proc.pid">
          <td class="procs__pick">
            <input
              type="checkbox"
              :checked="picked[proc.pid] !== undefined"
              :disabled="busy"
              :title="`PID ${proc.pid}`"
              @change="toggle(proc.pid, proc.name)"
            />
          </td>
          <td class="procs__name" :title="`PID ${proc.pid}`">{{ truncate(proc.name) }}</td>
          <td class="procs__num hud-num">{{ formatBytes(proc.memory_bytes) }}</td>
          <!--
            null means "this process was discovered after the last walk", not
            "this process is idle". Showing 0.0% for it puts a confident lie in a
            column the operator reads as a health check.
          -->
          <td class="procs__num hud-num" :style="{ color: cpuTone(proc.cpu_percent) }">
            {{ proc.cpu_percent === null ? '—' : proc.cpu_percent.toFixed(1) + '%' }}
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="hud-label procs__empty">未读取到进程</p>

    <!--
      The second click. Same shape as the cleanup confirmation, for the same reason:
      a tick is "which ones", and only a deliberate press on 确认 is "do it". Ending a
      process is not undoable, and the list the operator ticked may already be a
      minute old -- the name is re-checked at the moment of the act, so what is
      written here is what the tool will refuse if it no longer matches.
    -->
    <div v-if="confirming" class="procs__confirm">
      <p>
        即将<strong>结束</strong> {{ pickedList.length }} 个进程：{{ names }}。
        结束等于强杀，程序里没保存的东西会直接丢，多数情况下无法撤销。
      </p>
      <p class="procs__confirm-note">
        系统关键进程（lsass / svchost / dwm 等）和小夜自己的窗口会被逐个拒绝，
        即使它们被勾上。
      </p>
      <div class="procs__confirm-actions">
        <button class="hud-btn danger" type="button" :disabled="busy" @click="commit">确认结束</button>
        <button class="hud-btn" type="button" @click="confirming = false">取消</button>
      </div>
    </div>

    <div v-else-if="outcomes.length" class="procs__result">
      <div v-for="(entry, index) in outcomes" :key="index" class="procs__row">
        <span :class="entry.ok ? 'procs__ok' : 'procs__bad'">{{ entry.ok ? '已结束' : '未结束' }}</span>
        <span class="procs__row-name">{{ entry.name }}</span>
        <span class="hud-label">{{ entry.note }}</span>
      </div>
      <button class="hud-btn procs__dismiss" type="button" @click="outcomes = []">收起</button>
    </div>

    <p v-else-if="error" class="procs__error">{{ error }}</p>
  </section>
</template>

<script setup lang="ts">
/**
 * The process ranking, and the door from "I can see it" to "I can stop it".
 *
 * Ticks are keyed by PID but remember the name they were made under: the table is
 * redrawn every poll, and a tick that only stored a number would silently follow
 * that number if the process exited and Windows handed it to something else.
 */
import { computed, ref } from 'vue'
import { formatBytes, killProcesses, type ProcessTarget } from '@/api/bridge'
import { useSystemStore } from '@/stores/system'

const store = useSystemStore()

const picked = ref<Record<number, string>>({})
const confirming = ref(false)
const busy = ref(false)
const error = ref('')
const outcomes = ref<
  Array<{ pid: number; name: string; ok: boolean; note: string }>
>([])

const pickedList = computed<ProcessTarget[]>(() =>
  Object.entries(picked.value).map(([pid, name]) => ({ pid: Number(pid), name })),
)

const names = computed(() => pickedList.value.map((entry) => entry.name).join('、'))

function toggle(pid: number, name: string): void {
  const next = { ...picked.value }
  if (next[pid] === undefined) {
    next[pid] = name
  } else {
    delete next[pid]
  }
  picked.value = next
  outcomes.value = []
  error.value = ''
}

function ask(): void {
  error.value = ''
  outcomes.value = []
  confirming.value = true
}

async function commit(): Promise<void> {
  if (busy.value || pickedList.value.length === 0) return
  busy.value = true
  error.value = ''
  const targets = pickedList.value
  try {
    const report = await killProcesses(targets)
    outcomes.value = report.results
    error.value = report.error
    confirming.value = false
    if (!report.error) picked.value = {}
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    confirming.value = false
  } finally {
    busy.value = false
  }
}

function truncate(name: string): string {
  return name.length > 20 ? `${name.slice(0, 19)}…` : name
}

function cpuTone(percent: number | null): string {
  if (percent === null) return 'var(--hud-dim)'
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

.procs__tools {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
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

.procs__pick {
  width: 18px;
}

.procs__pick input {
  accent-color: var(--hud-red);
}

.procs__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 0;
  width: 54%;
}

.procs__num {
  text-align: right;
}

.procs__empty {
  padding: 6px 0;
}

.procs__confirm {
  border: 1px solid rgba(255, 93, 93, 0.45);
  border-radius: var(--hud-radius);
  background: rgba(255, 93, 93, 0.08);
  padding: 8px 12px;
}

.procs__confirm p {
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--hud-red);
}

.procs__confirm-note {
  color: var(--hud-dim) !important;
  font-size: 11px !important;
}

.procs__confirm-actions {
  display: flex;
  gap: 8px;
}

.procs__result {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-radius);
  background: rgba(3, 9, 16, 0.5);
  padding: 8px 10px;
  max-height: 40%;
  overflow-y: auto;
}

.procs__row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 11px;
}

.procs__row-name {
  color: var(--hud-text);
  font-family: var(--hud-mono);
}

.procs__ok {
  color: var(--hud-green);
}

.procs__bad {
  color: var(--hud-amber);
}

.procs__dismiss {
  align-self: flex-start;
  margin-top: 4px;
}

.procs__error {
  margin: 0;
  font-size: 12px;
  color: var(--hud-red);
}
</style>

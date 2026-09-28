import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { fetchSnapshot, type Metrics } from '@/api/bridge'

const HISTORY_LIMIT = 60

export interface Point {
  t: number
  v: number
}

const clampSeries = (series: Point[]): Point[] =>
  series.length > HISTORY_LIMIT ? series.slice(-HISTORY_LIMIT) : series

export const useSystemStore = defineStore('system', () => {
  const metrics = ref<Partial<Metrics>>({})
  const cpuHistory = ref<Point[]>([])
  const memoryHistory = ref<Point[]>([])
  const warnings = ref<string[]>([])
  const error = ref('')
  const connected = ref(false)
  const lastUpdated = ref(0)

  let timer: ReturnType<typeof setInterval> | undefined
  let inFlight = false
  // psutil's cpu_percent has no previous sample to compare against on the very
  // first read and returns 0.0. Memory is valid immediately, so only the CPU
  // series skips that first point — plotting it draws a false load cliff.
  let cpuPrimed = false

  async function tick(): Promise<void> {
    // Polling overlaps if a read is slow; skipping a beat is better than
    // queueing up a backlog of stale snapshots.
    if (inFlight) return
    inFlight = true
    try {
      const report = await fetchSnapshot()
      if (report.error) {
        error.value = report.error
        connected.value = false
        return
      }
      metrics.value = report.metrics
      warnings.value = report.warnings ?? []
      connected.value = true
      error.value = ''
      const at = (report.metrics.taken_at ?? Date.now() / 1000) * 1000
      if (report.metrics.cpu) {
        if (cpuPrimed) {
          cpuHistory.value = clampSeries([
            ...cpuHistory.value,
            { t: at, v: report.metrics.cpu.percent },
          ])
        }
        cpuPrimed = true
      }
      if (report.metrics.memory) {
        memoryHistory.value = clampSeries([
          ...memoryHistory.value,
          { t: at, v: report.metrics.memory.percent },
        ])
      }
      lastUpdated.value = Date.now()
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
      connected.value = false
    } finally {
      inFlight = false
    }
  }

  function start(intervalMs = 1500): void {
    if (timer !== undefined) return
    void tick()
    timer = setInterval(() => void tick(), intervalMs)
  }

  function stop(): void {
    if (timer === undefined) return
    clearInterval(timer)
    timer = undefined
  }

  const cpuPercent = computed(() => metrics.value.cpu?.percent ?? 0)
  const memoryPercent = computed(() => metrics.value.memory?.percent ?? 0)
  const cores = computed(() => metrics.value.cpu?.cores ?? 0)
  const disks = computed(() => metrics.value.disks ?? [])
  const processes = computed(() => metrics.value.top_processes ?? [])

  return {
    metrics,
    cpuHistory,
    memoryHistory,
    warnings,
    error,
    connected,
    lastUpdated,
    cpuPercent,
    memoryPercent,
    cores,
    disks,
    processes,
    tick,
    start,
    stop,
  }
})

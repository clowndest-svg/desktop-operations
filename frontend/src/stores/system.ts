import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { fetchSnapshot, onBackground, type Metrics } from '@/api/bridge'

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
  let detachBackground: (() => void) | undefined
  // True while the window is off screen, as the shell says it. Not derived from
  // document.hidden, which does not know about a hide-to-tray.
  let inTray = false
  let inFlight = false
  let requestId = 0
  let flightStartedAt = 0
  let intervalMs = 1500
  // psutil's cpu_percent has no previous sample to compare against on the very
  // first read and returns 0.0. Memory is valid immediately, so only the CPU
  // series skips that first point — plotting it draws a false load cliff. The
  // big numeral obeys the same rule: a 30px "0%" is a claim about the machine,
  // and for the first 1.5 seconds that claim is false.
  const cpuReady = ref(false)

  /**
   * Slow down while the window is hidden.
   *
   * Each poll costs ``psutil.process_iter`` over every process on the machine, and
   * that work is charged to *this* application's PID. A window nobody is looking at
   * has no reason to re-read the process table twice a second -- and "nobody is
   * looking at it" is the normal state of an assistant that lives in the background.
   *
   * ``document.hidden`` alone does not catch that normal state: closing the window
   * to the tray hides the WinForms form, and a hidden form's WebView2 page still
   * believes it is visible. The shell therefore says so itself, through
   * ``window.__jarvisBackground``.
   */
  const HIDDEN_INTERVAL_MS = 10_000

  /**
   * How long one telemetry read may take before the UI gives up on it.
   *
   * Generous on purpose: a normal walk is ~1.4 s and a voice-loaded machine can
   * stretch that a few-fold under the GIL. This is a liveness bound, not a
   * performance target -- anything past it is the "never answers at all" case, and
   * the cost of guessing wrong is only that a slow read is ignored.
   */
  const POLL_TIMEOUT_MS = 8_000

  /**
   * Whether the dashboard is supposed to be polling.
   *
   * This used to be spelled `timer !== undefined`, and that was a real bug: on the
   * first `start()` there is no timer yet, so `reschedule()` decided "not running,
   * nothing to do" and returned -- and no timer was ever created. The window polled
   * exactly once, at mount, and then showed that one reading forever. Every number
   * on screen was plausible and all of them were stale, which is the worst failure
   * this app can have: the whole reason the HUD exists is to be true about the
   * machine right now.
   *
   * A flag that says what it means, checked separately from the handle that holds
   * the interval, cannot be wrong in that direction.
   */
  let running = false

  function reschedule(): void {
    if (!running) return
    if (timer !== undefined) clearInterval(timer)
    const wait = inTray || document.hidden ? HIDDEN_INTERVAL_MS : intervalMs
    timer = setInterval(() => void tick(), wait)
  }

  function onBackgroundChange(foreground: boolean): void {
    inTray = !foreground
    reschedule()
    // Coming back is a request to see the machine now, the same as un-hiding a tab.
    if (foreground) void tick()
  }

  function onVisibility(): void {
    reschedule()
    // Coming back should show the current machine, not one from ten seconds ago.
    if (!document.hidden) void tick()
  }

  async function tick(): Promise<void> {
    const now = Date.now()
    // Polling overlaps if a read is slow; skipping a beat is better than queueing
    // up a backlog of stale snapshots.
    if (inFlight && now - flightStartedAt < POLL_TIMEOUT_MS) return
    if (inFlight) {
      // A bridge call that never answers used to stop the dashboard forever: the
      // flag stayed set, no further poll was issued, and the numbers on screen just
      // sat there looking current. Abandon that read, say so, and keep polling --
      // the window's whole job is to tell the truth about the machine.
      error.value = `遥测读取超过 ${POLL_TIMEOUT_MS / 1000} 秒未返回，已重新发起（界面数字可能停留在最后一次成功读取）`
      connected.value = false
    }
    const seq = ++requestId
    inFlight = true
    flightStartedAt = now
    try {
      const report = await fetchSnapshot()
      if (seq !== requestId) return // a newer poll already owns the screen
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
        if (cpuReady.value) {
          cpuHistory.value = clampSeries([
            ...cpuHistory.value,
            { t: at, v: report.metrics.cpu.percent },
          ])
        }
        cpuReady.value = true
      }
      if (report.metrics.memory) {
        memoryHistory.value = clampSeries([
          ...memoryHistory.value,
          { t: at, v: report.metrics.memory.percent },
        ])
      }
      lastUpdated.value = Date.now()
    } catch (err) {
      if (seq !== requestId) return
      error.value = err instanceof Error ? err.message : String(err)
      connected.value = false
    } finally {
      // Only the poll that still owns the screen may clear the flag; an abandoned
      // one must not reach back and unlock the wrong request.
      if (seq === requestId) inFlight = false
    }
  }

  function start(ms = 1500): void {
    if (running) return
    running = true
    intervalMs = ms
    void tick()
    reschedule()
    document.addEventListener('visibilitychange', onVisibility)
    detachBackground = onBackground(onBackgroundChange)
  }

  /**
   * Change the poll cadence while running. The settings panel saves a new interval
   * and the very next wait has to honour it -- "save, then restart to see the effect"
   * is exactly the sentence this round exists to delete.
   */
  function setIntervalMs(ms: number): void {
    intervalMs = ms
    reschedule()
  }

  function stop(): void {
    if (!running) return
    running = false
    if (timer !== undefined) {
      clearInterval(timer)
      timer = undefined
    }
    document.removeEventListener('visibilitychange', onVisibility)
    detachBackground?.()
    detachBackground = undefined
    inTray = false
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
    cpuReady,
    cpuPercent,
    memoryPercent,
    cores,
    disks,
    processes,
    tick,
    start,
    stop,
    setIntervalMs,
  }
})

/**
 * The only channel between the page and Python.
 *
 * Outside the desktop shell (plain browser during development) there is no
 * `pywebview`, so this falls back to a synthetic feed. That keeps `npm run dev`
 * usable without faking the whole backend — but the mock is clearly labelled so
 * nobody mistakes a browser screenshot for a real reading.
 */

import { ref } from 'vue'

export interface CpuMetrics {
  percent: number
  cores: number
  per_core: number[]
}

export interface MemoryMetrics {
  percent: number
  total_bytes: number
  used_bytes: number
  available_bytes: number
  swap_percent: number
}

export interface DiskMetrics {
  mount: string
  fstype: string
  percent: number
  total_bytes: number
  used_bytes: number
  free_bytes: number
}

export interface ProcessMetrics {
  pid: number
  name: string
  cpu_percent: number
  memory_bytes: number
}

export interface Metrics {
  taken_at: number
  uptime_seconds: number
  cpu: CpuMetrics
  memory: MemoryMetrics
  disks: DiskMetrics[]
  top_processes: ProcessMetrics[]
  warnings: string[]
}

export interface Report {
  metrics: Partial<Metrics>
  warnings: string[]
  error: string
}

export interface AppInfo {
  name: string
  version: string
  engine: string
}

export type JunkKind = 'exact' | 'loose_files'

export interface JunkItem {
  path: string
  size_bytes: number
  category: string
  is_directory: boolean
  modified_at: number
  /** `loose_files` means "this folder's scattered files", not the folder itself. */
  kind: JunkKind
  /** How many files the row stands for; 1 unless it is an aggregate. */
  member_count: number
  /** A few member names, so a collapsed row is still recognisable. */
  sample: string[]
  /** Which scan produced this row; a delete is only honoured against the current one. */
  scanned_at: number
}

export interface JunkGroup {
  category: string
  total_bytes: number
  items: JunkItem[]
}

export interface DiskPlan {
  groups: JunkGroup[]
  total_bytes: number
  skipped_protected: number
  truncated: boolean
  error: string
}

/** Voice availability: "can I talk to it". Mirrors jarvis.core.events.VoicePhase. */
export type VoicePhase = 'off' | 'loading' | 'running' | 'failed' | 'muted'

/** Turn state: "is it listening or thinking". Mirrors UiVoiceState. */
export type TurnPhase = 'idle' | 'listening' | 'processing'

export interface VoiceStatus {
  phase: VoicePhase
  detail: string
  keyword: string
}

export interface ChatReply {
  question: string
  answer: string
  error: string
}

export interface ChatTurn {
  role: 'user' | 'assistant'
  text: string
}

/** The live state Python pushes into the page; also pullable on mount. */
export interface UiSnapshot {
  voice_state: TurnPhase
  voice: VoicePhase
  voice_detail: string
  history: ChatTurn[]
  last_event: string
  interrupted: boolean
}

export interface DiskOutcome {
  deleted: string[]
  freed_bytes: number
  failed: [string, string][]
  log_path: string
  error: string
}

interface PywebviewApi {
  snapshot(): Promise<Report>
  app_info(): Promise<AppInfo>
  disk_scan(): Promise<DiskPlan>
  disk_delete(items: JunkItem[], confirmed: boolean): Promise<DiskOutcome>
  voice_status(): Promise<VoiceStatus>
  voice_enable(): Promise<VoiceStatus>
  voice_mute(): Promise<VoiceStatus>
  voice_talk(): Promise<VoiceStatus>
  chat_ask(text: string): Promise<ChatReply>
  chat_clear(): Promise<{ ok: boolean }>
  state_snapshot(): Promise<UiSnapshot>
}

declare global {
  interface Window {
    pywebview?: { api: PywebviewApi }
    /** Installed by the page; Python calls it through ``evaluate_js``. */
    __jarvisState?: (snapshot: UiSnapshot) => void
  }
}

const mock: PywebviewApi = {
  async snapshot() {
    const jitter = (base: number, spread: number) =>
      Math.max(0, Math.min(100, base + (Math.random() - 0.5) * spread))
    const total = 16 * 1024 ** 3
    const used = (total * jitter(62, 10)) / 100
    return {
      metrics: {
        taken_at: Date.now() / 1000,
        uptime_seconds: 262_300,
        cpu: {
          percent: jitter(34, 26),
          cores: 12,
          per_core: Array.from({ length: 12 }, () => jitter(34, 50)),
        },
        memory: {
          percent: (used / total) * 100,
          total_bytes: total,
          used_bytes: used,
          available_bytes: total - used,
          swap_percent: jitter(12, 8),
        },
        disks: [
          { mount: 'C:\\', fstype: 'NTFS', percent: 78, total_bytes: 512 * 1024 ** 3, used_bytes: 399 * 1024 ** 3, free_bytes: 113 * 1024 ** 3 },
          { mount: 'D:\\', fstype: 'NTFS', percent: 41, total_bytes: 1024 * 1024 ** 3, used_bytes: 420 * 1024 ** 3, free_bytes: 604 * 1024 ** 3 },
        ],
        top_processes: [
          { pid: 1, name: 'MockProcessA', cpu_percent: 12.5, memory_bytes: 1.4 * 1024 ** 3 },
          { pid: 2, name: 'MockProcessB', cpu_percent: 4.1, memory_bytes: 0.5 * 1024 ** 3 },
        ],
        warnings: [],
      },
      warnings: [],
      error: '',
    }
  },
  async app_info() {
    return { name: '小夜', version: 'dev', engine: 'browser-mock' }
  },
  async disk_scan() {
    const now = Date.now() / 1000
    const exact = (path: string, size_bytes: number, is_directory: boolean): JunkItem => ({
      path,
      size_bytes,
      category: '临时文件',
      is_directory,
      modified_at: now - 3600,
      kind: 'exact',
      member_count: 1,
      sample: [],
      scanned_at: now,
    })
    return {
      groups: [
        {
          category: '临时文件',
          total_bytes: 3.2 * 1024 ** 3,
          items: [
            exact('C:\\mock\\temp\\buildcache', 1024 ** 3, true),
            exact('C:\\mock\\temp\\loose.log', 200 * 1024 ** 2, false),
            {
              // The shape %TEMP% really produces: one row for a thousand leftovers.
              path: 'C:\\Users\\me\\AppData\\Local\\Temp',
              size_bytes: 2 * 1024 ** 3,
              category: '临时文件',
              is_directory: false,
              modified_at: now - 7200,
              kind: 'loose_files',
              member_count: 1646,
              sample: ['tmp8a3f.tmp', 'JET6E21.tmp', 'wct1A2B.tmp', '0013f4ab.log', 'notes.~tmp'],
              scanned_at: now,
            },
          ],
        },
      ],
      total_bytes: 3.2 * 1024 ** 3,
      skipped_protected: 1,
      truncated: false,
      error: '',
    }
  },
  async voice_status() {
    return { ...mockVoice.status() }
  },
  async voice_enable() {
    return mockVoice.enable()
  },
  async voice_mute() {
    return mockVoice.mute()
  },
  async voice_talk() {
    return mockVoice.talk()
  },
  async chat_ask(text) {
    const answer = '（浏览器预览模式的样例回答）我是小夜。桌面版里这句话来自真实模型。'
    // The desktop transcript is pushed from Python; the mock has to keep its own,
    // or the panel is empty in preview and nobody can tell that from a bug.
    mockHistory.push({ role: 'user', text }, { role: 'assistant', text: answer })
    return { question: text, answer, error: '' }
  },
  async chat_clear() {
    mockHistory.length = 0
    return { ok: true }
  },
  async state_snapshot() {
    return {
      voice_state: mockTurn,
      voice: mockVoice.status().phase,
      voice_detail: mockVoice.status().detail,
      history: [...mockHistory],
      last_event: 'mock',
      interrupted: false,
    }
  },
  async disk_delete(items) {
    const files = items.reduce((sum, item) => sum + Math.max(1, item.member_count), 0)
    return {
      deleted: items.map((item) => item.path),
      freed_bytes: items.reduce((sum, item) => sum + item.size_bytes, 0),
      failed: [],
      log_path: `C:\\mock\\deletions.jsonl (${files} files)`,
      error: '',
    }
  },
}

/** Stands in for the real microphone so ``npm run dev`` can exercise the panel. */
const mockVoice = {
  phase: 'off' as VoicePhase,
  detail: '',
  keyword: '你好小夜',
  timer: undefined as ReturnType<typeof setTimeout> | undefined,
  status(): VoiceStatus {
    return { phase: this.phase, detail: this.detail, keyword: this.phase === 'running' ? this.keyword : '' }
  },
  enable(): VoiceStatus {
    if (this.phase === 'running' || this.phase === 'loading') return this.status()
    this.phase = 'loading'
    this.detail = '正在加载语音模型（浏览器预览为模拟）'
    clearTimeout(this.timer)
    this.timer = setTimeout(() => {
      this.phase = 'running'
      this.detail = ''
    }, 1800)
    return this.status()
  },
  mute(): VoiceStatus {
    clearTimeout(this.timer)
    this.phase = 'muted'
    this.detail = '麦克风已释放'
    return this.status()
  },
  talk(): VoiceStatus {
    if (this.phase !== 'running') {
      return { ...this.status(), detail: MOCK_REFUSALS[this.phase] ?? '先点「启用语音」' }
    }
    mockTurn = 'listening'
    return { ...this.status(), detail: '在听，说完停一下即可' }
  },
}

const MOCK_REFUSALS: Record<VoicePhase, string> = {
  off: '先点「启用语音」，加载约 2 分钟',
  loading: '语音模型还在加载，等它变成「待唤醒」再按',
  running: '',
  failed: '语音不可用，状态条上有原因',
  muted: '麦克风已释放，先点「启用语音」',
}

let mockTurn: TurnPhase = 'idle'
const mockHistory: ChatTurn[] = []

const api = (): PywebviewApi | null => window.pywebview?.api ?? null

/**
 * Whether the current readings come from the synthetic browser feed.
 *
 * pywebview injects `window.pywebview` *after* the page script has run, so this
 * cannot be decided at import time — doing so labels real desktop readings as
 * mock data, which is worse than no badge at all.
 */
export const runningMocked = ref(true)

let resolved: Promise<PywebviewApi> | null = null

function waitForBridge(timeoutMs: number): Promise<PywebviewApi | null> {
  const existing = api()
  if (existing) return Promise.resolve(existing)
  return new Promise((resolve) => {
    const done = (value: PywebviewApi | null) => {
      window.removeEventListener('pywebviewready', onReady)
      resolve(value)
    }
    const onReady = () => done(api())
    window.addEventListener('pywebviewready', onReady, { once: true })
    setTimeout(() => done(api()), timeoutMs)
  })
}

function bridge(): Promise<PywebviewApi> {
  resolved ??= waitForBridge(2000).then((found) => {
    runningMocked.value = found === null
    return found ?? mock
  })
  return resolved
}

export function fetchSnapshot(): Promise<Report> {
  return bridge().then((target) => target.snapshot())
}

export function fetchAppInfo(): Promise<AppInfo> {
  return bridge().then((target) => target.app_info())
}

export function fetchDiskPlan(): Promise<DiskPlan> {
  return bridge().then((target) => target.disk_scan())
}

/**
 * Delete the entries a human ticked. `confirmed` is hardcoded true here on
 * purpose: this function is only called from the panel's second, explicit
 * confirmation step. Entries travel as whole objects rather than path strings
 * because one entry can stand for a thousand files, and Python has to be able to
 * tell that apart from "delete this folder".
 */
export function deleteJunk(items: JunkItem[]): Promise<DiskOutcome> {
  return bridge().then((target) => target.disk_delete(items, true))
}

/**
 * Ask a typed question. Blocks on the model, so callers own a busy flag.
 *
 * Errors arrive as ``{ error }`` rather than as rejections: the service turns
 * provider failures into that field, and the page should show the same shape for
 * "no API key" as for "the model said nothing".
 */
export function chatAsk(text: string): Promise<ChatReply> {
  return bridge().then((target) => target.chat_ask(text))
}

export function fetchVoiceStatus(): Promise<VoiceStatus> {
  return bridge().then((target) => target.voice_status())
}

export function enableVoice(): Promise<VoiceStatus> {
  return bridge().then((target) => target.voice_enable())
}

export function muteVoice(): Promise<VoiceStatus> {
  return bridge().then((target) => target.voice_mute())
}

/**
 * Open one spoken turn without the wake word.
 *
 * The answer is a status, not a boolean: when the press is refused, the `detail`
 * is the only thing the operator gets, so it has to say why.
 */
export function talkNow(): Promise<VoiceStatus> {
  return bridge().then((target) => target.voice_talk())
}

/** Forget the conversation and empty the transcript. */
export function clearChat(): Promise<{ ok: boolean }> {
  return bridge().then((target) => target.chat_clear())
}

/** Pull the current state once, so a page that mounted after the events still agrees. */
export function fetchUiSnapshot(): Promise<UiSnapshot> {
  return bridge().then((target) => target.state_snapshot())
}

/**
 * Receive pushed state. Returns the detach function.
 *
 * Python calls ``window.__jarvisState`` from its own thread; the page owns when it
 * starts listening, so registration and teardown live with the store that renders
 * it.
 */
export function onUiState(handler: (snapshot: UiSnapshot) => void): () => void {
  const previous = window.__jarvisState
  window.__jarvisState = (snapshot: UiSnapshot) => {
    if (snapshot.voice === 'running') {
      mockTurn = snapshot.voice_state
    }
    handler(snapshot)
  }
  return () => {
    window.__jarvisState = previous
  }
}

export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const exponent = Math.min(Math.floor(Math.log2(value) / 10), units.length - 1)
  const scaled = value / 2 ** (10 * exponent)
  return `${scaled.toFixed(scaled >= 100 || exponent === 0 ? 0 : 1)} ${units[exponent]}`
}

export function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '--'
  const days = Math.floor(seconds / 86_400)
  const hours = Math.floor((seconds % 86_400) / 3600)
  return days > 0 ? `${days}天 ${hours}时` : `${hours}时 ${Math.floor((seconds % 3600) / 60)}分`
}

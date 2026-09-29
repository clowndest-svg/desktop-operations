import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  enableVoice,
  fetchUiSnapshot,
  fetchVoiceStatus,
  muteVoice,
  onUiState,
  type TurnPhase,
  type UiSnapshot,
  type VoicePhase,
  type VoiceStatus,
} from '@/api/bridge'

/**
 * Voice availability and the current turn, as one honest indicator.
 *
 * Two axes arrive from two places and are kept apart on purpose:
 *
 * * ``phase`` -- "can I talk to it" -- is owned by Python's voice service and
 *   changes when models load or the microphone is released.
 * * ``turn`` -- "is it listening or thinking right now" -- is pushed from the
 *   capture thread while a conversation is in flight.
 *
 * Collapsing them is how a failed start ends up looking like a microphone that is
 * merely idle, which is the exact confusion this store exists to prevent.
 */
export const useVoiceStore = defineStore('voice', () => {
  const phase = ref<VoicePhase>('off')
  const detail = ref('')
  const keyword = ref('')
  const turn = ref<TurnPhase>('idle')
  const interrupted = ref(false)
  const busy = ref(false)
  const lastUpdated = ref(0)
  // Last bridge failure, shown in the header. Without this a rejected call leaves
  // the indicator on its previous value, and the operator's only feedback is
  // "nothing happened" -- indistinguishable from a dead button.
  const error = ref('')

  let detach: (() => void) | undefined

  function describe(err: unknown): string {
    return err instanceof Error ? err.message : String(err)
  }

  const enabled = computed(() => phase.value === 'running')
  const loading = computed(() => phase.value === 'loading')
  const failed = computed(() => phase.value === 'failed')
  const actionable = computed(() => !loading.value)

  /** What the indicator should say, in one short line. */
  const label = computed(() => {
    switch (phase.value) {
      case 'loading':
        return '语音加载中'
      case 'running':
        if (interrupted.value) return '已打断'
        return turn.value === 'processing' ? '思考中' : turn.value === 'listening' ? '聆听中' : '待唤醒'
      case 'muted':
        return '麦克风已释放'
      case 'failed':
        return '语音不可用'
      default:
        return '语音未启用'
    }
  })

  const dotClass = computed(() => {
    if (phase.value === 'failed') return 'error'
    if (phase.value === 'loading') return 'warn'
    if (phase.value !== 'running') return 'idle'
    // Barge-in is a headline feature; it should be visible for the moment it lasts.
    if (interrupted.value) return 'warn'
    // Armed and quiet pulses slowly; catching speech pulses fast; thinking is steady.
    return turn.value === 'listening' ? 'listening' : 'live'
  })

  const hint = computed(() => {
    if (error.value) return error.value
    if (phase.value === 'running' && keyword.value) return `说「${keyword.value}」唤醒`
    return detail.value
  })

  function apply(status: VoiceStatus): void {
    if (!status || typeof status.phase !== 'string') {
      error.value = '语音状态返回异常，未做任何变更'
      return
    }
    error.value = ''
    phase.value = status.phase
    detail.value = status.detail ?? ''
    if (status.keyword) keyword.value = status.keyword
    if (status.phase !== 'running') {
      // A released or failed loop cannot also be mid-turn.
      turn.value = 'idle'
      interrupted.value = false
    }
    lastUpdated.value = Date.now()
  }

  async function refresh(): Promise<void> {
    try {
      apply(await fetchVoiceStatus())
    } catch (err) {
      error.value = describe(err)
    }
  }

  async function enable(): Promise<void> {
    if (busy.value) return
    busy.value = true
    try {
      apply(await enableVoice())
    } catch (err) {
      error.value = describe(err)
    } finally {
      busy.value = false
    }
  }

  async function mute(): Promise<void> {
    if (busy.value) return
    busy.value = true
    try {
      apply(await muteVoice())
    } catch (err) {
      error.value = describe(err)
    } finally {
      busy.value = false
    }
  }

  /**
   * Start listening for pushes, then pull once.
   *
   * The order matters: a snapshot produced between the two is delivered by the
   * pull, so the indicator never sits on a stale reading just because the page
   * mounted after the microphone opened.
   */
  function applySnapshot(snapshot: UiSnapshot): void {
    phase.value = snapshot.voice
    detail.value = snapshot.voice_detail ?? ''
    turn.value = snapshot.voice === 'running' ? snapshot.voice_state : 'idle'
    interrupted.value = snapshot.interrupted
    lastUpdated.value = Date.now()
  }

  async function start(): Promise<void> {
    detach = onUiState(applySnapshot)
    // The pull is what makes the comment above true. ``voice_status()`` only knows
    // the phase (off/loading/running/...); the *turn* state -- listening vs thinking
    // -- is pushed from the capture thread, so a page that mounted mid-answer would
    // otherwise show "待唤醒" until the next transition happened to arrive.
    try {
      applySnapshot(await fetchUiSnapshot())
    } catch {
      // No bridge yet (plain browser, or the shell still starting): fall through to
      // the phase pull below rather than showing an error nobody can act on.
    }
    try {
      await refresh()
    } catch {
      // A missing bridge leaves the label at "语音未启用"; that is already honest.
    }
  }

  function stop(): void {
    detach?.()
    detach = undefined
  }

  return {
    phase,
    detail,
    error,
    keyword,
    turn,
    interrupted,
    busy,
    lastUpdated,
    enabled,
    loading,
    failed,
    actionable,
    label,
    dotClass,
    hint,
    refresh,
    enable,
    mute,
    start,
    stop,
  }
})

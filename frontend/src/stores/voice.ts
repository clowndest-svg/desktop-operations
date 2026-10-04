import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  clearChat,
  enableVoice,
  fetchUiSnapshot,
  fetchVoiceStatus,
  muteVoice,
  onUiState,
  talkNow,
  type ChatTurn,
  type ConversationCard,
  type StreamingTurn,
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
  /** The shared transcript: typed questions and spoken turns land in one list. */
  const history = ref<ChatTurn[]>([])
  /**
   * Every conversation tab, with what each one is doing. The panel shows one tab's
   * ``history`` at a time; this is how the others say they are still working.
   */
  const conversations = ref<ConversationCard[]>([])
  /** The answer currently being streamed, or null. Never part of ``history``. */
  const streaming = ref<StreamingTurn | null>(null)

  /**
   * Which animation the shell wants next to 「思考中」.
   *
   * It arrives on the snapshot rather than being fetched: the desktop figure draws
   * the same wait out of a different process, and the two disagreeing for a second
   * after a save is exactly what the operator would read as a bug.
   */
  const thinkingLoader = ref('dots')

  /**
   * Whether typed answers get read aloud -- the shell's answer, not a local guess.
   *
   * The switch also lives in the settings dialog, so a copy read from there on mount
   * would go stale the moment the other one moved.
   */
  const speaksTyped = ref(true)
  /**
   * One-shot answer to a press that was refused or accepted, shown next to the
   * button and cleared by the next pushed state. Distinct from ``error``, which is
   * a bridge failure and stays until it is fixed.
   */
  const notice = ref('')
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
    // A typed question is a turn with the microphone shut. Answering 「思考中」 only when
    // the voice stack happens to be loaded is what left the bar saying 「语音未启用」 for
    // six seconds while she was being asked something.
    if (turn.value === 'processing') return '思考中'
    switch (phase.value) {
      case 'loading':
        return '语音加载中'
      case 'running':
        if (interrupted.value) return '已打断'
        return turn.value === 'listening' ? '聆听中' : '待唤醒'
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
    if (turn.value === 'processing') return 'live'
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
      // The microphone being closed means it cannot have just been interrupted. It
      // does *not* mean nothing is in progress: a typed question is answered with the
      // microphone shut, and clearing the turn here would erase 「思考中」 mid-answer.
      // Snapshots own the turn axis; this call only knows about the phase.
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
    // Two axes, on purpose, and this is the line that used to merge them. ``voice``
    // answers "can I talk to it"; ``voice_state`` answers "what is it doing right now".
    // A typed question has a turn and no microphone, and pinning the turn to idle
    // whenever the mic was closed is what made the desktop figure sleep through a
    // conversation she was having.
    turn.value = snapshot.voice_state
    interrupted.value = snapshot.interrupted
    history.value = snapshot.history ?? []
    // The tab strip and the answer still arriving. Both optional in the type because a
    // bundle built against the older shell sends neither, and a panel that read them as
    // required would render an empty strip rather than a stale one.
    conversations.value = snapshot.conversations ?? []
    streaming.value = snapshot.streaming ?? null
    if (snapshot.thinking_loader) thinkingLoader.value = snapshot.thinking_loader
    if (typeof snapshot.speaks_typed === 'boolean') speaksTyped.value = snapshot.speaks_typed
    // A push supersedes whatever the last press said.
    notice.value = ''
    lastUpdated.value = Date.now()
  }

  /** Skip the wake word and open one spoken turn. */
  async function talk(): Promise<void> {
    if (busy.value) return
    busy.value = true
    try {
      const status = await talkNow()
      notice.value = status.detail ?? ''
      if (status.phase !== phase.value) apply(status)
    } catch (err) {
      error.value = describe(err)
    } finally {
      busy.value = false
    }
  }

  /** Forget the conversation, on both sides of the bridge. */
  async function clearTranscript(): Promise<void> {
    try {
      await clearChat()
      history.value = []
      notice.value = ''
    } catch (err) {
      error.value = describe(err)
    }
  }

  /**
   * Pull the transcript once without touching the indicator.
   *
   * A typed turn is answered by Python writing into the same transcript the
   * microphone writes into, but nothing guarantees a push arrives afterwards --
   * a quiet microphone has no reason to change state. Without this the panel
   * updates only when something else happens to move.
   */
  async function pullSnapshot(): Promise<void> {
    try {
      applySnapshot(await fetchUiSnapshot())
    } catch {
      // A missing bridge leaves whatever is already on screen; that is stale, not wrong.
    }
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
    notice,
    keyword,
    turn,
    interrupted,
    busy,
    history,
    conversations,
    streaming,
    thinkingLoader,
    speaksTyped,
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
    talk,
    clearTranscript,
    pullSnapshot,
    start,
    stop,
  }
})

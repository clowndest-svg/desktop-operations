/**
 * Wiring between the transport and the audio graph.
 *
 * Deliberately tiny and deliberately separate: ``api/bridge.ts`` knows how to talk
 * to Python, ``audio/speech.ts`` knows how to play samples, and neither should know
 * the other exists. This is the seam, plus the one behaviour that cannot live in
 * either of them -- an autoplay-blocked webview only unlocks on a gesture, so the
 * channel keeps a listener until the page is allowed to make sound.
 */

import { computed, ref } from 'vue'
import { onSpeech, reportAudioReadiness, requestSpeechStop, fetchAudioOutput } from '@/api/bridge'
import { speech, type Levels } from '@/audio/speech'

export type AudioOutput = 'unknown' | 'browser' | 'speaker'

/** Which device is playing the assistant. ``unknown`` only before the first read. */
export const output = ref<AudioOutput>('unknown')
export const outputReason = ref('')
/** True while Python is feeding samples to *this* page. */
export const talking = ref(false)
/** Preview mode only: the feed is synthesized here, not spoken by anyone. */
export const previewing = ref(false)

let detach: (() => void) | undefined
let unlock: (() => void) | undefined
let previewTimer: ReturnType<typeof setTimeout> | undefined

const apply = (state: { output: string; reason?: string }): void => {
  output.value = state.output === 'browser' ? 'browser' : 'speaker'
  outputReason.value = state.reason ?? ''
}

/**
 * Open the audio channel. Call once, after the bridge exists.
 *
 * The order is the whole design: install the page's entry point, *then* ask the
 * browser for an audio graph, *then* tell Python which of the two happened. Python
 * refuses to push anything until that last call lands, so a page that fails here
 * cannot be half-fed samples and half-silent.
 */
export async function startAudioChannel(): Promise<void> {
  detach?.()
  installAudioProbe()
  detach = onSpeech((message) => {
    if (message.flush) {
      speech.flush()
      talking.value = false
      return
    }
    speech.push(message)
    talking.value = true
  })
  const readiness = await speech.ensure()
  try {
    apply(await reportAudioReadiness(readiness.ok, readiness.reason))
  } catch {
    // No bridge (plain browser): the page is its own audience. Say so rather than
    // leaving the panel claiming the desktop shell told it something.
    output.value = readiness.ok ? 'browser' : 'speaker'
    outputReason.value = readiness.ok ? '' : readiness.reason || '没有桌面壳可通知'
  }
  if (!readiness.ok) armGestureUnlock()
  try {
    const current = await fetchAudioOutput()
    if (current.output === 'speaker' && current.reason) apply(current)
  } catch {
    // Preview mode has no opinion to fetch.
  }
}

/**
 * Chromium starts a fresh desktop webview with audio suspended, and the only thing
 * that clears it is a gesture the page can point at. The desktop shell relaxes the
 * policy for our own window, so this is the belt rather than the braces: a listener
 * that retries until the graph runs, then removes itself.
 */
function armGestureUnlock(): void {
  const resume = async () => {
    const readiness = await speech.ensure()
    if (!readiness.ok) return
    apply(await reportAudioReadiness(true, '').catch(() => ({ output: 'browser', reason: '' })))
    for (const kind of ['pointerdown', 'keydown'] as const) document.removeEventListener(kind, resume)
    unlock = undefined
  }
  for (const kind of ['pointerdown', 'keydown'] as const) document.addEventListener(kind, resume)
  unlock = () => {
    for (const kind of ['pointerdown', 'keydown'] as const) document.removeEventListener(kind, resume)
  }
}

export function stopAudioChannel(): void {
  detach?.()
  detach = undefined
  unlock?.()
  unlock = undefined
  clearTimeout(previewTimer)
  previewing.value = false
  talking.value = false
}

/** Tell Python to stop producing audio and retract what it already sent. */
export async function stopSpeech(): Promise<void> {
  speech.flush()
  talking.value = false
  previewing.value = false
  clearTimeout(previewTimer)
  try {
    await requestSpeechStop()
  } catch {
    // Preview mode: nothing else is talking, so the local stop already finished.
  }
}

export const isBrowserOutput = computed(() => output.value === 'browser')

/**
 * Set from the audio graph, not from a message count.
 *
 * "It is talking" has to stop when the queued samples stop, or the 「停下」 button
 * and the caption would outlive the voice by however long the last buffer happens to
 * be -- and after a flush, by forever.
 */
export function markTalking(value: boolean): void {
  if (talking.value !== value) talking.value = value
}

/** Read the analyser. Called from the draw loop, never from a reactive context. */
export function levels(): Levels {
  return speech.levels()
}

/**
 * A read-only look at the audio channel, for the webview's own devtools.
 *
 * A desktop window has no console attached and no network tab, so "the crown is
 * flat" would otherwise be undiagnosable from the machine that exhibits it -- and
 * flat-because-there-is-no-sound and flat-because-the-graph-never-started look
 * identical on screen. This costs one property and answers which of them happened
 * with ``-v`` open. It exposes no content and mutates nothing.
 */
export function installAudioProbe(): void {
  const target = window as unknown as { __jarvisAudioDebug?: () => Record<string, unknown> }
  target.__jarvisAudioDebug = () => ({
    output: output.value,
    reason: outputReason.value,
    talking: talking.value,
    previewing: previewing.value,
    ready: speech.ready,
    bytes_received: speech.receivedBytes,
    levels: speech.levels(),
  })
}

/**
 * Preview-only feed so ``npm run dev`` and a browser screenshot can show the
 * visualizer actually moving.
 *
 * It is synthetic on purpose and labelled as such wherever it renders: there is no
 * way to reach a real answer without the desktop shell and an API key, and a flat
 * line in a browser tab is not evidence about the desktop build either. What this
 * does prove is the part that is not about content -- that samples cross the
 * transport, get scheduled without seams, and reach the analyser.
 */
export function startPreviewSpeech(seconds = 3.2): void {
  if (window.pywebview) return
  previewing.value = true
  const rate = 24_000
  const chunks = 8
  let seq = 0
  const step = () => {
    if (!previewing.value) return
    const samples = Math.floor((rate * seconds) / chunks)
    const pcm = new Int16Array(samples)
    const base = 190 + 90 * Math.sin(seq / 2.4)
    for (let index = 0; index < samples; index += 1) {
      const t = index / rate
      const envelope = 0.35 + 0.65 * Math.abs(Math.sin(Math.PI * t * (3.2 + seq * 0.3)))
      const voiced =
        Math.sin(2 * Math.PI * base * t) * 0.6 +
        Math.sin(2 * Math.PI * base * 2.3 * t) * 0.26 +
        Math.sin(2 * Math.PI * base * 3.7 * t) * 0.12
      pcm[index] = Math.max(-32767, Math.min(32767, Math.round(voiced * envelope * 12000)))
    }
    speech.push({
      seq: seq + 1,
      sample_rate: rate,
      channels: 1,
      format: 'pcm_s16le',
      final: seq === chunks - 1,
      pcm: base64Of(pcm),
    })
    talking.value = true
    seq += 1
    if (seq < chunks) previewTimer = setTimeout(step, ((seconds * 1000) / chunks) * 0.8)
  }
  speech.flush()
  step()
}

function base64Of(pcm: Int16Array): string {
  const bytes = new Uint8Array(pcm.buffer)
  let binary = ''
  for (let index = 0; index < bytes.length; index += 8192) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 8192))
  }
  return btoa(binary)
}

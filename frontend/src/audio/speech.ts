/**
 * The page as the assistant's loudspeaker -- and therefore as its measuring tool.
 *
 * Python used to play every answer into PortAudio, which left the HUD with nothing
 * honest to draw: no samples, no level, and a visual that could only pretend to
 * pulse in time. Now the samples arrive here, go through an ``AnalyserNode`` on
 * their way to the output, and out of the same speaker the operator hears. The
 * rhythm and (next step) the avatar's mouth read *that* signal, so they cannot
 * drift out of sync with the voice -- there is exactly one voice.
 *
 * Three properties worth reading before changing anything:
 *
 * * **Nothing is invented.** ``levels()`` returns zeros when no audio has arrived.
 *   A flat line while it is quiet is the point; a moving one is evidence.
 * * **Scheduling is gapless.** Each slice starts at the exact moment the previous
 *   one ends. A chunk-per-request design would click at every seam, and the
 *   operator would hear the transport layer.
 * * **The context is created lazily and reported.** An autoplay policy that keeps
 *   the context ``suspended`` is a real possibility on a fresh webview, and the
 *   caller has to know so it can say "the speaker is playing this" on screen
 *   instead of showing a still picture and implying silence.
 */

/** One slice of synthesized speech, exactly as ``jarvis.ui.audio_bridge`` writes it. */
export interface SpeechSlice {
  seq: number
  sample_rate: number
  channels: number
  format: string
  final: boolean
  pcm: string
  /**
   * Schedule and measure it, but do not let it out of the speaker.
   *
   * Exactly one window may play the answer or the operator hears it twice, slightly
   * out of time -- but the desktop figure's mouth reads the same samples, so the other
   * window is fed the identical slices with this set. Playing them silently is not a
   * substitute for measuring: the analyser sits after the gain, so the levels are the
   * ones the voice is really making.
   */
  mute?: boolean
}

/** What the audio channel carries: a slice, or a command to stop playing. */
export type SpeechMessage = Partial<SpeechSlice> & { flush?: boolean }

export interface Levels {
  /** Root-mean-square of the waveform, 0..1. Drives the core's breathing. */
  rms: number
  /** Largest excursion in the last frame, 0..1. Drives the ring's spikes. */
  peak: number
  /** Five logarithmically-spaced bands, 0..1 each: rumble, voice, F1, F2, air. */
  bands: number[]
  /** True while samples are still queued to play, not merely while they arrive. */
  talking: boolean
}

/** Which device is producing the assistant's voice. */
export type OutputKind = 'browser' | 'speaker'

export interface AudioReadiness {
  ok: boolean
  output: OutputKind
  reason: string
}

const ANALYSER_SIZE = 2048
const RESUME_TIMEOUT_MS = 1200
/** How long to wait for an autoplay-blocked audio context before calling it blocked. */

const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))
const BAND_EDGES_HZ = [0, 160, 420, 1100, 2800, 8000]
/**
 * Bands chosen around what a voice actually occupies, not a music equaliser: the
 * fundamental of a Chinese female voice sits near 200 Hz, the first two formants
 * carry the vowel, and everything above 3 kHz is the consonants that make a
 * syllable audible as *speech* rather than as a hum. The mouth shape in the next
 * step needs exactly this split; a uniform FFT display would not give it.
 */

const EMPTY_LEVELS: Levels = { rms: 0, peak: 0, bands: [0, 0, 0, 0, 0], talking: false }

class SpeechPlayer {
  private context: AudioContext | null = null
  private master: GainNode | null = null
  private analyser: AnalyserNode | null = null
  private output: GainNode | null = null
  /**
   * The one gain node *after* the analyser, and therefore the only place a mute
   * belongs. Silencing the master would silence the measurement too, and the entire
   * reason a second window receives samples at all is to read them.
   */
  // Annotated as bare `Uint8Array` these would widen to `Uint8Array<ArrayBufferLike>`
  // and the analyser's own signatures (`<ArrayBuffer>`) would refuse them.
  private timeData = new Uint8Array(0)
  private frequencyData = new Uint8Array(0)
  private sources: AudioBufferSourceNode[] = []
  private queueEnd = 0
  private lastSeq = -1
  private bytes = 0
  private readiness: AudioReadiness = { ok: false, output: 'speaker', reason: '尚未初始化' }

  /**
   * Build the audio graph, or report why it cannot be the output device.
   *
   * Safe to call repeatedly: the page calls this on mount, again after the first
   * click (an autoplay-blocked context only unlocks on a gesture), and never in a
   * loop that would leak a second ``AudioContext``.
   */
  async ensure(): Promise<AudioReadiness> {
    if (this.context && this.readiness.ok) return this.readiness
    const Created = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!Created) {
      this.readiness = { ok: false, output: 'speaker', reason: '这个浏览器没有 AudioContext' }
      return this.readiness
    }
    try {
      if (!this.context) {
        this.context = new Created()
        this.master = this.context.createGain()
        this.analyser = this.context.createAnalyser()
        this.output = this.context.createGain()
        this.analyser.fftSize = ANALYSER_SIZE
        this.analyser.smoothingTimeConstant = 0.72
        this.master.connect(this.analyser)
        this.analyser.connect(this.output)
        this.output.connect(this.context.destination)
        this.timeData = new Uint8Array(this.analyser.fftSize)
        this.frequencyData = new Uint8Array(this.analyser.frequencyBinCount)
      }
      if (this.context.state !== 'running') {
        // Bounded on purpose. Chromium hands back a promise that can sit unresolved
        // until a gesture arrives, and a caller that never gets an answer is worse
        // than one that gets "not yet": the mount path would hang, Python would hear
        // nothing, and the assistant would be silent with no reason on screen.
        await Promise.race([this.context.resume(), wait(RESUME_TIMEOUT_MS)])
      }
    } catch (err) {
      this.readiness = {
        ok: false,
        output: 'speaker',
        reason: err instanceof Error ? err.message : String(err),
      }
      return this.readiness
    }
    const running = this.context.state === 'running'
    this.readiness = running
      ? { ok: true, output: 'browser', reason: '' }
      : {
          ok: false,
          output: 'speaker',
          reason: '音频上下文被浏览器挂起，点击界面一下即可恢复',
        }
    return this.readiness
  }

  get ready(): boolean {
    return this.readiness.ok
  }

  /** True once at least one real byte has crossed the bridge. */
  get hasSignal(): boolean {
    return this.bytes > 0
  }

  /** PCM bytes decoded since the page loaded. See ``installAudioProbe``. */
  get receivedBytes(): number {
    return this.bytes
  }

  /**
   * Queue one slice for playback.
   *
   * A slice whose format is not the promised s16le is dropped rather than played:
   * decoding MP3 as PCM produces a second of loud noise, and the operator would
   * reasonably conclude the assistant is broken instead of that an engine changed
   * its output format.
   */
  push(message: SpeechMessage): void {
    if (message.flush) {
      this.flush()
      return
    }
    const encoded = message.pcm
    const rate = message.sample_rate
    if (!encoded || !rate || message.format !== 'pcm_s16le' || !this.context || !this.master) {
      return
    }
    if (message.seq !== undefined && message.seq <= this.lastSeq) return
    if (message.seq !== undefined) this.lastSeq = message.seq
    // Decided per slice rather than once at mount: the same page can be the loudspeaker
    // for one answer and the audience for the next, depending which window the operator
    // is looking at when they press the wake word.
    this.setMuted(message.mute === true)
    const bytes = decodeBase64(encoded)
    this.bytes += bytes.byteLength
    const samples = Math.floor(bytes.byteLength / 2)
    if (samples <= 0) return
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    const buffer = this.context.createBuffer(1, samples, rate)
    const channel = buffer.getChannelData(0)
    for (let index = 0; index < samples; index += 1) {
      // Int16 → [-1, 1). Reading little-endian explicitly: Python writes s16le,
      // and the platform's native order is not a thing to depend on.
      channel[index] = view.getInt16(index * 2, true) / 0x8000
    }
    this.play(buffer)
  }

  private setMuted(muted: boolean): void {
    const gate = this.output
    if (gate) gate.gain.value = muted ? 0 : 1
  }

  private play(buffer: AudioBuffer): void {
    const context = this.context
    const master = this.master
    if (!context || !master) return
    const source = context.createBufferSource()
    source.buffer = buffer
    source.connect(master)
    source.onended = () => {
      this.sources = this.sources.filter((entry) => entry !== source)
    }
    // Start after everything already queued, with a small lead so the first slice
    // of an utterance is not already late when it arrives.
    const start = Math.max(context.currentTime + 0.04, this.queueEnd)
    source.start(start)
    this.queueEnd = start + buffer.duration
    this.sources.push(source)
  }

  /**
   * Stop now: the wake-word path, and the 「停下」 button.
   *
   * Cancelling synthesis in Python is not enough -- by the time a person starts
   * talking over the assistant, the samples have already crossed the bridge and are
   * scheduled here. Retracting them is the only way the interruption is audible
   * rather than merely logged.
   */
  flush(): void {
    const now = this.context ? this.context.currentTime : 0
    for (const source of this.sources) {
      try {
        source.stop(now)
      } catch {
        // Already finished, or never started. Neither is worth a log line per frame.
      }
    }
    this.sources = []
    this.queueEnd = 0
    this.lastSeq = -1
  }

  /**
   * Read the analyser. Returns all-zero levels when this page is not the output
   * device, which is the honest answer and the one the HUD renders as 「无实时电平」.
   */
  levels(): Levels {
    const analyser = this.analyser
    const context = this.context
    if (!analyser || !context || !this.readiness.ok) return EMPTY_LEVELS
    analyser.getByteTimeDomainData(this.timeData)
    analyser.getByteFrequencyData(this.frequencyData)
    let sumOfSquares = 0
    let highest = 0
    for (let index = 0; index < this.timeData.length; index += 1) {
      const deviation = (this.timeData[index] - 128) / 128
      sumOfSquares += deviation * deviation
      const magnitude = Math.abs(deviation)
      if (magnitude > highest) highest = magnitude
    }
    const binHz = context.sampleRate / analyser.fftSize
    const bands = BAND_EDGES_HZ.slice(1).map((upper, index) => {
      const lower = BAND_EDGES_HZ[index]
      const from = Math.max(0, Math.floor(lower / binHz))
      const to = Math.min(this.frequencyData.length, Math.ceil(upper / binHz))
      let peak = 0
      for (let at = from; at < to; at += 1) peak = Math.max(peak, this.frequencyData[at])
      return peak / 255
    })
    return {
      rms: Math.sqrt(sumOfSquares / this.timeData.length),
      peak: highest,
      bands,
      talking: this.queueEnd > context.currentTime,
    }
  }
}

function decodeBase64(encoded: string): Uint8Array {
  const binary = atob(encoded)
  const out = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) out[index] = binary.charCodeAt(index)
  return out
}

/**
 * One instance per page. Two would mean two audio graphs, and the second would
 * measure a signal that is not the one reaching the speaker -- the exact kind of
 * bug where the picture looks alive while the voice has already stopped.
 */
export const speech = new SpeechPlayer()

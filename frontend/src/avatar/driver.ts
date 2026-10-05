/**
 * Turning a measured signal into a face.
 *
 * The analyser hands over five band energies and a loudness; a character needs
 * visemes, gaze and blinks. Everything in here is a *mapping*, and the honest part
 * of that word matters: audio bands do not identify phonemes, so this does not
 * pretend to. It produces a mouth that opens with the voice, is widest where the
 * energy is, and closes when the voice closes -- which is what "口型同步" looks like
 * at the frame rate a person can actually scrutinise. A phoneme-accurate rig needs
 * a forced-aligner running on the same text, and that is a different, larger thing.
 *
 * Three behaviours are load-bearing:
 *
 * * **Fast attack, slow release.** A mouth that closes as fast as it opens looks
 *   like a sewing machine. Real jaws take ~120 ms to fall.
 * * **Silence is not a face mid-word.** When there is no signal the visemes decay
 *   to closed, so a stopped assistant cannot be mistaken for a talking one.
 * * **Nothing moves that was not asked to.** With no audio and no state change the
 *   only motion is breathing and an occasional blink, which is what a still
 *   character looks like -- and is the check that the idle path is not animating.
 */

import type { Levels } from '@/audio/speech'
import { neutralPose, type Mood, type Pose, type Viseme, VISEMES } from '@/avatar/character'

/** Typical speech RMS for the shipped voice; the loudness scale is anchored here. */
const SPEECH_RMS = 0.09

/** The head-and-attitude channels a mood sets, plus whether that mood sits down. */
type MoodPose = {
  pitch: number
  yaw: number
  roll: number
  brows: number
  gaze: number
  lids: number
  /** 0 = 站着, 1 = 坐下。只有有腿的那具身体会用它（``Pose.sit``）；全息半身像会略过。 */
  sit: number
}

const MOOD_POSE: Record<Mood, MoodPose> = {
  dormant: { pitch: 0.1, yaw: 0.05, roll: 0.02, brows: -0.18, gaze: -0.35, lids: 0.42, sit: 0 },
  armed: { pitch: -0.02, yaw: 0, roll: 0, brows: 0.02, gaze: 0, lids: 0, sit: 0 },
  listening: { pitch: -0.07, yaw: 0.1, roll: -0.05, brows: 0.22, gaze: 0.1, lids: 0, sit: 0 },
  // 坐着想，不是打盹。上一版是 pitch 0.3（低头）+ lids 0.52（半闭眼）+ gaze -0.5（往
  // 下看）—— 三个都往"睡"的方向走，用户说"她没反应"其实是在说她不像是忙，是像是关机。
  // 现在：头歪一点、眼睛开着看自己膝盖上那块屏幕、眉头稍微收。真正的"在想"是下面
  // shapeThinking 那点慢漂移和偶尔一次沉吟，不是把眼睛闭上。
  thinking: { pitch: 0.15, yaw: -0.16, roll: 0.13, brows: 0.04, gaze: -0.17, lids: 0.12, sit: 1 },
  speaking: { pitch: -0.02, yaw: 0, roll: 0, brows: 0.08, gaze: 0, lids: 0, sit: 0 },
}

/** How far the head may turn to follow the pointer, in radians. */
const LOOK_YAW = 0.5
const LOOK_PITCH = 0.24

/** A pointer still for this long is a person doing something else, not an audience. */
const LOOK_AWAY_AFTER = 4

export interface DriverInput {
  levels: Levels
  mood: Mood
  /** Seconds since the driver started; the caller owns the clock. */
  elapsed: number
  delta: number
  /** Reduced motion: keep the mouth, drop the sway and the nods. */
  calm: boolean
  /**
   * Where the OS pointer is relative to this figure, -1..1 on both axes (1 = right
   * and down). Only the desktop pet can supply it -- the HUD's figure has no idea
   * where the mouse is -- so it is optional, and absent means "no tracking".
   */
  pointer?: { x: number; y: number } | null
}

export class FaceDriver {
  private readonly pose: Pose = neutralPose()
  private envelope = 0
  private previous = 0
  private nod = 0
  private browLift = 0
  private nextBlink = 2.5
  private blinkPhase = 0
  private attackRate = 0
  private releaseRate = 0
  private readonly look = { yaw: 0, pitch: 0 }
  private readonly lastPointer = { x: 0, y: 0 }
  private pointerAge = 99
  /** How far the thinking drift has faded in (0 when she is not thinking), where its
   *  cycle is, and the one slow chin-dip that is currently running. */
  private thinkAmount = 0
  private thinkPhase = 0
  private thinkNod = 0
  private nextThinkNod = 4
  private readonly think = { pitch: 0, roll: 0 }
  private readonly targets = { pitch: 0, yaw: 0, roll: 0, brows: 0, gaze: 0, lids: 0, sit: 0 }

  /**
   * One frame of face.
   *
   * Returns the same mutable object every call: this runs 30-60 times a second and
   * a fresh object per frame is garbage the collector has to keep up with for the
   * life of the window.
   */
  update(input: DriverInput): Pose {
    const { levels, mood, delta } = input
    const loud = Math.min(1, levels.rms / SPEECH_RMS)
    // Exponential smoothing that is correct at any frame rate, rather than a fixed
    // lerp factor that doubles its speed on a 120 Hz display.
    this.attackRate = 1 - Math.exp(-delta / 0.035)
    this.releaseRate = 1 - Math.exp(-delta / 0.13)
    this.envelope += (loud - this.envelope) * (loud > this.envelope ? this.attackRate : this.releaseRate)

    this.detectOnset(loud, delta)
    this.shapeVisemes(levels)
    this.shapeMood(mood, delta, levels.talking)
    this.shapeThinking(mood, input.elapsed, delta, input.calm)
    this.shapeBlink(input.elapsed, delta)
    this.trackPointer(input.pointer, delta, input.calm)

    if (input.calm) {
      this.nod = 0
      this.pose.pitch = this.targets.pitch
      this.pose.yaw = this.targets.yaw
      this.pose.roll = this.targets.roll
    } else {
      // The pointer offset goes on *outside* the mood targets: those ease over a
      // quarter second and a cursor moves in one frame, and folding one into the
      // other would make her either ignore the mouse or forget her mood.
      this.pose.pitch = clampRange(
        this.targets.pitch - this.nod * 0.16 + this.look.pitch + this.think.pitch,
        -0.42,
        0.4,
      )
      this.pose.yaw = clampRange(this.targets.yaw + this.look.yaw, -0.9, 0.9)
      this.pose.roll = this.targets.roll + this.nod * 0.04 - this.look.yaw * 0.12 + this.think.roll
    }
    this.pose.brows = clamp01(this.targets.brows + this.browLift)
    // The eyes lead the turn by a third of it. A head that rotates without the eyes
    // moving first is the single most reliable tell of a rigged character.
    this.pose.gaze = this.targets.gaze + this.look.pitch * 0.4
    this.previous = loud
    return this.pose
  }

  /**
   * Look at the cursor, and look away once it stops moving.
   *
   * This is most of what "灵动" means for a desktop pet: the figure acknowledges
   * what the person is doing without being touched. Two details carry it -- the
   * influence fades out over a still pointer (a pet that stares at a mouse that has
   * not moved for a minute is uncanny, and it is what the first version did), and
   * the value is eased rather than applied, because a head that snaps to a cursor
   * reads as a security camera.
   */
  private trackPointer(pointer: { x: number; y: number } | null | undefined, delta: number, calm: boolean): void {
    if (calm || !pointer) {
      this.pointerAge = LOOK_AWAY_AFTER + 1
    } else {
      const moved = Math.abs(pointer.x - this.lastPointer.x) + Math.abs(pointer.y - this.lastPointer.y)
      if (moved > 0.012) {
        this.pointerAge = 0
        this.lastPointer.x = pointer.x
        this.lastPointer.y = pointer.y
      } else {
        this.pointerAge += delta
      }
    }
    // Fade over 1.5 s rather than cut: an abrupt zero is a head twitch.
    const attention = clamp01((LOOK_AWAY_AFTER - this.pointerAge) / 1.5)
    const rate = 1 - Math.exp(-delta / 0.26)
    this.look.yaw += (clampRange(pointer?.x ?? 0, -1, 1) * LOOK_YAW * attention - this.look.yaw) * rate
    this.look.pitch += (clampRange(pointer?.y ?? 0, -1, 1) * LOOK_PITCH * attention - this.look.pitch) * rate
  }

  /**
   * A rising edge in loudness is a syllable starting, and a head that nods once per
   * syllable is most of what makes a 3D face look like it is *speaking* rather than
   * like a mouth moving on a statue.
   */
  private detectOnset(loud: number, delta: number): void {
    if (this.previous < 0.16 && loud > 0.3) {
      this.nod = 1
      this.browLift = 0.35
    }
    const decay = 1 - Math.exp(-delta / 0.16)
    this.nod -= this.nod * decay
    this.browLift -= this.browLift * (1 - Math.exp(-delta / 0.22))
  }

  /**
   * Band energies → five viseme weights.
   *
   * The bands are the five ranges a voice actually occupies (rumble, voicing, F1,
   * F2, air). Rounded vowels concentrate low, spread vowels concentrate high, and
   * an open jaw is simply loud. Normalising to the loudest component keeps the
   * mouth from settling in a mushy half-open state where all five are at 0.4 --
   * which reads as a permanent yawn.
   */
  private shapeVisemes(levels: Levels): void {
    const [low = 0, voice = 0, f1 = 0, f2 = 0, air = 0] = levels.bands
    const total = Math.max(0.0001, low + voice + f1 + f2 + air)
    const rounded = (low + voice) / total
    const spread = (f2 + air) / total
    const raw: Record<Viseme, number> = {
      aa: this.envelope,
      oh: this.envelope * rounded * 1.15,
      ou: this.envelope * Math.max(0, rounded - 0.42) * 2.1,
      ee: this.envelope * spread * 1.5,
      ih: this.envelope * (f1 / total) * 1.3,
    }
    const peak = Math.max(...VISEMES.map((name) => raw[name]), 0.0001)
    VISEMES.forEach((name) => {
      const weight = (raw[name] / peak) * this.envelope
      const current = this.pose.visemes[name]
      const rate = weight > current ? this.attackRate : this.releaseRate
      this.pose.visemes[name] = current + (weight - current) * rate
    })
    this.pose.jaw = this.pose.visemes.aa
  }

  private shapeMood(mood: Mood, delta: number, talking: boolean): void {
    const preset = MOOD_POSE[talking && mood === 'listening' ? 'speaking' : mood]
    const rate = 1 - Math.exp(-delta / 0.28)
    this.targets.pitch += (preset.pitch - this.targets.pitch) * rate
    this.targets.yaw += (preset.yaw - this.targets.yaw) * rate
    this.targets.roll += (preset.roll - this.targets.roll) * rate
    this.targets.brows += (preset.brows - this.targets.brows) * rate
    this.targets.gaze += (preset.gaze - this.targets.gaze) * rate
    this.targets.lids += (preset.lids - this.targets.lids) * rate
    // A body-wide move gets its own, slower ease: the head may snap to "thinking" in a
    // quarter second, but a person who drops to a seat in the same instant looks broken.
    const sitRate = 1 - Math.exp(-delta / 0.55)
    this.targets.sit += (preset.sit - this.targets.sit) * sitRate
    this.pose.sit = this.targets.sit
    // While it talks, the lids open a fraction wider: closed-by-default eyes plus a
    // moving mouth reads as sleeping, which is a hard thing to unsee.
    const speaking = talking ? -0.06 : 0
    this.pose.blink = clamp01(this.blinkAmount() + this.targets.lids + speaking)
  }

  /**
   * The one thing that makes a held pose read as *thinking* rather than as paused.
   *
   * A figure that stands perfectly still for twenty seconds is a figure with nothing
   * happening -- which is exactly what the previous preset did once the head had bowed.
   * So: a slow drift of a degree or two on pitch and roll (two different periods, so it
   * never visibly loops), and one small chin-dip every four to seven seconds, which is
   * the "hmm" a person actually makes while reading something.
   *
   * It fades in and out rather than switching: a head that stops mid-drift is its own
   * jerk, and reduced-motion has to reach this too, so the whole thing is gated on
   * ``calm`` and left at zero when the caller does not want it.
   */
  private shapeThinking(mood: Mood, elapsed: number, delta: number, calm: boolean): void {
    const active = mood === 'thinking' && !calm
    const rate = 1 - Math.exp(-delta / (active ? 0.6 : 0.3))
    this.thinkAmount += ((active ? 1 : 0) - this.thinkAmount) * rate
    if (active) {
      this.thinkPhase += delta
      if (elapsed >= this.nextThinkNod) {
        this.nextThinkNod = elapsed + 3.8 + Math.random() * 3.4
        this.thinkNod = 1
      }
    }
    this.thinkNod -= this.thinkNod * (1 - Math.exp(-delta / 0.55))
    this.think.pitch =
      (Math.sin(this.thinkPhase * 1.15) * 0.021 + this.thinkNod * 0.05) * this.thinkAmount
    this.think.roll = Math.sin(this.thinkPhase * 0.62 + 1.1) * 0.03 * this.thinkAmount
  }

  private shapeBlink(elapsed: number, delta: number): void {
    if (elapsed >= this.nextBlink) {
      this.nextBlink = elapsed + 2.4 + Math.random() * 4.2
      this.blinkPhase = 0
    }
    this.blinkPhase = Math.min(1, this.blinkPhase + delta / 0.19)
  }

  /** A down-then-up ramp over ~0.19 s, which is roughly how long a blink is. */
  private blinkAmount(): number {
    if (this.blinkPhase >= 1) return 0
    return Math.sin(this.blinkPhase * Math.PI)
  }
}

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value))
}

function clampRange(value: number, low: number, high: number): number {
  return Math.max(low, Math.min(high, value))
}

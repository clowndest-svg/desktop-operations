<template>
  <div ref="host" class="stage" :class="{ 'stage--dim': dim }">
    <canvas ref="canvas" class="stage__canvas"></canvas>
    <!--
      WebGL can genuinely be unavailable: this window runs on WebView2, whose
      renderer can be launched with a software rasteriser, on a machine with the GPU
      driver blocked, or inside a VM with no 3D at all. Saying so is the whole
      difference between a panel that looks broken and a panel that told you why --
      and the voice core keeps working either way, because it never needed 3D.
    -->
    <p v-if="failure" class="stage__notice">{{ failure }}</p>
    <!--
      Which figure is on screen, said out loud. A dropped-in model and the built
      projection are different things with different guarantees, and the operator
      should not have to open a console to find out which one they are looking at --
      especially when "it silently fell back" is the failure mode most likely to be
      mistaken for "the drop-in feature does not work".
    -->
    <p v-else-if="source" class="stage__source">{{ source }}</p>
  </div>
</template>

<script setup lang="ts">
/**
 * The 3D character, on screen.
 *
 * Owns the renderer, the camera and the frame loop; owns nothing about how the face
 * should look (:mod:`character`) or what the audio means (:mod:`driver`).
 *
 * Two things here are cost decisions rather than taste:
 *
 * * **A timer, not requestAnimationFrame.** Same reasoning as the voice core: this
 *   window is expected to stay open for hours, and the realistic idle case is a
 *   visible window nobody is looking at. rAF would happily shade a GPU at 60 Hz for
 *   nobody. 30 Hz is above the threshold where a stylized face looks smooth.
 * * **Nothing renders while hidden.** A minimised HUD drawing 3D frames is the
 *   single easiest way to make an assistant look like it is eating the machine.
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as THREE from 'three'
import { createCharacter, type Character, type Mood } from '@/avatar/character'
import { loadVrmCharacter } from '@/avatar/vrm'
import { FaceDriver } from '@/avatar/driver'
import { levels, talking } from '@/audio/channel'
import { skin } from '@/theme'
import { useVoiceStore } from '@/stores/voice'

/*
 * Two rates, because the two jobs are different.
 *
 * Talking needs ~30 Hz: a mouth that updates at 12 Hz looks like a slideshow, and
 * the whole point of the figure is that the mouth is *measured*. Not talking, the
 * most the face does is breathe and blink, and 20 Hz is past the threshold for
 * either. That halves is worth having: the render loop is the one part of this
 * window that costs CPU continuously, and it is the difference between "an app that
 * is always on" and "a laptop fan that is always on".
 */
const FRAME_MS_TALKING = 33
const FRAME_MS_IDLE = 50

const host = ref<HTMLDivElement | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const failure = ref('')
const dim = ref(false)
const source = ref('载入人物模型…')

const voice = useVoiceStore()

let renderer: THREE.WebGLRenderer | null = null
let scene: THREE.Scene | null = null
let camera: THREE.PerspectiveCamera | null = null
let character: Character | null = null
let timer: ReturnType<typeof setInterval> | undefined
let rate = FRAME_MS_IDLE
let observer: ResizeObserver | undefined
let mediaQuery: MediaQueryList | undefined
let clockStart = 0
let lastFrame = 0
let calm = false

const driver = new FaceDriver()

/**
 * Samples first, then the turn, then the microphone -- in that order because that is
 * the order of evidence. A typed question has no microphone but is still something she
 * is thinking about, and the old order put `phase !== 'running'` first, which put the
 * figure to sleep for every conversation held without the microphone.
 */
function moodNow(): Mood {
  if (talking.value) return 'speaking'
  if (voice.turn === 'processing') return 'thinking'
  if (voice.turn === 'listening') return 'listening'
  // 「聆听」关着不等于她睡着了：muted 是"模型还装着、只是不听"，那一身灯该是醒的。
  // 只有真的什么都没加载（off / loading / failed）才打盹。
  return voice.phase === 'running' || voice.phase === 'muted' ? 'armed' : 'dormant'
}

function frame(): void {
  const stage = scene
  const view = renderer
  const eye = camera
  const figure = character
  if (!stage || !view || !eye || !figure) return
  if (document.hidden) return
  const now = performance.now() / 1000
  const elapsed = now - clockStart
  const delta = Math.min(0.25, elapsed - lastFrame)
  lastFrame = elapsed
  const sample = levels()
  figure.setPose(driver.update({ levels: sample, mood: moodNow(), elapsed, delta, calm }))
  figure.advance(elapsed, delta, calm)
  view.render(stage, eye)
}

function resize(): void {
  const element = host.value
  const view = renderer
  const eye = camera
  if (!element || !view || !eye) return
  const width = element.clientWidth
  const height = element.clientHeight
  if (width === 0 || height === 0) return
  view.setSize(width, height, false)
  eye.aspect = width / height
  eye.fov = height < width * 1.1 ? 46 : 34
  // Pull back until the figure fits, on whichever axis the panel is short on.
  // Hard-coding a distance instead would compose the shot for one model and crop
  // whatever the operator drops in next.
  const framing = character?.framing ?? { target: 0, span: 3.5, beam: 2.4 }
  const half = Math.tan(THREE.MathUtils.degToRad(eye.fov) / 2)
  const distance = Math.max(framing.span / 2 / half, framing.beam / 2 / half / eye.aspect)
  eye.position.set(0, framing.target, distance)
  eye.lookAt(0, framing.target, 0)
  eye.updateProjectionMatrix()
}

function onMotionChange(): void {
  calm = mediaQuery?.matches ?? false
}

/**
 * Put the right figure on screen: the dropped-in model if there is one, the built
 * hologram if not, in the current skin either way.
 *
 * Also what a skin change calls. Rebuilding is the honest reaction to a palette
 * change: materials bake their colours at construction, and a figure recoloured by
 * walking live materials would differ from one built that way -- two code paths for
 * one look is how skins drift apart.
 */
async function swapFigure(stage: THREE.Scene): Promise<void> {
  const previous = character
  if (previous) {
    stage.remove(previous.root)
    previous.dispose()
    character = null
  }
  const built = createCharacter()
  stage.add(built.root)
  character = built
  source.value = built.describe()
  resize()

  const dropped = await loadVrmCharacter()
  if (!dropped || scene !== stage || !character) return
  stage.remove(character.root)
  character.dispose()
  stage.add(dropped.root)
  character = dropped
  source.value = dropped.describe()
  resize()
}

onMounted(() => {
  void setup()
})

async function setup(): Promise<void> {
  const element = host.value
  const surface = canvas.value
  if (!element || !surface) return
  try {
    renderer = new THREE.WebGLRenderer({ canvas: surface, alpha: true, antialias: true })
  } catch (err) {
    failure.value = `这台机器开不了 3D（${err instanceof Error ? err.message : String(err)}），声核的电平不受影响`
    return
  }
  // 1.5 rather than 2: past that, a wireframe's lines are being supersampled for a
  // difference nobody can see at this panel size, and the fill rate quadruples.
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5))

  scene = new THREE.Scene()
  // No lights at this point: the figure below is built from emissive and additive
  // materials, which is what a projection is. Adding a key light to a hologram would
  // do nothing except invite the next reader to wonder why it has no effect.

  camera = new THREE.PerspectiveCamera(30, 1, 0.1, 20)
  camera.position.set(0, -0.5, 5.6)
  camera.lookAt(0, -0.5, 0)

  // The built figure goes up first, then the model file replaces it. Reversed, the
  // panel is empty for as long as a ten-megabyte glTF takes to parse off disk, and an
  // empty panel is indistinguishable from the WebGL failure above it.
  mediaQuery = window.matchMedia?.('(prefers-reduced-motion: reduce)')
  calm = mediaQuery?.matches ?? false
  mediaQuery?.addEventListener('change', onMotionChange)
  observer = new ResizeObserver(resize)
  observer.observe(element)
  document.addEventListener('visibilitychange', dimOnHidden)
  clockStart = performance.now() / 1000
  lastFrame = 0
  frame()
  startLoop(FRAME_MS_IDLE)
  await swapFigure(scene)
}

function startLoop(interval: number): void {
  if (timer !== undefined) clearInterval(timer)
  rate = interval
  timer = setInterval(frame, rate)
}

/**
 * Speed up only while there is a mouth to sync. The flag flips on the first sample
 * and on the last one, so a sentence that ends is followed by a slower loop within
 * the same second it ends.
 */
watch(talking, (active) => {
  startLoop(active ? FRAME_MS_TALKING : FRAME_MS_IDLE)
})

watch(skin, () => {
  const stage = scene
  if (stage) void swapFigure(stage)
})

function dimOnHidden(): void {
  dim.value = document.hidden
}

onBeforeUnmount(() => {
  if (timer !== undefined) clearInterval(timer)
  mediaQuery?.removeEventListener('change', onMotionChange)
  document.removeEventListener('visibilitychange', dimOnHidden)
  observer?.disconnect()
  character?.dispose()
  renderer?.dispose()
  // Cleared rather than left in place: a model still parsing when the panel goes
  // away would otherwise be added to a disposed renderer. `setup` checks these.
  scene = null
  character = null
})
</script>

<style scoped>
.stage {
  position: relative;
  min-height: 0;
  overflow: hidden;
}

.stage__canvas {
  display: block;
  width: 100%;
  height: 100%;
}

/*
 * A hidden window should not be lit. This is the visible half of "we stopped
 * rendering": the last frame stays on the canvas, and dimming it says *that is a
 * frozen frame* rather than leaving the operator to wonder whether the face just
 * went blank on purpose.
 */
.stage--dim .stage__canvas {
  opacity: 0.45;
  transition: opacity 0.4s ease;
}

.stage__notice {
  position: absolute;
  inset: auto 8% 12%;
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  text-align: center;
  color: var(--hud-dim);
}

/* Out of the way on purpose: it is a diagnostic, not part of the composition. */
.stage__source {
  position: absolute;
  inset: auto auto 6px 8px;
  margin: 0;
  font-size: 9px;
  letter-spacing: 0.04em;
  color: var(--hud-dim);
  opacity: 0.55;
}
</style>

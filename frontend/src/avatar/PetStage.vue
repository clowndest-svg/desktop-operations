<template>
  <div ref="host" class="pet" :class="{ 'pet--alpha': alpha }">
    <canvas ref="canvas" class="pet__canvas"></canvas>

    <p v-if="failure" class="pet__notice">{{ failure }}</p>
    <!--
      What it just said, in as many words as fit under the figure. The voice is the
      answer; this is the part that survives a muted laptop and a noisy room.
    -->
    <p v-else-if="caption" class="pet__line">{{ caption }}</p>

    <!--
      The only part of this window that takes a click. Everything outside it is
      click-through at the OS level, which is why the handle and the 收起 control are
      one strip: the shell watches the pointer against the rectangle reported here,
      and a second small target would be a second thing to aim at.
    -->
    <div ref="grip" class="pet__grip" :class="{ 'pet__grip--held': held }">
      <span
        class="pet__hold"
        :title="alpha ? '按住这里拖动小夜；点一下就收起' : '按住这里拖动小夜；屏幕其他地方照常点得动'"
        @pointerdown="hold"
        @pointerup="release"
        @pointercancel="release"
        >⡀⠈ 小夜</span
      >
      <!--
        Panel mode only. Once the shell composites this canvas into a window with
        real per-pixel alpha, the page is off screen and receives no clicks at all --
        the window the figure appears in is a Win32 one, and the tap is handled
        there. A button that is drawn but cannot be pressed is worse than no button.
      -->
      <button v-if="!alpha" class="pet__hide" type="button" title="收起桌面宠物" @click="dismiss">
        收起
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * The desktop figure: the same character as the HUD panel, arriving through a
 * wormhole instead of sitting in a box.
 *
 * Deliberately a second component rather than a prop on ``AvatarStage``. The HUD's
 * figure is framed by a panel, a voice core and a gauge row; the pet is framed by
 * nothing at all, and it has a whole animation language of its own (arrival, halo,
 * sparks). One component serving both means a pile of ``if (pet)`` in the middle of
 * code that already works, and the risk is the part nobody re-tests.
 *
 * Cost rules, same as the HUD figure: a timer rather than requestAnimationFrame, a
 * slower one when nothing is talking, and no drawing at all while the window is
 * hidden -- which the shell has to *say*, because a hidden WebView2 page still
 * reports itself visible.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import * as THREE from 'three'
import { createCharacter, type Character, type Mood } from '@/avatar/character'
import { loadVrmCharacter } from '@/avatar/vrm'
import { FaceDriver } from '@/avatar/driver'
import { levels, startAudioChannel, stopAudioChannel, talking } from '@/audio/channel'
import { pushPetFrame, reportPetGrip, setPetDragging, togglePet } from '@/api/bridge'
import { currentSkin } from '@/theme'
import { useVoiceStore } from '@/stores/voice'

const FRAME_MS_TALKING = 33
const FRAME_MS_IDLE = 100
// 20 fps measured 97% of a core against 43% with the pet hidden. Half of that is
// this loop: a figure that only breathes does not need 20 frames a second, and
// every draw costs a PNG encode, a bridge trip, a decode and a premultiply.
const EMERGE_SECONDS = 1.7
const SPARK_COUNT = 220
const FULL_BODY = 1

const host = ref<HTMLDivElement | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const failure = ref('')
const held = ref(false)
const grip = ref<HTMLDivElement | null>(null)
/** True once the shell says it is compositing our frames into an alpha window. */
const alpha = ref(false)
const voice = useVoiceStore()

let renderer: THREE.WebGLRenderer | null = null
let scene: THREE.Scene | null = null
let camera: THREE.PerspectiveCamera | null = null
let character: Character | null = null
let ring: THREE.Mesh<THREE.TorusGeometry, THREE.MeshBasicMaterial> | null = null
let halo: THREE.Mesh<THREE.PlaneGeometry, THREE.MeshBasicMaterial> | null = null
let sparks: THREE.Points<THREE.BufferGeometry, THREE.PointsMaterial> | null = null
let timer: ReturnType<typeof setInterval> | undefined
let observer: ResizeObserver | undefined
let mediaQuery: MediaQueryList | undefined
let clockStart = 0
let lastFrame = 0
let emergeAt = -100
let fastArrival = false
let asleep = false
let calm = false
/**
 * Where the OS pointer is, as -1..1 of this window. The shell pushes it: the page
 * cannot see a cursor it is not under, and in alpha mode this window is off screen
 * anyway. ``null`` means the shell has not said anything yet, which is different
 * from "the pointer is at the centre" -- the driver treats them differently.
 */
let pointer: { x: number; y: number } | null = null
/** One frame in flight at a time; see ``shipFrame``. */
let pushing = false

const driver = new FaceDriver()

/** The last thing the assistant said, shortened to what reads at a glance. */
const caption = computed(() => {
  const turns = voice.history
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index]
    if (turn && turn.role === 'assistant') {
      const text = turn.text.replace(/\s+/g, ' ').trim()
      return text.length > 78 ? `${text.slice(0, 78)}…` : text
    }
  }
  return ''
})

function moodNow(): Mood {
  if (voice.phase !== 'running') return 'dormant'
  if (talking.value || voice.turn === 'processing') return 'speaking'
  if (voice.turn === 'listening') return 'listening'
  return 'armed'
}

const clamp01 = (value: number): number => Math.min(1, Math.max(0, value))
const easeOut = (value: number): number => 1 - (1 - value) ** 3

function elapsed(): number {
  return performance.now() / 1000 - clockStart
}

function frame(): void {
  const stage = scene
  const view = renderer
  const eye = camera
  if (!stage || !view || !eye || asleep) return
  const now = elapsed()
  const delta = Math.min(0.25, now - lastFrame)
  lastFrame = now
  const progress = clamp01((now - emergeAt) / EMERGE_SECONDS)
  const opened = easeOut(progress)
  if (progress >= 1 && fastArrival) {
    fastArrival = false
    startLoop(talking.value ? FRAME_MS_TALKING : FRAME_MS_IDLE)
  }

  const figure = character
  if (figure) {
    // The arrival is a scale, not a fade: the materials are additive and shared by
    // two render passes, and walking their opacities would be a second code path for
    // the same look. Growing out of the collapsing light reads as emerging anyway.
    figure.root.scale.setScalar(0.22 + opened * 0.78)
    figure.setPose(driver.update({ levels: levels(), mood: moodNow(), elapsed: now, delta, calm, pointer }))
    figure.advance(now, delta, calm)
  }

  if (ring) {
    ring.scale.setScalar(0.16 + opened * 1.0)
    // Flat on to start (a mouth seen edge-on), then opening into a halo behind her.
    // It has to end near zero: a torus lies in its own plane, so any tilt left at
    // the end of the animation reads as a grey band across her shoulders forever.
    ring.rotation.x = 1.3 - opened * 1.24 + Math.sin(now * 0.35) * 0.04
    ring.rotation.z += delta * (1.7 * (1 - opened) + 0.1)
    // All the way to zero. What is left over reads as a grey band across her
    // shoulders once the window has no panel to hide it against -- on the desktop,
    // a 0.35-alpha torus is a smudge.
    ring.material.opacity = Math.max(0, 0.95 - opened * 1.15)
  }
  if (halo) {
    // Ends at zero, not at a resting glow. A window keyed on black can only hide a
    // pixel that is *exactly* black: a halo left at 0.1 over black is (24,24,24),
    // which is not the key, and it showed up on the desktop as a dark disc with a
    // visible circular edge -- a rectangle again, merely a rounder one.
    const flare = progress < 0.6 ? progress / 0.6 : 1 - (progress - 0.6) / 0.4
    halo.material.opacity = Math.max(0, flare) * 0.9
    halo.scale.setScalar(0.3 + opened * 1.5)
  }
  if (sparks) {
    // One scale on the whole shell is the convergence: every point walks toward the
    // centre without a per-particle update, which is the difference between 220
    // floats a frame and none.
    sparks.scale.setScalar(3.1 - opened * 2.95)
    sparks.rotation.y += delta * 0.9
    const fading = progress < 0.72 ? progress / 0.72 : 1 - (progress - 0.72) / 0.28
    sparks.material.opacity = Math.max(0, fading) * 0.95
  }

  view.render(stage, eye)
  if (alpha.value) shipFrame(view)
}

/**
 * Hand the last rendered frame to the shell, which draws it into a window that can
 * actually be transparent.
 *
 * ``toDataURL`` is the whole cost: measured on this machine at 8 ms to encode a
 * 420x640 PNG and 9.5 ms for the base64 string to cross the bridge and decode, so
 * the pipe carries ~50 fps and the pet asks for 20. One frame in flight, because
 * the bridge queues nothing and a backlog would be a window showing last second's
 * pose -- which on a moving cursor is exactly the lag you notice.
 */
function shipFrame(view: THREE.WebGLRenderer): void {
  if (pushing) return
  const surface = view.domElement as HTMLCanvasElement
  pushing = true
  void pushPetFrame(surface.toDataURL('image/png'))
    .catch(() => undefined)
    .finally(() => {
      pushing = false
    })
}

function startLoop(interval: number): void {
  if (timer !== undefined) clearInterval(timer)
  timer = setInterval(frame, interval)
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
  eye.fov = 34
  const framing = character?.framing ?? { target: 0, span: 3.5, beam: 2.4 }
  const half = Math.tan(THREE.MathUtils.degToRad(eye.fov) / 2)
  const distance = Math.max(framing.span / 2 / half, framing.beam / 2 / half / eye.aspect)
  eye.position.set(0, framing.target, distance)
  eye.lookAt(0, framing.target, 0)
  eye.updateProjectionMatrix()
  // How many world units one CSS pixel is *at the plane the sprites live in*.
  // Derived from the camera rather than from ``framing.span``: the span only equals
  // the visible height when the vertical constraint wins, and in a window this tall
  // and narrow the horizontal one does -- which is how the handle ended up standing
  // on her shoulders at twice the size it was asked for.
  reportGrip()
}

/**
 * Where the grabbable strip is, as fractions of this viewport.
 *
 * The shell cannot see the page's layout and the page cannot touch the window
 * style, so the geometry crosses the bridge and the pointer does the rest.
 */
function reportGrip(): void {
  const element = grip.value
  if (!element || !window.innerWidth || !window.innerHeight) return
  const rect = element.getBoundingClientRect()
  // The shell both hit-tests this rectangle and draws the pill over it, so the page
  // measuring its own layout is what keeps the label and the clickable patch in the
  // same place -- in panel mode the pill is the DOM element, in alpha mode it is
  // painted by the shell at exactly these fractions.
  void reportPetGrip({
    x: rect.left / window.innerWidth,
    y: rect.top / window.innerHeight,
    width: rect.width / window.innerWidth,
    height: rect.height / window.innerHeight,
  }).catch(() => undefined)
}

function hold(event: PointerEvent): void {
  held.value = true
  void setPetDragging(true).catch(() => undefined)
  // Capture keeps the drag alive while the pointer is over the rest of the window,
  // where the page would otherwise stop receiving events.
  ;(event.currentTarget as Element | null)?.setPointerCapture?.(event.pointerId)
}

function release(): void {
  if (!held.value) return
  held.value = false
  void setPetDragging(false).catch(() => undefined)
}

async function dismiss(): Promise<void> {
  await togglePet().catch(() => undefined)
}

function glowTexture(): THREE.Texture {
  const size = 128
  const surface = document.createElement('canvas')
  surface.width = size
  surface.height = size
  const paint = surface.getContext('2d')
  if (paint) {
    const gradient = paint.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2)
    gradient.addColorStop(0, 'rgba(255,255,255,0.95)')
    gradient.addColorStop(0.3, 'rgba(170,238,255,0.5)')
    gradient.addColorStop(1, 'rgba(0,0,0,0)')
    paint.fillStyle = gradient
    paint.fillRect(0, 0, size, size)
  }
  return new THREE.CanvasTexture(surface)
}

function buildWormhole(): void {
  const stage = scene
  if (!stage) return
  const skin = currentSkin()
  const ringGeometry = new THREE.TorusGeometry(1.15, 0.05, 10, 96)
  ring = new THREE.Mesh(
    ringGeometry,
    new THREE.MeshBasicMaterial({
      color: skin.glow,
      transparent: true,
      opacity: 0.35,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    }),
  )
  ring.position.set(0, 0.15, -0.4)
  stage.add(ring)

  halo = new THREE.Mesh(
    new THREE.PlaneGeometry(4, 4),
    new THREE.MeshBasicMaterial({
      map: glowTexture(),
      transparent: true,
      opacity: 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    }),
  )
  halo.position.set(0, 0.15, -0.6)
  stage.add(halo)

  const positions = new Float32Array(SPARK_COUNT * 3)
  for (let index = 0; index < SPARK_COUNT; index += 1) {
    // A shell of radius 0.55~1.5: scaling the object converges every spark toward
    // the centre, and the variance keeps it from reading as one expanding ring.
    const theta = Math.random() * Math.PI * 2
    const phi = Math.acos(2 * Math.random() - 1)
    const radius = 0.55 + Math.random() * 0.95
    positions[index * 3] = Math.sin(phi) * Math.cos(theta) * radius
    positions[index * 3 + 1] = Math.cos(phi) * radius * 1.25
    positions[index * 3 + 2] = Math.sin(phi) * Math.sin(theta) * radius * 0.4
  }
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  sparks = new THREE.Points(
    geometry,
    new THREE.PointsMaterial({
      color: skin.line,
      size: 0.06,
      transparent: true,
      opacity: 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    }),
  )
  sparks.position.set(0, 0.15, -0.2)
  stage.add(sparks)
}

async function swapFigure(stage: THREE.Scene): Promise<void> {
  const previous = character
  if (previous) {
    stage.remove(previous.root)
    previous.dispose()
    character = null
  }
  const built = createCharacter()
  built.reframe(FULL_BODY)
  stage.add(built.root)
  character = built
  resize()

  const dropped = await loadVrmCharacter()
  if (!dropped || scene !== stage || !character) return
  stage.remove(character.root)
  character.dispose()
  dropped.reframe(FULL_BODY)
  stage.add(dropped.root)
  character = dropped
  resize()
}

/** The shell's cue channel: emerge, start or stop drawing, and the two live inputs. */
function installPetCommand(): void {
  const target = window as unknown as {
    __jarvisPet?: (command: string) => void
    __jarvisPetPointer?: (x: number, y: number) => void
  }
  target.__jarvisPet = (command: string) => {
    if (command === 'emerge') {
      emergeAt = elapsed()
      asleep = false
      // The arrival is the one moment worth 30 fps; it is also the only one that
      // lasts less than two seconds.
      startLoop(FRAME_MS_TALKING)
    } else if (command === 'wake') {
      asleep = false
      startLoop(talking.value ? FRAME_MS_TALKING : FRAME_MS_IDLE)
    } else if (command === 'sleep') {
      asleep = true
    } else if (command === 'alpha') {
      // The compositor is up: stop painting a panel, start shipping frames. The
      // handle is the shell's hit target now, so it has to be in the frame.
      alpha.value = true
    }
  }
  target.__jarvisPetPointer = (x: number, y: number) => {
    pointer = { x, y }
  }
}

function onMotionChange(): void {
  calm = mediaQuery?.matches ?? false
}

onMounted(() => {
  const element = host.value
  const surface = canvas.value
  if (!element || !surface) return
  // This component is the pet window's application root (see ``main.ts``), so it
  // owns what the HUD's root owns: the voice state it renders and the audio channel
  // that drives the mouth. Without the second call the figure is silent and still.
  void voice.start()
  void startAudioChannel()
  installPetCommand()
  try {
    // ``preserveDrawingBuffer`` because ``toDataURL`` reads the canvas *after* the
    // frame has been composited, and WebGL drops the buffer by default -- without
    // this the alpha path ships blank PNGs. It costs a copy per frame on a window
    // this small; the HUD's figure leaves it off and is not shipped anywhere.
    renderer = new THREE.WebGLRenderer({ canvas: surface, alpha: true, antialias: true, preserveDrawingBuffer: true })
  } catch (err) {
    failure.value = `这台机器开不了 3D（${err instanceof Error ? err.message : String(err)}）`
    return
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5))
  // Explicit, because the whole point of this window is what it does not paint:
  // a clear colour with no alpha would put an opaque rectangle over the desktop.
  renderer.setClearColor(0x000000, 0)
  scene = new THREE.Scene()
  camera = new THREE.PerspectiveCamera(34, 1, 0.1, 24)
  buildWormhole()
  mediaQuery = window.matchMedia?.('(prefers-reduced-motion: reduce)')
  calm = mediaQuery?.matches ?? false
  mediaQuery?.addEventListener('change', onMotionChange)
  observer = new ResizeObserver(resize)
  observer.observe(element)
  clockStart = performance.now() / 1000
  lastFrame = 0
  // The arrival starts on its own: the window only exists because someone asked for
  // the pet, and a figure that simply appears already there wastes that moment.
  emergeAt = 0
  fastArrival = true
  frame()
  startLoop(FRAME_MS_IDLE)
  void swapFigure(scene)
  window.addEventListener('pointerup', release)
})

onBeforeUnmount(() => {
  voice.stop()
  stopAudioChannel()
  if (timer !== undefined) clearInterval(timer)
  window.removeEventListener('pointerup', release)
  mediaQuery?.removeEventListener('change', onMotionChange)
  observer?.disconnect()
  const target = window as unknown as {
    __jarvisPet?: (command: string) => void
    __jarvisPetPointer?: (x: number, y: number) => void
  }
  target.__jarvisPet = undefined
  target.__jarvisPetPointer = undefined
  character?.dispose()
  renderer?.dispose()
  scene = null
  character = null
  ring = null
  halo = null
  sparks = null
})
</script>

<style scoped>
.pet {
  position: fixed;
  inset: 0;
  overflow: hidden;
  /*
   * A field, not a window. The shell could not make this surface transparent (see
   * ``jarvis.ui.pet._paint_backdrop``), so the rectangle is designed for instead of
   * apologised for: light gathering behind her head, the floor lit from under the
   * portal, and a rim that says "this edge is on purpose".
   */
  background:
    radial-gradient(80% 46% at 50% 30%, rgba(28, 86, 128, 0.5), transparent 72%),
    radial-gradient(120% 70% at 50% 112%, rgba(16, 52, 84, 0.55), transparent 66%),
    #04070d;
  user-select: none;
}

.pet::after {
  content: '';
  position: absolute;
  inset: 6px;
  border: 1px solid rgba(77, 216, 255, 0.16);
  border-radius: 18px;
  pointer-events: none;
}

.pet--alpha {
  /*
    Nothing behind her. The shell is compositing exactly these pixels into a window
    that carries real per-pixel alpha, so any background here becomes a rectangle
    painted into the image and shipped to the desktop -- which is the whole thing
    this mode exists to remove. The pill and the caption keep their own translucent
    fills: those are objects, not a backdrop, and the caption has to stay readable
    over whatever the person happens to have open.
  */
  background: none;
}

.pet--alpha::after {
  border: none;
}

.pet__canvas {
  display: block;
  width: 100%;
  height: 100%;
}

.pet__line {
  position: absolute;
  left: 8%;
  right: 8%;
  bottom: 12%;
  margin: 0;
  padding: 6px 10px;
  border-radius: 12px;
  background: rgba(4, 10, 18, 0.62);
  color: #d6f4ff;
  font: 12px/1.5 'Segoe UI', 'Microsoft YaHei', sans-serif;
  text-align: center;
  text-shadow: 0 0 10px rgba(77, 216, 255, 0.35);
  pointer-events: none;
}

.pet__notice {
  position: absolute;
  left: 10%;
  right: 10%;
  top: 42%;
  margin: 0;
  color: #ffb547;
  font: 12px/1.6 'Segoe UI', 'Microsoft YaHei', sans-serif;
  text-align: center;
}

.pet__grip {
  position: absolute;
  top: 6px;
  right: 10px;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 4px 3px 9px;
  border: 1px solid rgba(77, 216, 255, 0.34);
  border-radius: 999px;
  background: rgba(6, 16, 28, 0.74);
  color: #7fe3ff;
  font: 11px/1 'Segoe UI', 'Microsoft YaHei', sans-serif;
  letter-spacing: 0.08em;
  opacity: 0.4;
  transition: opacity 0.18s, border-color 0.18s;
}

.pet__grip:hover,
.pet__grip--held {
  opacity: 1;
  border-color: rgba(77, 216, 255, 0.75);
}

.pet__hold {
  cursor: grab;
}

.pet__grip--held .pet__hold {
  cursor: grabbing;
}

.pet__hide {
  padding: 2px 9px;
  border: 1px solid rgba(77, 216, 255, 0.3);
  border-radius: 999px;
  background: rgba(77, 216, 255, 0.1);
  color: #cdefff;
  font: inherit;
  cursor: pointer;
}

.pet__hide:hover {
  background: rgba(77, 216, 255, 0.22);
}
</style>

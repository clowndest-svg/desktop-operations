<template>
  <div class="moon-portrait" :data-mood="mood" :data-character="character" :data-emotion="emotion">
    <div class="moon-portrait__moon" aria-hidden="true" />
    <canvas ref="canvas" class="moon-portrait__figure" role="img" aria-label="小夜写实动态形象" />
    <span v-if="failed" class="moon-portrait__failure">形象暂未加载</span>
    <div v-if="label" class="moon-portrait__caption"><span />{{ label }}</div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import portraitUrl from '@/assets/brand/portrait.webp'
import { mouthEnvelope, type PortraitMood } from './portraitMotion'
import { hasPortraitHd, portraitAsset, portraitHdAsset, portraitPatchAsset } from './portraitCatalog'
import rigs from '@/assets/portraits/rigs.json'
import type { PortraitEmotion } from './portraitEmotion'

const props = withDefaults(defineProps<{
  mood?: PortraitMood
  label?: string
  readAudio?: () => { rms: number; talking: boolean }
  character?: string
  emotion?: PortraitEmotion
  gaze?: boolean
  hd?: boolean
}>(), { mood: 'idle', label: '', character: 'p01', emotion: 'neutral', gaze: true, hd: false })

const canvas = ref<HTMLCanvasElement | null>(null)
const failed = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
let alive = true
let visible = true
let observer: IntersectionObserver | undefined
let resizer: ResizeObserver | undefined
let previous = 0
let mouth = 0
let blinkTimer: ReturnType<typeof setTimeout> | undefined
let blinkUntil = 0
let calm: MediaQueryList | undefined
let baseImage: HTMLImageElement | undefined
let mouthFrames: HTMLImageElement[] = []
let patchImages: Record<string, HTMLImageElement> = {}
let generation = 0
let gazeX = 0
let gazeY = 0
let targetX = 0
let targetY = 0
let lastPointer = 0
let activeEmotion: PortraitEmotion = 'neutral'
let expression = 0
let buffer: HTMLCanvasElement | undefined
let bufferCtx: CanvasRenderingContext2D | null = null
let bufferSide = 0
type Rig = { face: number[]; eyes: number[][]; mouth: number[] }
let rig: Rig | undefined

async function load(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = reject
    image.src = url
  })
}

async function optional(url: string): Promise<HTMLImageElement | undefined> {
  if (!url) return undefined
  try { return await load(url) } catch { return undefined }
}

async function changeCharacter(): Promise<void> {
  const token = ++generation
  failed.value = false
  try {
    const standard = portraitAsset(props.character, 0)
    const fallback = standard || portraitUrl
    const high = props.hd && hasPortraitHd(props.character)
    let loaded: HTMLImageElement
    try {
      loaded = await load(high ? portraitHdAsset(props.character, 0) : fallback)
    } catch (error) {
      if (!high) throw error
      loaded = await load(fallback)
    }
    if (!alive || token !== generation) return
    baseImage = loaded
    rig = (rigs as Record<string, Rig>)[props.character]
    patchImages = {}
    mouthFrames = []
    const kinds = ['blink', 'happy', 'pout', 'soft', 'pout-brows']
    const entries = await Promise.all(
      kinds.map(async kind => [kind, await optional(portraitPatchAsset(props.character, kind))] as const),
    )
    if (!alive || token !== generation) return
    for (const [kind, image] of entries) if (image) patchImages[kind] = image
    const levels = await Promise.all(
      ['mouth-25', 'mouth-50', 'mouth-75', 'mouth-100'].map(kind => optional(portraitPatchAsset(props.character, kind))),
    )
    if (!alive || token !== generation) return
    mouthFrames = levels.filter((image): image is HTMLImageElement => Boolean(image))
    mouth = expression = 0
    activeEmotion = props.emotion
    if (canvas.value) canvas.value.dataset.loadedCharacter = props.character
    frame()
  } catch {
    if (alive && token === generation) failed.value = true
  }
}

function pointer(event: PointerEvent): void {
  if (event.pointerType === 'touch' || calm?.matches || !props.gaze) return
  const box = canvas.value?.getBoundingClientRect()
  if (!box) return
  targetX = Math.max(-1, Math.min(1, (event.clientX - box.left - box.width / 2) / (innerWidth * .45)))
  targetY = Math.max(-1, Math.min(1, (event.clientY - box.top - box.height * .4) / (innerHeight * .5)))
  lastPointer = performance.now()
}

function gazeEye(ctx: CanvasRenderingContext2D, image: HTMLImageElement, box: number[], side: number): void {
  const [nx, ny, nw, nh] = box as [number, number, number, number]
  const x = nx * side, y = ny * side, width = nw * side, height = nh * side
  ctx.save()
  ctx.beginPath()
  ctx.ellipse(x + width / 2, y + height / 2, width * .46, height * .44, 0, 0, Math.PI * 2)
  ctx.clip()
  // A coarse lattice is enough for a 1-2% iris shift; the old 6x12 grid was
  // four times the draw calls for a movement nobody could see.
  const cols = 4, rows = 8
  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < cols; col += 1) {
      const weight = Math.sin(Math.PI * (col + .5) / cols) * Math.sin(Math.PI * (row + .5) / rows)
      const dx = gazeX * width * .06 * weight
      const dy = gazeY * height * .07 * weight
      ctx.drawImage(
        image,
        (x + col * width / cols - dx) / side * image.width,
        (y + row * height / rows - dy) / side * image.height,
        width / cols / side * image.width,
        height / rows / side * image.height,
        x + col * width / cols,
        y + row * height / rows,
        width / cols + .0004,
        height / rows + .0004,
      )
    }
  }
  ctx.restore()
}

function drawMouth(ctx: CanvasRenderingContext2D, side: number): void {
  if (!mouthFrames.length || mouth < 0.004) return
  const points = [0, .25, .5, .75, 1]
  let index = 0
  while (index < 3 && mouth > points[index + 1]!) index += 1
  const fraction = Math.min(1, Math.max(0, (mouth - points[index]!) / (points[index + 1]! - points[index]!)))
  if (index === 0) {
    const image = mouthFrames[0]
    if (image) { ctx.globalAlpha = fraction; ctx.drawImage(image, 0, 0, side, side) }
    return
  }
  const lower = mouthFrames[index - 1]
  if (lower) { ctx.globalAlpha = 1; ctx.drawImage(lower, 0, 0, side, side) }
  if (fraction > 0.01) {
    const upper = mouthFrames[index]
    if (upper) { ctx.globalAlpha = fraction; ctx.drawImage(upper, 0, 0, side, side) }
  }
}

function drawExpression(ctx: CanvasRenderingContext2D, side: number): void {
  if (expression < 0.01 || mouth >= 0.12) return
  const name = activeEmotion === 'happy' ? 'happy' : activeEmotion === 'pout' ? 'pout' : activeEmotion === 'soft' ? 'soft' : ''
  if (!name) return
  const image = patchImages[name]
  if (image) { ctx.globalAlpha = expression; ctx.drawImage(image, 0, 0, side, side) }
  if (name === 'pout' && patchImages['pout-brows']) {
    ctx.globalAlpha = expression * .9
    ctx.drawImage(patchImages['pout-brows']!, 0, 0, side, side)
  }
}

function ensureBuffer(side: number): CanvasRenderingContext2D | null {
  if (!buffer || bufferSide !== side) {
    buffer = document.createElement('canvas')
    buffer.width = side
    buffer.height = side
    bufferCtx = buffer.getContext('2d')
    bufferSide = side
  }
  return bufferCtx
}

function frame(): void {
  clearTimeout(timer)
  if (!alive) return
  if (!visible || document.hidden || !baseImage) {
    // Keep a slow watchdog alive. An IntersectionObserver that reported "not
    // visible" once must not be the only path back; a single missed callback
    // used to leave the figure frozen for the rest of the session.
    timer = setTimeout(frame, 200)
    return
  }
  const view = canvas.value
  const ctx = view?.getContext('2d')
  if (!view || !ctx) return
  const now = performance.now()
  const delta = Math.min(0.2, (now - (previous || now)) / 1000)
  previous = now
  if (now - lastPointer > 3500 || !props.gaze || calm?.matches) targetX = targetY = 0
  gazeX += (targetX - gazeX) * Math.min(1, delta * 10)
  gazeY += (targetY - gazeY) * Math.min(1, delta * 10)
  if (activeEmotion !== props.emotion) {
    expression = Math.max(0, expression - delta * 6)
    if (expression <= 0.001) { activeEmotion = props.emotion; expression = 0 }
  } else {
    expression = Math.min(1, expression + delta * 6)
  }
  const audio = props.readAudio?.() ?? { rms: 0, talking: false }
  mouth = mouthEnvelope(mouth, audio.rms, audio.talking, delta)
  const blinking = !calm?.matches && now < blinkUntil
  const side = Math.min(view.width, view.height)
  const bctx = ensureBuffer(side)
  if (!bctx) return
  bctx.setTransform(1, 0, 0, 1, 0, 0)
  bctx.clearRect(0, 0, side, side)
  bctx.imageSmoothingEnabled = true
  bctx.imageSmoothingQuality = 'high'
  bctx.globalAlpha = 1
  bctx.drawImage(baseImage, 0, 0, side, side)
  if (!blinking && rig && Math.abs(gazeX) + Math.abs(gazeY) > .01) {
    for (const eye of rig.eyes) gazeEye(bctx, baseImage, eye, side)
  }
  drawMouth(bctx, side)
  drawExpression(bctx, side)
  if (blinking && patchImages['blink']) {
    bctx.globalAlpha = 1
    bctx.drawImage(patchImages['blink'], 0, 0, side, side)
  }
  bctx.globalAlpha = 1
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.clearRect(0, 0, view.width, view.height)
  ctx.imageSmoothingEnabled = true
  ctx.imageSmoothingQuality = 'high'
  const offsetX = view.width - side
  const offsetY = view.height - side
  ctx.translate(offsetX, offsetY)
  if (!calm?.matches) {
    const tilt = props.mood === 'listening' ? -0.007 : props.mood === 'thinking' ? 0.006 : 0
    const cx = side * .5, cy = side * .7
    ctx.translate(cx, cy)
    ctx.rotate(tilt + Math.sin(now / 3400) * .002 + gazeX * .004)
    ctx.translate(-cx, -cy + Math.sin(now / 2400) * side * .0012)
  }
  ctx.drawImage(buffer!, 0, 0, side, side)
  view.dataset.mouth = mouth.toFixed(3)
  view.dataset.playing = String(audio.talking)
  view.dataset.blinking = String(blinking)
  view.dataset.gazeX = gazeX.toFixed(3)
  view.dataset.gazeY = gazeY.toFixed(3)
  timer = setTimeout(frame, calm?.matches ? 200 : audio.talking || blinking ? 33 : 50)
}

function resizeCanvas(): void {
  const view = canvas.value
  if (!view) return
  const box = view.getBoundingClientRect()
  if (!box.width || !box.height) return
  const ratio = Math.min(2, Math.max(1, window.devicePixelRatio || 1))
  const width = Math.min(2048, Math.max(256, Math.round(box.width * ratio)))
  const height = Math.min(2048, Math.max(256, Math.round(box.height * ratio)))
  if (view.width !== width || view.height !== height) {
    view.width = width
    view.height = height
  }
}

function scheduleBlink(): void {
  clearTimeout(blinkTimer)
  blinkTimer = setTimeout(() => {
    if (!calm?.matches && !document.hidden) blinkUntil = performance.now() + 190
    scheduleBlink()
  }, 3600 + Math.random() * 2600)
}

onMounted(async () => {
  calm = matchMedia('(prefers-reduced-motion: reduce)')
  const view = canvas.value!
  resizeCanvas()
  document.addEventListener('visibilitychange', frame)
  window.addEventListener('pointermove', pointer, { passive: true })
  window.addEventListener('resize', resizeCanvas)
  observer = new IntersectionObserver(([entry]) => {
    visible = entry?.isIntersecting ?? true
    frame()
  })
  observer.observe(view)
  resizer = new ResizeObserver(() => { resizeCanvas(); frame() })
  resizer.observe(view)
  scheduleBlink()
  await changeCharacter()
})
watch(() => props.character, changeCharacter)

onBeforeUnmount(() => {
  alive = false
  clearTimeout(timer)
  clearTimeout(blinkTimer)
  observer?.disconnect()
  resizer?.disconnect()
  document.removeEventListener('visibilitychange', frame)
  window.removeEventListener('pointermove', pointer)
  window.removeEventListener('resize', resizeCanvas)
  buffer = undefined
  bufferCtx = null
  mouthFrames = []
  patchImages = {}
})
</script>

<style scoped>
.moon-portrait { position: relative; isolation: isolate; overflow: hidden; min-height: 180px; }
.moon-portrait__figure { position: relative; display: block; width: 100%; height: 100%; object-fit: contain; object-position: center bottom; z-index: 1; mask-image: linear-gradient(#000 85%, transparent); }
.moon-portrait__moon { position: absolute; width: 76%; aspect-ratio: 1; border-radius: 50%; top: 8%; left: 12%; background: radial-gradient(circle at 38% 30%, #d2cbd3, #7e768c 54%, #302b40 70%); opacity: .27; box-shadow: 0 0 48px #9990af20; }
.moon-portrait__moon::after { content: ''; position: absolute; inset: 4% 0 0 13%; border-radius: inherit; background: #15121d; }
.moon-portrait__caption { position: absolute; z-index: 2; bottom: 6px; left: 50%; transform: translateX(-50%); white-space: nowrap; color: #e6e0ec; font-size: 12px; letter-spacing: .12em; padding: 7px 14px; border: 1px solid #b6a8cb26; border-radius: 24px; background: #15121dd9; }
.moon-portrait__caption span { display: inline-block; width: 5px; height: 5px; margin-right: 8px; background: #aca0bf; border-radius: 50%; }
.moon-portrait[data-mood='speaking'] .moon-portrait__caption span { background: #ded4ed; box-shadow: 0 0 7px #d6c3e466; }
.moon-portrait__failure { position: absolute; inset: 40% 0; text-align: center; color: #b8aec5; font-size: 13px; }
</style>

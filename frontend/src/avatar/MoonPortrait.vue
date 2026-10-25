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
import speakingUrl from '@/assets/brand/speaking.webp'
import blinkUrl from '@/assets/brand/blink.webp'
import { mouthEnvelope, type PortraitMood } from './portraitMotion'
import { hasPortraitHd, portraitAsset, portraitHdAsset } from './portraitCatalog'
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
let nextBlink = 3200
let blinkUntil = 0
let calm: MediaQueryList | undefined
let images: HTMLImageElement[] = []
let generation = 0
let gazeX = 0, gazeY = 0, targetX = 0, targetY = 0, lastPointer = 0
let activeEmotion: PortraitEmotion = 'neutral'
let expression = 0
type Rig = { face: number[]; eyes: number[][]; mouth: number[] }
let rig: Rig | undefined
const patches = new Map<string, HTMLCanvasElement>()

async function changeCharacter(): Promise<void> {
  const token = ++generation
  failed.value = false
  try {
    const standard = Array.from({ length: 6 }, (_, i) => portraitAsset(props.character, i))
    const fallback = standard.every(Boolean) ? standard : [portraitUrl, speakingUrl, blinkUrl]
    const hd = props.hd && hasPortraitHd(props.character)
      ? Array.from({ length: 6 }, (_, i) => portraitHdAsset(props.character, i)) : []
    let loaded: HTMLImageElement[]
    try {
      loaded = await Promise.all((hd.length ? hd : fallback).map(load))
    } catch (error) {
      if (!hd.length) throw error
      loaded = await Promise.all(fallback.map(load))
    }
    if (!alive || token !== generation) return
    images = loaded
    rig = (rigs as Record<string, Rig>)[props.character]
    patches.clear()
    mouth = expression = 0
    if (canvas.value) canvas.value.dataset.loadedCharacter=props.character
    frame()
  } catch { if (alive && token === generation) failed.value = true }
}

function pointer(event: PointerEvent): void {
  if (event.pointerType === 'touch' || calm?.matches || !props.gaze) return
  const box = canvas.value?.getBoundingClientRect()
  if (!box) return
  targetX = Math.max(-1, Math.min(1, (event.clientX - box.left - box.width / 2) / (innerWidth * .45)))
  targetY = Math.max(-1, Math.min(1, (event.clientY - box.top - box.height * .4) / (innerHeight * .5)))
  lastPointer = performance.now()
}

function gazeEye(ctx: CanvasRenderingContext2D, image: HTMLImageElement, box: number[]): void {
  const [x, y, w, h] = box as [number, number, number, number]
  // A small texture warp moves the original iris; eyelid corners remain anchored.
  ctx.save()
  ctx.beginPath(); ctx.ellipse(x+w/2, y+h/2, w*.46, h*.44, 0, 0, Math.PI*2); ctx.clip()
  for (let row=0; row<6; row++) for (let col=0; col<12; col++) {
    const weight = Math.sin(Math.PI*(col+.5)/12)*Math.sin(Math.PI*(row+.5)/6)
    const dx=gazeX*w*.095*weight, dy=gazeY*h*.11*weight
    ctx.drawImage(image, (x+col*w/12-dx)*image.width, (y+row*h/6-dy)*image.height,
      w/12*image.width, h/6*image.height, x+col*w/12, y+row*h/6, w/12+.0004, h/6+.0004)
  }
  ctx.restore()
}

function load(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = reject
    image.src = url
  })
}

// Only the feature patch changes, so generation differences cannot make the
// hair/shoulders flicker when a syllable arrives.
function patch(ctx: CanvasRenderingContext2D, image: HTMLImageElement,
               box: number[], alpha: number): void {
  if (alpha < 0.01) return
  const [x, y, width, height] = box as [number, number, number, number]
  const key = image.src + box.join(',')
  let layer = patches.get(key)
  if (!layer) {
    layer = document.createElement('canvas'); layer.width = 512; layer.height = 512
    const part = layer.getContext('2d')!
    part.imageSmoothingQuality = 'high'
    part.drawImage(image, x*image.width, y*image.height, width*image.width, height*image.height, 0, 0, 512, 512)
    part.globalCompositeOperation = 'destination-in'
    const fade = part.createRadialGradient(256, 256, 170, 256, 256, 256)
    fade.addColorStop(0, '#fff'); fade.addColorStop(1, '#fff0')
    part.fillStyle=fade; part.fillRect(0, 0, 512, 512)
    patches.set(key, layer)
  }
  ctx.save(); ctx.globalAlpha=alpha; ctx.drawImage(layer, x,y,width,height); ctx.restore()
}

function frame(): void {
  clearTimeout(timer)
  if (!alive || !visible || document.hidden || images.length < 3) return
  const view = canvas.value
  const ctx = view?.getContext('2d')
  if (!view || !ctx) return
  const now = performance.now()
  const delta = Math.min(0.2, (now - (previous || now)) / 1000)
  previous = now
  if (now-lastPointer > 3500 || !props.gaze || calm?.matches) targetX=targetY=0
  gazeX += (targetX-gazeX)*Math.min(1,delta*8)
  gazeY += (targetY-gazeY)*Math.min(1,delta*8)
  if (activeEmotion !== props.emotion) {
    expression = Math.max(0, expression-delta*4)
    if (!expression) activeEmotion=props.emotion
  } else expression=Math.min(1,expression+delta*3)
  const audio = props.readAudio?.() ?? { rms: 0, talking: false }
  mouth = mouthEnvelope(mouth, audio.rms, audio.talking, delta)
  if (!calm?.matches && now > nextBlink) {
    blinkUntil = now + 160
    nextBlink = now + 3400 + Math.random() * 2200
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.clearRect(0, 0, view.width, view.height)
  ctx.imageSmoothingEnabled = true
  ctx.imageSmoothingQuality = 'high'
  // The canvas box is wider than it is tall on the desktop. Draw the square
  // portrait inside it instead of stretching the face horizontally.
  const side = Math.min(view.width, view.height)
  ctx.translate(view.width - side, view.height - side)
  ctx.scale(side, side)
  if (!calm?.matches) {
    const tilt = props.mood === 'listening' ? -0.016 : props.mood === 'thinking' ? 0.014 : 0
    ctx.translate(0.5, 0.7)
    ctx.rotate(tilt + Math.sin(now / 3400) * 0.004 + gazeX*.008)
    ctx.translate(-0.5, -0.7 + Math.sin(now / 2200) * 0.002)
  }
  ctx.drawImage(images[0]!, 0, 0, 1, 1)
  const emotionIndex = { neutral: 0, happy: 3, pout: 4, soft: 5 }[activeEmotion]
  const face = rig?.face
  if (face && images[emotionIndex] && emotionIndex) {
    patch(ctx, images[emotionIndex]!, [face[0]!-.025,face[1]!-.015,face[2]!+.05,face[3]!+.05], expression)
  }
  patch(ctx, images[1]!, rig?.mouth ?? [0.475, 0.493, 0.14, 0.08], mouth)
  if (now < blinkUntil && !calm?.matches) {
    for (const eye of rig?.eyes ?? [[0.404,0.323,0.105,0.067],[0.575,0.315,0.095,0.068]]) patch(ctx, images[2]!, eye, 1)
  } else if (rig && Math.abs(gazeX)+Math.abs(gazeY)>.01) {
    const source=images[emotionIndex] ?? images[0]!
    for (const eye of rig.eyes) gazeEye(ctx, source, eye)
  }
  view.dataset.mouth = mouth.toFixed(3)
  view.dataset.playing = String(audio.talking)
  view.dataset.gazeX=gazeX.toFixed(3); view.dataset.gazeY=gazeY.toFixed(3)
  timer = setTimeout(frame, audio.talking || now < blinkUntil ? 33 : 100)
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
  nextBlink += performance.now()
  await changeCharacter()
})
watch(() => props.character, changeCharacter)

onBeforeUnmount(() => {
  alive = false
  clearTimeout(timer)
  observer?.disconnect()
  resizer?.disconnect()
  document.removeEventListener('visibilitychange', frame)
  window.removeEventListener('pointermove', pointer)
  window.removeEventListener('resize', resizeCanvas)
  patches.clear()
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

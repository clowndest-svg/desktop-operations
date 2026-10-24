<template>
  <div class="moon-portrait" :data-mood="mood">
    <div class="moon-portrait__moon" aria-hidden="true" />
    <canvas ref="canvas" class="moon-portrait__figure" role="img" aria-label="小夜写实动态形象" />
    <span v-if="failed" class="moon-portrait__failure">形象暂未加载</span>
    <div v-if="label" class="moon-portrait__caption"><span />{{ label }}</div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import portraitUrl from '@/assets/brand/portrait.webp'
import speakingUrl from '@/assets/brand/speaking.webp'
import blinkUrl from '@/assets/brand/blink.webp'
import { mouthEnvelope, type PortraitMood } from './portraitMotion'

const props = withDefaults(defineProps<{
  mood?: PortraitMood
  label?: string
  readAudio?: () => { rms: number; talking: boolean }
}>(), { mood: 'idle', label: '' })

const canvas = ref<HTMLCanvasElement | null>(null)
const failed = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
let alive = true
let visible = true
let observer: IntersectionObserver | undefined
let previous = 0
let mouth = 0
let nextBlink = 3200
let blinkUntil = 0
let calm: MediaQueryList | undefined
const images: HTMLImageElement[] = []

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
  ctx.save()
  ctx.globalAlpha = alpha
  ctx.beginPath()
  ctx.ellipse(x + width / 2, y + height / 2, width / 2, height / 2, 0, 0, Math.PI * 2)
  ctx.clip()
  ctx.drawImage(image, x * image.width, y * image.height, width * image.width,
    height * image.height, x, y, width, height)
  ctx.restore()
}

function frame(): void {
  clearTimeout(timer)
  if (!alive || !visible || document.hidden || images.length !== 3) return
  const view = canvas.value
  const ctx = view?.getContext('2d')
  if (!view || !ctx) return
  const now = performance.now()
  const delta = Math.min(0.2, (now - (previous || now)) / 1000)
  previous = now
  const audio = props.readAudio?.() ?? { rms: 0, talking: false }
  mouth = mouthEnvelope(mouth, audio.rms, audio.talking, delta)
  if (!calm?.matches && now > nextBlink) {
    blinkUntil = now + 160
    nextBlink = now + 3400 + Math.random() * 2200
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.clearRect(0, 0, view.width, view.height)
  ctx.scale(view.width, view.height)
  if (!calm?.matches) {
    const tilt = props.mood === 'listening' ? -0.016 : props.mood === 'thinking' ? 0.014 : 0
    ctx.translate(0.5, 0.7)
    ctx.rotate(tilt + Math.sin(now / 3400) * 0.004)
    ctx.translate(-0.5, -0.7 + Math.sin(now / 2200) * 0.002)
  }
  ctx.drawImage(images[0]!, 0, 0, 1, 1)
  patch(ctx, images[1]!, [0.475, 0.493, 0.14, 0.08], mouth)
  if (now < blinkUntil && !calm?.matches) {
    patch(ctx, images[2]!, [0.404, 0.323, 0.105, 0.067], 1)
    patch(ctx, images[2]!, [0.575, 0.315, 0.095, 0.068], 1)
  }
  view.dataset.mouth = mouth.toFixed(3)
  view.dataset.playing = String(audio.talking)
  timer = setTimeout(frame, audio.talking || now < blinkUntil ? 33 : 100)
}

onMounted(async () => {
  calm = matchMedia('(prefers-reduced-motion: reduce)')
  const view = canvas.value!
  view.width = 640
  view.height = 630
  document.addEventListener('visibilitychange', frame)
  observer = new IntersectionObserver(([entry]) => {
    visible = entry?.isIntersecting ?? true
    frame()
  })
  observer.observe(view)
  try {
    const loaded = await Promise.all([load(portraitUrl), load(speakingUrl), load(blinkUrl)])
    if (!alive) return
    images.push(...loaded)
    nextBlink += performance.now()
    frame()
  } catch { if (alive) failed.value = true }
})

onBeforeUnmount(() => {
  alive = false
  clearTimeout(timer)
  observer?.disconnect()
  document.removeEventListener('visibilitychange', frame)
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

<template>
  <div class="moon-portrait" :data-mood="mood" :data-character="character" :data-emotion="emotion">
    <div class="moon-portrait__moon" aria-hidden="true" />
    <canvas ref="canvas" class="moon-portrait__figure" role="img" aria-label="小夜写实动态形象" />
    <span v-if="failed" class="moon-portrait__failure">形象暂未加载</span>
    <div v-if="label" class="moon-portrait__caption"><span />{{ label }}</div>
  </div>
</template>

<script setup lang="ts">
/**
 * 写实形象：一张照片 + 三件会动的东西。
 *
 * 这一版把三个"动起来很假"的地方逐个换掉了：
 *
 * * **眨眼**。上一版是"睁眼"和"闭眼"两帧之间 190 ms 硬切 —— 没有中间帧，所以是闪
 *   一下。现在按真实眼睑的运动做：一条 75 ms 的闭合曲线 + 135 ms 的睁开，而且闭合
 *   不是换图，是把眼睛上方那条**皮肤**向下拉伸盖住眼睛（人闭眼就是上睑盖下来），
 *   闭到最深时才用随包的闭眼贴片补上眼线和睫毛。几何负责连续，贴片负责细节。
 * * **眼神**。上一版把眼睛区域切成 4×8 块再逐块推拉，那是拉伸原图像素，所以用户
 *   圈着圈说"糊"。现在整块平移、幅度只有眼宽的 5%，椭圆裁剪掉边缘 —— 移动不是变形。
 * * **口型**。四档贴片仍然按音量取两档做线性混合，但多了一道 60 ms 的低通：RMS 每帧
 *   都在抖，不滤的话嘴在以 30 Hz 抽搐。另外说话时表情不再被清零，只是压低 ——
 *   一张说话时说笑就消失的脸，比错一帧口型更假。
 */
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

/** 真实眨眼的两个阶段（秒）。闭合比睁开快，这是它读起来"自然"的关键一半。 */
const BLINK_CLOSE = 0.075
const BLINK_OPEN = 0.135

const canvas = ref<HTMLCanvasElement | null>(null)
const failed = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
let alive = true
let visible = true
let observer: IntersectionObserver | undefined
let resizer: ResizeObserver | undefined
let previous = 0
let mouth = 0
/** 低通后的 RMS：嘴不该跟着每一个采样点的抖动走。 */
let mouthSignal = 0
let blinkTimer: ReturnType<typeof setTimeout> | undefined
let blinkAt = -1
let blinkPeak = 1
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
    mouth = expression = mouthSignal = 0
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

/**
 * 眼神：整块平移，不是变形。
 *
 * 4×8 栅格逐块推拉那一版会拉伸原图像素 —— 用户圈着眼睛说"糊"，圈的就是它。这里
 * 只把眼睛区域整体移 5% 个眼宽，椭圆裁掉边缘，像素一个都不变形。
 */
function gazeEye(ctx: CanvasRenderingContext2D, image: HTMLImageElement, box: number[], side: number): void {
  const [nx, ny, nw, nh] = box as [number, number, number, number]
  const x = nx * side, y = ny * side, width = nw * side, height = nh * side
  const dx = gazeX * width * .05
  const dy = gazeY * height * .06
  if (Math.abs(dx) + Math.abs(dy) < .25) return
  ctx.save()
  ctx.beginPath()
  ctx.ellipse(x + width / 2, y + height / 2, width * .44, height * .42, 0, 0, Math.PI * 2)
  ctx.clip()
  ctx.drawImage(
    image,
    Math.max(0, (x - dx) / side * image.width),
    Math.max(0, (y - dy) / side * image.height),
    width / side * image.width,
    height / side * image.height,
    x, y, width, height,
  )
  ctx.restore()
}

/**
 * 上眼睑：把眼睛上方一条皮肤向下拉伸，盖住眼睛。
 *
 * 这就是"闭眼"的物理过程，不是两张图之间的透明度切换。取的那条皮肤在眉毛下面、
 * 眼睛上面 —— 往上取多了会把眉毛一起拉下来，那是这一版最早做错的地方。
 */
function drawLid(ctx: CanvasRenderingContext2D, box: number[], side: number, amount: number): void {
  const figure = baseImage
  if (!figure || amount < .01) return
  const [nx, ny, nw, nh] = box as [number, number, number, number]
  const eyeW = nw * side, eyeH = nh * side
  const left = nx * side - eyeW * .05
  const width = eyeW * 1.1
  const band = Math.max(1, eyeH * .34)
  const bandTop = ny * side - band
  const drop = eyeH * 1.12 * amount
  ctx.save()
  ctx.beginPath()
  ctx.rect(left, bandTop, width, band + eyeH * 1.16)
  ctx.clip()
  ctx.drawImage(
    figure,
    (left / side) * figure.width,
    Math.max(0, (bandTop / side) * figure.height),
    (width / side) * figure.width,
    (band / side) * figure.height,
    left, bandTop, width, band + drop,
  )
  ctx.restore()
}

/**
 * 四档口型之间线性混合。
 *
 * 贴片只在下颌/嘴部有像素，其余全透明，所以"先铺下界、再按比例叠上界"在数学上就是
 * lerp —— 不会出现第三张嘴。真正把人看吐的是嘴巴在 30 Hz 抽搐，那不是这里的活，
 * 是 ``mouthSignal`` 那道低通。
 */
function drawMouth(ctx: CanvasRenderingContext2D, side: number): void {
  if (!mouthFrames.length || mouth < .004) return
  const scaled = Math.min(.999, mouth) * 3
  const lower = Math.floor(scaled)
  const fraction = scaled - lower
  const from = mouthFrames[lower]
  if (from) { ctx.globalAlpha = 1; ctx.drawImage(from, 0, 0, side, side) }
  if (fraction > .01) {
    const to = mouthFrames[Math.min(mouthFrames.length - 1, lower + 1)]
    if (to) { ctx.globalAlpha = fraction; ctx.drawImage(to, 0, 0, side, side) }
  }
}

function drawExpression(ctx: CanvasRenderingContext2D, side: number): void {
  if (expression < .01) return
  const name = activeEmotion === 'happy' ? 'happy' : activeEmotion === 'pout' ? 'pout' : activeEmotion === 'soft' ? 'soft' : ''
  if (!name) return
  // 说话时表情压低而不是清零：一张笑起来说话的脸，比"说话时笑容消失"自然得多。
  const weight = expression * (mouth >= .12 ? .45 : 1)
  const image = patchImages[name]
  if (image) { ctx.globalAlpha = weight; ctx.drawImage(image, 0, 0, side, side) }
  if (name === 'pout' && patchImages['pout-brows']) {
    ctx.globalAlpha = weight * .9
    ctx.drawImage(patchImages['pout-brows']!, 0, 0, side, side)
  }
}

/** 0 → 1 → 0：闭合快、睁开慢，且每次闭合深度略有不同。 */
function blinkAmount(now: number): number {
  if (blinkAt < 0) return 0
  const t = (now - blinkAt) / 1000
  if (t < 0) return 0
  if (t < BLINK_CLOSE) return blinkPeak * (t / BLINK_CLOSE)
  const open = (t - BLINK_CLOSE) / BLINK_OPEN
  if (open >= 1) { blinkAt = -1; return 0 }
  // 睁开走 ease-out：真实眼睑抬起是先快后慢，匀速会读成机械。
  return blinkPeak * (1 - open * open)
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
  // 一道 60 ms 低通再进包络：RMS 每个采样点都在变，不滤的话嘴在以 30 Hz 抽搐 ——
  // 这比口型不准更毁观感。
  mouthSignal += (audio.rms - mouthSignal) * Math.min(1, delta / 0.06)
  mouth = mouthEnvelope(mouth, mouthSignal, audio.talking, delta)
  const blinking = calm?.matches ? 0 : blinkAmount(now)
  const side = Math.min(view.width, view.height)
  const bctx = ensureBuffer(side)
  if (!bctx) return
  bctx.setTransform(1, 0, 0, 1, 0, 0)
  bctx.clearRect(0, 0, side, side)
  bctx.imageSmoothingEnabled = true
  bctx.imageSmoothingQuality = 'high'
  bctx.globalAlpha = 1
  bctx.drawImage(baseImage, 0, 0, side, side)
  if (rig) {
    if (Math.abs(gazeX) + Math.abs(gazeY) > .01 && blinking < .55) {
      for (const eye of rig.eyes) gazeEye(bctx, baseImage, eye, side)
    }
    drawMouth(bctx, side)
    drawExpression(bctx, side)
    if (blinking > .01) {
      // 随包的闭眼贴片本来就只覆盖眼睛那一块、自带羽化边 —— 它是主力，按开合度
      // 全程渐变（75 ms 从透明到覆盖，人眼读作眼皮落下，而不是"换了一张图"）。
      const patch = patchImages['blink']
      if (patch) {
        bctx.globalAlpha = Math.min(1, blinking * 1.15)
        bctx.drawImage(patch, 0, 0, side, side)
      }
      // 几何只做很轻的一点下压，给眼皮一点厚度。幅度大了会在眉毛下面拉出一条横向
      // 接缝 —— 第一版用的是 0.86 倍眼高，那张截图里她左眼上方的方块就是这么来的。
      for (const eye of rig.eyes) drawLid(bctx, eye, side, blinking * .26)
    }
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
  view.dataset.blinking = String(blinking > .5)
  view.dataset.blink = blinking.toFixed(3)
  view.dataset.gazeX = gazeX.toFixed(3)
  view.dataset.gazeY = gazeY.toFixed(3)
  timer = setTimeout(frame, calm?.matches ? 200 : audio.talking || blinking > .01 ? 33 : 50)
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

/**
 * 真人两三秒眨一次，偶尔连眨两下。
 *
 * 上一版固定 3.6~6.2 秒、每次都一样深，加上硬切，读起来像机器在做定期自检。
 */
function scheduleBlink(): void {
  clearTimeout(blinkTimer)
  blinkTimer = setTimeout(() => {
    if (!calm?.matches && !document.hidden && blinkAt < 0) {
      blinkAt = performance.now()
      blinkPeak = .82 + Math.random() * .18
      if (Math.random() < .22) {
        window.setTimeout(() => {
          if (!document.hidden) { blinkAt = performance.now(); blinkPeak = .72 }
        }, 280)
      }
    }
    scheduleBlink()
  }, 2400 + Math.random() * 3400)
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

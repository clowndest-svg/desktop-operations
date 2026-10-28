<template>
  <div v-if="visible" class="splash" :class="{ 'splash--out': leaving }" aria-hidden="true">
    <canvas ref="canvas" class="splash__canvas" />
    <div class="splash__stage">
      <p class="splash__kicker">LOCAL · PRIVATE · ALWAYS ON</p>
      <h1 class="splash__title" :class="{ 'splash__title--in': titleIn }"><span style="--i: 0">小</span><span style="--i: 1">夜</span></h1>
      <p class="splash__sub">XIAOYE</p>
      <div class="splash__bar"><i :style="{ width: `${Math.round(progress * 100)}%` }" /></div>
      <p class="splash__status">{{ status }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 开机那两秒半。
 *
 * 它要解决的是"点开 exe 先看到一片黑/白，然后界面突然跳出来"—— 那一下比任何动画都
 * 显得粗糙。所以顺序是：index.html 里的静态垫底先把背景铺上（没有任何 JS 参与），
 * 这个组件挂载后接管，播粒子汇聚 + 网格推进 + 标题解码，结束时淡出露出主界面。
 *
 * 三条约束：不超过 2.5 秒（它是等待，不是节目）；「减少动态效果」直接跳过（不播就是
 * 尊重设置，播快一点不是）；不碰主线程的重活（纯 2D canvas，粒子数固定，结束即销毁）。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

const DURATION = 2400
const LEAVE = 520

const visible = ref(true)
const leaving = ref(false)
const progress = ref(0)
const titleIn = ref(false)
const status = ref('正在唤醒')
const canvas = ref<HTMLCanvasElement | null>(null)

let frame = 0
let start = 0
let observer: ResizeObserver | undefined
let timer: number | undefined
let leaveTimer: number | undefined

type Particle = { angle: number; radius: number; spin: number; size: number; delay: number }
let particles: Particle[] = []
let stars: { x: number; y: number; r: number; phase: number }[] = []

function build(width: number, height: number): void {
  const count = Math.round(Math.min(220, Math.max(90, (width * height) / 12000)))
  particles = Array.from({ length: count }, () => ({
    angle: Math.random() * Math.PI * 2,
    radius: 0.22 + Math.random() * 0.62,
    spin: (Math.random() - 0.5) * 1.4,
    size: 0.6 + Math.random() * 1.8,
    delay: Math.random() * 0.28,
  }))
  stars = Array.from({ length: 90 }, () => ({
    x: Math.random() * width,
    y: Math.random() * height,
    r: 0.4 + Math.random() * 1.1,
    phase: Math.random() * Math.PI * 2,
  }))
}

const easeOut = (value: number): number => 1 - (1 - value) ** 3
const clamp01 = (value: number): number => Math.max(0, Math.min(1, value))

function draw(now: number): void {
  const view = canvas.value
  const ctx = view?.getContext('2d')
  if (!view || !ctx) return
  const t = clamp01((now - start) / DURATION)
  const width = view.width
  const height = view.height
  const cx = width / 2
  const cy = height * 0.5

  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.clearRect(0, 0, width, height)

  // 底色：一团很慢的呼吸光，避免纯黑显得像没启动
  const breath = 0.5 + 0.5 * Math.sin(now / 900)
  const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, Math.max(width, height) * 0.62)
  glow.addColorStop(0, `rgba(126, 106, 156, ${0.22 + breath * 0.08})`)
  glow.addColorStop(0.45, 'rgba(38, 30, 52, 0.55)')
  glow.addColorStop(1, 'rgba(9, 7, 13, 1)')
  ctx.fillStyle = glow
  ctx.fillRect(0, 0, width, height)

  // 星点
  for (const star of stars) {
    const twinkle = 0.35 + 0.65 * Math.abs(Math.sin(now / 620 + star.phase))
    ctx.fillStyle = `rgba(220, 208, 236, ${twinkle * 0.7 * clamp01(t * 4)})`
    ctx.beginPath()
    ctx.arc(star.x, star.y, star.r, 0, Math.PI * 2)
    ctx.fill()
  }

  // 透视网格：地平线以下，横线朝观者推进
  const horizon = height * 0.72
  ctx.strokeStyle = `rgba(180, 160, 210, ${0.16 * clamp01((t - 0.05) * 3)})`
  ctx.lineWidth = 1
  const lanes = 14
  for (let lane = 0; lane < lanes; lane += 1) {
    const spread = (lane / lanes - 0.5) * width * 2.4
    ctx.beginPath()
    ctx.moveTo(cx + spread * 0.06, horizon)
    ctx.lineTo(cx + spread, height)
    ctx.stroke()
  }
  const rows = 9
  for (let row = 0; row < rows; row += 1) {
    const phase = (row / rows + (now / 2600) % (1 / rows)) % 1
    const y = horizon + (height - horizon) * phase ** 2.4
    ctx.globalAlpha = 0.2 * (1 - phase) * clamp01((t - 0.08) * 3)
    ctx.beginPath()
    ctx.moveTo(0, y)
    ctx.lineTo(width, y)
    ctx.stroke()
  }
  ctx.globalAlpha = 1

  // 粒子汇聚：从外圈旋进来，越晚越亮
  const gather = easeOut(clamp01((t - 0.12) / 0.55))
  for (const particle of particles) {
    const local = clamp01((gather - particle.delay) / (1 - particle.delay || 1))
    if (local <= 0) continue
    const radius = Math.min(width, height) * particle.radius * (1 - local) ** 1.6
    const angle = particle.angle + particle.spin * local * 2.2
    const x = cx + Math.cos(angle) * radius * 1.35
    const y = cy + Math.sin(angle) * radius * 0.9
    // 收进中心的同时淡出：不这么做的话 220 个粒子会挤成标题中间的一个亮点，
    // 看起来像「小·夜」中间多了个句号。
    ctx.fillStyle = `rgba(226, 214, 244, ${(0.15 + local * 0.75) * (1 - local ** 2.2)})`
    ctx.beginPath()
    ctx.arc(x, y, particle.size * (0.4 + local), 0, Math.PI * 2)
    ctx.fill()
  }

  // 能量环：从大到小收进中心，然后炸开一层
  const ringT = clamp01((t - 0.18) / 0.42)
  if (ringT > 0 && ringT < 1) {
    const radius = Math.min(width, height) * (0.46 * (1 - ringT) + 0.02)
    ctx.strokeStyle = `rgba(214, 196, 240, ${0.5 * (1 - ringT)})`
    ctx.lineWidth = 2 + 3 * (1 - ringT)
    ctx.beginPath()
    ctx.arc(cx, cy, radius, 0, Math.PI * 2)
    ctx.stroke()
  }
  const burst = clamp01((t - 0.6) / 0.3)
  if (burst > 0 && burst < 1) {
    ctx.strokeStyle = `rgba(236, 226, 252, ${0.55 * (1 - burst)})`
    ctx.lineWidth = 1.5
    ctx.beginPath()
    ctx.arc(cx, cy, Math.min(width, height) * 0.05 * burst * 6, 0, Math.PI * 2)
    ctx.stroke()
  }

  // 扫描线：一条带拖尾的亮线从上往下
  const scan = clamp01((t - 0.45) / 0.5)
  if (scan > 0 && scan < 1) {
    const y = height * scan
    // 拖尾短而淡：第一版是 90 px / 0.28，扫到中间时整条上半屏变亮，读起来像地平线
    // 而不是一道掠过的光。
    const trail = ctx.createLinearGradient(0, y - 46, 0, y)
    trail.addColorStop(0, 'rgba(198, 180, 226, 0)')
    trail.addColorStop(1, `rgba(222, 208, 244, ${0.14 * (1 - scan)})`)
    ctx.fillStyle = trail
    ctx.fillRect(0, y - 46, width, 46)
    ctx.fillStyle = `rgba(240, 232, 252, ${0.62 * (1 - scan)})`
    ctx.fillRect(0, y, width, 1)
  }

  // 收尾那一下白闪
  const flash = clamp01((t - 0.86) / 0.14)
  if (flash > 0) {
    ctx.fillStyle = `rgba(244, 238, 252, ${0.12 * (1 - flash)})`
    ctx.fillRect(0, 0, width, height)
  }

  progress.value = t
  status.value = t < 0.42 ? '正在唤醒' : t < 0.78 ? '载入形象与记忆' : '就绪'

  // 标题由 DOM 自己浮上来（逐字 delay + 字距收紧），这里只负责扣扳机。
  // 上一版在 rAF 里把字替换成随机符号做「解码」，中途会出现两个菱形，
  // 看起来像占位符而不是正在成形的名字。
  titleIn.value = t > 0.34

  if (t < 1) frame = requestAnimationFrame(draw)
}

function resize(): void {
  const view = canvas.value
  if (!view) return
  const ratio = Math.min(2, window.devicePixelRatio || 1)
  view.width = Math.round(window.innerWidth * ratio)
  view.height = Math.round(window.innerHeight * ratio)
  build(view.width, view.height)
}

onMounted((): void => {
  const calm = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  // 验收脚本用 ?nosplash=1 跳过：那 2.4 秒里它盖住整屏、吃掉所有点击，逐项点开弹窗的
  // 脚本会卡在第一下。显式传参而不是默认可关 —— 启动动画自己由单独一条判据验。
  if (new URLSearchParams(location.search).has('nosplash')) {
    document.getElementById('boot')?.remove()
    visible.value = false
    return
  }
  // 垫底那一层（index.html 里的 #boot）现在由这里接管。
  document.getElementById('boot')?.remove()
  if (calm) {
    start = performance.now() - DURATION * 0.86
  } else {
    resize()
    observer = new ResizeObserver(resize)
    observer.observe(document.documentElement)
    start = performance.now()
    frame = requestAnimationFrame(draw)
  }
  leaveTimer = window.setTimeout(() => { leaving.value = true }, calm ? 220 : DURATION)
  timer = window.setTimeout(() => { visible.value = false }, calm ? 220 + LEAVE : DURATION + LEAVE)
})

onBeforeUnmount((): void => {
  cancelAnimationFrame(frame)
  observer?.disconnect()
  window.clearTimeout(timer)
  window.clearTimeout(leaveTimer)
})
</script>

<style scoped>
.splash {
  position: fixed;
  z-index: 100;
  inset: 0;
  overflow: hidden;
  background: #09070d;
  transition: opacity 0.5s cubic-bezier(0.4, 0, 0.2, 1), transform 0.5s cubic-bezier(0.4, 0, 0.2, 1);
}

.splash--out {
  opacity: 0;
  transform: scale(1.03);
  pointer-events: none;
}

.splash__canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}

.splash__stage {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  pointer-events: none;
  text-align: center;
}

.splash__kicker {
  margin: 0 0 6px;
  font: 10px/1.6 'Segoe UI', 'Microsoft YaHei', sans-serif;
  letter-spacing: 0.42em;
  color: #8f84a3;
  opacity: 0.85;
}

.splash__title {
  margin: 0;
  font: 300 clamp(40px, 6vw, 74px)/1.1 'Microsoft YaHei', 'Segoe UI', sans-serif;
  /* 字距从松到紧：字是「收拢」成这个名字的，不是啪一下出现。 */
  letter-spacing: 0.92em;
  text-indent: 0.92em;
  color: #f0eaf6;
  text-shadow: 0 0 26px rgba(198, 176, 232, 0.55), 0 0 78px rgba(140, 116, 184, 0.35);
  transition: letter-spacing 0.7s cubic-bezier(0.2, 0.8, 0.2, 1), text-indent 0.7s cubic-bezier(0.2, 0.8, 0.2, 1), text-shadow 0.7s ease;
}

.splash__title--in {
  letter-spacing: 0.34em;
  text-indent: 0.34em;
  text-shadow: 0 0 34px rgba(210, 190, 244, 0.75), 0 0 96px rgba(140, 116, 184, 0.45);
}

.splash__title span {
  display: inline-block;
  opacity: 0;
  filter: blur(4px);
  transform: translateY(0.1em) scale(0.97);
  transition: opacity 0.55s ease, filter 0.55s ease, transform 0.55s cubic-bezier(0.2, 0.8, 0.2, 1);
  transition-delay: calc(var(--i) * 0.08s);
}

.splash__title--in span {
  opacity: 1;
  filter: none;
  transform: none;
}

.splash__sub {
  margin: 2px 0 18px;
  font: 11px/1.4 'Segoe UI', sans-serif;
  letter-spacing: 0.66em;
  text-indent: 0.66em;
  color: #a99cb8;
}

.splash__bar {
  width: min(280px, 46vw);
  height: 2px;
  border-radius: 2px;
  background: rgba(198, 184, 220, 0.16);
  overflow: hidden;
}

.splash__bar i {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, rgba(178, 158, 214, 0.2), #d8c9ee);
  box-shadow: 0 0 12px rgba(216, 201, 238, 0.7);
  transition: width 0.12s linear;
}

.splash__status {
  margin: 6px 0 0;
  font: 11px/1.5 'Segoe UI', 'Microsoft YaHei', sans-serif;
  letter-spacing: 0.24em;
  color: #9b8fae;
}
</style>

/**
 * Skins: one set of numbers that recolours both the CSS and the figure.
 *
 * A theme that only touched CSS variables would leave the 3D panel in the old
 * palette -- a blue dashboard with a cyan hologram in the corner is not a skin, it
 * is a bug with good intentions. So the avatar reads its inks from here too, and
 * the stage rebuilds the figure when the skin changes.
 *
 * The choice lives in ``localStorage`` rather than in Python's preferences on
 * purpose: it is a property of *this browser surface*, it must apply before the
 * first frame (a flash of the wrong palette is exactly the kind of thing that
 * reads as "the app restarted wrong"), and it is not worth a bridge round-trip on
 * every launch.
 */

import { ref } from 'vue'

export interface Skin {
  id: string
  label: string
  /**
   * The three inks every figure is drawn with: edges, the light inside them, and the
   * fill a projection is made of. A dropped-in model is re-inked the same way, so a
   * skin changes the figure rather than just tinting whatever was already there.
   */
  line: number
  glow: number
  fill: number
}

/**
 * Every skin, in the order the picker draws them.
 *
 * 六套，都只动"带色相的那几个 token"：几何、层次、间距在每套里完全一样 —— 一套连布局
 * 都改的主题不是配色，是另一个界面在假装是配色。语义色（--hud-red/amber/green/violet）
 * **不跟着变**：报警必须一直是红的，那是颜色在说话，不是配色。
 */
export const SKINS: readonly Skin[] = [
  { id: 'blue', label: '蓝 · 赛博', line: 0x2f8fff, glow: 0x9fd4ff, fill: 0x06203f },
  { id: 'cyan', label: '青 · 初版', line: 0x62e4ff, glow: 0xbdf3ff, fill: 0x0d3550 },
  { id: 'violet', label: '紫 · 夜航', line: 0x9d7cff, glow: 0xd3c4ff, fill: 0x1b1038 },
  { id: 'green', label: '翠 · 终端', line: 0x35e08a, glow: 0xb9ffd8, fill: 0x06301f },
  { id: 'amber', label: '琥珀 · 暖阳', line: 0xffb04a, glow: 0xffe0b0, fill: 0x3a2408 },
  { id: 'rose', label: '玫 · 警戒', line: 0xff5f8a, glow: 0xffc2d4, fill: 0x3a0d1c },
]

const STORAGE_KEY = 'jarvis.skin'

function stored(): string {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    return SKINS.some((entry) => entry.id === saved) ? (saved as string) : SKINS[0].id
  } catch {
    return SKINS[0].id
  }
}

export const skin = ref<string>(stored())

export function currentSkin(): Skin {
  return SKINS.find((entry) => entry.id === skin.value) ?? SKINS[0]
}

export function setSkin(id: string): void {
  if (!SKINS.some((entry) => entry.id === id)) return
  skin.value = id
  try {
    localStorage.setItem(STORAGE_KEY, id)
  } catch {
    // Private mode or a locked-down webview: the skin still applies for this run.
  }
  document.documentElement.dataset.skin = id
}

/** Called once on mount, before the first frame is drawn. */
export function applyStoredSkin(): void {
  document.documentElement.dataset.skin = skin.value
}

/** ``0x2f8fff`` → ``#2f8fff``, for the swatches and the button's dot. */
export function hex(value: number): string {
  return `#${value.toString(16).padStart(6, '0')}`
}

/**
 * 皮肤跟着窗口走：同源的另一扇窗改了 localStorage，这里收到 storage 事件。
 *
 * 这一条是**尽力而为**，不是唯一的路：宠物窗口是另一个 WebView2 实例，两个实例是否互发
 * storage 事件取决于 Chromium 的进程分组，不敢当保证用。真正的保证在桥面上 ——
 * 点了皮肤之后页面会调 ``skin_apply``，由壳子把 id 推进另一扇窗（见
 * ``jarvis/ui/pet.py`` 的 ``apply_skin``）。两条路都留着，谁先到都行。
 */
try {
  window.addEventListener('storage', (event) => {
    const next = event.newValue
    if (event.key !== STORAGE_KEY || typeof next !== 'string') return
    if (!SKINS.some((entry) => entry.id === next)) return
    skin.value = next
    document.documentElement.dataset.skin = next
  })
} catch {
  // 没有 window（单测、SSR）就没有这一条，不影响本地切换。
}

/**
 * The shell's cue channel for a skin, the same shape as the pet's other cues.
 *
 * ``jarvis.ui.pet.apply_skin`` calls this on the *other* window; the id arrives already
 * validated on the Python side as well, and :func:`setSkin` refuses anything not in
 * ``SKINS``, so a bad value cannot reach the DOM.
 */
export function installSkinChannel(): void {
  const target = window as unknown as { __jarvisSetSkin?: (id: string) => void }
  target.__jarvisSetSkin = (id: string) => {
    setSkin(id)
  }
}

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

export const SKINS: readonly Skin[] = [
  { id: 'blue', label: '蓝 · 赛博', line: 0x2f8fff, glow: 0x9fd4ff, fill: 0x06203f },
  { id: 'cyan', label: '青 · 初版', line: 0x62e4ff, glow: 0xbdf3ff, fill: 0x0d3550 },
  { id: 'violet', label: '紫 · 夜航', line: 0x9d7cff, glow: 0xd3c4ff, fill: 0x1b1038 },
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

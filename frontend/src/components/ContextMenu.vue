<script setup lang="ts">
/**
 * 右键菜单本体。挂在 App 最后一层，谁都能叫它（见 `composables/useContextMenu.ts`）。
 *
 * 三条是规范要求的，不是装饰：
 *  - **点外面、Esc、窗口失焦都要关**。关不掉的菜单比没有菜单更糟。
 *  - **键盘可达**：打开即聚焦，上下键走、回车执行。只用鼠标也能用，但只用键盘不该卡住。
 *  - **贴边翻转**：这一窗口的气泡常常在右下角，菜单在那儿打开会有一半在屏幕外。
 */

import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { isSeparator, useContextMenu } from '../composables/useContextMenu'

const { state, closeMenu } = useContextMenu()

const panel = ref<HTMLElement | null>(null)
const active = ref(0)
const placed = ref({ x: 0, y: 0 })

const actions = computed(() => state.entries.map((entry, index) => ({ entry, index })))

function runnable(index: number): boolean {
  const entry = state.entries[index]
  return entry !== undefined && !isSeparator(entry) && !entry.disabled
}

function step(delta: number): void {
  let moved = active.value
  for (let i = 0; i < state.entries.length; i += 1) {
    moved = (moved + delta + state.entries.length) % state.entries.length
    if (runnable(moved)) {
      active.value = moved
      return
    }
  }
}

watch(
  () => [state.open, state.x, state.y] as const,
  async ([open]) => {
    if (!open) return
    active.value = Math.max(
      state.entries.findIndex((entry) => !isSeparator(entry) && !entry.disabled),
      0,
    )
    await nextTick()
    const box = panel.value?.getBoundingClientRect()
    const width = box?.width ?? 180
    const height = box?.height ?? 120
    // 放不下就往反方向翻：宁可盖住一点触发它的东西，也不要露出一半到屏幕外。
    placed.value = {
      x: Math.max(6, Math.min(state.x, window.innerWidth - width - 6)),
      y: Math.max(6, Math.min(state.y, window.innerHeight - height - 6)),
    }
    panel.value?.focus({ preventScroll: true })
  },
)

function choose(index: number): void {
  const entry = state.entries[index]
  if (!entry || isSeparator(entry) || entry.disabled) return
  closeMenu()
  void entry.run()
}

function onKeydown(event: KeyboardEvent): void {
  if (!state.open) return
  if (event.key === 'Escape') {
    event.preventDefault()
    closeMenu()
  } else if (event.key === 'ArrowDown') {
    event.preventDefault()
    step(1)
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    step(-1)
  } else if (event.key === 'Home') {
    event.preventDefault()
    step(-999)
  } else if (event.key === 'End') {
    event.preventDefault()
    step(999)
  } else if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    choose(active.value)
  }
}

function onPointerDown(event: PointerEvent): void {
  if (!state.open) return
  if (panel.value?.contains(event.target as Node)) return
  closeMenu()
}

function onScroll(): void {
  closeMenu()
}

window.addEventListener('keydown', onKeydown, true)
window.addEventListener('pointerdown', onPointerDown, true)
window.addEventListener('resize', closeMenu)
window.addEventListener('blur', closeMenu)
window.addEventListener('scroll', onScroll, true)

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown, true)
  window.removeEventListener('pointerdown', onPointerDown, true)
  window.removeEventListener('resize', closeMenu)
  window.removeEventListener('blur', closeMenu)
  window.removeEventListener('scroll', onScroll, true)
})
</script>

<template>
  <Teleport to="body">
    <div
      v-if="state.open"
      ref="panel"
      class="ctx"
      :style="{ left: placed.x + 'px', top: placed.y + 'px' }"
      tabindex="-1"
      role="menu"
      @contextmenu.prevent
    >
      <template v-for="({ entry, index }, slot) in actions" :key="slot">
        <span v-if="isSeparator(entry)" class="ctx__sep" />
        <button
          v-else
          class="ctx__item"
          :class="{ 'ctx__item--danger': entry.danger, 'ctx__item--off': entry.disabled }"
          :disabled="entry.disabled"
          :aria-disabled="entry.disabled ? 'true' : 'false'"
          role="menuitem"
          type="button"
          :data-active="index === active ? '1' : null"
          @click="choose(index)"
          @mouseenter="runnable(index) ? (active = index) : null"
        >
          {{ entry.label }}
        </button>
      </template>
    </div>
  </Teleport>
</template>

<style scoped>
.ctx {
  position: fixed;
  z-index: 60;
  min-width: 148px;
  padding: 4px;
  border: 1px solid var(--hud-line);
  border-radius: 10px;
  background: var(--hud-scrim), var(--hud-bg-deep);
  box-shadow: 0 12px 30px rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(10px);
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.ctx__item {
  display: block;
  width: 100%;
  padding: 6px 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--hud-text);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}

.ctx__item[data-active='1'] {
  background: rgba(77, 216, 255, 0.14);
}

.ctx__item--danger {
  color: var(--hud-red);
}

.ctx__item--off {
  color: var(--hud-dim);
  cursor: not-allowed;
}

.ctx__sep {
  height: 1px;
  margin: 4px 6px;
  background: var(--hud-line);
}
</style>

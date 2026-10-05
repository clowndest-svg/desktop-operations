<template>
  <div v-if="open" class="sk" role="dialog" aria-modal="true" aria-label="皮肤">
    <div class="sk__scrim" @click="emit('close')"></div>

    <section class="sk__box">
      <header class="sk__head">
        <h2 class="hud-title">皮肤 · SKIN</h2>
        <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
      </header>

      <!--
        一屏看全，不用翻页：颜色这种东西要并排看才知道自己更喜欢哪个，"下一个"式的
        轮换把选择变成了一次次盲试。每张卡都拿该皮肤的三个色画一个迷你仪表盘示意，
        再加上她的发光色 —— 因为皮肤不只染面板，也染桌面上那个人。
      -->
      <ul class="sk__grid">
        <li v-for="entry in SKINS" :key="entry.id">
          <button
            class="sk__card"
            type="button"
            :class="{ 'sk__card--on': entry.id === skin }"
            :disabled="busy"
            :title="`${entry.label}（点击就换，立即生效）`"
            @click="choose(entry.id)"
          >
            <span class="sk__preview" :style="previewStyle(entry)">
              <span class="sk__preview-line" :style="{ background: hex(entry.line) }"></span>
              <span class="sk__preview-block" :style="{ background: hex(entry.fill) }"></span>
              <span class="sk__preview-dot" :style="{ background: hex(entry.glow) }"></span>
            </span>
            <span class="sk__label">{{ entry.label }}</span>
            <span class="sk__state">{{ entry.id === skin ? '● 在用' : '○ 换成这个' }}</span>
          </button>
        </li>
      </ul>

      <p v-if="note" class="sk__note" :class="{ 'sk__note--bad': noteBad }">{{ note }}</p>

      <p class="sk__hint">
        换皮肤会同时改仪表盘和她的颜色——桌面上的那个人也在内，立即生效，下次打开还是这个。
        色号是固定的六套，语义色（告警红、完成绿、琥珀）不跟着变。
      </p>
    </section>
  </div>
</template>

<script setup lang="ts">
/**
 * The skin picker: six palettes, heard with the eyes instead of cycled blind.
 *
 * Two windows have to agree. This page repaints itself the moment ``setSkin`` runs, and
 * then tells the shell (``skinApply``) so the *pet* window — a separate page, with its
 * own copy of the figure — re-inks too. A palette that only reached the dashboard would
 * leave a blue figure standing next to an amber interface, which reads as a bug rather
 * than a theme.
 */
import { ref } from 'vue'
import { reportPetPalette, skinApply } from '@/api/bridge'
import { SKINS, currentSkin, hex, setSkin, skin, type Skin } from '@/theme'

const props = defineProps<{ open: boolean }>()
void props
const emit = defineEmits<{ (e: 'close'): void }>()

const busy = ref(false)
const note = ref('')
const noteBad = ref(false)

function previewStyle(entry: Skin): Record<string, string> {
  return {
    background: `${hex(entry.fill)}`,
    borderColor: `${hex(entry.line)}66`,
    boxShadow: `0 0 12px ${hex(entry.glow)}33`,
  }
}

async function choose(id: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  note.value = ''
  // 自己这一页先换：那是即时可见的，不该等一次桥面往返。
  setSkin(id)
  // 桌上的两张卡是 Python 画的，它只认这三个墨色 —— 报过去，否则人是琥珀色的、
  // 字还是青色的。这一步不影响选皮肤成败，所以不占用 note。
  void reportPetPalette(currentSkin()).catch(() => undefined)
  try {
    const answer = await skinApply(id)
    if (!answer.ok) {
      note.value = answer.error || '壳子没能把皮肤送到另一扇窗'
      noteBad.value = true
    } else if (!answer.pet) {
      // 宠物没开的时候这不是错误，但值得说清楚"她下次出现就是新颜色了"。
      note.value = '已换。宠物现在没开，下次她出来就是这套颜色'
      noteBad.value = false
    }
  } catch (err) {
    note.value = err instanceof Error ? err.message : String(err)
    noteBad.value = true
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.sk {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
}

.sk__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.sk__box {
  position: relative;
  width: min(560px, 94vw);
  /* The card grid grows with the number of skins, and this box had neither a height limit
     nor a scroller: past the viewport edge the last row was simply unreachable. */
  max-height: 84vh;
  overflow-y: auto;
  padding: 16px 20px 14px;
  border: 1px solid var(--hud-line);
  border-top: 1px solid var(--hud-rim);
  border-radius: var(--hud-radius);
  background: linear-gradient(180deg, rgba(10, 24, 40, 0.96), rgba(4, 8, 14, 0.98));
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}

.sk__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.sk__grid {
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.sk__card {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  padding: 8px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-radius);
  background: rgba(6, 16, 28, 0.4);
  color: var(--hud-text);
  font-family: inherit;
  font-size: 11px;
  text-align: left;
  cursor: pointer;
}

.sk__card:hover {
  border-color: var(--hud-rim);
}

.sk__card--on {
  border-color: var(--hud-rim);
  background: rgba(255, 255, 255, 0.04);
}

.sk__preview {
  width: 100%;
  height: 46px;
  display: flex;
  align-items: flex-end;
  gap: 4px;
  padding: 6px;
  border: 1px solid;
  border-radius: 8px;
}

.sk__preview-line {
  flex: 1 1 auto;
  height: 3px;
  border-radius: 2px;
  align-self: flex-start;
}

.sk__preview-block {
  flex: 0 0 26px;
  height: 18px;
  border-radius: 4px;
  opacity: 0.9;
}

.sk__preview-dot {
  flex: 0 0 10px;
  height: 10px;
  border-radius: 50%;
}

.sk__label {
  color: var(--hud-text);
}

.sk__state {
  color: var(--hud-dim);
}

.sk__card--on .sk__state {
  color: var(--hud-cyan);
}

.sk__note {
  margin: 10px 0 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--hud-green);
}

.sk__note--bad {
  color: var(--hud-red);
}

.sk__hint {
  margin: 10px 0 0;
  padding-top: 8px;
  border-top: 1px solid var(--hud-line);
  font-size: 10px;
  line-height: 1.6;
  color: var(--hud-dim);
}
</style>

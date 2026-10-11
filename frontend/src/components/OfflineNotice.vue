<template>
  <!--
    断线那一句只说一次。
    「没网了」是这一整个窗口里唯一一个不由操作员点开的弹窗，所以它必须和别的弹窗一样挂在
    顶层、挂在设置面板**之后**（见 App.vue）：同一个 z-index 层里靠文档顺序压上去，
    挂在对话区里面就是"从设置里看不见的那张卡"，这个项目为这件事付过一次账。
  -->
  <div v-if="open" class="off" role="alertdialog" aria-modal="true" aria-label="没网了">
    <div class="off__scrim" @click="emit('close')"></div>

    <section class="off__box">
      <h2 class="hud-title off__head">没网了 · OFFLINE</h2>
      <p class="off__why">{{ reason }}</p>
      <p class="off__age">{{ ageLine }}</p>
      <button class="hud-btn hud-btn--primary" type="button" @click="emit('close')">知道了</button>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

/**
 * ``detail`` / ``secondsAgo`` come from the store, not from a fetch here.
 *
 * The probe is Python's and the poll is the store's; this card is a projection of one
 * reading at the moment it flipped. Asking again would be a second door that can answer
 * differently from the chip sitting next to it.
 */
const props = defineProps<{ open: boolean; detail: string; secondsAgo: number | null }>()
const emit = defineEmits<{ (e: 'close'): void }>()

/** 那句原因是服务端写的（「全部出口都不通」/「DNS 解析不了」…）。空着的时候不许留一格空白。 */
const reason = computed(() => props.detail || '探测没有给出原因')

/**
 * 这一跳多旧，和顶栏那颗 chip 上同一个数。
 *
 * ``null`` 单独说一句：把没有时间戳印成「距今 null 秒」是这一轮刚被批评过的那类假读数。
 */
const ageLine = computed(() =>
  props.secondsAgo === null ? '这条读数没有时间戳，界面不知道它是多久前的' : `这是 ${props.secondsAgo} 秒前的读数`,
)
</script>

<style scoped>
/*
 * z-index: 40 —— 这个窗口里每一张桌面弹窗都是这个数（助手 44，放大后的对话区 39），
 * 顺序由挂载点决定：App.vue 把它挂在所有弹窗的最后，所以它压得住前面那几张。
 */
.off {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
}

.off__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.off__box {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
  width: min(420px, 92vw);
  padding: 16px 20px 16px;
  border: 1px solid var(--hud-line);
  border-top: 1px solid var(--hud-red);
  border-radius: var(--hud-radius);
  background: linear-gradient(180deg, rgba(10, 24, 40, 0.96), rgba(4, 8, 14, 0.98));
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}

.off__head {
  color: var(--hud-red);
}

.off__why {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--hud-text);
  word-break: break-word;
}

.off__age {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--hud-dim);
}
</style>

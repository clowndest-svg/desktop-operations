<template>
  <article class="turn" :class="`turn--${turn.role}`">
    <div v-if="turn.role === 'assistant'" class="turn__avatar" aria-hidden="true">
      <span class="turn__orb" />
    </div>

    <div class="turn__col">
      <div class="bubble" :class="{ 'bubble--tappable': true }" @click="reveal">
        <template v-for="(block, index) in blocks" :key="index">
          <div v-if="block.kind === 'code'" class="code">
            <div class="code__bar">
              <span>{{ block.lang || '代码' }}</span>
              <button type="button" @click.stop="copy(block.body)">复制</button>
            </div>
            <pre><code>{{ block.body }}</code></pre>
          </div>
          <p v-else class="bubble__text" v-html="renderInline(block.body)" />
        </template>
        <span v-if="streaming" class="caret" aria-hidden="true" />
      </div>

      <div class="turn__foot">
        <time v-if="turn.at" class="turn__time">{{ clock }}</time>
        <button v-if="streaming" type="button" class="turn__skip" @click="reveal">
          显示全部
        </button>
        <span v-else-if="copied" class="turn__done">已复制</span>
        <div v-else class="acts">
          <button type="button" @click="copy(turn.text)">复制</button>
          <button v-if="turn.role === 'assistant'" type="button" @click="$emit('speak')">
            朗读
          </button>
          <button v-if="canRegenerate" type="button" @click="$emit('regenerate')">重来</button>
          <button type="button" class="acts__bad" @click="$emit('remove')">删除</button>
        </div>
      </div>
    </div>
  </article>
</template>

<script setup lang="ts">
/**
 * 一条消息。左边是她的，右边是我的，和所有对话应用一样 —— 这一条不需要创新。
 *
 * 两个细节是刻意做的：
 *  - **逐字上屏**（`streaming`）由外面驱动，这里只负责把光标画出来和"点一下直接看全"。
 *    长答案一个字一个字等三十秒是折磨，所以外面那层是按"总时长固定"来放字的，
 *    这里再给一个"显示全部"的出口。
 *  - **动作收在时间那一行**，默认就在，不做"长按才出现"。长按在 WebView 里会先触发
 *    系统的文本选择菜单（`user-select: none` 只能挡一半），用户按下去看到的是一片蓝色高亮，
 *    第一反应是"这 App 卡住了"。看得见的一排小按钮更土，但它每次都能按到。
 */
import { computed, ref } from 'vue'
import type { Turn } from './api'
import { renderInline, splitFences } from './rich'

const props = defineProps<{
  turn: Turn
  streaming?: boolean
  canRegenerate?: boolean
}>()

const emit = defineEmits<{
  speak: []
  regenerate: []
  remove: []
  /** 不等逐字上屏了，直接把这条显示全。 */
  skip: []
}>()

const copied = ref(false)
const blocks = computed(() => splitFences(props.turn.text))
const clock = computed(() =>
  props.turn.at
    ? new Date(props.turn.at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : '',
)

/** 点气泡 = 别等了，全给我。 */
function reveal(): void {
  if (props.streaming) emit('skip')
}

async function copy(text: string): Promise<void> {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // 没有剪贴板权限或老 WebView：退回选中 + execCommand 那条老路。
    const box = document.createElement('textarea')
    box.value = text
    box.setAttribute('readonly', '')
    box.style.position = 'fixed'
    box.style.top = '-1000px'
    box.style.opacity = '0'
    document.body.appendChild(box)
    box.select()
    try {
      document.execCommand('copy')
    } catch {
      // 复制不了也不该报错：屏幕上已经有原文，用户能自己选。
    }
    document.body.removeChild(box)
  }
  copied.value = true
  window.setTimeout(() => {
    copied.value = false
  }, 1400)
}
</script>

<style scoped>
.turn {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  max-width: 100%;
}

.turn--user {
  justify-content: flex-end;
}

.turn__avatar {
  flex: 0 0 auto;
  width: 28px;
  height: 28px;
  border-radius: var(--xy-pill);
  background: var(--xy-card-2);
  border: 1px solid var(--xy-line);
  display: grid;
  place-items: center;
  margin-top: 2px;
}

.turn__orb {
  width: 12px;
  height: 12px;
  border-radius: var(--xy-pill);
  background: var(--xy-accent);
  box-shadow: 0 0 10px var(--xy-accent-soft);
}

.turn__col {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  max-width: 82%;
}

.turn--user .turn__col {
  align-items: flex-end;
}

.bubble {
  padding: 10px 14px;
  border-radius: var(--xy-r-lg);
  font-size: 15px;
  line-height: 1.65;
  word-break: break-word;
  overflow-wrap: anywhere;
}

.bubble--tappable {
  cursor: default;
}

.turn--assistant .bubble {
  background: var(--xy-card);
  border: 1px solid var(--xy-line);
  border-top-left-radius: var(--xy-r-xs);
  color: var(--xy-text);
}

.turn--user .bubble {
  background: var(--xy-accent-soft);
  border: 1px solid var(--xy-accent-line);
  border-top-right-radius: var(--xy-r-xs);
  color: var(--xy-text);
}

.bubble__text {
  margin: 0;
}

.bubble__text + .bubble__text {
  margin-top: 8px;
}

.bubble__text :deep(strong) {
  font-weight: 600;
  color: var(--xy-text);
}

.bubble__text :deep(code) {
  padding: 1px 5px;
  border-radius: 6px;
  background: var(--xy-card-2);
  border: 1px solid var(--xy-line);
  font-family: var(--xy-mono);
  font-size: 13px;
}

/* 打字光标：一个呼吸的小竖条。它只是"还在往外蹦字"的意思，不是真流式的证据。 */
.caret {
  display: inline-block;
  width: 2px;
  height: 15px;
  margin-left: 2px;
  vertical-align: -2px;
  border-radius: 1px;
  background: var(--xy-accent);
  animation: xy-blink 1s steps(2, start) infinite;
}

@keyframes xy-blink {
  to {
    visibility: hidden;
  }
}

.code {
  margin: 8px 0;
  border-radius: var(--xy-r-sm);
  background: var(--xy-bg-soft);
  border: 1px solid var(--xy-line);
  overflow: hidden;
}

.code__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 5px 10px;
  border-bottom: 1px solid var(--xy-line);
  font-size: 11px;
  color: var(--xy-text-3);
}

.code__bar button {
  border: 0;
  background: none;
  color: var(--xy-text-2);
  font-size: 11px;
  padding: 2px 4px;
}

.code pre {
  margin: 0;
  padding: 10px;
  overflow-x: auto;
  font-family: var(--xy-mono);
  font-size: 12.5px;
  line-height: 1.55;
}

.turn__foot {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 18px;
  padding: 0 2px;
}

.turn--user .turn__foot {
  justify-content: flex-end;
}

.turn__time {
  font-size: 11px;
  color: var(--xy-text-3);
  font-variant-numeric: tabular-nums;
}

.turn__skip,
.turn__done {
  border: 0;
  background: none;
  padding: 0;
  font-size: 11px;
  color: var(--xy-text-3);
}

.turn__skip {
  color: var(--xy-accent);
}

.acts {
  display: flex;
  gap: 4px;
}

.acts button {
  border: 0;
  background: none;
  padding: 2px 6px;
  border-radius: var(--xy-r-xs);
  font-size: 11px;
  color: var(--xy-text-3);
}

.acts button:active {
  background: var(--xy-card-2);
  color: var(--xy-text);
}

.acts__bad {
  color: var(--xy-text-3);
}
</style>

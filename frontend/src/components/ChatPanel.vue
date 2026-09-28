<template>
  <section class="hud-panel chat">
    <header class="hud-title">
      对话 · CONSOLE
      <button class="hud-btn chat__clear" type="button" :disabled="busy || lines.length === 0" @click="clear">
        清空
      </button>
    </header>

    <div ref="listRef" class="chat__log">
      <p v-if="lines.length === 0" class="hud-label chat__empty">
        打一句话问小夜。文字问答只需要 API Key；语音唤醒要在上方点「启用语音」，加载约 1-2 分钟。
      </p>
      <div v-for="(line, index) in lines" :key="index" class="chat__turn" :class="`chat__turn--${line.role}`">
        <span class="chat__who">{{ line.role === 'user' ? '你' : '小夜' }}</span>
        <span class="chat__text">{{ line.text }}</span>
      </div>
      <div v-if="error" class="chat__error">{{ error }}</div>
    </div>

    <form class="chat__form" @submit.prevent="send">
      <input
        v-model="draft"
        class="chat__input"
        type="text"
        :disabled="busy"
        placeholder="问点什么…（Enter 发送）"
        maxlength="2000"
      />
      <button class="hud-btn" type="submit" :disabled="busy || draft.trim() === ''">
        {{ busy ? '思考中…' : '发送' }}
      </button>
    </form>
  </section>
</template>

<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { chatAsk } from '@/api/bridge'

interface Line {
  role: 'user' | 'assistant'
  text: string
}

const lines = ref<Line[]>([])
const draft = ref('')
const busy = ref(false)
const error = ref('')
const listRef = ref<HTMLElement | null>(null)

async function scrollToEnd(): Promise<void> {
  await nextTick()
  const element = listRef.value
  if (element) element.scrollTop = element.scrollHeight
}

async function send(): Promise<void> {
  const question = draft.value.trim()
  if (question === '' || busy.value) return
  lines.value = [...lines.value, { role: 'user', text: question }]
  draft.value = ''
  error.value = ''
  busy.value = true
  await scrollToEnd()
  try {
    const reply = await chatAsk(question)
    if (reply.error) {
      // Keep the question on screen: it is the only record of what was attempted.
      error.value = reply.error
      draft.value = question
      return
    }
    lines.value = [...lines.value, { role: 'assistant', text: reply.answer }]
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    draft.value = question
  } finally {
    busy.value = false
    await scrollToEnd()
  }
}

function clear(): void {
  lines.value = []
  error.value = ''
}
</script>

<style scoped>
.chat {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  min-height: 0;
}

.chat__clear {
  margin-left: auto;
  padding: 2px 10px;
  font-size: 11px;
}

.chat__log {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-right: 2px;
}

.chat__empty {
  line-height: 1.7;
}

.chat__turn {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  gap: 8px;
  font-size: 12px;
  line-height: 1.6;
}

.chat__who {
  color: var(--hud-dim);
  letter-spacing: 0.06em;
}

.chat__text {
  white-space: pre-wrap;
  word-break: break-word;
}

.chat__turn--assistant .chat__who {
  color: var(--hud-cyan);
}

.chat__turn--assistant .chat__text {
  color: var(--hud-text);
}

.chat__error {
  color: var(--hud-red);
  font-size: 12px;
}

.chat__form {
  display: flex;
  gap: 8px;
}

.chat__input {
  flex: 1;
  min-width: 0;
  padding: 6px 10px;
  background: rgba(6, 16, 28, 0.85);
  border: 1px solid var(--hud-line);
  color: var(--hud-text);
  font-size: 12px;
  font-family: inherit;
  outline: none;
}

.chat__input:focus {
  border-color: rgba(77, 216, 255, 0.55);
}

.chat__input:disabled {
  color: var(--hud-dim);
}
</style>

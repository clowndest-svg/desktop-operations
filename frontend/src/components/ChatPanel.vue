<template>
  <section class="hud-panel chat">
    <header class="hud-title">
      对话 · CONSOLE
      <button
        class="hud-btn chat__talk"
        type="button"
        :disabled="!canTalk"
        :title="talkTitle"
        @click="talk"
      >
        {{ listening ? '聆听中…' : '按一下说' }}
      </button>
      <button class="hud-btn chat__clear" type="button" :disabled="busy || lines.length === 0" @click="clear">
        清空
      </button>
    </header>

    <div ref="listRef" class="chat__log">
      <p v-if="lines.length === 0" class="hud-label chat__empty">
        打一句话、或点「按一下说」直接开口。文字问答只需要 API Key；
        语音要在上方点「启用语音」，加载约 1-2 分钟。
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

    <p v-if="notice" class="chat__notice">{{ notice }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { chatAsk } from '@/api/bridge'
import { useVoiceStore } from '@/stores/voice'

const voice = useVoiceStore()

const draft = ref('')
const busy = ref(false)
const localError = ref('')
const listRef = ref<HTMLElement | null>(null)

/**
 * The transcript is Python's, not this component's.
 *
 * Spoken turns arrive on the push channel, so a locally-owned list would show the
 * typed half of the conversation and drop the spoken half -- two histories in one
 * chat panel is a reader having to work out which one is real.
 */
const lines = computed(() => voice.history)
const error = computed(() => localError.value || voice.error)
const notice = computed(() => voice.notice)
const listening = computed(() => voice.turn === 'listening')
const canTalk = computed(() => voice.enabled && !busy.value)

const talkTitle = computed(() => {
  if (voice.enabled) return '跳过唤醒词，直接说这一句'
  return voice.label
})

async function scrollToEnd(): Promise<void> {
  await nextTick()
  const element = listRef.value
  if (element) element.scrollTop = element.scrollHeight
}

async function talk(): Promise<void> {
  await voice.talk()
  await scrollToEnd()
}

async function send(): Promise<void> {
  const question = draft.value.trim()
  if (question === '' || busy.value) return
  draft.value = ''
  localError.value = ''
  busy.value = true
  await scrollToEnd()
  try {
    const reply = await chatAsk(question)
    if (reply.error) {
      // Keep the question in the box: it is the only record of what was attempted.
      localError.value = reply.error
      draft.value = question
      return
    }
    // The turns come back through the state bridge; pull once so the panel does
    // not wait for a push that a quiet microphone will never send.
    await voice.pullSnapshot()
  } catch (err) {
    localError.value = err instanceof Error ? err.message : String(err)
    draft.value = question
  } finally {
    busy.value = false
    await scrollToEnd()
  }
}

async function clear(): Promise<void> {
  await voice.clearTranscript()
  localError.value = ''
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

.chat__talk {
  margin-left: auto;
  padding: 2px 10px;
  font-size: 11px;
}

.chat__clear {
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

.chat__notice {
  margin: 0;
  font-size: 11px;
  color: var(--hud-amber);
}
</style>

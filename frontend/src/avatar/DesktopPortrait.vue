<template><MoonPortrait hd :mood="mood" :label="label" :read-audio="readAudio" :character="portraitState.character" :gaze="portraitState.gaze" :emotion="emotion" /></template>
<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { portraitState, loadPortraitPreferences } from './portraitState'
import { replyEmotion } from './portraitEmotion'
import { characters } from './portraitCatalog'
import MoonPortrait from './MoonPortrait.vue'
import type { PortraitMood } from './portraitMotion'
import { levels, markTalking, talking } from '@/audio/channel'
import { useVoiceStore } from '@/stores/voice'
const voice = useVoiceStore()
onMounted(loadPortraitPreferences)
const emotion=computed(() => portraitState.emotions ? replyEmotion(voice.streaming?.text || (voice.history.at(-1)?.role==='assistant' ? voice.history.at(-1)?.text : '') || '') : 'neutral')
const mood = computed<PortraitMood>(() => talking.value ? 'speaking'
  : voice.turn === 'processing' ? 'thinking' : voice.turn === 'listening' ? 'listening'
  : voice.error ? 'error' : 'idle')
const label = computed(() => ({ idle: (characters.find(row=>row.id===portraitState.character)?.name || '小夜')+' · 与你相伴', speaking: '正在说话',
  thinking: '让我想一想', listening: '我在听', error: '语音暂不可用' })[mood.value])
const readAudio = () => {
  const signal = levels()
  markTalking(signal.talking)
  return signal
}
</script>

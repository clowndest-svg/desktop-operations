<template><MoonPortrait :mood="mood" :label="label" :read-audio="readAudio" /></template>
<script setup lang="ts">
import { computed } from 'vue'
import MoonPortrait from './MoonPortrait.vue'
import type { PortraitMood } from './portraitMotion'
import { levels, markTalking, talking } from '@/audio/channel'
import { useVoiceStore } from '@/stores/voice'
const voice = useVoiceStore()
const mood = computed<PortraitMood>(() => talking.value ? 'speaking'
  : voice.turn === 'processing' ? 'thinking' : voice.turn === 'listening' ? 'listening'
  : voice.error ? 'error' : 'idle')
const label = computed(() => ({ idle: '月色作伴 · 小夜', speaking: '正在说话',
  thinking: '让我想一想', listening: '我在听', error: '语音暂不可用' })[mood.value])
const readAudio = () => {
  const signal = levels()
  markTalking(signal.talking)
  return signal
}
</script>

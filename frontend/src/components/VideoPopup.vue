<template>
  <div v-if="open" class="vp" role="dialog" aria-modal="true" aria-label="本地视频">
    <div class="vp__scrim" @click="emit('close')"></div>
    <section class="vp__box">
      <header><h2 class="hud-title">本地视频 · AnimateDiff</h2><button class="hud-btn" @click="emit('close')">关闭</button></header>
      <p>用你熟悉的语言描述画面和动作，软件会整句翻成英文。翻译需要联网，视频在本机生成。</p>
      <label class="hud-label" for="video-prompt">视频内容</label>
      <textarea id="video-prompt" v-model="prompt" class="hud-field" rows="4" placeholder="一只戴着蓝色围巾的橘猫在雪地里跑步" :disabled="busy"></textarea>
      <div class="vp__options">
        <label>时长 <select v-model.number="frames" class="hud-field" :disabled="busy"><option :value="16">2 秒</option><option :value="24">3 秒</option><option :value="32">4 秒</option></select></label>
        <label>步数 <input v-model.number="steps" class="hud-field" type="number" min="12" max="30" :disabled="busy" /></label>
      </div>
      <p>256×256 · 8 帧/秒。适合这台 4GB 显卡的短动画；图片与视频依次生成。</p>
      <button class="hud-btn hud-btn--primary" :disabled="busy || !prompt.trim()" @click="generate">{{ busy ? `正在生成 · ${elapsed} 秒` : '生成视频' }}</button>
      <p v-if="busy">{{ state?.detail || '正在翻译并启动本地模型…' }} 关闭弹窗后任务仍继续。</p>
      <p v-if="error" class="vp__error" role="alert">{{ error }}</p>
      <template v-if="answer?.ok">
        <video v-if="videoUrl" :src="videoUrl" controls loop playsinline class="vp__video"></video>
        <p>原文：{{ answer.prompt_input }}</p><p>实际英文：{{ answer.prompt }}</p>
        <p>{{ answer.prompt_note }}</p><p>生成用了{{ answer.seconds }}秒。</p>
        <div class="vp__options"><button class="hud-btn" @click="openFile('open')">打开视频</button><button class="hud-btn" @click="openFile('folder')">所在文件夹</button></div>
        <p class="vp__path">{{ answer.path }}</p>
      </template>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { generateVideo, fetchVideoState, artifactVideo, openArtifact, type VideoAnswer, type VideoState } from '@/api/bridge'
const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()
const prompt = ref(''), frames = ref(16), steps = ref(16), busy = ref(false), error = ref('')
const answer = ref<VideoAnswer | null>(null), state = ref<VideoState | null>(null), videoUrl = ref(''), elapsed = ref(0)
let timer: ReturnType<typeof setInterval> | undefined
watch(() => props.open, async (open) => { if (open) { try { state.value = await fetchVideoState() } catch { /* bridge may still be starting */ } } })
async function generate() {
  busy.value = true; error.value = ''; elapsed.value = 0
  const started = Date.now()
  timer = setInterval(() => {
    elapsed.value = Math.floor((Date.now() - started) / 1000)
    fetchVideoState().then(s => { state.value = s }).catch(() => {})
  }, 1500)
  try {
    const result = await generateVideo(prompt.value, frames.value, steps.value)
    answer.value = result
    if (!result.ok) { error.value = result.error; return }
    const preview = await artifactVideo(result.path)
    videoUrl.value = preview.data_url
    if (!preview.ok) error.value = preview.error
  } catch (e) { error.value = String(e) }
  finally { busy.value = false; clearInterval(timer); timer = undefined }
}
async function openFile(action: 'open' | 'folder') {
  if (!answer.value?.path) return
  const result = await openArtifact(answer.value.path, action)
  if (!result.ok) error.value = result.error
}
onBeforeUnmount(() => clearInterval(timer))
</script>

<style scoped>
.vp { position: fixed; inset: 0; z-index: 1100; display: grid; place-items: center; }
.vp__scrim { position: absolute; inset: 0; background: #000a; }
.vp__box { position: relative; width: min(720px, 92vw); max-height: 88vh; overflow: auto; padding: 22px; border: 1px solid var(--hud-border, #285264); border-radius: 14px; background: var(--hud-panel, #171c23); color: var(--hud-text, #e7ebf3); }
header, .vp__options { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
header h2 { flex: 1; } textarea { display: block; width: 100%; box-sizing: border-box; margin: 8px 0 14px; resize: vertical; }
p { font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; } input { width: 64px; } .vp__video { display: block; width: min(100%, 400px); margin: 18px auto; border-radius: 8px; } .vp__error { color: #ff8b8b; } .vp__path { user-select: text; }
</style>

<template>
  <section class="workspace" role="dialog" aria-modal="true" aria-label="AI 工作台">
    <header><div><small>小夜 · 随身工作空间</small><h2>AI 工作台</h2></div><button type="button" aria-label="关闭工作台" @click="$emit('close')">关闭</button></header>
    <nav aria-label="工作台分类"><button v-for="item in tabs" :key="item.id" type="button" :class="{ active: tab === item.id }" @click="tab = item.id">{{ item.name }}</button></nav>
    <div class="body">
      <template v-if="tab === 'assistants'">
        <p class="hint">选一个助手，开启独立的新对话。使用你在设置里配置的模型。</p>
        <div class="presets"><button v-for="preset in PRESETS" :key="preset.id" type="button" :disabled="busy || connected" @click="$emit('assistant', preset.id)"><strong>{{ preset.name }}</strong><span>{{ preset.description }}</span><em>开始 →</em></button></div>
        <p v-if="connected" class="hint">当前为电脑遥控模式。断开电脑后可创建手机助手会话。</p>
        <label class="field"><span>当前会话的自定义指令（最多 2000 字）</span><textarea v-model="instruction" maxlength="2000" rows="5" placeholder="例如：回答简洁，先给结论；解释术语时附一个例子。" :disabled="connected || busy" /></label>
        <button type="button" :disabled="connected || busy" @click="$emit('instruction', instruction)">保存当前会话指令</button>
      </template>
      <template v-else-if="tab === 'saved'">
        <input v-model="query" type="search" aria-label="搜索收藏" placeholder="搜索收藏和原问题" />
        <p v-if="!filtered.length" class="empty">{{ query ? '没有找到匹配的收藏' : '回答旁点「收藏」，有用的内容就会留在这里。' }}</p>
        <article v-for="item in filtered" :key="item.id" class="saved"><small>{{ item.source }} · {{ new Date(item.at).toLocaleDateString() }}</small><h3>{{ item.question || '收藏的回答' }}</h3><p>{{ item.text }}</p><div class="row"><button type="button" :disabled="busy" @click="$emit('reuse', item.text)">放入输入框</button><button type="button" @click="$emit('export-answer', item)">导出</button><button type="button" @click="$emit('unsave', item.id)">取消收藏</button></div></article>
      </template>
      <ProductivityPanel v-else-if="['prompts', 'notes', 'preferences'].includes(tab)" :tab="tab" :workspace="workspace" :busy="busy" @reuse="$emit('reuse', $event)" @save-prompt="$emit('save-prompt', $event)" @remove-prompt="$emit('remove-prompt', $event)" @save-note="$emit('save-note', $event)" @remove-note="$emit('remove-note', $event)" @attach-note="$emit('attach-note', $event)" @export-note="$emit('export-note', $event)" @reading="$emit('reading', $event)" />
      <template v-else>
        <article class="saved"><h3>导出当前对话</h3><p>保存为 Markdown，包含提问和回答，方便继续编辑与分享。</p><button type="button" :disabled="busy" @click="$emit('export-chat')">导出 Markdown</button></article>
        <article class="saved"><h3>备份手机内容</h3><p>{{ workspace.sessions.length }} 段会话 · {{ workspace.saved.length }} 条收藏 · {{ workspace.notes.length.toLocaleString() }} 条笔记与待办 · {{ workspace.prompts.length }} 条自建提示词。备份包含私人聊天、草稿和自定义指令，请妥善保存；不含模型 Key、电脑配对令牌或电脑历史。</p><button type="button" :disabled="busy" @click="$emit('backup')">保存 JSON 备份</button></article>
        <article class="saved"><h3>恢复备份</h3><p>先检查文件再确认恢复。恢复后会替换手机会话和收藏，建议先保存当前备份。</p><label class="file">选择小夜 JSON 备份<input type="file" accept=".json,application/json" :disabled="busy || connected" @change="restore" /></label><p v-if="connected" class="hint">请先断开电脑，再恢复手机备份。</p></article>
        <article class="saved"><h3>资料问答</h3><p>在聊天输入框的「＋ → 读文件」选择 TXT、Markdown、CSV、JSON 或文本代码，每份最多 128 KB / 24000 字。发送后资料正文会保存在手机会话和备份中，并随相关历史交给当前模型；未发送时可直接移除。</p><button type="button" :disabled="busy" @click="$emit('document')">选择文本资料</button></article>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { PRESETS, type Workspace, type SavedAnswer } from './workspace'
import ProductivityPanel from './ProductivityPanel.vue'
import type { PromptTemplate, NotebookEntry, ReadingPreferences } from './productivity'
const props = defineProps<{ workspace: Workspace; busy: boolean; connected: boolean; currentInstruction: string }>()
const emit = defineEmits<{
  close: []; assistant: [id: string]; instruction: [text: string]; reuse: [text: string]; unsave: [id: string]
  'export-answer': [answer: SavedAnswer]; 'export-chat': []; backup: []; restore: [file: File]; document: []
  'save-prompt': [prompt: PromptTemplate]; 'remove-prompt': [id: string]; 'save-note': [note: NotebookEntry]; 'remove-note': [id: string]; 'attach-note': [note: NotebookEntry]; 'export-note': [note: NotebookEntry]; reading: [reading: ReadingPreferences]
}>()
const tabs = [{ id: 'assistants', name: '助手' }, { id: 'prompts', name: '提示词' }, { id: 'notes', name: '笔记待办' }, { id: 'saved', name: '收藏' }, { id: 'data', name: '资料备份' }, { id: 'preferences', name: '偏好统计' }]
const tab = ref('assistants'), query = ref(''), instruction = ref(props.currentInstruction)
watch(() => props.currentInstruction, value => { instruction.value = value })
const filtered = computed(() => props.workspace.saved.filter(s => `${s.text}\n${s.question}`.toLowerCase().includes(query.value.trim().toLowerCase())))
function restore(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) emit('restore', file)
  input.value = ''
}
</script>

<style scoped>
.workspace { position: absolute; inset: 0; z-index: 15; display: flex; flex-direction: column; background: var(--xy-bg); padding-top: env(safe-area-inset-top); color: var(--xy-text); }
header { display:flex; justify-content:space-between; align-items:center; padding:18px; gap:12px; } h2 { margin:4px 0 0; font-size:24px; } small,.hint { color:var(--xy-text-2); font-size:12px; line-height:1.7; }
button,.file { min-height:44px; border:1px solid var(--xy-line-2); border-radius:12px; padding:10px 14px; background:var(--xy-card); color:var(--xy-text); font:inherit; cursor:pointer; } button:disabled,.file:has(input:disabled) { opacity:.45; } button:focus-visible,.file:focus-within { outline:2px solid var(--xy-accent); }
nav { display:flex; flex-shrink:0; overflow-x:auto; gap:8px; padding:0 18px 14px; border-bottom:1px solid var(--xy-line); } nav button { flex:0 0 auto; padding:8px 12px; font-size:13px; } .active { background:var(--xy-accent-soft); color:var(--xy-accent); border-color:var(--xy-accent-line); }
.body { flex:1; overflow-y:auto; padding:16px 18px calc(24px + env(safe-area-inset-bottom)); } .presets { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin:16px 0 24px; } .presets button { text-align:left; min-height:140px; display:flex; flex-direction:column; gap:10px; } .presets strong { font-size:16px; } .presets span { font-size:12px; line-height:1.6; color:var(--xy-text-2); } em { font-style:normal; font-size:12px; color:var(--xy-accent); margin-top:auto; }
.field { display:flex; flex-direction:column; gap:10px; margin:18px 0 12px; font-size:13px; } textarea,input[type=search] { box-sizing:border-box; width:100%; border:1px solid var(--xy-line-2); border-radius:12px; background:var(--xy-card); padding:12px; color:var(--xy-text); font:inherit; } textarea { resize:vertical; }
.saved { border:1px solid var(--xy-line); padding:16px; border-radius:16px; background:var(--xy-card); margin-bottom:12px; overflow-wrap:anywhere; } h3 { margin:8px 0; font-size:15px; } .saved p { white-space:pre-wrap; font-size:13px; line-height:1.8; max-height:320px; overflow:auto; color:var(--xy-text-2); } .row { display:flex; flex-wrap:wrap; gap:8px; } .row button { font-size:12px; } .empty { color:var(--xy-text-2); padding:32px 0; line-height:1.8; } .file { display:inline-flex; align-items:center; position:relative; overflow:hidden; font-size:13px; } .file input { position:absolute; inset:0; opacity:0; width:100%; cursor:pointer; }
@media(max-width:340px) { .presets { grid-template-columns:1fr; } nav { gap:4px; } }
</style>

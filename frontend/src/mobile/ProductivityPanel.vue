<template>
  <div class="productivity">
    <template v-if="tab === 'prompts'">
      <p class="hint">选择模板、填写内容，检查后放入输入框。你决定何时发送。</p>
      <input v-model="query" type="search" aria-label="搜索提示词" placeholder="搜索提示词名称与内容" />
      <div class="filters"><button v-for="name in categories" :key="name" type="button" :aria-pressed="category === name" @click="category = name">{{ name }}</button></div>
      <button type="button" :disabled="busy" @click="editPrompt()">＋ 自建提示词</button>
      <form v-if="promptEditor" class="card" @submit.prevent="savePrompt">
        <label>名称<input v-model="promptDraft.title" required maxlength="80" /></label>
        <label>分类<input v-model="promptDraft.category" maxlength="40" placeholder="自建" /></label>
        <label>提示词正文<textarea v-model="promptDraft.body" required maxlength="8000" rows="6" placeholder="使用双花括号添加变量，例如：请总结 {{资料}}" /></label>
        <small>变量格式：双花括号包住字段名。保存后选择模板即可填写。</small>
        <div class="row"><button type="submit" :disabled="busy || !promptDraft.title.trim() || !promptDraft.body.trim()">保存提示词</button><button type="button" @click="promptEditor = false">取消编辑</button></div>
      </form>
      <article v-for="item in filteredPrompts" :key="item.id" class="card">
        <small>{{ item.category }} · {{ workspace.prompts.some(p => p.id === item.id) ? '自建' : '内置' }}</small><h3>{{ item.title }}</h3><p class="preview">{{ item.body }}</p>
        <div class="row"><button type="button" :disabled="busy" @click="selectPrompt(item)">使用模板</button><template v-if="workspace.prompts.some(p => p.id === item.id)"><button type="button" :disabled="busy" @click="editPrompt(item)">编辑</button><button type="button" :disabled="busy" @click="$emit('remove-prompt', item.id)">删除提示词</button></template></div>
      </article>
      <p v-if="!filteredPrompts.length" class="hint">没有匹配的提示词，试试其他分类或关键词。</p>
      <form v-if="selected" class="template-form" @submit.prevent="useTemplate">
        <div class="row"><h3>{{ selected.title }}</h3><button type="button" @click="selected = null">收起模板</button></div>
        <label v-for="field in fields" :key="field">{{ field }}<textarea v-model="values[field]" required rows="3" maxlength="24000" :aria-label="field" /></label>
        <p v-if="templateError" role="alert" class="error">{{ templateError }}</p>
        <button type="submit" :disabled="busy">填好，放入输入框</button>
      </form>
    </template>
    <template v-else-if="tab === 'notes'">
      <p class="hint">离线记录灵感和待办，也可以把笔记作为资料交给 AI。保存笔记不会上传内容。</p>
      <input v-model="query" type="search" aria-label="搜索笔记" placeholder="搜索笔记、标签与待办" />
      <div class="filters"><button v-for="filter in noteFilters" :key="filter" type="button" :aria-pressed="noteFilter === filter" @click="noteFilter = filter">{{ filter }}</button></div>
      <div class="row"><button type="button" :disabled="busy" @click="editNote()">＋ 新建笔记</button><button type="button" :disabled="busy" @click="editNote(undefined, 'task')">＋ 新建待办</button></div>
      <form v-if="noteEditor" class="card" @submit.prevent="saveNote">
        <label>标题<input v-model="noteDraft.title" required maxlength="80" /></label>
        <label>内容<textarea v-model="noteDraft.text" maxlength="24000" rows="7" /></label>
        <label>标签<input v-model="noteDraft.tags" maxlength="120" placeholder="例如：工作 学习 灵感" /></label>
        <div class="row"><button type="submit" :disabled="busy || !noteDraft.title.trim()">保存笔记</button><button type="button" @click="noteEditor = false">取消编辑</button></div>
      </form>
      <article v-for="item in filteredNotes" :key="item.id" class="card" :class="{ completed: item.done }">
        <small>{{ item.pinned ? '置顶 · ' : '' }}{{ item.kind === 'task' ? (item.done ? '已完成' : '待办') : '笔记' }} · {{ new Date(item.updated).toLocaleDateString() }}</small><h3>{{ item.title }}</h3><p class="note-text">{{ item.text }}</p><small>{{ item.tags }}</small>
        <div class="row"><button v-if="item.kind === 'task'" type="button" :disabled="busy" @click="$emit('save-note', { ...item, done: !item.done })">{{ item.done ? '重新打开' : '标记完成' }}</button><button type="button" :disabled="busy" @click="editNote(item)">编辑笔记</button><button type="button" :disabled="busy" @click="$emit('attach-note', item)">交给 AI 阅读</button><button type="button" :disabled="busy" @click="$emit('save-note', { ...item, pinned: !item.pinned })">{{ item.pinned ? '取消置顶' : '置顶' }}</button><button type="button" :disabled="busy" @click="$emit('export-note', item)">导出笔记</button><button type="button" :disabled="busy" @click="$emit('remove-note', item.id)">删除笔记</button></div>
      </article>
      <p v-if="!filteredNotes.length" class="hint">{{ query || noteFilter !== '全部' ? '没有匹配的记录。' : '还没有笔记，记下第一个想法吧。' }}</p>
    </template>
    <template v-else>
      <article class="card"><h3>阅读与对话</h3>
        <label>聊天字号<select :value="workspace.reading.fontSize" @change="changeReading('fontSize', ($event.target as HTMLSelectElement).value)"><option value="normal">标准</option><option value="large">大字</option></select></label>
        <label class="check"><input type="checkbox" :checked="workspace.reading.compact" @change="changeReading('compact', ($event.target as HTMLInputElement).checked)" />紧凑显示消息操作</label>
        <label>手机模型携带的历史消息<select :value="workspace.reading.context" @change="changeReading('context', Number(($event.target as HTMLSelectElement).value))"><option :value="10">最近 10 条 · 节省用量</option><option :value="30">最近 30 条 · 均衡</option><option :value="60">最近 60 条 · 较多上下文</option></select></label>
        <label class="check"><input type="checkbox" :checked="workspace.reading.tools" @change="changeReading('tools', ($event.target as HTMLInputElement).checked)" />允许 AI 请求手机工具</label><p class="hint">关闭后只聊天；打开后仍需确认有外部影响的动作。上述对话设置仅影响独立模式。图片不保留在聊天历史里。</p>
      </article>
      <article class="card"><h3>手机内容概览</h3><dl><dt>会话 / 归档</dt><dd>{{ workspace.sessions.length }} / {{ workspace.sessions.filter(s => s.archived).length }}</dd><dt>已保存消息</dt><dd>{{ stats.messages }}</dd><dt>消息字数</dt><dd>{{ stats.characters.toLocaleString() }}</dd><dt>收藏 / 自建提示词</dt><dd>{{ workspace.saved.length }} / {{ workspace.prompts.length }}</dd><dt>笔记 / 未完成待办</dt><dd>{{ workspace.notes.length }} / {{ workspace.notes.filter(n => n.kind === 'task' && !n.done).length }}</dd><dt>内容占用</dt><dd>{{ stats.kb }} KB / 8192 KB</dd></dl><p class="hint">字数与存储占用是本机内容统计，不代表模型 token 或计费量。</p></article>
    </template>
    <p v-if="feedback" class="feedback" role="status">{{ feedback }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import type { Workspace } from './workspace'
import { PROMPTS, promptFields, fillPrompt, type PromptTemplate, type NotebookEntry, type ReadingPreferences } from './productivity'
const props = defineProps<{ tab: string; workspace: Workspace; busy: boolean }>()
const emit = defineEmits<{
  reuse: [text: string]; 'save-prompt': [prompt: PromptTemplate]; 'remove-prompt': [id: string]
  'save-note': [note: NotebookEntry]; 'remove-note': [id: string]; 'attach-note': [note: NotebookEntry]; 'export-note': [note: NotebookEntry]; reading: [reading: ReadingPreferences]
}>()
const query = ref(''), category = ref('全部'), noteFilter = ref('全部'), feedback = ref('')
const noteFilters = ['全部', '笔记', '待办', '已完成']
const categories = computed(() => ['全部', ...new Set([...PROMPTS, ...props.workspace.prompts].map(p => p.category))])
const filteredPrompts = computed(() => [...props.workspace.prompts, ...PROMPTS].filter(p => (category.value === '全部' || p.category === category.value) && `${p.title}\n${p.body}`.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase())))
const selected = ref<PromptTemplate | null>(null), values = reactive<Record<string, string>>({}), templateError = ref('')
const fields = computed(() => promptFields(selected.value?.body || ''))
function selectPrompt(prompt: PromptTemplate): void { selected.value = prompt; for (const key of Object.keys(values)) delete values[key]; templateError.value = '' }
function useTemplate(): void { try { emit('reuse', fillPrompt(selected.value!.body, values)) } catch (error) { templateError.value = String((error as Error).message) } }
const promptEditor = ref(false), promptDraft = reactive<PromptTemplate>({ id: '', title: '', category: '', body: '' })
function editPrompt(prompt?: PromptTemplate): void { Object.assign(promptDraft, prompt || { id: '', title: '', category: '自建', body: '' }); promptEditor.value = true; feedback.value = '' }
function savePrompt(): void {
  if (!promptDraft.id && props.workspace.prompts.length >= 50) { feedback.value = '自建提示词最多 50 条，请先删除不需要的内容'; return }
  emit('save-prompt', { ...promptDraft, title: promptDraft.title.trim(), category: promptDraft.category.trim() || '自建' }); promptEditor.value = false
}
const noteEditor = ref(false), noteDraft = reactive<NotebookEntry>({ id: '', title: '', text: '', tags: '', kind: 'note', done: false, pinned: false, updated: 0 })
function editNote(note?: NotebookEntry, kind: NotebookEntry['kind'] = 'note'): void { Object.assign(noteDraft, note || { id: '', title: '', text: '', tags: '', kind, done: false, pinned: false, updated: 0 }); noteEditor.value = true; feedback.value = '' }
function saveNote(): void {
  if (!noteDraft.id && props.workspace.notes.length >= 100) { feedback.value = '笔记和待办最多 100 条，请先导出并删除不需要的内容'; return }
  emit('save-note', { ...noteDraft, title: noteDraft.title.trim() }); noteEditor.value = false
}
const filteredNotes = computed(() => props.workspace.notes.filter(n => `${n.title}\n${n.text}\n${n.tags}`.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase()) && (noteFilter.value === '全部' || (noteFilter.value === '笔记' ? n.kind === 'note' : n.kind === 'task' && (noteFilter.value === '已完成' ? n.done : !n.done)))).sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.updated - a.updated))
function changeReading(key: keyof ReadingPreferences, value: string | number | boolean): void { emit('reading', { ...props.workspace.reading, [key]: value }) }
const stats = computed(() => {
  const turns = props.workspace.sessions.flatMap(s => s.turns)
  return { messages: turns.length, characters: turns.reduce((sum, t) => sum + t.text.length, 0), kb: Math.ceil(new TextEncoder().encode(JSON.stringify(props.workspace)).length / 1024) }
})
watch(() => props.tab, () => { query.value = ''; selected.value = null; feedback.value = '' })
</script>

<style scoped>
.hint,small { color:var(--xy-text-2); font-size:12px; line-height:1.8; } button { min-height:44px; border:1px solid var(--xy-line-2); border-radius:12px; padding:10px 12px; background:var(--xy-card); color:var(--xy-text); font:inherit; font-size:13px; cursor:pointer; } button:disabled { opacity:.45; } button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible { outline:2px solid var(--xy-accent); outline-offset:2px; }
input:not([type=checkbox]),textarea,select { width:100%; box-sizing:border-box; min-height:44px; border:1px solid var(--xy-line-2); border-radius:12px; padding:12px; font:inherit; color:var(--xy-text); background:var(--xy-card); } textarea { resize:vertical; } label { display:flex; flex-direction:column; gap:8px; margin:14px 0; font-size:13px; } .check { flex-direction:row; align-items:center; min-height:44px; } .check input { width:20px; height:20px; accent-color:var(--xy-accent); }
.filters,.row { display:flex; flex-wrap:wrap; gap:8px; margin:12px 0; align-items:center; } .filters button[aria-pressed=true] { color:var(--xy-accent); border-color:var(--xy-accent); background:var(--xy-accent-soft); } .card { margin:14px 0; padding:16px; border:1px solid var(--xy-line); border-radius:16px; background:var(--xy-card); overflow-wrap:anywhere; } h3 { font-size:16px; margin:8px 0; } .preview,.note-text { white-space:pre-wrap; line-height:1.8; font-size:13px; color:var(--xy-text-2); max-height:240px; overflow:auto; } .completed h3 { text-decoration:line-through; color:var(--xy-text-2); }
.template-form { position:sticky; bottom:-1px; padding:16px; border:1px solid var(--xy-accent-line); border-radius:16px; background:var(--xy-bg); box-shadow:0 -6px 24px #0002; max-height:70vh; overflow:auto; } .template-form .row { justify-content:space-between; } .error,.feedback { color:var(--xy-accent); line-height:1.8; font-size:13px; } dl { display:grid; grid-template-columns:1fr 1fr; gap:12px; font-size:13px; } dt { color:var(--xy-text-2); } dd { margin:0; text-align:right; overflow-wrap:anywhere; }
</style>

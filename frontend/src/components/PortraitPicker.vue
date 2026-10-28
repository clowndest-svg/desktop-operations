<template>
  <div v-if="open" class="portraits" @keydown.esc.stop="close" @keydown.tab="trap">
    <div class="portraits__scrim" @click="close" />
    <section ref="dialog" class="portraits__dialog" role="dialog" aria-modal="true" aria-label="形象与声音" tabindex="-1">
      <header><div><h2>形象与声音</h2><p>选一个喜欢的陪伴，也可以为她搭配自己的音色。</p></div><button class="hud-btn" :disabled="busy" @click="close">完成</button></header>
      <div class="portraits__tools">
        <input v-model="query" class="portraits__search" aria-label="搜索形象" placeholder="搜索名字、长发、眼镜、休闲…" />
        <button class="hud-btn portraits__random" :disabled="busy || filtered.length < 2" @click="randomPick">随机换一位</button>
        <label class="portraits__filter"><input v-model="favoritesOnly" type="checkbox" />只看收藏（{{ portraitState.favorites.length }}）</label>
      </div>
      <div class="portraits__layout">
        <div class="portraits__grid" aria-label="20 个形象">
          <button v-for="person in filtered" :key="person.id" :data-character="person.id" :aria-pressed="portraitState.character===person.id" :disabled="busy" @click="select(person)">
            <img :src="portraitAsset(person.id, 'thumb')" alt="" loading="lazy" width="140" height="140" />
            <strong>{{ person.name }} <span v-if="portraitState.character===person.id">✓</span><span v-if="portraitState.favorites.includes(person.id)" class="portraits__star" aria-label="已收藏">★</span></strong><small>{{ person.description }}</small>
          </button>
          <p v-if="!filtered.length">{{ favoritesOnly && !portraitState.favorites.length ? '还没有收藏，先点右侧「收藏当前形象」。' : '没有找到匹配的形象。' }}</p>
        </div>
        <aside>
          <h3>{{ current.name }}的声音</h3>
          <p>切换形象会带上已保存的音色；首次使用采用推荐搭配。</p>
          <label>音色<select v-model="selectedVoice" :disabled="busy || !voices?.choices.length" @change="bindVoice"><option v-for="item in voices?.choices" :key="item.id" :value="item.id">{{ item.label }}</option></select></label>
          <p class="portraits__actual">当前生效：{{ voices?.choices.find(item=>item.id===voices?.current)?.label || voices?.current || '读取中' }}</p>
          <button class="hud-btn" :disabled="busy || !selectedVoice" @click="preview">{{ busy ? '请稍候…' : '试听声音' }}</button>
          <button class="portraits__link" :disabled="busy" @click="toggleFavorite">{{ portraitState.favorites.includes(portraitState.character) ? '★ 取消收藏' : '☆ 收藏当前形象' }}</button>
          <button class="portraits__link" :disabled="busy" @click="emit('advanced')">语速、音量与录制音色 ›</button>
          <label class="portraits__check"><input type="checkbox" :checked="portraitState.gaze" :disabled="busy" @change="toggle('gaze')" />眼神跟随鼠标</label>
          <label class="portraits__check"><input type="checkbox" :checked="portraitState.emotions" :disabled="busy" @change="toggle('emotions')" />随回答改变表情</label>
          <small>20 个独立写实形象。部分角色共用内置音色，音色可自由更换。在线音色需要联网；动态口型由实际播放音频驱动。</small>
          <p v-if="problem" class="portraits__problem" role="alert">{{ problem }}</p>
          <p v-else class="portraits__saved" role="status">{{ notice }}</p>
        </aside>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { fetchVoices, pickVoice, previewVoice, type VoiceList } from '@/api/bridge'
import { characters, portraitAsset, type Character } from '@/avatar/portraitCatalog'
import { portraitState, savePortraitPreferences, loadPortraitPreferences } from '@/avatar/portraitState'
import { toggleFavoriteId } from '@/avatar/portraitFavorites'
const props=defineProps<{open:boolean}>()
const emit=defineEmits<{close:[]; advanced:[]}>()
const query=ref(''), busy=ref(false), problem=ref(''), notice=ref(''), selectedVoice=ref('')
const favoritesOnly=ref(false)
const voices=ref<VoiceList>()
const dialog=ref<HTMLElement>()
let returnFocus: HTMLElement | null=null
const current=computed(()=>characters.find(row=>row.id===portraitState.character) ?? characters[0]!)
const filtered=computed(()=>characters.filter(row=>(row.name+row.description).includes(query.value.trim()) && (!favoritesOnly.value || portraitState.favorites.includes(row.id))))
watch(()=>props.open, async open=>{
  if(!open) return
  returnFocus=document.activeElement instanceof HTMLElement ? document.activeElement : null
  query.value=''; problem.value=''; notice.value=''; busy.value=true; favoritesOnly.value=false
  try { await loadPortraitPreferences(); voices.value=await fetchVoices(); selectedVoice.value=voices.value.current; problem.value=voices.value.error }
  catch(error){problem.value=String(error)} finally{busy.value=false; await nextTick(); dialog.value?.focus()}
})
function close():void { if(busy.value)return; emit('close'); void nextTick(()=>returnFocus?.focus()) }
function trap(event:KeyboardEvent):void {
  const items=[...dialog.value?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled)') ?? []]
  const index=items.indexOf(document.activeElement as HTMLElement)
  if(event.shiftKey && index<=0){event.preventDefault();items.at(-1)?.focus()}
  else if(!event.shiftKey && (index===items.length-1 || index<0)){event.preventDefault();items[0]?.focus()}
}
async function run(action:()=>Promise<void>):Promise<void>{
  if(busy.value)return
  busy.value=true;problem.value='';notice.value=''
  try{await action()}catch(error){problem.value=error instanceof Error?error.message:String(error)}finally{busy.value=false}
}
async function select(person:Character):Promise<void>{
  await run(async()=>{
    const before=voices.value?.current || ''
    const desired=portraitState.bindings[person.id] || person.voice
    const available=voices.value?.choices.some(row=>row.id===desired)
    let applied=before
    if(available){
      const answer=await pickVoice(desired); voices.value=answer; selectedVoice.value=answer.current
      if(answer.error || answer.current!==desired)throw new Error(answer.error || '音色未切换，请重试')
      applied=answer.current
    }
    try{await savePortraitPreferences({...portraitState, character:person.id,bindings:{...portraitState.bindings,...(applied?{[person.id]:applied}:{})}})}
    catch(error){
      if(before && applied!==before){voices.value=await pickVoice(before);selectedVoice.value=voices.value.current}
      throw error
    }
    if(!available)problem.value='当前语音引擎不提供推荐音色，已保留实际音色。可以在右侧重新搭配。'
    else notice.value='形象与声音已保存，下次打开继续使用。'
  })
}
async function bindVoice():Promise<void>{
  await run(async()=>{
    const answer=await pickVoice(selectedVoice.value);voices.value=answer
    if(answer.error || answer.current!==selectedVoice.value){selectedVoice.value=answer.current;throw new Error(answer.error || '音色未切换')}
    await savePortraitPreferences({...portraitState,bindings:{...portraitState.bindings,[portraitState.character]:answer.current}})
    notice.value='已为'+current.value.name+'记住这个声音。'
  })
}
async function preview():Promise<void>{await run(async()=>{const result=await previewVoice(selectedVoice.value);if(!result.ok)throw new Error(result.error || '试听未能播放');notice.value='正在试听当前声音。'})}
async function randomPick():Promise<void>{
  const pool=filtered.value.length>1?filtered.value:characters.filter(row=>row.id!==portraitState.character)
  if(!pool.length)return
  await select(pool[Math.floor(Math.random()*pool.length)]!)
}
async function toggleFavorite():Promise<void>{
  await run(async()=>{
    const next=toggleFavoriteId(portraitState.favorites, portraitState.character)
    await savePortraitPreferences({...portraitState, favorites: next})
    notice.value=next.includes(portraitState.character)?'已加入收藏。':'已取消收藏。'
  })
}
async function toggle(key:'gaze'|'emotions'):Promise<void>{await run(async()=>{await savePortraitPreferences({...portraitState,[key]:!portraitState[key]});notice.value='设置已保存。'})}
</script>

<style scoped>
.portraits{position:fixed;inset:0;z-index:70;display:grid;place-items:center;padding:24px}.portraits__scrim{position:absolute;inset:0;background:#08060bc9;backdrop-filter:blur(10px)}
.portraits__dialog{position:relative;width:min(1080px,100%);max-height:90vh;display:flex;flex-direction:column;box-sizing:border-box;padding:24px;background:#211c27;border:1px solid #bfaaca33;border-radius:22px;box-shadow:0 28px 100px #0008;overflow:hidden;outline:none}
header{display:flex;justify-content:space-between;gap:16px;align-items:center}h2{margin:0;font-size:22px}header p,aside p{color:#bcb0c4;font-size:12px;line-height:1.7}.portraits__search{border:1px solid #bcadca33;border-radius:10px;background:#17131d;color:#eee7f1;padding:12px;margin:12px 0 18px;font:inherit}
.portraits__layout{display:grid;grid-template-columns:minmax(0,1fr) 240px;gap:24px;min-height:0;overflow:auto}.portraits__grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;align-content:start;overflow:auto;max-height:62vh;padding:2px 8px 8px 2px}.portraits__grid button{padding:0 0 12px;overflow:hidden;display:flex;flex-direction:column;text-align:left;gap:5px;color:#eee7f1;border:1px solid #bdaacc22;border-radius:13px;background:#17141d;cursor:pointer;font:inherit}.portraits__grid button[aria-pressed=true]{border-color:#d4bdde;box-shadow:0 0 0 1px #d4bdde}.portraits__grid button:hover{background:#312936}.portraits__grid img{width:100%;height:auto;aspect-ratio:1;object-fit:cover;background:#2b2630}.portraits__grid strong,.portraits__grid small{padding:0 10px}.portraits__grid small{font-size:10px;color:#bcb0c4}.portraits__grid strong{font-size:13px}.portraits__grid strong span{float:right;color:#d4bdde}
aside{padding-right:4px}aside h3{font-size:17px;margin:0 0 12px}aside label{display:block;font-size:12px}aside select{display:block;width:100%;margin:10px 0;padding:9px;color:#eee7f1;background:#17131d;border:1px solid #bcadca44;border-radius:8px}aside>small{display:block;color:#a99db2;line-height:1.8;font-size:11px}.portraits__check{margin:18px 0}.portraits__check input{margin-right:8px;accent-color:#b4a0c8}.portraits__link{display:block;background:none;border:0;padding:14px 0;color:#c6b5d5;cursor:pointer;font:inherit;font-size:12px}.portraits__problem{color:#f0bdad!important}.portraits__saved{color:#b9caba!important}button:disabled{opacity:.55;cursor:wait}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #d4bdde;outline-offset:2px}
.portraits__grid{grid-auto-rows:max-content}.portraits__grid button{min-height:210px}.portraits__grid img{flex:none}.portraits__grid strong,.portraits__grid small{flex:none}
.portraits__tools{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:12px 0 18px}.portraits__tools .portraits__search{flex:1 1 260px;margin:0}.portraits__random{white-space:nowrap}.portraits__filter{display:flex;align-items:center;gap:6px;font-size:12px;color:#bcb0c4;white-space:nowrap}.portraits__filter input{accent-color:#b4a0c8}.portraits__star{float:none!important;margin-left:6px;color:#e3c778;font-size:12px}
@media(max-width:760px){.portraits{padding:10px}.portraits__dialog{padding:16px}.portraits__layout{grid-template-columns:1fr}.portraits__grid{grid-template-columns:repeat(3,minmax(0,1fr));max-height:42vh}.portraits__grid button{min-height:0}aside{padding:12px 2px}.portraits__dialog{max-height:96vh}}
</style>

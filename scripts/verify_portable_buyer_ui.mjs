/** Actual relocated buyer EXE: clean defaults, Chinese image/video and voice input. */
import fs from 'node:fs'
import path from 'node:path'
import assert from 'node:assert/strict'
import {createRequire} from 'node:module'
const {chromium}=createRequire(import.meta.url)('playwright')
const app=path.resolve(process.argv[2]), output=path.resolve(process.argv[3])
fs.mkdirSync(output,{recursive:true})
const browser=await chromium.connectOverCDP('http://127.0.0.1:9336')
const page=browser.contexts().flatMap(c=>c.pages()).find(p=>p.url().includes('127.0.0.1')&&!p.url().includes('mode=pet'))
assert.ok(page)
const report={ok:false,checks:[],microphone_opened:false}
const api=(method,...args)=>page.evaluate(async({method,args})=>window.pywebview.api[method](...args),{method,args})
const check=(name,value)=>{assert.ok(value,name);report.checks.push(name);console.log('PASS '+name)}
function save(){fs.writeFileSync(path.join(output,'portable-buyer.json'),JSON.stringify(report,null,2))}
try{
  await page.waitForFunction(()=>window.pywebview?.api&&document.querySelector('.app__portrait canvas')?.dataset.loadedCharacter)
  const metadata=JSON.parse(fs.readFileSync(path.join(app,'_internal/jarvis/buildinfo.json'),'utf8'))
  check('actual relocated buyer edition, without preview fixtures',metadata.edition==='buyer' && await page.locator('.app__mock').count()===0)
  const settings=await api('settings_get')
  check('no author cloud credentials inherited',settings.models.every(row=>!row.key_set))
  const state=await api('image_state')
  check('engine resolves beside the relocated EXE',state.vendored_engine && path.resolve(state.comfy_dir)===path.join(app,'本地引擎','ComfyUI'))
  await api('settings_apply',{local_model:'Qwen3-0.6B-Q8_0.gguf'})
  await api('chat_pick','local','qwen3-0.6b')
  await api('chat_tuning','off',5,false)
  await api('image_tune','local','512x512',12,1)
  const prompt='一只红色的小鸟站在树枝上，背景是绿色叶子。'
  console.log('Generating real PNG through buyer bridge (Chinese prompt)')
  const image=await api('image_generate',prompt,'512x512')
  delete image.preview
  report.image=image;save()
  check('Chinese prompt translated and actual bundled PNG generated',image.ok && image.engine==='local' && image.prompt_input===prompt && !/[\u3400-\u9fff]/.test(image.prompt) && fs.statSync(image.path).size>1000)
  fs.copyFileSync(image.path,path.join(output,'generated.png'))
  await api('video_set_engine','local')
  console.log('Generating real MP4 through buyer bridge (Chinese prompt)')
  const interval=setInterval(()=>{void api('video_state').then(s=>console.log('video phase: '+s.phase)).catch(()=>{})},15000)
  let clip
  try{clip=await api('video_generate','一只红色小鸟在树枝上轻轻活动，绿叶随微风摇动。',null,16,'256x256',8,2)}finally{clearInterval(interval)}
  report.video=clip;save()
  check('actual bundled MP4 generated with 16 frames',clip.ok && clip.engine==='local' && clip.frames===16 && fs.statSync(clip.path).size>1000)
  fs.copyFileSync(clip.path,path.join(output,'generated.mp4'))
  console.log('Loading bundled recognizer without opening microphone')
  await api('call_warmup')
  const payload=JSON.parse(fs.readFileSync('reports/portraits-r127/asr-input.json','utf8'))
  const spoken=await api('call_turn',payload.pcm,16000)
  report.voice={error:spoken.error,heard:spoken.heard,answer:spoken.answer,returned_audio_bytes:spoken.audio?.length||0}
  check('packaged recognizer hears recorded QA speech',Boolean(spoken.heard?.includes('你好')))
  const size=fs.readdirSync(path.join(app,'用户数据','models'),{recursive:true,withFileTypes:true}).filter(entry=>entry.isFile()).reduce((sum,entry)=>sum+fs.statSync(path.join(entry.parentPath,entry.name)).size,0)
  report.first_run_model_cache_bytes=size
  check('first run did not download recognizer weights',size<20*1024*1024)
  report.ok=true;save()
  await page.screenshot({path:path.join(output,'home.png')})
}catch(error){report.error=String(error);report.engine=await api('image_state').catch(()=>null);save();throw error}
finally{save();await browser.close()}

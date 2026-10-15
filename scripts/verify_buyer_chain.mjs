// 仅对隔离买家副本运行，创建测试会话、联网查询及知识笔记。
import fs from 'node:fs'
const port = process.argv[2] || '9230'
const out = process.argv[3] || 'D:/AI/XiaoYe/qa/buyer-chain.json'
const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
const page = pages.find(p => p.type === 'page' && !p.url.includes('mode=pet') && !p.url.startsWith('devtools:'))
if (!page) throw Error('HUD not found')
const ws = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((resolve,reject) => { ws.addEventListener('open',resolve); ws.addEventListener('error',reject) })
let id=0
const pending=new Map()
ws.addEventListener('message', e => {
  const v=JSON.parse(e.data), p=pending.get(v.id)
  if (p) { clearTimeout(p.timer);pending.delete(v.id);v.error?p.reject(Error(JSON.stringify(v.error))):p.resolve(v.result) }
})
function send(method,params) {
  return new Promise((resolve,reject)=>{
    const n=++id,timer=setTimeout(()=>{pending.delete(n);reject(Error(`timeout ${method}`))},30000)
    pending.set(n,{resolve,reject,timer});ws.send(JSON.stringify({id:n,method,params}))
  })
}
async function ev(body) {
  const r=await send('Runtime.evaluate',{expression:`(async()=>{${body}})()`,returnByValue:true,awaitPromise:true})
  if(r.exceptionDetails) throw Error(JSON.stringify(r.exceptionDetails))
  return r.result?.value
}
const api=(method,...args)=>ev(`return await window.pywebview.api.${method}(...${JSON.stringify(args)})`)
const delay=ms=>new Promise(r=>setTimeout(r,ms))
async function done(task) {
  if(!task?.task_id) throw Error(JSON.stringify(task))
  const until=Date.now()+180000
  while(Date.now()<until) {
    const tasks=await api('chat_tasks'),row=tasks.tasks.find(t=>t.task_id===task.task_id)
    if(row && ['done','failed','cancelled'].includes(row.state)) {
      if(row.state!=='done') throw Error(JSON.stringify(row))
      return row
    }
    await delay(600)
  }
  throw Error('chat timed out')
}
const report={ok:false,checks:[],page:page.url}
function check(name,value) {
  report.checks.push({name,pass:Boolean(value)});console.log(JSON.stringify(report.checks.at(-1)))
  if(!value) throw Error(name)
}
try {
  const until=Date.now()+40000
  while(!await ev('return Boolean(window.pywebview?.api)')) { if(Date.now()>until)throw Error('bridge missing');await delay(400) }
  const initialSettings=await api('settings_get')
  report.credentialFlags=initialSettings.models.map(m=>({name:m.name,key_set:m.key_set}))
  check('buyer did not inherit injected author API credentials',report.credentialFlags.every(m=>!m.key_set))
  report.localSettings=await api('settings_apply',{local_model:'Qwen3-0.6B-Q8_0.gguf'})
  check('bundled light model configured',Object.keys(report.localSettings.problems||{}).length===0)
  report.models=await api('chat_pick','local','qwen3-0.6b')
  report.tuning=await api('chat_tuning',false,5,true)
  check('local model selected',!report.models.error)
  if(process.argv.includes('--offline-only')) {
    const fresh=await api('chat_new')
    report.offlineTask=await done(await api('chat_send','不要联网，仅根据知识库，列出美国大学排名资料的出处和查询时间。',[],fresh.conversation))
    const rows=(await api('chat_messages',fresh.conversation)).messages
    report.offlineAnswer=rows.filter(r=>r.role==='assistant').at(-1)?.content || ''
    check('restarted app retrieves and quotes saved sources offline',report.offlineAnswer.includes('知识库检索原文') && report.offlineAnswer.includes('http') && !report.offlineTask.tools_used.includes('search_web'))
  } else {
    const fresh=await api('chat_new'),original=fresh.conversation
    await done(await api('chat_send','只回复：前文已记录。',[],original))
    const question='Please search the web: 美国大学排名和榜单年份'
    report.onlineTask=await done(await api('chat_send',question,[],original))
    check('packaged local chat used key-free web search',report.onlineTask.tools_used.includes('search_web'))
    const before=(await api('chat_messages',original)).messages
    report.onlineAnswer=before.filter(r=>r.role==='assistant').at(-1)?.content||''
    check('web answer contains actual source links',report.onlineAnswer.includes('http') && report.onlineAnswer.includes('联网'))
    let hits
    for(let i=0;i<30;i++) { hits=await api('knowledge_probe','美国大学排名');if(hits.hits?.length)break;await delay(500) }
    report.knowledge=await api('knowledge_state');report.hits=hits
    check('background knowledge persisted and can be retrieved',report.knowledge.documents.length>0 && hits.hits?.length>0)
    const target=before.find(r=>r.role==='user' && r.content===question)
    const edit='只回复：修改提问已生效。'
    report.branchStart=await api('chat_edit_question',original,target.id,edit,[])
    await done(report.branchStart)
    const branch=report.branchStart.conversation
    const after=(await api('chat_messages',original)).messages
    const branched=(await api('chat_messages',branch)).messages
    report.branchMessages=branched
    check('edit creates a separate conversation',branch && branch!==original)
    check('original transcript is unchanged',JSON.stringify(before)===JSON.stringify(after))
    check('branch retains the earlier exchange',branched[0].content===before[0].content && branched[1].content===before[1].content)
    check('old question and old future answer are excluded',branched.length===4 && branched[2].content===edit && !branched.some(r=>r.content===question))
  }
  const shot=await send('Page.captureScreenshot',{format:'png'})
  fs.writeFileSync(out.replace(/\.json$/,'.png'),Buffer.from(shot.data,'base64'))
  report.ok=report.checks.every(c=>c.pass)
} catch(e) { report.error=String(e);console.error(report.error);process.exitCode=1 }
finally { fs.writeFileSync(out,JSON.stringify(report,null,2));ws.close() }

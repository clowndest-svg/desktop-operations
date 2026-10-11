/** Behaviour tests for the actual TypeScript request/agent modules, with network/device ports replaced. */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from '../frontend/node_modules/typescript/lib/typescript.js'
import { requestHistory, CONTEXT_MESSAGES } from '../frontend/src/mobile/conversation.ts'
import { checkModel, modelHeaders, modelProbeResult } from '../frontend/src/mobile/settings.ts'
import { readMigrated } from '../frontend/src/mobile/secure-migration.ts'

let passed = 0
async function test(name, work) { await work(); passed++; console.log(`[pass] ${name}`) }
await test('current question occurs once, empty streaming placeholder is excluded', () => {
  assert.deepEqual(requestHistory([{role:'user',text:'hello'}, {role:'assistant',text:''}], 'hello'), [{role:'user',text:'hello'}])
})
await test('photo caption replaces the display marker rather than adding another user message', () => {
  assert.deepEqual(requestHistory([{role:'user',text:'question 📷'}], 'question'), [{role:'user',text:'question'}])
})
await test('regenerate retains the previous exchanges but not a duplicated question', () => {
  assert.deepEqual(requestHistory([{role:'user',text:'one'}, {role:'assistant',text:'two'}, {role:'user',text:'three'}], 'three').map(x=>x.text), ['one','two','three'])
})
await test('long history is bounded and begins with a user', () => {
  const history=Array.from({length:200}, (_,i)=>({role:i%2?'assistant':'user',text:String(i)}))
  const result=requestHistory(history,'next')
  assert(result.length<=CONTEXT_MESSAGES); assert.equal(result[0].role,'user'); assert.equal(result.at(-1).text,'next')
})
await test('no Authorization for empty or whitespace keys', () => {
  assert.deepEqual(modelHeaders('  '), {'Content-Type':'application/json'})
  assert.equal(modelHeaders(' token ').Authorization, 'Bearer token')
})
await test('mobile input agrees with native HTTPS-only policy', () => {
  assert(checkModel({baseUrl:'http://127.0.0.1/v1',model:'local',pin:''}).some(x=>x.field==='baseUrl'))
  assert(checkModel({baseUrl:'https://',model:'local',pin:''}).some(x=>x.field==='baseUrl'))
  assert(checkModel({baseUrl:'https://user:pass@example.com/v1',model:'local',pin:''}).some(x=>x.field==='baseUrl'))
})
await test('HTTP 200 HTML and malformed JSON are not successful model probes', () => {
  for(const body of ['<html>login</html>', '{}', '{"error":"bad"}', '{"choices":[]}']) assert.equal(modelProbeResult(200,body).ok,false)
  assert.equal(modelProbeResult(200,JSON.stringify({choices:[{message:{role:'assistant',content:'ok'}}]})).ok,true)
})
function store(values={}) { const data=new Map(Object.entries(values)); return {data, async get({key}){return {value:data.get(key)??null}}, async set({key,value}){data.set(key,value)}, async remove({key}){data.delete(key)}} }
await test('migration writes securely before removing the legacy secret', async () => {
  const old=store({key:'secret'}), secure=store(); assert.equal(await readMigrated('key',secure,old),'secret'); assert.equal(old.data.has('key'),false); assert.equal(secure.data.get('key'),'secret')
})
await test('failed secure writes preserve the old configuration', async () => {
  const old=store({key:'secret'}), secure=store(); secure.set=async()=>{throw new Error('keystore unavailable')}
  await assert.rejects(()=>readMigrated('key',secure,old)); assert.equal(old.data.get('key'),'secret')
})
await test('an interrupted migration never overwrites a newer secure value', async () => {
  const old=store({key:'old'}), secure=store({key:'new'}); assert.equal(await readMigrated('key',secure,old),'new'); assert.equal(old.data.has('key'),false)
})

const root=new URL('../frontend/src/mobile/',import.meta.url)
async function moduleWithPorts(name, ports, globals={}) {
  const context=vm.createContext({console,URL,setTimeout,clearTimeout,AbortController,TextDecoder,...globals})
  const cache=new Map()
  async function load(file) {
    if(cache.has(file)) return cache.get(file)
    if(ports[file]) {
      const values=ports[file], m=new vm.SyntheticModule(Object.keys(values), function(){for(const [k,v] of Object.entries(values))this.setExport(k,v)}, {context})
      cache.set(file,m); return m
    }
    const text=fs.readFileSync(new URL(file+'.ts',root),'utf8')
    const js=ts.transpileModule(text,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText
    const m=new vm.SourceTextModule(js,{context,identifier:file});cache.set(file,m)
    await m.link(specifier=>load(specifier.startsWith('./')?specifier.slice(2):specifier));return m
  }
  const m=await load(name);await m.evaluate();return m.namespace
}

let captured, response={status:200,body:JSON.stringify({choices:[{message:{content:'ok'}}]}),error:''}, native=false, listener, streaming
const net={
  async request(request){captured=request;return response},
  async stream(request){captured=request;return streaming()},
  async cancelStream(){return {ok:true}},
  async addListener(_event,fn){listener=fn;return {remove:async()=>{listener=null}}},
}
const api=await moduleWithPorts('api', {
  '@capacitor/core':{Capacitor:{isNativePlatform:()=>native},registerPlugin:()=>net},
  '@capacitor/preferences':{Preferences:store()},
  secure:{readSecret:async()=>null,writeSecret:async()=>{}},
})
const cfg={baseUrl:'https://example.test/v1',apiKey:'',model:'local',pin:''}
await test('rawChat really sends a key-free request, not just a passing settings probe', async()=>{
  api.beginChat();assert.equal((await api.rawChat([{role:'user',content:'hello'}],cfg)).content,'ok');assert.equal(captured.headers.Authorization,undefined)
})
await test('cancel during a stream before HTTP status returns retains partial text', async()=>{
  native=true;api.beginChat();streaming=async()=>{listener({kind:'chunk',text:'data: {"choices":[{"delta":{"content":"part"}}]}\n\n'.replaceAll('\\n','\n')});await api.cancelChat();return {status:0,body:'',error:''}}
  const result=await api.rawChat([{role:'user',content:'hello'}],cfg,[],{onDelta:()=>{}});assert.equal(result.stopped,true);assert.equal(result.content,'part');assert.equal(result.toolCalls.length,0)
})
await test('a cancelled question never starts a later model request', async()=>{
  const previous=captured;assert.equal((await api.rawChat([{role:'user',content:'again'}],cfg)).stopped,true);assert.equal(captured,previous)
})
let stopped=false, ran=[], asked=0
const agent=await moduleWithPorts('agent', {
 api:{beginChat:()=>{stopped=false},chatWasCancelled:()=>stopped,rawChat:async()=>{asked++;return {content:'',toolCalls:[{id:'1',name:'first',arguments:'{}'},{id:'2',name:'second',arguments:'{}'}],stopped:false}}},
 actions:{findAction:()=>({leavesDevice:false}),toolSchemas:()=>[{}],execute:async(name,_args,approve)=>{if(!await approve('confirm'))return {ok:false,text:'cancelled'};ran.push(name);stopped=true;return {ok:true,text:'done'}}},
})
await test('cancellation after one tool prevents the second tool and the next round', async()=>{
  await agent.askAgent([{role:'user',text:'test'}],cfg,[],async()=>true,{onAction:()=>{}});assert.deepEqual(ran,['first']);assert.equal(asked,1)
})
await test('a pending human confirmation cannot outlive cancellation', async()=>{
  ran=[];asked=0;await agent.askAgent([{role:'user',text:'test'}],cfg,[],async()=>{stopped=true;return true},{onAction:()=>{}});assert.deepEqual(ran,[]);assert.equal(asked,1)
})
console.log(`${passed} behavioural checks passed`)

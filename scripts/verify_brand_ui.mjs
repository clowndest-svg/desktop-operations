/** Real Chromium acceptance of branding, immersive layout and audio-driven mouth. */
import fs from 'node:fs'
import path from 'node:path'
import http from 'node:http'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

// Resolve Playwright from the environment (NODE_PATH may point at Codex's runtime).
// 这台机器上只有 qa-tools 里那份 playwright-core；launch 时显式给浏览器路径就够。
const { chromium } = (() => {
  try { return createRequire(import.meta.url)('playwright') }
  catch { return createRequire(import.meta.url)('playwright-core') }
})()
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = path.resolve(process.argv[2] || path.join(root, 'reports/brand-r124'))
fs.mkdirSync(output, { recursive: true })
const checks = []
const errors = []
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost')
  const mobile = url.pathname.startsWith('/mobile/')
  const base = path.join(root, mobile ? 'frontend/dist-mobile' : 'jarvis/ui/web')
  const relative = mobile ? url.pathname.slice(7) : url.pathname
  const file = path.resolve(base, '.' + (relative === '/' ? '/index.html' : relative))
  if (!file.startsWith(base + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return res.writeHead(404).end()
  res.setHeader('Content-Type', ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.webp': 'image/webp', '.png': 'image/png' })[path.extname(file)] || 'application/octet-stream')
  fs.createReadStream(file).pipe(res)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const url = `http://127.0.0.1:${server.address().port}`
const browser = await chromium.launch({ executablePath: process.env.XIAOYE_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--autoplay-policy=no-user-gesture-required'] })
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(url + '?nosplash=1')
  const face = page.locator('.app__portrait canvas')
  await page.waitForFunction(() => document.querySelector('.app__portrait canvas')?.dataset.mouth !== undefined)
  assert.equal(await page.locator('html').getAttribute('data-skin'), 'moon')
  assert.ok(await page.locator('.bar__mark').evaluate(image => image.complete && image.naturalWidth > 0))
  assert.equal(await face.getAttribute('data-mouth'), '0.000')
  checks.push('new logo loaded, moon theme and silent closed mouth')
  await page.screenshot({ path: path.join(output, 'desktop.png') })
  await page.getByRole('button', { name: '形象与声音', exact: true }).click()
  const gallery=page.getByRole('dialog',{name:'形象与声音',exact:true})
  await gallery.getByRole('button',{name:'完成',exact:true}).waitFor()
  await page.waitForFunction(()=>document.querySelectorAll('.portraits__grid button:not(:disabled)').length===20)
  assert.equal(await gallery.locator('.portraits__grid button').count(),20)
  await page.screenshot({path:path.join(output,'gallery.png')})
  for(let i=1;i<=20;i++){
    const id=`p${String(i).padStart(2,'0')}`
    await gallery.locator(`button[data-character="${id}"]`).click()
    await page.waitForFunction(id=>document.querySelector('.app__portrait canvas')?.dataset.loadedCharacter===id,id)
    await page.waitForFunction(()=>document.querySelectorAll('.portraits__grid button:not(:disabled)').length===20)
    if(process.argv.includes('--stress-portraits')){
      await gallery.getByRole('button',{name:'完成',exact:true}).click()
      await page.evaluate(()=>{
        window.__jarvisAudio({flush:true})
        const pcm=new Int16Array(24000)
        for(let j=0;j<pcm.length;j++)pcm[j]=Math.sin(j/24000*Math.PI*2*220)*10000
        const bytes=new Uint8Array(pcm.buffer)
        let raw='';for(const byte of bytes)raw+=String.fromCharCode(byte)
        window.__jarvisAudio({seq:0,sample_rate:24000,channels:1,format:'pcm_s16le',final:true,pcm:btoa(raw)})
      })
      await page.waitForFunction(()=>Number(document.querySelector('.app__portrait canvas')?.dataset.mouth)>.8)
      await page.locator('.app__portrait').screenshot({path:path.join(output,`mouth-max-${id}.png`)})
      await page.evaluate(()=>window.__jarvisAudio({flush:true}))
      await page.waitForFunction(()=>document.querySelector('.app__portrait canvas')?.dataset.mouth==='0.000')
      await page.getByRole('button',{name:'形象与声音',exact:true}).click()
      await page.waitForFunction(()=>document.querySelectorAll('.portraits__grid button:not(:disabled)').length===20)
    }
  }
  await gallery.locator('button[data-character="p01"]').click()
  await page.waitForFunction(()=>document.querySelector('.app__portrait canvas')?.dataset.loadedCharacter==='p01')
  await gallery.getByRole('button',{name:'完成',exact:true}).click()
  checks.push('all 20 independent character assets load and select without console errors')
  await page.mouse.move(20,150)
  await page.waitForFunction(()=>Number(document.querySelector('.app__portrait canvas')?.dataset.gazeX)<-.3)
  await page.mouse.move(1420,650)
  await page.waitForFunction(()=>Number(document.querySelector('.app__portrait canvas')?.dataset.gazeX)>.15)
  checks.push('gaze follows mouse in both directions')
  for(const [text,emotion] of [['太好了，恭喜你！😊','happy'],['哼，我生气了，气鼓鼓。','pout'],['很抱歉听到你伤心的消息。','soft'],['别生气，我们一起解决。','neutral']]){
    await page.evaluate(text=>window.__jarvisState({voice_state:'idle',voice:'off',voice_detail:'',history:[{role:'assistant',text}],last_event:'',interrupted:false}),text)
    await page.waitForFunction(emotion=>document.querySelector('.app__portrait')?.dataset.emotion===emotion,emotion)
    await page.waitForTimeout(550)
    await page.screenshot({path:path.join(output,`expression-${emotion}.png`)})
  }
  checks.push('assistant reply drives happy, pout, soft and negated neutral expressions')
  assert.ok(await page.locator('.bar__actions > button').count() <= 3)
  assert.equal(await page.locator('.chat__options').isVisible(), false)
  await page.getByRole('button', { name: '功能', exact: true }).click()
  await page.getByRole('textbox', { name: '搜索功能' }).fill('语音')
  await page.getByRole('button', { name: '语音控制', exact: false }).click()
  await page.getByRole('button', { name: '试听', exact: true }).click()
  await page.waitForFunction(() => Number(document.querySelector('.app__portrait canvas')?.dataset.mouth) > 0.1)
  assert.ok(await page.evaluate(() => window.__jarvisAudioDebug().bytes_received > 0))
  await page.screenshot({ path: path.join(output, 'speaking.png') })
  await page.getByRole('button', { name: '停下', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.app__portrait canvas')?.dataset.mouth === '0.000')
  checks.push('actual PCM analyser opens mouth; stop closes it')
  await page.getByRole('button', { name: '功能', exact: true }).click()
  await page.getByRole('textbox', { name: '搜索功能' }).fill('系统')
  await page.getByRole('button', { name: '系统与工具', exact: false }).click()
  await page.locator('.app__diagnostics canvas').first().waitFor()
  await page.screenshot({ path: path.join(output, 'systems.png') })
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('.app__diagnostics').count(), 0)
  checks.push('system tools expand and close with Escape')
  await page.getByRole('button', { name: '对话选项', exact: true }).click()
  await page.getByRole('button', { name: '放大', exact: true }).click()
  const expanded = await page.locator('.chat--max').boundingBox()
  assert.ok(expanded.width > 1300)
  await page.screenshot({ path: path.join(output, 'expanded-chat.png') })
  await page.locator('.chat__close').click()
  checks.push('expanded chat still covers viewport and closes')
  await page.setViewportSize({ width: 900, height: 650 })
  await page.screenshot({ path: path.join(output, 'desktop-small.png') })
  const input = await page.locator('.chat__input').boundingBox()
  assert.ok(input.y >= 0 && input.y + input.height <= 650)
  checks.push('minimum desktop viewport keeps input reachable')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.getByRole('button', { name: '功能', exact: true }).click()
  await page.getByRole('button', { name: '语音控制', exact: false }).click()
  await page.getByRole('button', { name: '试听', exact: true }).click()
  await page.waitForFunction(() => Number(document.querySelector('.app__portrait canvas')?.dataset.mouth) > 0.1)
  checks.push('reduced motion preserves audio mouth')
  await page.getByRole('button', { name: '功能', exact: true }).click()
  await page.getByRole('textbox', { name: '搜索功能' }).fill('画图')
  await page.getByRole('button', { name: '画图', exact: false }).click()
  assert.ok(await page.locator('[role="dialog"]').first().isVisible())
  checks.push('searchable consolidated menu opens image creation')
  await page.locator('[role="dialog"]').getByRole('button', { name: '关闭', exact: true }).click()
  const destinations = [
    ['做视频', '视频生成'], ['提醒与助手', '助手能力'], ['连接手机', '手机接入'],
    ['手机投屏', '手机投屏'], ['电脑操作权限', '电脑控制权限'], ['外观', '皮肤'],
    ['扩展', '扩展'], ['操作记录', '最近动作'], ['提醒与告警', '告警'],
  ]
  for (const [label, dialogName] of destinations) {
    await page.getByRole('button', { name: '功能', exact: true }).click()
    await page.getByRole('textbox', { name: '搜索功能' }).fill(label)
    await page.locator('.feature-menu__group button').first().click()
    const dialog = page.getByRole('dialog', { name: dialogName, exact: true })
    await dialog.waitFor()
    await dialog.getByRole('button', { name: '关闭', exact: true }).first().click()
    await dialog.waitFor({ state: 'hidden' })
  }
  checks.push('all consolidated feature destinations open and close')
  const realAudio = path.join(root, 'build/probe-audio.json')
  if (fs.existsSync(realAudio)) {
    await page.reload()
    await page.waitForFunction(() => document.querySelector('.app__portrait canvas')?.dataset.mouth !== undefined)
    await page.locator('.chat__input').click()
    const recording = JSON.parse(fs.readFileSync(realAudio, 'utf8'))
    await page.evaluate(messages => { for (const message of messages) window.__jarvisAudio(message) }, recording.messages)
    await page.waitForFunction(() => Number(document.querySelector('.app__portrait canvas')?.dataset.mouth) > 0.1)
    const debug = await page.evaluate(() => window.__jarvisAudioDebug())
    assert.ok(debug.bytes_received >= 400000)
    await page.screenshot({ path: path.join(output, 'real-tts-speaking.png') })
    await page.evaluate(() => window.__jarvisAudio({ flush: true }))
    await page.waitForFunction(() => document.querySelector('.app__portrait canvas')?.dataset.mouth === '0.000')
    checks.push('real Edge-TTS speech drives mouth; bridge interruption closes it')
  }
  const phone = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })
  phone.on('pageerror', error => errors.push(error.message))
  await phone.goto(url + '/mobile/')
  await phone.waitForFunction(() => document.querySelector('.mobile-portrait-backdrop canvas')?.dataset.mouth !== undefined)
  assert.ok(await phone.locator('.top__logo').evaluate(image => image.complete && image.naturalWidth > 0))
  await phone.screenshot({ path: path.join(output, 'mobile.png') })
  await phone.getByRole('button', { name: '会话', exact: true }).click()
  await phone.screenshot({ path: path.join(output, 'mobile-drawer.png') })
  checks.push('mobile logo, portrait and session drawer render')
  let mobileVoice='zh-CN-XiaoxiaoNeural'
  const voiceIds=['zh-CN-XiaoxiaoNeural','zh-CN-XiaoyiNeural','zh-TW-HsiaoChenNeural','zh-TW-HsiaoYuNeural']
  await phone.route('https://pc.example.test:8737/**',async route=>{
    const method=new URL(route.request().url()).pathname.split('/').at(-1)
    if(method==='tts_pick')mobileVoice=route.request().postDataJSON().voice
    const data=method==='tts_voices' || method==='tts_pick'
      ? {error:'',engine:'edge_tts',current:mobileVoice,choices:voiceIds.map(id=>({id,label:id,current:id===mobileVoice,kind:'builtin'})),speed:1,volume:1,speed_min:.5,speed_max:1.5,volume_min:0,volume_max:1,cloning:{available:false,reason:'QA 不录音',min_ms:3000,comfortable_ms:10000,max_ms:30000,max_voices:20}}
      : method==='voice_clone_list' ? {error:'',voices:[],cloning:{available:false,reason:'QA 不录音',min_ms:3000,comfortable_ms:10000,max_ms:30000,max_voices:20}}
      : method==='chat_sessions' ? {sessions:[],current:''} : method==='chat_messages' ? {messages:[]} : {}
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)})
  })
  await phone.evaluate(()=>localStorage.setItem('CapacitorStorage.xiaoye.pc',JSON.stringify({host:'pc.example.test',port:8737,pin:'sha256:'+'a'.repeat(64),token:'qa-only',name:'QA电脑',methods:[]})))
  await phone.reload()
  await phone.getByRole('button',{name:'更多',exact:true}).click()
  await phone.getByRole('button',{name:'形象与声音',exact:true}).click()
  assert.equal(await phone.locator('.vn__portraits button').count(),20)
  await phone.locator('.vn__portraits button').nth(18).click()
  await phone.waitForFunction(()=>JSON.parse(localStorage.getItem('CapacitorStorage.xiaoye.portrait.v1') || '{}').character==='p19')
  assert.equal(mobileVoice,'zh-TW-HsiaoChenNeural')
  await phone.screenshot({path:path.join(output,'mobile-gallery.png')})
  await phone.locator('.vn__x').click()
  await phone.reload()
  await phone.waitForFunction(()=>document.querySelector('.mobile-portrait-backdrop canvas')?.dataset.loadedCharacter==='p19')
  checks.push('mobile 20-character gallery applies paired voice RPC and restores local character after reload (QA transport)')
  assert.deepEqual(errors, [])
} finally {
  await browser.close()
  server.close()
  fs.writeFileSync(path.join(output, 'ui.json'), JSON.stringify({ checks, errors }, null, 2))
  console.log(JSON.stringify({ checks, errors }))
}

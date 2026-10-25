/** Actual packaged WebView2: persistence, voice binding and refusal handling. */
import fs from 'node:fs'
import path from 'node:path'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
const {chromium}=createRequire(import.meta.url)('playwright')
const mode=process.argv[2] || 'initial', output=path.resolve(process.argv[3] || 'reports/portraits-r125/frozen')
fs.mkdirSync(output,{recursive:true})
const browser=await chromium.connectOverCDP('http://127.0.0.1:9336')
const page=browser.contexts().flatMap(c=>c.pages()).find(p=>p.url().includes('127.0.0.1') && !p.url().includes('mode=pet'))
assert.ok(page)
const checks=[],errors=[]
page.on('pageerror',e=>errors.push(e.message))
const api=(method,...args)=>page.evaluate(async({method,args})=>window.pywebview.api[method](...args),{method,args})
try{
  await page.waitForFunction(()=>document.querySelector('.app__portrait canvas')?.dataset.loadedCharacter)
  if(mode==='restart'){
    const prefs=(await api('settings_get')).portrait_preferences
    assert.equal(prefs.character,'p20');assert.equal(prefs.gaze,false);assert.equal(prefs.bindings.p20,'zh-CN-XiaoyiNeural')
    assert.equal(await page.locator('.app__portrait').getAttribute('data-character'),'p20')
    assert.equal((await api('tts_voices')).current,'zh-CN-XiaoyiNeural')
    await page.mouse.move(30,30)
    await page.waitForFunction(()=>Math.abs(Number(document.querySelector('.app__portrait canvas')?.dataset.gazeX))<.01)
    checks.push('character, voice association and disabled gaze survive process restart and random HTTP port')
  }else{
    await page.getByRole('button',{name:'形象与声音',exact:true}).click()
    const gallery=page.getByRole('dialog',{name:'形象与声音',exact:true})
    await page.waitForFunction(()=>document.querySelectorAll('.portraits__grid button:not(:disabled)').length===20)
    // Refuse the voice call at the boundary; prove the UI does not commit a character.
    await page.evaluate(()=>{
      const api=window.pywebview.api; window.__portraitOriginalPick=api.tts_pick
      api.tts_pick=async()=>({...await api.tts_voices(),error:'QA 模拟音色拒绝'})
    })
    const before=(await api('settings_get')).portrait_preferences.character
    await gallery.locator('button[data-character="p02"]').click()
    await gallery.getByRole('alert').filter({hasText:'QA 模拟音色拒绝'}).waitFor()
    assert.equal((await api('settings_get')).portrait_preferences.character,before)
    await page.evaluate(()=>{window.pywebview.api.tts_pick=window.__portraitOriginalPick})
    checks.push('refused voice switch cannot falsely commit a new character')
    // Refuse preference persistence after a voice switch; the previous voice must be restored.
    const voiceBefore=(await api('tts_voices')).current
    await page.evaluate(()=>{
      const api=window.pywebview.api;window.__portraitOriginalSave=api.settings_apply
      api.settings_apply=async()=>({...await api.settings_get(),error:'QA 模拟保存拒绝'})
    })
    await gallery.locator('button[data-character="p02"]').click()
    await gallery.getByRole('alert').filter({hasText:'QA 模拟保存拒绝'}).waitFor()
    assert.equal((await api('tts_voices')).current,voiceBefore)
    assert.equal((await api('settings_get')).portrait_preferences.character,before)
    await page.evaluate(()=>{window.pywebview.api.settings_apply=window.__portraitOriginalSave})
    checks.push('failed preference save rolls back the already switched voice')
    for(let i=1;i<=20;i++){
      const id=`p${String(i).padStart(2,'0')}`
      await gallery.locator(`button[data-character="${id}"]`).click()
      await page.waitForFunction(id=>document.querySelector('.app__portrait canvas')?.dataset.loadedCharacter===id,id)
      await page.waitForFunction(()=>document.querySelectorAll('.portraits__grid button:not(:disabled)').length===20)
      assert.equal((await api('settings_get')).portrait_preferences.character,id)
    }
    await gallery.getByRole('combobox').selectOption('zh-CN-XiaoyiNeural')
    await page.waitForFunction(()=>!document.querySelector('.portraits__dialog select').disabled)
    await gallery.locator('button[data-character="p03"]').click()
    await page.waitForFunction(()=>document.querySelector('.app__portrait canvas')?.dataset.loadedCharacter==='p03')
    await gallery.locator('button[data-character="p20"]').click()
    await page.waitForFunction(()=>document.querySelector('.app__portrait canvas')?.dataset.loadedCharacter==='p20')
    assert.equal((await api('tts_voices')).current,'zh-CN-XiaoyiNeural')
    await gallery.getByRole('checkbox',{name:'眼神跟随鼠标'}).uncheck()
    await page.waitForFunction(()=>!document.querySelector('.portraits__dialog select').disabled)
    await page.screenshot({path:path.join(output,'gallery.png')})
    await gallery.getByRole('button',{name:'完成',exact:true}).click()
    checks.push('all 20 characters save through Python; custom character voice is recalled')
  }
  assert.deepEqual(errors,[])
  await page.screenshot({path:path.join(output,`${mode}.png`)})
}finally{
  fs.writeFileSync(path.join(output,`${mode}.json`),JSON.stringify({checks,errors},null,2))
  console.log(JSON.stringify({checks,errors}));await browser.close()
}

import { reactive } from 'vue'
import { Preferences } from '@capacitor/preferences'
export const mobilePortrait=reactive({character:'p01',bindings:{} as Record<string,string>})
export async function readMobilePortrait():Promise<void>{
  try{const {value}=await Preferences.get({key:'xiaoye.portrait.v1'});const saved=JSON.parse(value || '{}') as typeof mobilePortrait
    if(/^p(?:0[1-9]|1[0-9]|20)$/.test(saved.character))mobilePortrait.character=saved.character
    if(saved.bindings && typeof saved.bindings==='object')mobilePortrait.bindings=saved.bindings
  }catch{/* An unreadable optional preference falls back to the default portrait. */}
}
export async function saveMobilePortrait(character:string,voice:string):Promise<void>{
  const next={character,bindings:{...mobilePortrait.bindings,[character]:voice}}
  await Preferences.set({key:'xiaoye.portrait.v1',value:JSON.stringify(next)})
  Object.assign(mobilePortrait,next)
}

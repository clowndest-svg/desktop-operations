import { reactive } from 'vue'
import { fetchSettings, saveSettings } from '@/api/bridge'

export interface PortraitPreferences {
  character: string
  gaze: boolean
  emotions: boolean
  bindings: Record<string, string>
  favorites: string[]
}
export const portraitState = reactive<PortraitPreferences>({ character: 'p01', gaze: true, emotions: true, bindings: {}, favorites: [] })
let pending: Promise<void> | undefined
export function loadPortraitPreferences(): Promise<void> {
  return pending ??= fetchSettings().then(answer => {
    if (answer.portrait_preferences) Object.assign(portraitState, answer.portrait_preferences)
  }).catch(() => { pending = undefined })
}
export async function savePortraitPreferences(next: PortraitPreferences): Promise<void> {
  const answer = await saveSettings({ portrait_preferences: next })
  if (answer.error || !answer.portrait_preferences) throw new Error(answer.error || '形象设置未保存')
  Object.assign(portraitState, answer.portrait_preferences)
}

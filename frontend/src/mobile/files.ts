import { Capacitor, registerPlugin } from '@capacitor/core'

interface XyFilesPlugin { saveText(options: { name: string; text: string; mime: string }): Promise<{ saved: boolean }> }
const XyFiles = registerPlugin<XyFilesPlugin>('XyFiles')

/** Android's document picker asks the user where to save; no broad storage permission. */
export async function saveText(name: string, text: string, mime: string): Promise<boolean> {
  if (Capacitor.isNativePlatform()) return (await XyFiles.saveText({ name, text, mime })).saved
  const url = URL.createObjectURL(new Blob([text], { type: mime }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
  return true
}

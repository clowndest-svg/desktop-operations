/** Android uses hardware/OS-backed key storage; a browser preview is not a secret vault. */
import { Capacitor, registerPlugin } from '@capacitor/core'
import { Preferences } from '@capacitor/preferences'
import { readMigrated, type ValueStore } from './secure-migration'

const XySecure = registerPlugin<ValueStore>('XySecure')
export async function readSecret(key: string): Promise<string | null> {
  if (!Capacitor.isNativePlatform()) return (await Preferences.get({ key })).value
  return readMigrated(key, XySecure, Preferences)
}
export async function writeSecret(key: string, value: string | null): Promise<void> {
  if (!Capacitor.isNativePlatform()) {
    if (value === null) await Preferences.remove({ key })
    else await Preferences.set({ key, value })
    return
  }
  if (value === null) await XySecure.remove({ key })
  else await XySecure.set({ key, value })
  await Preferences.remove({ key })
}

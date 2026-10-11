/** Storage migration is commit-then-remove: a failed secure write must never lose the old value. */
export interface ValueStore {
  get(options: { key: string }): Promise<{ value: string | null }>
  set(options: { key: string; value: string }): Promise<void>
  remove(options: { key: string }): Promise<void>
}

export async function readMigrated(key: string, secure: ValueStore, legacy: ValueStore): Promise<string | null> {
  const current = await secure.get({ key })
  if (current.value !== null) {
    // Retry cleanup after an interrupted migration, without replacing the secure value.
    await legacy.remove({ key })
    return current.value
  }
  const old = await legacy.get({ key })
  if (old.value === null) return null
  await secure.set({ key, value: old.value })
  await legacy.remove({ key })
  return old.value
}

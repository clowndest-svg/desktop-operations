export function toggleFavoriteId(ids: string[], id: string): string[] {
  return ids.includes(id) ? ids.filter(row => row !== id) : [...ids, id].slice(0, 20)
}

import assert from 'node:assert/strict'
import test from 'node:test'
import { toggleFavoriteId } from '../src/avatar/portraitFavorites.ts'

test('toggling adds once and removes on the second press', () => {
  assert.deepEqual(toggleFavoriteId([], 'p03'), ['p03'])
  assert.deepEqual(toggleFavoriteId(['p03'], 'p03'), [])
  assert.deepEqual(toggleFavoriteId(['p01'], 'p03'), ['p01', 'p03'])
})

test('favorite list is capped at twenty distinct ids', () => {
  let ids = []
  for (let number = 1; number <= 20; number += 1) ids = toggleFavoriteId(ids, `p${String(number).padStart(2, '0')}`)
  assert.equal(ids.length, 20)
  assert.equal(toggleFavoriteId(ids, 'p01').length, 19)
})

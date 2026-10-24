import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mouthEnvelope } from '../src/avatar/portraitMotion.ts'

test('stopping playback closes the mouth immediately, including mid syllable', () => {
  assert.equal(mouthEnvelope(0.9, 0.2, false, 0.033), 0)
})
test('silence never opens a speaking mouth; quiet gaps decay', () => {
  assert.equal(mouthEnvelope(0, 0, true, 0.033), 0)
  assert.equal(mouthEnvelope(0, 0.005, true, 0.033), 0)
  assert.ok(mouthEnvelope(0.8, 0, true, 0.1) < 0.3)
})
test('actual sound drives bounded movement with fast attack', () => {
  const quiet = mouthEnvelope(0, 0.02, true, 0.033)
  const loud = mouthEnvelope(0, 0.15, true, 0.033)
  assert.ok(loud > quiet && loud > 0.5 && loud <= 1)
  assert.equal(mouthEnvelope(0, Number.NaN, true, 0.033), 0)
})

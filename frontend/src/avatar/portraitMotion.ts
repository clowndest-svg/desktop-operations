/** Audio envelope only: silence never produces a speaking mouth. */
export function mouthEnvelope(previous: number, rms: number, playing: boolean, delta: number): number {
  if (!playing) return 0
  const signal = Number.isFinite(rms) ? Math.max(0, rms - 0.008) : 0
  const target = Math.min(1, signal / 0.09)
  const amount = 1 - Math.exp(-Math.max(0, delta) / (target > previous ? 0.035 : 0.085))
  return Math.max(0, Math.min(1, previous + (target - previous) * amount))
}

export type PortraitMood = 'idle' | 'listening' | 'thinking' | 'speaking' | 'error'

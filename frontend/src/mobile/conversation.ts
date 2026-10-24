/** Pure request history rules: the on-screen streaming placeholder is not a message. */
export interface HistoryTurn { role: 'user' | 'assistant'; text: string; context?: string }
export const CONTEXT_MESSAGES = 49

export function requestHistory(history: readonly HistoryTurn[], question: string, limit = CONTEXT_MESSAGES): HistoryTurn[] {
  const clean = history
    .filter(turn => (turn.role === 'user' || turn.role === 'assistant') && turn.text.trim())
    .map(turn => ({ role: turn.role, text: turn.context || turn.text }))
  const current: HistoryTurn = { role: 'user', text: question.trim() }
  if (clean.at(-1)?.role === 'user') clean[clean.length - 1] = current
  else clean.push(current)
  const window = clean.slice(-Math.max(1, Math.min(60, Math.trunc(limit) || CONTEXT_MESSAGES)))
  // Do not begin the replay with an orphaned answer after trimming.
  if (window[0]?.role === 'assistant') window.shift()
  return window
}

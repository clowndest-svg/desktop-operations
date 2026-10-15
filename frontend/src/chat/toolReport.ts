const UNRECORDED = '这条历史未记录工具调用信息'

/** Stored history from older shells can carry {} instead of a live tool report. */
export function describeToolReport(value: unknown): string {
  if (!value || typeof value !== 'object') return UNRECORDED
  const report = value as { used?: unknown; count?: unknown; pack?: unknown; degraded?: unknown }
  if (!Array.isArray(report.used)) return UNRECORDED
  const used = report.used.filter((name): name is string => typeof name === 'string')
  const pack = typeof report.pack === 'string' && report.pack ? `（能力包 ${report.pack}）` : ''
  if (used.length) return `本轮调用了 ${used.join('、')}${pack}`
  if (report.degraded === 'overflow') return '上下文装不下工具表，这一轮没带工具，所以她没能动手'
  if (typeof report.count !== 'number' || !Number.isFinite(report.count) || report.count < 0) {
    return UNRECORDED
  }
  if (report.degraded === 'model') return `带了 ${report.count} 个工具${pack}，她一个都没调用就答了`
  if (!report.count) return '这一轮没有携带工具'
  return `带了 ${report.count} 个工具${pack}，本轮没用到`
}

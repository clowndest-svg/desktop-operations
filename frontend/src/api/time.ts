/**
 * 屏上时间戳的唯一写法：`YYYY-MM-DD HH:MM:SS`，本地时区。
 *
 * 之前每一处各切各的：会话列表直接把后端那串 `2026-10-07T11:47:05.624638+0000` 原样贴出来
 * （UTC、带微秒、带 T 和偏移，用户读不动），对话区那条则 `slice(5, 10)` 只留月-日。
 * 一个软件里三种时间写法，"这条是几点"就得每个地方重新学一遍。
 */

const PAD = (value: number): string => String(value).padStart(2, '0')

export function stamp(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return ''
  // 后端两种都发过：秒级 epoch 数字，和 ISO 字符串。认不出来就原样返回，
  // 宁可屏上出现一串读不懂的原文，也不要悄悄显示成"1970-01-01"。
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return (
    `${date.getFullYear()}-${PAD(date.getMonth() + 1)}-${PAD(date.getDate())} ` +
    `${PAD(date.getHours())}:${PAD(date.getMinutes())}:${PAD(date.getSeconds())}`
  )
}

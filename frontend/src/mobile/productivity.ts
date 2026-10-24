/** Local productivity data. Templates prepare drafts; they never send requests themselves. */
export interface PromptTemplate { id: string; title: string; category: string; body: string }
export interface NotebookEntry { id: string; title: string; text: string; tags: string; kind: 'note' | 'task'; done: boolean; pinned: boolean; updated: number }
export interface ReadingPreferences { fontSize: 'normal' | 'large'; compact: boolean; context: 10 | 30 | 60; tools: boolean }
export const defaultReading = (): ReadingPreferences => ({ fontSize: 'normal', compact: false, context: 30, tools: true })
export const MAX_PROMPTS = 50
export const MAX_NOTES = 100
export const PROMPTS: readonly PromptTemplate[] = [
  { id: 'polish', title: '自然润色', category: '写作', body: '请润色以下文字，语气为{{语气}}。保留事实和原意，直接给出成稿。\n\n{{原文}}' },
  { id: 'email', title: '写一封邮件', category: '工作', body: '请给{{收件人}}写一封邮件，主题是{{主题}}，需要表达：{{要点}}。不要补充未提供的事实。' },
  { id: 'weekly', title: '周报整理', category: '工作', body: '请根据以下工作记录整理周报，包含完成事项、问题和下周计划。缺少的数据标注待补充。\n\n{{工作记录}}' },
  { id: 'meeting', title: '会议行动项', category: '工作', body: '请将会议记录整理为结论、行动项、负责人和截止时间，未提供的负责人和时间标为待确认。\n\n{{会议记录}}' },
  { id: 'translate', title: '多语种翻译', category: '语言', body: '请将以下文字翻译为{{目标语言}}，保留格式和语气，简要说明有歧义的表达。\n\n{{原文}}' },
  { id: 'language', title: '口语陪练', category: '语言', body: '请作为{{语言}}口语陪练，与我模拟{{场景}}。每次只问一个问题，在我的回复后指出表达问题并给出自然说法。' },
  { id: 'explain', title: '通俗讲解', category: '学习', body: '请向{{学习者水平}}的学习者解释{{知识点}}，用一个生活中的例子，并给出两道附答案的练习。' },
  { id: 'flashcards', title: '生成复习卡', category: '学习', body: '根据以下资料生成问答复习卡，仅依据原文，先列问题再列答案。\n\n{{资料}}' },
  { id: 'study', title: '学习计划', category: '学习', body: '我想学习{{主题}}，当前水平为{{当前水平}}，每天有{{可用时间}}。请制定可执行的学习计划和自测方法。' },
  { id: 'summary', title: '长文摘要', category: '阅读', body: '请概括以下资料的核心观点、关键事实和未解决的问题，区分原文与推论。\n\n{{资料}}' },
  { id: 'compare', title: '对比与决策', category: '生活', body: '请根据我的需求{{需求}}，对比以下选项，列出优缺点、适用场景和需要进一步核实的信息：\n{{选项}}' },
  { id: 'trip', title: '行程草案', category: '生活', body: '请为{{目的地}}的{{天数}}行程制定草案，偏好为{{偏好}}。注明价格、营业时间和交通需要出行前核实，不要编造实时信息。' },
  { id: 'recipe', title: '冰箱食谱', category: '生活', body: '现有食材：{{食材}}。饮食限制：{{限制}}。请建议简单菜谱，说明用量、步骤和安全熟制要点。' },
  { id: 'debug', title: '代码排错', category: '编程', body: '请分析以下代码和报错，给出可能原因、最小修改和验证方法。区分推测与已知事实。\n代码：\n{{代码}}\n报错：\n{{报错}}' },
  { id: 'review', title: '代码审阅', category: '编程', body: '请审阅以下代码，按严重程度列出正确性、边界处理和可维护性问题，说明触发条件和修复建议。\n\n{{代码}}' },
  { id: 'brainstorm', title: '灵感发散', category: '写作', body: '围绕{{主题}}，面向{{受众}}提出十个有差异的创意，每个用一句话说明，最后推荐三个最可执行的。' },
]
export function promptFields(body: string): string[] {
  return [...new Set([...body.matchAll(/\{\{([^{}\n]{1,40})\}\}/g)].map(match => match[1]!.trim()))]
}
export function fillPrompt(body: string, values: Record<string, string>): string {
  const fields = promptFields(body)
  if (fields.some(key => !values[key]?.trim())) throw new Error('请填写全部模板字段')
  const text = body.replace(/\{\{([^{}\n]{1,40})\}\}/g, (_, name: string) => values[name.trim()]!.trim())
  if (text.length > 40_000) throw new Error('生成的提示词不能超过 40000 字')
  return text
}
export function matchingMessages(turns: readonly { text: string }[], query: string): number[] {
  const q = query.trim().toLocaleLowerCase()
  return q ? turns.flatMap((turn, index) => turn.text.toLocaleLowerCase().includes(q) ? [index] : []) : []
}

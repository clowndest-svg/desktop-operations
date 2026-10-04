/**
 * 手机上的工具环：模型说要做什么 → 人点头 → 原生层真做 → 结果回给模型 → 她说成人话。
 *
 * 为什么单独一个文件：这条环是"AI 控制手机"这件事的全部风险所在，
 * 它必须只有一处、只有一种走法。散进界面代码里，就会出现某条路径忘了要确认。
 *
 * 三条硬规矩：
 * 1. 执行只走 `actions.execute()`，而它是唯一会碰原生插件的出口；
 * 2. 需要人点头的动作必须带着 `approve` 回调进来（回调是界面给的，模型造不出来）；
 * 3. 每一笔都写进台账，**包括被取消的和失败的**——只记成功的那本账不是台账，是宣传。
 */
import { execute, findAction, toolSchemas, type Approve } from './actions'
import { rawChat, type Attachment, type ChatMessage, type ModelConfig, type Turn } from './api'
import type { Delta } from './sse'
import type { ActionRow } from './types'

/** 一轮问答里最多让模型连做几步。超了就停下回话，不做"无限自我循环"的助手。 */
const MAX_ROUNDS = 4

export interface AgentHooks {
  /** 每执行一笔调一次：界面拿它写台账。 */
  onAction: (row: ActionRow) => void
  /** 只在**降级**的时候响：这个模型不接工具，那她确实动不了这台手机，
   * 这句话必须让人看见，不能悄悄退化成"只会聊天的助手"。 */
  onNote?: (text: string) => void
  /** 流式：每来一段正文调一次，界面直接往气泡里追加。给了就走流式。 */
  onDelta?: (delta: Delta) => void
}

function toMessages(history: Turn[], attachments: Attachment[]): ChatMessage[] {
  const messages: ChatMessage[] = history.map((turn) => ({ role: turn.role, content: turn.text }))
  if (attachments.length) {
    const question = messages.pop()
    messages.push({
      role: 'user',
      content: [
        { type: 'text', text: typeof question?.content === 'string' ? question.content : '' },
        ...attachments.map((item) => ({ type: 'image_url', image_url: { url: item.data } })),
      ],
    })
  }
  return messages
}

function parse(raw: string): Record<string, unknown> {
  try {
    const value = JSON.parse(raw || '{}') as unknown
    return value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
  } catch {
    // 模型写坏一段 JSON 是常事。把它当成"没参数"报回去，让她自己纠正，
    // 而不是在这里抛异常把整轮问答打断。
    return {}
  }
}

/**
 * 问一句，允许她在手机上做几件事，最后回一句人话。
 *
 * 三个回调都在 `hooks` 里，见 `AgentHooks`。流式时**跨轮也往同一个气泡里追加**：
 * 她先说"我先把音量调一下"、调完再说"调好了"，读起来是一句连贯的话；
 * 分成两个气泡反而要人去猜哪条是最终答案。
 */
export async function askAgent(
  history: Turn[],
  cfg: ModelConfig,
  attachments: Attachment[],
  approve: Approve,
  hooks: AgentHooks,
): Promise<string> {
  const messages = toMessages(history, attachments)
  const options = hooks.onDelta ? { onDelta: hooks.onDelta } : {}
  let spoken = ''
  // 不是所有 OpenAI 兼容端点都接 `tools`：老一点的会直接 4xx。这时候退回纯聊天，
  // 但要把"退回了"说出去——静默降级的结果是用户以为她不愿意动，而不是动不了。
  let tools: object[] = toolSchemas()
  for (let round = 0; round < MAX_ROUNDS; round += 1) {
    let turn
    try {
      turn = await rawChat(messages, cfg, tools, options)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      if (!tools.length || !/tools?|function/i.test(message)) throw error
      tools = []
      hooks.onNote?.('这个模型不接工具，她动不了这台手机，先只聊天')
      turn = await rawChat(messages, cfg, tools, options)
    }
    spoken = turn.content || spoken
    // 人按了"不听了"：手里这一轮的文本留下，但**不要**拿半截的 tool_calls 去动手机。
    // 参数是逐字流出来的，取消的时候 `{"percent":4` 这种半句完全可能出现。
    if (turn.stopped) return turn.content || spoken
    if (!turn.toolCalls.length) return turn.content || spoken
    messages.push({
      role: 'assistant',
      content: turn.content || '',
      tool_calls: turn.toolCalls.map((call) => ({
        id: call.id,
        type: 'function',
        function: { name: call.name, arguments: call.arguments },
      })),
    })
    for (const call of turn.toolCalls) {
      const known = findAction(call.name)
      const result = await execute(call.name, parse(call.arguments), approve)
      hooks.onAction({
        at: Date.now(),
        name: call.name,
        ok: result.ok,
        detail: result.text,
        outbound: Boolean(known?.leavesDevice),
      })
      messages.push({ role: 'tool', tool_call_id: call.id, content: result.text })
    }
  }
  return (
    spoken ||
    '她在这台手机上连着做了好几步还没说完，先停在这里。要接着做请再说一次具体要做哪一件。'
  )
}

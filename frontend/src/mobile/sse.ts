/**
 * OpenAI 兼容端点的流式回答（SSE）怎么一段一段拼回一条完整回答。
 *
 * 单独一个文件、只吃字符串不碰任何插件，是为了能在 Node 里直接测：
 * 流式解析的错基本都是**边界**错——半个 UTF-8 字符、`data:` 被网络切成两半、
 * `[DONE]` 前面还有半行、`tool_calls` 的 `arguments` 一个字一个字往外漏。
 * 这些在手机上"看起来能用"，撞上某家端点的分帧习惯就整段丢字，
 * 而在装机的时候没人会知道答案被截了——**屏幕上只是少了几句，不报错**。
 *
 * 三条规矩：
 * 1. 只认 `data:` 行，别的（`: ping`、`event:`、空行）按 SSE 规范丢掉；
 * 2. 收不到任何 `data:` 而整体又是一段合法 JSON 时，按**非流式**回答处理——
 *    有些端点被要求 `stream: true` 仍然整块返回，这时候不该显示空白；
 * 3. 拼不出内容也不抛异常，交回空字符串让上层决定怎么说。
 */

export interface ToolCallFragment {
  index: number
  id: string
  name: string
  arguments: string
}

export interface Delta {
  content: string
  reasoning: string
}

export interface Accumulated {
  content: string
  reasoning: string
  toolCalls: { id: string; name: string; arguments: string }[]
  finishReason: string
  /** 这一段是不是按 SSE 帧读出来的。false 说明端点根本没流式，整块 JSON 兜底。 */
  streamed: boolean
}

interface WireToolCall {
  index?: number
  id?: string
  function?: { name?: string; arguments?: string }
}

interface WireChoice {
  delta?: { content?: unknown; reasoning_content?: unknown; tool_calls?: WireToolCall[] }
  message?: { content?: unknown; reasoning_content?: unknown; tool_calls?: WireToolCall[] }
  finish_reason?: string
}

interface WireChunk {
  choices?: WireChoice[]
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/**
 * 一个增量累加器。
 *
 * 用法：`const acc = createAccumulator(); acc.push(chunk); ... acc.finish()`。
 * `push` 返回这一帧里**新出现**的正文和推理，界面直接往气泡里追加就行。
 */
export function createAccumulator(): {
  push(chunk: string): Delta
  finish(): Accumulated
} {
  let pending = ''
  let content = ''
  let reasoning = ''
  let finishReason = ''
  let streamed = false
  const tools = new Map<number, ToolCallFragment>()

  function absorb(choice: WireChoice | undefined): Delta {
    const out: Delta = { content: '', reasoning: '' }
    if (!choice) return out
    const part = choice.delta ?? choice.message ?? {}
    const text = str(part.content)
    if (text) {
      content += text
      out.content = text
    }
    const thought = str(part.reasoning_content)
    if (thought) {
      reasoning += thought
      out.reasoning = thought
    }
    const calls = Array.isArray(part.tool_calls) ? part.tool_calls : []
    calls.forEach((call, position) => {
      const index = typeof call.index === 'number' ? call.index : position
      const row = tools.get(index) ?? { index, id: '', name: '', arguments: '' }
      // 参数是一个字符一个字符漏出来的，名字只在第一帧出现，id 也常常只在第一帧。
      if (call.id) row.id = call.id
      if (call.function?.name) row.name += call.function.name
      if (call.function?.arguments) row.arguments += call.function.arguments
      tools.set(index, row)
    })
    if (choice.finish_reason) finishReason = choice.finish_reason
    return out
  }

  function line(raw: string): Delta {
    const empty: Delta = { content: '', reasoning: '' }
    const text = raw.replace(/\r$/, '')
    if (!text.startsWith('data:')) return empty
    streamed = true
    const payload = text.slice('data:'.length).trim()
    if (!payload || payload === '[DONE]') return empty
    try {
      return absorb((JSON.parse(payload) as WireChunk).choices?.[0])
    } catch {
      // 单帧坏掉不该带走整条回答：丢掉这一帧，继续拼后面的。
      return empty
    }
  }

  return {
    push(chunk: string): Delta {
      const merged: Delta = { content: '', reasoning: '' }
      pending += chunk
      for (; ; ) {
        const cut = pending.indexOf('\n')
        if (cut < 0) break
        const head = pending.slice(0, cut)
        pending = pending.slice(cut + 1)
        const piece = line(head)
        merged.content += piece.content
        merged.reasoning += piece.reasoning
      }
      return merged
    },
    finish(): Accumulated {
      const rows = [...tools.values()].sort((left, right) => left.index - right.index)
      const result: Accumulated = {
        content,
        reasoning,
        toolCalls: rows.map((row) => ({ id: row.id, name: row.name, arguments: row.arguments })),
        finishReason,
        streamed,
      }
      if (streamed) return result
      // 端点没按 SSE 回：把攒着的整块当普通 JSON 读一次。
      const whole = (pending + '').trim()
      if (!whole) return result
      try {
        const parsed = JSON.parse(whole) as WireChunk
        const piece = absorb(parsed.choices?.[0])
        result.content = content
        result.reasoning = reasoning
        result.finishReason = finishReason
        result.toolCalls = [...tools.values()]
          .sort((left, right) => left.index - right.index)
          .map((row) => ({ id: row.id, name: row.name, arguments: row.arguments }))
        void piece
      } catch {
        // 既不是 SSE 也不是 JSON：交回空内容，让上层说"她回了空"而不是崩。
      }
      return result
    },
  }
}

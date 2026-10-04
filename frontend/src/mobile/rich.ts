/**
 * 把模型回的那段字变成屏幕上能看的样子：代码块、粗体、行内代码。
 *
 * 为什么要自己写而不是引一个 markdown 库：手机上要渲染的只有这三种，而一个完整的
 * markdown 解析器会带来链接、图片、HTML 直通这些**这个场景不需要、却都要防**的能力
 * ——答案是从模型来的，也就是从外面来的字符串，任何一条"直通"都是一扇门。
 *
 * 所以这里的顺序是**先转义、后加标签**：`<script>` 进来先变成 `&lt;script&gt;`，
 * 之后那几个正则只往里插 `<strong>` / `<code>` / `<br>` 这几种我们自己写死的标签。
 * 反过来的顺序（先加标签再转义，或者直接 `v-html` 原串）就是把模型的话当代码执行。
 */

/** 转义成"放进 HTML 里只会显示成字"的形态。引号也转，因为要进属性。 */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

export interface Block {
  kind: 'text' | 'code'
  /** 代码块的语言标记，比如 ```python 里的 python。文本块是空串。 */
  lang: string
  body: string
}

/**
 * 按 ``` 围栏切块。
 *
 * 奇数个围栏（模型答到一半被截断）时，最后那段**当成代码块收尾**而不是丢掉：
 * 丢掉的话用户看到的是"答案突然少了一截"，而这正是最需要看见原文的时候。
 */
export function splitFences(text: string): Block[] {
  const blocks: Block[] = []
  const parts = text.split('```')
  parts.forEach((part, index) => {
    if (index % 2 === 1) {
      const newline = part.indexOf('\n')
      const lang = newline === -1 ? '' : part.slice(0, newline).trim()
      const body = newline === -1 ? part : part.slice(newline + 1)
      blocks.push({ kind: 'code', lang, body: body.replace(/\n+$/, '') })
      return
    }
    if (part.trim()) blocks.push({ kind: 'text', lang: '', body: part })
  })
  return blocks
}

/**
 * 文本块里的行内格式。输入必须是**已经转义过**的字符串。
 *
 * `**粗**` 与 `` `代码` `` 的替换都在转义之后做，所以替换进去的标签是我们写的，
 * 而用户/模型给的尖括号这时已经变成实体了。
 */
export function inlineHtml(escaped: string): string {
  return escaped
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\n/g, '<br>')
}

/** 一步到位：原文 → 可以安全塞进 v-html 的片段。 */
export function renderInline(text: string): string {
  return inlineHtml(escapeHtml(text))
}

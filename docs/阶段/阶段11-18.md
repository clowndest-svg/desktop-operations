# 阶段十一 ~ 十八:从「会说话的助手」到「有记忆、会查资料、能用工具的助手」

> 状态:✅ 代码与自动化门禁全绿(ruff / black / mypy strict 217 文件 / pytest 1149 项)
> **但真模型、真依赖的路径没有在本轮验证过**,见 §5「没验证的部分」。
> 这一节比 §1 更重要,报价和文案只能引用 §5 里标 🟢 的行。

## 1. 交付了什么

| 阶段 | 包 | 一句话 |
|---|---|---|
| 11 | `database` | 一个 SQLite 文件 + 按命名空间的迁移 + 类型化取值助手 |
| 11 | `vector` | 向量索引(余弦相似度),默认离线哈希嵌入,可换 OpenAI 兼容 `/embeddings` |
| 11 | `memory` | 长期记忆:事实抽取、混合召回、对话持久化、长对话压缩 |
| 11 | `knowledge` | RAG:文档加载、按句切块、混合召回、**带引用的**问答 |
| 12 | `tools` | 工具框架(JSON Schema + 风险/权限双闸)+ 12 个内置工具 |
| 13 | `ocr` / `vision` | 离线 OCR(RapidOCR)与截图/屏幕文字理解 |
| 14 | `browser` / `computer` | Playwright 浏览与鼠标键盘控制(域名黑白名单、演练模式) |
| 15 | `mcp` | MCP 客户端(标准库 JSON-RPC)+ 工具桥接 |
| 16 | `scheduler` / `workflow` | 持久化定时任务与 YAML 工作流 |
| 18 | `plugins` | 插件发现/装载/热更新 |
| 5+ | `prompt` | 提示词集中管理:一处定义 + 版本号 + 配置可覆盖 |
| 10 | `planner` | 任务分解 + 计划校验(环/悬空依赖)+ 重规划 |

加上组合根(`jarvis/__main__.py` 的 `_register_capabilities`)与六个命令行入口:
`--tools` / `--ingest` / `--ask` / `--memory` / `--prompts` / `--plan`。

测试从 438 项增加到 1094 项。最后补的两个包是 `prompt` 与 `planner` ——
它们本来是架构表里仅剩的空壳,补完之后**二十个阶段的包没有一个还是空的**。

## 2. 几个值得记下来的设计决定

**向量库不用 FAISS。** 原始规划写的是 FAISS,实际用了 SQLite BLOB + 余弦相似度。
理由:FAISS 是原生 wheel,换个 Python 版本要重编译,在 Windows 上是持续的安装
摩擦;而且它产出的是不可读的索引文件,检索结果看着不对时无从下手。本项目的
语料规模(几千个 chunk)线性扫描远低于一毫秒。`Embedder` 是协议,以后想换
FAISS 是实现一个新类,不是重写。

**嵌入默认离线。** `hashing` 引擎把字符 n-gram 做带符号的特征哈希。它不如
真嵌入模型,但足够完成知识库真正在做的事——从手册里找到讲这件事的那三段。
选它当默认是因为另一个选项会让"装完就能用知识库"变成"先配 API Key"。

**召回是混合的,而且是加权求和,不是二选一。** 纯向量检索在个人助手的常见问题上
会失效:名字、订单号、"我上次说的那个"。哈希嵌入会把这些精确字符串糊成一团。
纯关键词检索则对付不了换句话问。两条路都跑,分数相加——但关键词的贡献按
**命中 token 的比例**给,而不是"命中就给满分"。否则两条都含"用户"的记忆得分
相同,语义信号被抹平(这是实现过程中真的踩到的坑,`test_recall_finds_a_semantic_match`
就是为此写的)。

**记忆抽取前面有一道关键词闸。** 每轮都问模型"这句话要不要记住"是每轮一次
额外调用,整天累积。只有含"我 / 记住 / 以后 / my …"的句子才进模型。闸门故意
偏松:误判一次多花一次便宜调用,漏判一次是用户以为助手记住了、其实没记。

**工具的风险和权限是两个独立闸门。** `RiskLevel` 说"错了多严重",权限说
"哪个开关管它"。`write_file` 是 CAUTION + `write`,由 `tools.allow_write` 管;
`delete_path` 是 DANGEROUS + `write`,要那个开关**和**一次人工确认。
模型没法自己打开第二道闸——`confirmed` 只由面向人的那一层传进来。

**路径先 `resolve()` 再判包含。** 反过来会让 `<root>/../../etc` 通过前缀检查
再变成别的东西。相对路径按**第一个允许根目录**解析,不按进程工作目录:桌面程序
从快捷方式启动时 cwd 可以是任何地方(`C:\Windows\System32` 是真实例子)。

**工作流的条件求值器不碰 `eval`。** 用 AST 白名单,和计算器工具共用同一套思路。
`test_condition_evaluator_*` 里有一组注入尝试,要求全部被拒。

**MCP 客户端是标准库实现的。** 官方 `mcp` SDK 没有安装,也不打算装:stdio 传输
就是换行分隔的 JSON-RPC,自己实现约 150 行,还省掉一个依赖。用 `importlib` 延迟
导入,顺带让"没装依赖时报错信息可读"这件事只需要写一次。

**`core/text.py` 是新加的共享原语。** CJK 分词与匹配率计算 `memory` 和
`knowledge` 都要用,两者同层、不允许互相依赖。各写一份就是让分词规则有两个
可能走偏的地方。

## 3. 顺手修掉的既有问题

- `tests/test_asr_engines.py` 有 7 个 mypy 错误(`object.__new__` 之后直接赋私有
  属性),门禁名义上全绿、实际是红的。改成 `cast(Any, ...)`。
- `jarvis/config/service.py` 的 `start()` 不幂等。组合根需要在装配前拿到数据目录
  (数据库文件路径),因此必须能提前 `start()` 一次而不怕 `Application.start()`
  再调一次。已加守卫,与其它组件的约定一致。
- `jarvis/memory/service.py` 里一处日志用 `%d` 格式化 dict,每次启动都会往
  日志里打一段格式化异常。已修。

## 4. 怎么自己验一遍

```bash
# 门禁
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m black --check .
.venv/Scripts/python -m mypy
.venv/Scripts/python -m pytest

# 真的跑一次(不需要 Key、不需要麦克风)
.venv/Scripts/python -m jarvis --tools
.venv/Scripts/python -m jarvis --ingest <某个文件夹>
.venv/Scripts/python -m jarvis --memory
```

## 5. 没验证的部分(报价前必读)

自动化测试跑的是**假引擎**:假 LLM、假 scheduler、假浏览器、假截图器。它们证明
的是"代码路径正确",不是"接了真东西也能用"。以下这些**本轮没有真机验证**:

| 项目 | 为什么没验 | 验它需要什么 |
|---|---|---|
| 记忆自动抽取 | 没有 `DEEPSEEK_API_KEY` | 配一个 Key,说几句话,看 `--memory` |
| 知识库问答(模型总结) | 同上 | 同上;不带 Key 时降级为直接给原文片段,这条验过了 |
| HTTP 嵌入引擎 | 同上 | 配 Key 并把 `vector.embedding.engine` 改成 `http` |
| OCR / 视觉 | `.[vision]` 依赖未安装 | `pip install -e .[vision]`(约 15 MB 模型) |
| 浏览器 / 桌面控制 | `.[automation]` 依赖未安装 | `pip install -e .[automation]` + 下浏览器二进制 |
| MCP | 没有连过真实 MCP 服务器 | 配一个 `mcp.servers` 条目 |
| 定时任务真的到点触发 | 只测了 `run_now()` 与假 scheduler | 加一个 1 分钟后的 job 等它 |
| 插件 | 用真目录测过加载/卸载/热更新 | 这条基本算验过了 |
| PDF / DOCX 入库 | `.[docs]` 未安装 | `pip install -e .[docs]` |
| 7×24 常驻 | 阶段十遗留项,本轮仍未做 | 长时间运行观测 |
| 真实麦克风下的 Barge-In | 阶段十遗留项 | 真麦克风实测 |

## 5.5 交付前发现并修掉的桌面端黑屏

阶段十七的桌面 HUD **在交付时其实是一片黑**：窗口正常打开、标题栏正常、
日志里一个字都没有，内容全黑。之前验收只看了日志和进程数，就说了"启动成功"——
**这是漏检，不是没 bug。**

根因：Vite 产出的是 ES module（`<script type="module">`），而 HTML 规范要求
模块脚本必须走 CORS；`file://` 页面的 origin 是 `null`，本地文件又不带
`Access-Control-Allow-Origin` 响应头 → 脚本和样式全被浏览器拦掉，`#app` 永远是空的，
只剩 `#04070d` 的背景色。

**修法**：界面不再用 `index.as_uri()`，改由 `jarvis/ui/static_server.py` 用回环 HTTP
承载（只绑 `127.0.0.1`、临时端口、只服务 `jarvis/ui/web`、禁目录列表、`no-store`）。
顺带把 Vite 默认加的 `crossorigin` 属性在构建时去掉（`vite.config.ts` 的
`stripCrossorigin` 插件），否则双击 `index.html` 也是黑屏。

**为什么之前没人发现**：这个失败在 Python 侧完全不可见。pywebview 报告窗口成功，
`webview.start()` 正常阻塞，日志全绿。唯一的错误在 WebView2 自己的控制台里。

所以这次不只修 bug，还补了三道能看见它的检查：

| 检查 | 位置 | 拦住什么 |
|---|---|---|
| `bundle_hint()` 增加 `crossorigin` 判定 | `jarvis/ui/desktop.py` | 启动前就拒绝这类产物，并说明原因 |
| 25 秒加载看门狗 | 同文件 `_watch_for_blank_window` | 页面没加载完就打印可操作的排查步骤，而不是继续黑着 |
| 28 项界面测试 | `tests/test_ui_bundle.py` | 产物完整性、HTTP 服务安全姿态、看门狗行为 |
| `scripts/capture_window.py` | 新增 | 真的看一眼像素；`PrintWindow` + `PW_RENDERFULLCONTENT`，不依赖 Pillow |

**还有一个与代码无关的原因**：Chromium 沙箱在某些受限进程环境里起不来，
渲染进程一起来就死（表现是 `index.html` 被请求了一次，但 `/assets/*.js` 一个都没请求）。
这种环境要 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--no-sandbox`，但那是排障手段，
**不能做成默认值**——为了少数环境给所有人摘掉浏览器沙箱是安全倒退。

顺带修掉一个真实小 bug：`--desktop -v` 只开 DevTools 不改日志级别
（`_run_desktop` 没走 `-v` → DEBUG 的替换，只有非桌面路径走了），
而这恰好是排查黑屏最需要的那些 DEBUG 行。现在三条路径共用一个
`_logging_settings_factory`。

## 6. 最后补的两个空壳

### `prompt` —— 项目自己的规则一直没被执行

架构表从阶段五就写着 `prompt`「禁止散落硬编码」,而实际状况是五处常量各占一个包:
persona 在 `app/chat_service.py`、路由指令在 `orchestration/graph.py`、
记忆抽取契约在 `memory/extractor.py`,等等。**没有任何办法把它们放在一起读,
也没有办法改一句措辞而不动代码。**

现在 `jarvis/prompt/templates.py` 是"这个助手到底对模型说了什么"的唯一答案。
每条模板带一个 `version`,因为对一个提示词驱动的助手来说,**措辞就是行为**——
改了一句话却查不出"哪个版本在跑"是真实存在的排查困境。

另外带来两件具体的事:

* `python -m jarvis --prompts` 能看到全部措辞、变量和理由。
* 配置里的 `prompt.overrides` 可以换掉任意一条,不需要开发者。写错模板名只会
  打一条 warning 并用内置措辞继续跑,不会让助手起不来。

代价是给 `memory` / `knowledge` / `orchestration` / `vision` 各加了一条
`prompt` 依赖边(表格与文档已同步)。

### `planner` —— 出计划,不执行

拆解交给模型,但**校验器才是决定计划能不能跑的地方**:重复编号、引用不存在的工具、
悬空依赖,以及**环**。有环的依赖会让执行器变成死循环,所以这是硬门禁不是 warning。

两个设计决定值得记下来:

* **计划是不可变的**。推进一个步骤返回新计划,因为计划同时被产出它的模型、
  执行它的执行器、看进度的人三处读;一个会在读者眼皮底下变的对象,
  就是"第 3 步失败了"变成"哪一次跑?"的来源。
* **`Plan.status` 是派生属性,不是字段**。写成字段时,直接构造一个含已完成步骤的
  计划会一直报 `DRAFT`,直到有人调用 `with_step`——"做没做完"有两个真相来源,
  就是计划显示已完成而某一步还写着 failed 的原因。这是实现过程中真的踩到的坑。

`planner` 不执行步骤:执行需要工具注册表和事件流,两者都在它上面一层。
拆开之后,规划器可以用一个假模型、不需要任何注册表就能测,而且"谁决定的"
和"谁做的"是两个可以分开回答的问题。

### 顺带修掉的

* `Plan.status` 的第二个真相来源(上面那条)。
* `replan` 的"输出空数组 = 放弃"分支不可达:解析器把空数组当失败抛异常,
  而提示词明确告诉模型用空数组表示"做不到"。改成解析器只负责解析,
  由调用方决定空结果的含义。
* 工作流目录不存在时每次启动打一条 warning,而且操作者不知道该把 YAML 放哪。
  现在服务自己创建目录。

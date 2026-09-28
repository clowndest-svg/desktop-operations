# 阶段五:LLM 访问层

> 状态:✅ 完成 | 门禁:Ruff / Black / MyPy strict / pytest 全绿(105 passed,mypy 56 文件)

## 1. 交付物

```
jarvis/llm/
  __init__.py        公共 API 导出(LlmClient / LlmService / 类型 / 错误族)
  types.py           供应商无关的消息与响应类型(全 frozen)
  client.py          LlmClient 协议 —— 消费者唯一可依赖的形状
  errors.py          LlmError 细分:Auth/RateLimit/Server/Request/Response/Timeout/Connection
  transport.py       HttpTransport 协议 + UrllibTransport(纯标准库)
  openai_compat.py   OpenAI 兼容客户端:流式 SSE、重试退避、metrics 记账
  service.py         LlmService 生命周期组件(第三个注册组件)
jarvis/config/
  schema.py          += require_float、ProviderSection、LlmSection
  defaults.yaml      += llm 节(deepseek/openai/kimi/qwen 四个预置端点)
tests/
  test_config_llm.py(10) test_llm_client.py(17) test_llm_service.py(7)
```

## 2. 架构决策

### 2.1 一套实现覆盖所有供应商
GPT / Claude / Kimi / DeepSeek / Qwen / Gemini 全部经 OpenAI 兼容协议
(`POST {base_url}/chat/completions`)接入。**供应商差异是数据不是代码**:
新增一家 = 在 YAML `llm.providers` 下加一个块,零代码改动。

### 2.2 API Key 绝不落盘配置
`ProviderSection` 只存 `api_key_env`(环境变量名),值在**每次请求时**读取:
- 泄露配置文件 ≠ 泄露密钥;
- 换 Key 无需重启;
- 阶段十四(安全)可在同一查找点后面换成加密存储,接口不变。
启动时缺 Key 只告警不阻塞(离线也能启动),真正调用时抛 `LlmAuthError`。

### 2.3 传输层与协议层分离
`HttpTransport` 协议(post_json / post_sse)是网络的唯一出入口:
- 生产实现 `UrllibTransport` **只用标准库**——不引入 httpx/openai SDK,
  PyInstaller 打包零额外负担;
- 测试注入 FakeTransport,17 项客户端测试无一联网;
- 传输层"故意很笨":不懂重试、不懂 JSON 结构,只搬字节并报三种信号
  (状态码/超时/网络),由客户端映射为公共错误族。

### 2.4 错误分类驱动重试
可重试:`LlmRateLimitError(429)` / `LlmServerError(5xx)` / `LlmTimeoutError` /
`LlmConnectionError`;不可重试:`LlmAuthError(401/403)` / `LlmRequestError(4xx)` /
`LlmResponseError(坏响应体)`。指数退避 `base * 2^(n-1)`,`sleep` 可注入,
测试可断言精确等待序列(0.5s → 1.0s)。

### 2.5 流式(为语音链路铺路)
`stream()` 逐块 yield `StreamChunk`,SSE 解析容忍供应商差异(usage 块、
空 choices、[DONE] 哨兵)。重试只发生在**第一个字节之前**——一旦开始产出,
部分文本已交付,重试会造成重复输出。中途故障映射为公共错误直接上抛。

### 2.6 记账进日志(规格第十五章)
每次请求结束输出 `latency_ms / tokens_in / tokens_out / cost_usd`
(经阶段四 `metrics()`,`jarvis.llm.client` 命名空间)。价格是 provider 级
配置(`cost_*_per_1m`,默认 0 = 不报告成本)。

### 2.7 Tool-call 直通
线格式已完整建模(`ToolCall` 解析 + 回传序列化 + `ChatMessage.tool_result`),
阶段十二的工具层直接消费,届时无需再动本包。

## 3. 配置(defaults.yaml 摘录)

```yaml
llm:
  default_provider: deepseek     # JARVIS_LLM__DEFAULT_PROVIDER=kimi 即可切换
  timeout_seconds: 60
  max_retries: 2
  retry_backoff_seconds: 0.5
  providers:
    deepseek: { base_url: https://api.deepseek.com/v1, model: deepseek-chat, api_key_env: DEEPSEEK_API_KEY, ... }
    openai:   { ... }  kimi: { ... }  qwen: { ... }
```

## 4. Requirements

**零新增依赖**:HTTP 走标准库 `urllib`。`requirements.txt` 仍只有 PyYAML。

## 5. 验证

| 项 | 结果 |
|---|---|
| Ruff / Black / MyPy strict | ✅ 全绿(56 文件) |
| pytest | ✅ 105 passed(新增 34) |
| 冒烟:无 Key 启动 | ✅ 四条 WARNING + `llm ready (default=deepseek, ...)`,3 组件正常 start/stop |
| 冒烟:`JARVIS_LLM__DEFAULT_PROVIDER=kimi` | ✅ `llm ready (default=kimi, ...)` |
| 冒烟:无 Key 调用 | ✅ `LlmAuthError: API key environment variable 'DEEPSEEK_API_KEY' is not set` |

## 6. 下一阶段计划(阶段六:Wake Word)

1. `jarvis/wakeword`:`WakeWordEngine` 协议(帧输入 → 唤醒事件回调)
2. OpenWakeWord 实现(免 Key、可离线)为默认;Porcupine 实现为可选(需 AccessKey)
3. 音频采集抽象(`sounddevice`/`pyaudiowpatch` 取舍分析)与 16kHz 单声道帧流水线
4. 配置节 `wakeword`:引擎选择、灵敏度、唤醒词列表(Jarvis / 贾维斯)
5. CPU 占用实测数据进报告;门禁全绿

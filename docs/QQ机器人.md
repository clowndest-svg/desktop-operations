# QQ 机器人

项目使用腾讯官方 `qq-botpy` SDK，监听 QQ 好友私聊、群聊 @ 和频道 @，再通过项目现有的 OpenAI-compatible LLM 层生成回复。

## 启动

1. 复制 `.env.example` 为 `.env`，填入 QQ 开放平台的 `AppID`、`AppSecret` 和模型服务的 API Key。
2. 按服务商实际支持的模型修改 `QQBOT_LLM_MODEL`。
3. 安装依赖并启动：

```powershell
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m jarvis.qqbot
```

QQ 开放平台需要为机器人开启对应的消息 intents。程序会订阅好友私聊、群聊和频道消息；群聊或频道里需要 `@` 机器人。

## 命令

- `/清空`、`/reset`、`/clear`：清除当前用户/群会话上下文。
- `/帮助`、`/help`：显示帮助。

## 安全

`.env` 已被 `.gitignore` 忽略。不要把 `AppSecret` 或模型 API Key 写进源码、配置模板或提交记录。凭据一旦出现在聊天、日志或截图中，应在对应平台立即轮换。

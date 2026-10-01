# 交付与分发：把这套软件放到别人电脑上（distribution）

> 写给"要拿这个软件去卖/给朋友装"的场景。**能用的部分和会卡住的部分都写在这里**，
> 不要等客户装到一半来问你。实测数据标注了来源。

## 0. 一句话结论

**能装，但不是"发个 exe 就行"。** 三件事必须客户自己做或你提前替他做：
① 提供**他自己的**大模型 API Key；② 接受首次约 **900 MB ~ 1.7 GB** 的语音模型下载
（或你直接把模型目录打包给他）；③ 处理 Windows **未签名**带来的 SmartScreen 提示。

## 1. 两种交付方式

### A. 打包版（推荐给非技术客户）

| 项 | 实测值 |
| --- | --- |
| 产物 | `小夜.exe` + `_internal/`（PyInstaller **onedir**） |
| 体积 | **923 MB**（不含语音模型权重） |
| 客户机需要装 Python 吗 | **不需要** |
| 客户机需要装什么 | **WebView2 运行时**（Win11 自带；Win10 多数已随 Office/Edge 装上） |
| 首次启动 | 约 3~8 秒出界面；语音要点「启用语音」才加载（实测按压→待唤醒约 31 秒） |
| 模型 | 首次启用语音时从 ModelScope 下载 SenseVoice 权重（约 900 MB；本机模型目录实测 **1.7 GB**） |

**打包给客户的最省事做法**：把 `JarvisData\models\` 整个目录一起给他，
让他解压到任意非 C 盘位置，再用环境变量指过去（见 §3），**首次就不用下载了**。

### B. 源码版（推荐给开发者自己）

```powershell
git clone <你的仓库>
cd <项目>
py -3.13 -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[desktop,build]"     # desktop 里含 pywebview/psutil/pystray/pillow/pyautogui
cd frontend && npm install && npm run build && cd ..
python -m jarvis --desktop
```

要出 exe：`python -m PyInstaller --distpath ..\out packaging\jarvis.spec`

## 2. 系统要求（照实说）

| 项 | 要求 | 不满足会怎样 |
| --- | --- | --- |
| 操作系统 | **Windows 10 / 11，64 位** | macOS/Linux 目前**不支持**：托盘、热键、UAC 分档、宠物镂空窗口都是 Win32 直调 |
| CPU | 建议 4 核以上 | 语音栈 + 3D 人物会吃满 |
| 内存 | 建议 16 GB | 语音加载后常驻 RSS 实测约 **1.5~1.7 GB** |
| 磁盘 | 程序 923 MB + 模型 1.7 GB + 数据若干 | 建议留 5 GB；**默认不装 C 盘**（见 §3） |
| 显卡 | 无硬性要求 | 3D 人物走 WebGL，纯软渲染也能动，只是帧率低 |
| 麦克风/扬声器 | 用语音就必须有 | 只打字聊天不需要 |
| 网络 | 大模型 API + edge-tts 合成需联网 | 完全离线要换本地 TTS（未做） |

## 3. 不占 C 盘（这是产品要求，不是可选项）

软件所有可写数据都由一个环境变量决定位置：

```powershell
[Environment]::SetEnvironmentVariable("JARVIS_HOME", "D:\XiaoYeData", "User")
```

指向哪，数据就落在哪：`database\`、`models\`、`config\`、`logs\`、`audit\`、`preferences.json`。
不设的话会落到用户目录（即 C 盘）——**给客户装机时这条必须先做**。
模型缓存的重定向在代码里跟着 `JARVIS_HOME` 走，不需要单独设。

## 4. API Key：绝对不能带客户的、也不能带你自己的

**软件从不把密钥写进任何文件。** 只写两件事：
配置文件里存**变量名**（`api_key_env: QWENAI_API_KEY`），界面上填的 key 只进
**当前进程环境变量 + 用户级注册表环境变量**（`HKCU\Environment`，并广播 `WM_SETTINGCHANGE`）。

因此：

- **你不能把你的 key 打进包里给他用**——那等于把你的账单挂在你头上。
- 每个客户必须有**自己的** key（DeepSeek / 通义 / Kimi / OpenAI 任一，或任何 OpenAI 兼容网关）。
- 界面上"设置 → 模型"可以填地址、模型名、key，可以配**多个模型**并保存即生效（不用重启）。
- 日志和界面**只出现变量名，永不出现 key 的值**；这条有测试守着。
- 想让客户零配置试用？唯一正路是做一个**你自己的中转服务**并给他额度，
  而不是发你的 key。

## 5. 装完第一次会看到什么（提前告诉客户，省一轮客服）

1. **SmartScreen「Windows 已保护你的电脑」**：因为 exe **未签名**。
   点「更多信息」→「仍要运行」。要消掉这个提示需要**代码签名证书**（一年几百到几千块，
   且要走 OV 审核），这是卖软件时**第一笔该花的钱**——不签名，客户看到的第一眼就是警告。
2. 部分杀软（尤其 360/火绒的启发式）会对 PyInstaller 产物报可疑：**onedir 比 onefile 好很多**，
   本项目就是因此用 onedir。彻底解决还是要签名 + 提交白名单。
3. 界面右上角点「启用语音」→ 等约 30 秒（首次要下载模型，看网速）→ 状态变「待唤醒」→
   说唤醒词。**不点就完全不开麦克风**。
4. 托盘图标在 **Windows 11 会被收进 `^` 溢出区**，第一次要点开 `^` 找它，
   或在 设置 → 个性化 → 任务栏 → 其他系统托盘图标 里打开。
5. 点窗口右上角 ✕ **不会退出**，是收进托盘；要真退：托盘右键「退出小夜」。

## 6. 客户要知道的三件"它现在还不能做什么"

**别藏着，卖之前讲清楚**，否则第一周就被退货：

| 限制 | 现状 | 影响 |
| --- | --- | --- |
| 它**看不见屏幕** | 视觉链路（截图进模型）未接 | "打开微信点发送"这类只能盲操作（Win 键→打字→回车），点不准 |
| 开着桌面宠物**多耗约 68% 单核** | 实测：宠物关 26.7% → 开 94.3%（同一构建、麦克风关闭） | 低配本上建议关掉宠物，或等优化 |
| 删除类操作**AI 一律不能自动执行** | 磁盘清理必须人在界面上逐项勾选确认；这是设计不是缺陷 | 想"全自动清理"的客户要提前对齐预期 |

另外这几条在做但**还没做完**，别说成已有：思考过程显示、思考强度/上下文设置、
跨会话搜索、结束进程开放给模型（带确认）。

## 7. 隐私与合规（客户公司采购一定会问）

- **麦克风**：只有点了「启用语音」才开；开启后**常驻待命等唤醒词**，
  音频在本地做 VAD/唤醒检测，**唤醒之后的那句话**才送云端识别（FunASR 可全本地）。
  托盘图标颜色会显示当前是否在听；「释放麦克风」立刻关掉。
- **全局热键**用 `RegisterHotKey`，**不是键盘钩子**——它只在你按下那组组合键时收到一条消息，
  看不见你敲的其他键。这一条写进代码注释和测试，别被改成钩子。
- **本地存什么**：对话历史、记忆条目、知识库索引、Token 用量、操作审计，全在 `JARVIS_HOME` 下
  一个 SQLite + 几个 JSONL。**没有云端上传、没有遥测上报、没有账号系统**。
  删掉 `JARVIS_HOME` 目录就等于彻底清除数据。
- **知识库「忘掉」只删索引，不碰磁盘上的原文件**（界面上明写着）。
- 界面/文档里出现的数字都来自实测；没有读数时显示"无读数"，不编数。

## 8. 卸载

没有安装器，也就没有卸载程序：

1. 托盘右键「退出小夜」；
2. 删掉程序目录（onedir 那个文件夹）；
3. 删掉 `JARVIS_HOME` 指向的数据目录（默认在用户目录下）；
4. 若开过开机自启：托盘菜单里取消勾选，或删 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
   里的 `XiaoYeDesktop` 一项；
5. 桌面上删快捷方式。

## 9. 仓库与分支（商业边界）

- 公开仓库**只有一个分支的内容可以对外**：`public-main` → 推到远端 `main`。
- **`commercial/` 目录（能力清单、报价、需求确认单、文案、演示脚本）永远不进公开仓库。**
  本地 `main` 分支的历史里含有它，所以**本地 `main` 绝对不能直接 push**。
- 推之前必须机器验证，不靠人眼：
  ```bash
  git ls-tree -r public-main | grep -c commercial     # 必须是 0
  git grep -InE "sk-[A-Za-z0-9]{16,}" public-main      # 必须为空
  ```
- 配置模板里只有厂商公网端点（deepseek/openai/moonshot/dashscope），
  **不含任何私有网关地址和密钥**；私有网关在未跟踪的本地 `config.yaml` 里。

### 9.1 同步 main → public-main 的固定配方（2026-10-02 实测）

**为什么要有配方**：第一次做这件事时，`git checkout main -- .` 把 10 个
`commercial/` 文件（报价、需求确认单、文案）一并搬进了公开分支的索引，
差一步就推到公网。**这类事故只能靠机器校验挡住，不能靠人记住。**

```bash
# 1) 用独立 worktree，绝不在共享工作树里切分支
git worktree add ../xiaoye-public public-main && cd ../xiaoye-public
git checkout main -- .
git rm -r --cached commercial            # 关键：checkout 会把 commercial 带进来
echo 'commercial/' >> .gitignore && git add .gitignore
# 清掉"只在 public 存在"的孤儿（checkout 不会删除 main 里已删的文件）
git diff --name-status $(git write-tree) main | awk '/^D/{print $2}' | xargs -r -n1 git rm -q --cached

# 2) 三道机器校验，任何一条不过就不许 push
git ls-tree -r --name-only HEAD | grep -c '^commercial/'   # 必须是 0
git grep -I -c -E 'sk-[A-Za-z0-9]{16,}' HEAD -- . | wc -l  # 必须是 0
git diff --name-status HEAD main | grep -v '^A'            # 必须只有 M .gitignore

# 3) 推，然后回读远端确认（别只信本地）
git push origin HEAD:main && git fetch -q origin && git rev-parse --short origin/main
```

第三条校验最值钱：它证明公开树**严格等于** main 减 commercial，
而不是"我看着应该没带进去"。

## 10. 交付前自检清单（复制这一串给客户或自己跑）

```powershell
python -m jarvis --version              # 能起来
python -m jarvis --tools                # 工具清单，确认没有"注册了但永远失败"的条目
python -m jarvis --desktop              # 界面 + 托盘 + 热键
# 日志在 $env:JARVIS_HOME\logs\jarvis.log，看有没有 ERROR
```

界面上依次验：遥测数字在动 → 趋势曲线有历史 → 存储面板列出盘符 → 「扫描」出清理清单
（**只扫不删**）→ 打字问一句能答 → 点「启用语音」等到「待唤醒」→ 说唤醒词能应答 →
「助手」里建一条 2 分钟后的提醒，到点出声 → 「收进托盘」后托盘图标状态变化 →
托盘右键「退出小夜」能真退出（麦克风释放）。全过 = 可交付。

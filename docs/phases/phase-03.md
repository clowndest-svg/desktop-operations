# 阶段三交付报告:配置系统

> 状态:✅ 完成 | 日期:2026-07-29 | 门禁:Ruff / Black / MyPy strict / pytest 全绿

## 1. 交付内容

### 1.1 分层加载(优先级从低到高)

| 层 | 来源 | 说明 |
|---|---|---|
| 1 | `jarvis/config/defaults.yaml` | 随包分发的内置默认值,**完整**(每个已知键都有值),同时充当配置键文档 |
| 2 | 用户文件 | `<data>/config/config.yaml`,或 `JARVIS_CONFIG` 环境变量指定的文件(**部分**,只写想改的键) |
| 3 | 环境变量 | `JARVIS_<SECTION>__<KEY>=value`,双下划线表示嵌套,值按 YAML 标量解析(`true`→bool,`9`→int) |

CLI 另支持 `--config FILE`(优先级高于 `JARVIS_CONFIG`)。显式指定的文件不存在时**立即报错**,绝不静默忽略;默认位置的文件不存在则正常(全新安装)。

### 1.2 类型化不可变模型(`schema.py`)

- `AppConfig`(根)→ `AppSection`(environment/language)+ `LoggingSection`(level/console/file_enabled/max_bytes/backup_count,供阶段四直接消费)
- 全部 `@dataclass(frozen=True, slots=True)`,加载后不可变
- **fail-fast 校验**:未知键(防拼写错误)、类型错误、非法枚举值、越界数值,错误信息带精确点分键路径,例:
  `invalid configuration at 'logging.levle': unknown key (allowed: backup_count, console, file_enabled, level, max_bytes)`

### 1.3 Windows 数据目录布局(`paths.py`)

解析顺序:`JARVIS_HOME` 环境变量 → `%LOCALAPPDATA%\Jarvis` → `~/.jarvis`(非 Windows 兜底)。

```
<data root>/
  config/     用户 config.yaml
  database/   SQLite / FAISS
  logs/       滚动日志(阶段四)
  models/     本地模型(ASR/TTS/VAD/唤醒词)
  cache/      可随时删除的缓存
```

导入期零 I/O,目录仅在 `ConfigService.start()` 时创建(幂等)。

### 1.4 ConfigService(`service.py`)

第一个真实 `LifecycleComponent`:`start()` 解析路径→建目录→加载校验配置;`stop()` 释放快照且幂等;启动前访问 `.config`/`.paths` 抛 `ConfigurationError`。已在 `__main__.py` 注册进 `Application`。其他组件一律由组合根构造注入配置,**任何包不得自行读 YAML/环境变量**。

### 1.5 pydantic 取舍分析(阶段二承诺)

**结论:不引入 pydantic,使用手写 frozen dataclass + 校验辅助函数。**理由:
1. 配置校验需求简单(标量+一层嵌套),pydantic 的收益(复杂 coercion、JSON Schema)用不上;
2. pydantic-core 是原生二进制,增加打包(PyInstaller)与升级风险,而 L1 层应最稳;
3. 手写校验完全掌控错误文案(点分键路径、允许键列表),这是本阶段的核心体验;
4. 若后续 LLM/工具层需要 JSON Schema(Tool Calling 参数校验),届时在那一层按需引入,不影响 L1。

## 2. 目录结构(本阶段新增/变更)

```
jarvis/config/
  __init__.py        公共 API 导出(AppConfig/AppPaths/ConfigService...)
  defaults.yaml      内置默认配置(包数据,随包分发)
  paths.py           数据目录布局与解析
  schema.py          类型化模型 + fail-fast 校验
  loader.py          分层加载/深合并/环境变量覆盖
  service.py         ConfigService 生命周期组件
tests/
  test_config_paths.py     6 项
  test_config_loader.py    20 项
  test_config_service.py   7 项
jarvis/__main__.py   注册 ConfigService,新增 --config 参数
pyproject.toml       dependencies += PyYAML;dev += types-PyYAML;package-data
```

## 3. Requirements 变更

- 运行时:`PyYAML>=6.0.1`(首个运行时依赖)
- 开发:`types-PyYAML>=6.0.12`(MyPy strict 桩)

## 4. 验证结果(真实执行)

| 门禁 | 结果 |
|---|---|
| Ruff | ✅ All checks passed |
| Black | ✅ 无差异 |
| MyPy strict | ✅ 42 个文件零问题 |
| pytest | ✅ **54 passed**(新增 33 项配置测试) |
| 冒烟 `python -m jarvis` | ✅ ConfigService 正常 start/stop,目录创建成功 |
| 冒烟 `JARVIS_LOGGING__LEVEL=DEBUG` | ✅ 环境变量覆盖生效 |
| 冒烟 错误配置 `--config` | ✅ fail-fast,错误信息含键路径与允许键列表 |

## 5. 下一阶段计划(阶段四:日志系统)

1. `LoggingService` 组件:消费 `LoggingSection`,替换 `__main__` 的 bootstrap logging
2. 控制台 + `<data>/logs` 按大小滚动文件双通道;第三方库降噪
3. 结构化字段预留:latency / token / cost(规格第十五章要求的记账基础)
4. 异常统一出口:未捕获异常记录完整堆栈后干净退出
5. 门禁全绿,`ConfigService` → `LoggingService` 成为前两个注册组件

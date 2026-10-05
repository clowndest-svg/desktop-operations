"""手机版的源码级不变量：不构建、不装机也能挡住的那类错。

这些检查存在的理由都是同一类事故：**JS 调了一个原生端根本没有的方法**，或者
**手机页面偷偷用了只有桌面才有的东西**。它们都要在真机上点一下才会暴露，
而真机不是随时都在。所以能在源码里判的，就先在源码里判掉。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "frontend"
ANDROID = FRONTEND / "android" / "app" / "src" / "main"
APP_GRADLE = FRONTEND / "android" / "app" / "build.gradle"
MOBILE_TS = FRONTEND / "src" / "mobile"


def read(path: Path) -> str:
    assert path.is_file(), f"缺少文件：{path}"
    return path.read_text(encoding="utf-8")


def mobile_sources() -> list[Path]:
    files = sorted((FRONTEND / "src" / "mobile").rglob("*"))
    files += sorted((FRONTEND / "src").glob("mobile-main.ts"))
    return [path for path in files if path.suffix in {".ts", ".vue", ".html", ".css"}]


def test_the_phone_page_never_reaches_for_the_desktop_bridge() -> None:
    """手机上没有 ``window.pywebview``，也不能 import 桌面那套桥。

    引了会怎样：每一个面板调用都变成 ``undefined``，界面既不报错也不出东西——
    这是"看起来在做"和"真在做"之间最安静的一种错法。
    """
    touching = [path.name for path in mobile_sources() if "window.pywebview" in read(path)]
    importing = [path.name for path in mobile_sources() if "@/api/bridge" in read(path)]
    assert touching == []
    assert importing == []


def test_the_mobile_entry_loads_the_mobile_root() -> None:
    html = read(FRONTEND / "mobile" / "index.html")
    assert "mobile-main.ts" in html
    # 没有 viewport 这一行 Android 会按 980px 排版再缩放, 按钮小得按不准。
    assert "width=device-width" in html
    assert "viewport-fit=cover" in html


def test_capacitor_points_at_the_mobile_bundle() -> None:
    config = read(FRONTEND / "capacitor.config.ts")
    assert "webDir: 'dist-mobile'" in config
    assert "appId: 'com.xiaoye.assistant'" in config
    # 混合内容会把 https 页面里的请求降级成 http, WebView 直接掐掉。
    assert "allowMixedContent: false" in config
    assert "androidScheme: 'https'" in config


def test_the_mobile_build_does_not_overwrite_the_desktop_bundle() -> None:
    """两份产物必须落在两个目录。上一轮"一份产物两个用途"踩过构建竞态。"""
    mobile = read(FRONTEND / "vite.mobile.config.ts")
    assert "../jarvis/ui/web" not in mobile
    assert "dist-mobile" in mobile


def _plugin_methods(java_path: Path) -> set[str]:
    """所有 ``@PluginMethod`` 修饰的方法名。

    ``final`` 可能出现在返回类型前，也可能出现在参数前——只写一种的话，
    这条检查会漏掉一半的方法，然后报出一个"原生端没实现"的假案。
    """
    text = read(java_path)
    pattern = (
        r"@PluginMethod[^\n]*\n\s*public\s+(?:final\s+)?void\s+(\w+)\s*"
        r"\(\s*(?:final\s+)?PluginCall"
    )
    return set(re.findall(pattern, text))


def _declared(interface: str, ts_path: Path) -> set[str]:
    text = read(ts_path)
    match = re.search(rf"interface {interface}\s*\{{(.*?)\n\}}", text, re.S)
    assert match, f"找不到 interface {interface}"
    found = set(re.findall(r"^\s{2}(\w+)\(", match.group(1), re.M))
    # 这几个是 Capacitor 核心给每个插件都挂上的，不是原生那边的 @PluginMethod。
    # 不排掉的话，接口里一写 `addListener` 就会报一个"原生端没实现"的假案。
    return found - {"addListener", "removeListener", "removeAllListeners"}


def test_every_method_the_web_layer_calls_exists_in_kotlin_or_java() -> None:
    """JS 说"我要用 XySpeech.speak"，原生端就得真有一个 ``speak``。

    少一个方法的后果不是崩溃，而是 Capacitor 的 ``unimplemented`` 报错——在手机上表现为
    "点了说话键没反应"，而这正是用户最容易误报成"这软件是假的"的那类故障。
    手机动作那一组在 `test_the_phone_apis_the_web_layer_names_all_exist_in_java` 里，
    它还要多查一层：注册表实际调用的方法名。
    """
    speech = _plugin_methods(
        ANDROID / "java" / "com" / "xiaoye" / "assistant" / "XySpeechPlugin.java"
    )
    net = _plugin_methods(ANDROID / "java" / "com" / "xiaoye" / "assistant" / "XyNetPlugin.java")
    assert _declared("XySpeechPlugin", MOBILE_TS / "speech.ts") <= speech
    assert _declared("XyNetPlugin", MOBILE_TS / "api.ts") <= net


def _rpc_calls() -> dict[str, set[str]]:
    """手机会发的 RPC：``{方法名: 它传的参数名}``，从调用点抄出来。

    只认键名不认值：源码里写的是 ``{ text, attachments }``（简写）和
    ``{ session_id: id }``（带值）两种形态。
    """
    out: dict[str, set[str]] = {}
    for path in sorted(MOBILE_TS.glob("*.ts")) + sorted(MOBILE_TS.glob("*.vue")):
        text = read(path)
        found: list[tuple[str, str]] = list(
            re.findall(
                r"pcCall(?:<[^>]*>)?\(\s*[\w.]+\s*,\s*'([^']+)'(?:\s*,\s*\{(.*?)\})?",
                text,
                re.S,
            )
        )
        # 面板页把方法名藏在 grab('snapshot') 后面, 漏了它等于整页没白名单检查。
        found += [(name, "") for name in re.findall(r"grab(?:<[^>]*>)?\('(\w+)'\)", text)]
        for name, inline in found:
            keys = out.setdefault(name, set())
            for piece in inline.split(","):
                match = re.match(r"\s*(\w+)\s*(?::|$)", piece)
                if match:
                    keys.add(match.group(1))
    assert out, "手机源码里一个 RPC 都没写，这份检查就没有意义了"
    return out


def _read_keys() -> dict[str, list[str]]:
    """手机 `types.ts` 里的 `READ_KEYS`：``{方法名: 它要读的键}``。

    不在这里再抄一份 —— 抄一份就是又养了一个会过期的替身，而这条检查要抓的
    正是"手机声明的"和"桥面给的"两边不同步。
    """
    block = read(MOBILE_TS / "types.ts").split("READ_KEYS", 1)[1]
    table: dict[str, list[str]] = {}
    for name, keys in re.findall(r"(\w+):\s*\[([^\]]*)\]", block):
        table[name] = re.findall(r"'([^']+)'", keys)
    assert table, "types.ts 里没有 READ_KEYS，这条检查就没有意义了"
    return table


class _RecordingSettings:
    """给 `chat_models` 喂一个形状正确的返回，不碰真配置。

    这里要验的是"手机要读的键，桥面给不给"，不是"某台机器配了什么"——
    所以形状照 `SettingsService.model_choices()` 来，值随便。
    """

    def model_choices(self) -> dict[str, object]:
        return {
            "error": "",
            "providers": [
                {
                    "name": "alpha",
                    "base_url": "https://alpha.example/v1",
                    "models": [{"id": "alpha-1", "label": "alpha-1"}],
                    "default_model": "alpha-1",
                    "key_set": True,
                    "key_variable": "ALPHA_API_KEY",
                    "key_optional": False,
                    "timeout_seconds": None,
                    "current": True,
                }
            ],
            "provider": "alpha",
            "model": "alpha-1",
            "thinking": "medium",
            "turns": 8,
            "thinking_levels": ["off", "low", "medium", "high"],
            "turns_bounds": [0, 50],
        }


def _fields(interface: str, ts_path: Path) -> set[str]:
    """一个 TS interface 里的字段名（不要求有括号，`_declared` 是函数签名用的）。"""
    text = read(ts_path)
    match = re.search(rf"export interface {interface}\s*\{{(.*?)\n\}}", text, re.S)
    assert match, f"{ts_path.name} 里找不到 export interface {interface}"
    return set(re.findall(r"^\s{2}(\w+)\??:", match.group(1), re.M))


def test_the_phone_reads_the_same_turn_fields_the_computer_writes() -> None:
    """两边各自声明"一轮对话长什么样"，字段名必须是同一套。

    中过的事故：手机把 `content` 写成 `text`，翻开电脑上的历史时每条气泡都是空的，
    看着像"历史没了"。桌面那侧用的是 `StoredTurn`，它和桥面是一起改的，
    所以拿它当参照物——手机声明的字段必须都能在上面找到。
    """
    desktop = _fields("StoredTurn", FRONTEND / "src" / "api" / "bridge.ts")
    phone = _fields("PcTurn", MOBILE_TS / "types.ts")
    assert phone, "手机上没声明 PcTurn"
    invented = sorted(phone - desktop)
    assert invented == [], f"手机读了电脑上没有的字段：{invented}"


def test_the_rpc_the_phone_uses_is_whitelisted_and_named_right() -> None:
    """电脑按**关键字**调桥面方法，所以参数名也是接口的一部分。

    桌面上看不出这一点：pywebview 走位置传参，``chat_switch(sessionId)`` 一样调得通。
    手机上写错一个字母的表现是"点了没反应"，得装机才发现——所以这里先挡住。
    """
    from inspect import signature

    from jarvis.ui.desktop import HudBridge
    from jarvis.ui.lan_server import ALLOWED_METHODS

    problems: list[str] = []
    for name, keys in _rpc_calls().items():
        if name not in ALLOWED_METHODS:
            problems.append(f"{name} 不在白名单")
            continue
        target = getattr(HudBridge, name, None)
        if target is None:
            problems.append(f"电脑上没有 {name}")
            continue
        params = signature(target).parameters
        problems += [f"{name}({key})" for key in sorted(keys) if key not in params]
    assert problems == []


def test_the_keys_the_phone_reads_are_keys_the_bridge_actually_returns() -> None:
    """`READ_KEYS` 是手机对桥面返回形状的**声明**，声明错了就是空白面板。

    中过的事故：服务商从"一层 choices[]"改成"两级 providers[] → models[]"，
    手机的 `READ_KEYS['chat_models']` 跟着改了，但契约自检里那个手写替身没改 ——
    于是自检报「缺 providers, models」，看起来像桥面断了，实际是替身过期。
    这里直接从真桥面拿一次返回，手机声明要读的顶层键必须都在，不带替身、
    不需要起真服务（`HudBridge` 只要求设置服务存在，模型键由假替身喂）。
    """
    from jarvis.ui.desktop import HudBridge

    keys = _read_keys()
    wanted = keys.get("chat_models", [])
    assert wanted, "types.ts 里没声明 chat_models 要读什么，这条检查就没意义了"

    writer = cast("Any", _RecordingSettings())
    bridge = HudBridge.__new__(HudBridge)
    bridge._settings = writer
    returned = bridge.chat_models()

    absent = sorted(set(wanted) - set(returned))
    # 行内键（providers[] 里每条带的 models / key_set）不在顶层，`READ_KEYS`
    # 只是"整个 JSON 里必须出现这些字符串"，所以对整个序列化结果再比一次。
    dumped = json.dumps(returned, ensure_ascii=False, default=str)
    still_absent = [key for key in absent if f'"{key}"' not in dumped]
    assert still_absent == [], f"真桥面没给手机要读的键：{still_absent}"


def test_the_manifest_declares_what_the_phone_apis_need() -> None:
    manifest = read(ANDROID / "AndroidManifest.xml")
    assert "android.permission.RECORD_AUDIO" in manifest
    assert "android.permission.INTERNET" in manifest
    # Android 11+ 的包可见性: 不写这两条 queries 系统会一直报"没有可用引擎"。
    assert "android.speech.RecognitionService" in manifest
    assert "android.intent.action.TTS_SERVICE" in manifest
    # 手机动作要的那几样：震动马达、手电筒用的那颗灯、以及"叫得动别的应用"的可见性。
    assert "android.permission.VIBRATE" in manifest
    assert "android.permission.CAMERA" in manifest
    assert 'android:name="android.hardware.camera.flash"' in manifest
    assert "android.intent.action.MAIN" in manifest
    assert "android.intent.category.LAUNCHER" in manifest
    assert "android.intent.action.DIAL" in manifest
    assert "android.intent.action.SENDTO" in manifest
    # 相机/闪光灯都不是必须硬件: 写了 required="true" 会把没闪光灯的手机整个挡在门外。
    assert 'android:required="false"' in manifest


def test_both_image_paths_share_one_size_cap() -> None:
    """拍照和翻相册必须守同一个"这张图多大才发得出去"。

    两处各写一个数，就会有一条路在遥控模式下吃 413，屏幕上只留下"她没看见这张图"。
    """
    api = read(MOBILE_TS / "api.ts")
    declared = re.search(r"export const MAX_DATA_CHARS = (\d+)", api)
    assert declared, "上限要住在 api.ts 里"
    cap = int(declared.group(1))
    assert cap <= 400_000, "不能超过电脑侧 MAX_IMAGE_DATA_CHARS"
    for name in ("photo.ts", "actions.ts"):
        text = read(MOBILE_TS / name)
        assert "MAX_DATA_CHARS" in text, f"{name} 没在用这个上限"
        assert not re.search(r"const MAX_DATA_CHARS\s*=", text), f"{name} 自己另写了一个上限"


def test_taking_a_photo_asks_permission_first() -> None:
    """清单里既然声明了 CAMERA, 叫系统相机之前就必须先要一次权限。

    这条顺序是声明 CAMERA 的连带代价（手电筒要它）：Android 见到"声明了但未授权"的
    相机应用会拒绝为我们出图, 表现是"拍照这个功能是坏的", 而没人会想到根因在手电筒上。
    """
    text = read(MOBILE_TS / "photo.ts")
    ask = text.find("requestPermissions")
    shoot = text.find("getPhoto")
    assert 0 <= ask < shoot, "先 requestPermissions 再 getPhoto, 顺序反了拍照就会静默失败"


def test_the_app_is_not_backed_up_to_the_cloud() -> None:
    """App 私有目录里存着用户自己填的模型 Key。云备份会把它带出这台手机。"""
    manifest = read(ANDROID / "AndroidManifest.xml")
    assert 'android:allowBackup="false"' in manifest


def test_the_keyboard_is_allowed_to_resize_the_window() -> None:
    """键盘盖住输入框那条，第一层挡箭牌。

    Android 默认把软键盘**盖**在窗口上（布局视口一点不变），于是 `100dvh` 的盒子
    下半截正好压在键盘底下 —— 输入框和发送键都在那儿。`adjustResize` 让系统缩窗口
    而不是盖一层，这是根上那一条。

    它丢了不会报任何错：界面照常显示，只是用户每次打字都看不见自己打的是什么。
    """
    manifest = read(ANDROID / "AndroidManifest.xml")
    assert 'android:windowSoftInputMode="adjustResize"' in manifest


def test_the_page_does_not_also_resize_itself_for_the_keyboard() -> None:
    """键盘这件事**只留一个负责人**：原生层。

    `interactive-widget=resizes-content` 会让布局视口再按键盘高度缩一次。
    原生层已经把 WebView 缩过一次了，两个一起上就是减两次，
    Android 15+ 上界面会凭空少一大块 —— 而且这种错看起来像"布局写错了"，
    不会有人想到是两个机制在抢同一块地方。

    viewport meta 里那两条必须留着（没有 `width=device-width` 按钮就按不准，
    没有 `viewport-fit=cover` 刘海屏会啃掉内容），只认 `content="..."` 里那一段，
    不认整份文件的子串 —— 上面那段解释性注释里也写着那个词，搜整份文件会假绿。
    """
    html = read(FRONTEND / "mobile" / "index.html")
    match = re.search(r'name="viewport"\s+content="([^"]+)"', html)
    assert match, "index.html 里没有 viewport meta"
    content = match.group(1)
    assert "width=device-width" in content
    assert "viewport-fit=cover" in content
    assert "interactive-widget" not in content


def _without_comments(text: str) -> str:
    """去掉注释再找。

    注释里提到一个坏写法，不该被当成那个坏写法本身 —— 反过来也一样：
    解释性注释里写了某个词，会让"这个词还在不在"的检查变成假绿（这个坑踩过一次，
    `interactive-widget` 那条就是这么假绿的）。两头都错，所以先剥注释再看。

    只剥注释，不动字符串字面量：真代码里的 `type="password"` 一定在标签里，
    不在引号包的字符串里。
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


def test_no_password_input_in_the_phone_ui() -> None:
    """手机界面上不许出现 `type="password"`。

    这是 2026-10-03 在 Android 16 的 vivo X100 上量出来的：设置面板里点
    `type=password` 的 API Key 框，**整个界面消失**，屏幕上只剩 Activity 的窗口背景
    （那张 Capacitor 启动图）—— 进程还活着、没有崩溃日志、没有异常。
    同一个面板里普通文本框一切正常。

    判据是拿截图量的，不是猜的：同一块顶栏区域，文本框有 3276 个亮像素，
    密码框是 0。聚焦密码框会让输入法切到安全输入/独立编辑那一套，
    在部分 ROM 上把 WebView 整个顶掉。

    遮挡改用 CSS 的 `-webkit-text-security: disc`，视觉一样，不碰系统密码通路。
    """
    needle = 'type="password"'
    offenders = [
        path.name
        for path in sorted(MOBILE_TS.rglob("*"))
        if path.suffix in {".ts", ".vue"} and needle in _without_comments(read(path))
    ]
    assert offenders == [], f"这些文件里有 {needle}：{offenders}"

    # 遮挡得还在，不能因为去掉 password 就把 Key 明晃晃地摆在屏幕上。
    assert "-webkit-text-security: disc" in read(MOBILE_TS / "MobileApp.vue")


def test_the_page_never_guesses_the_viewport_itself() -> None:
    """尺寸只有一个主人：键盘高度由原生**报**进来，页面照着算。

    这一条是**反着写的**，因为这里连着写错过两次：

    1. 第一版让网页自己读 `visualViewport`，把高度和 `offsetTop` 写进 CSS 变量、
       再用 `translateY` 跟着浏览器平移。在设置面板里点一个靠底部的输入框时整屏变黑 ——
       浏览器为了露出光标平移了可视区，那套逻辑就把整个界面推到了屏幕外。
    2. 第二版让原生层直接把 IME 高度写成 WebView 的下外边距。聊天页好了，
       同一个设置面板还是整屏变黑：WebView 被压到没有高度，屏幕上只剩 Activity 的
       窗口背景（那张 Capacitor 启动图）—— 没有崩溃、没有日志。

    两次的共性都是"页面之外还有人在改布局"。所以这里钉住的是：
    `dvh` / `100vh` / 自己算的 `--xy-view` / `translateY` 一个都不许出现，
    让位给键盘只能用 `--xy-kb`。

    另外 `.app` 必须是 `position: fixed`：在面板里点一个靠底部的输入框时，
    Chromium 会为了露出光标去滚**文档**，正常流里的界面会被整块滚出屏幕 ——
    表现同样是"整屏黑"。`overflow: hidden/clip` 挡不住文档滚动，只有 fixed 挡得住。
    """
    app = read(MOBILE_TS / "MobileApp.vue")
    assert "bottom: var(--xy-kb, 0px)" in app
    assert "position: fixed" in app
    assert "height: 100dvh" not in app
    assert "height: 100vh" not in app
    assert "--xy-view" not in app
    assert "--xy-pan" not in app
    # 只禁"跟着视口平移"那一种。动画里的 `transform: translateY(0)` 是另一回事，
    # 拿它当证据会逼着人把一句正常的动画也删掉 —— 检查太宽和没有检查一样糟。
    assert "translateY(var(" not in app

    # 整页不许滚：整页一旦可滚，浏览器就会去平移可视区露光标，
    # 而 absolute/fixed 的界面不跟着走。滚动只发生在消息流和面板内部。
    css = read(MOBILE_TS / "mobile.css")
    assert "overflow: hidden" in css
    assert "--xy-kb: 0px" in css


def test_the_native_layer_reports_the_keyboard_height() -> None:
    """原生层只**报数**，不改 WebView 的几何尺寸。

    为什么非要在原生层量：Android 15+ 上目标 SDK 35 的应用被强制 edge-to-edge，
    `adjustResize` 从此不再缩窗口，`dvh` 和 `visualViewport` 也一起失灵 ——
    网页层三条全落空，输入框就还压在键盘底下（2026-10-03 在 Android 16 的
    vivo X100 上实测到的就是这个）。

    为什么只报数：直接改 WebView 尺寸 = 两个层同时拥有布局的控制权，
    出错时表现为整屏空白。这里连 `setLayoutParams` 都不许出现。

    监听器必须挂在**父布局**上。挂在 WebView 自己身上会取代它的
    `onApplyWindowInsets`，而它正是靠那个方法算 `env(safe-area-inset-*)` 的 ——
    第一版就是这么写的，键盘修好了，顶栏却整个贴到状态栏底下。
    """
    activity = read(ANDROID / "java" / "com" / "xiaoye" / "assistant" / "MainActivity.java")
    assert "WindowInsetsCompat.Type.ime()" in activity
    assert "--xy-kb" in activity
    assert "evaluateJavascript" in activity
    # 只报数，不动布局。
    assert "setLayoutParams" not in activity
    assert "bottomMargin" not in activity
    # 挂在父布局上，不是挂在 WebView 上。
    assert "getParent()" in activity
    assert "setOnApplyWindowInsetsListener(" in activity
    assert "setOnApplyWindowInsetsListener(\n                web," not in activity


def test_release_build_refuses_to_fall_back_to_debug_signing() -> None:
    """debug 签名的 APK 和正式版互不兼容，装上去之后覆盖安装会失败在用户手机上。"""
    gradle = read(APP_GRADLE)
    assert "GradleException" in gradle
    assert "signingConfig signingConfigs.release" in gradle
    assert "keystore.properties" in gradle


def test_the_signing_material_is_not_in_the_repository() -> None:
    """密钥库和口令必须在仓库外：这个仓库要能公开分享。"""
    leaks = [
        str(path.relative_to(REPO))
        for path in list(REPO.glob("**/*.keystore")) + list(REPO.glob("**/*.jks"))
    ]
    assert leaks == []
    assert not (REPO / "frontend/android/keystore.properties").exists()


def test_the_page_shows_the_version_that_was_built() -> None:
    """屏幕上写的版本号必须就是 gradle 打进 APK 的那个。

    远程帮忙时第一句总是"你装的是哪一版"；两处各写各的，答上来的就不是答案。
    """
    gradle = re.search(r'versionName\s+"([^"]+)"', read(APP_GRADLE))
    shown = re.search(r"APP_VERSION\s*=\s*'([^']+)'", read(MOBILE_TS / "api.ts"))
    assert gradle and shown, "版本号得同时写在 gradle 和页面上"
    assert gradle.group(1) == shown.group(1)


def test_the_phone_apis_the_web_layer_names_all_exist_in_java() -> None:
    """JS 说"我要用 XyAction.setVolume"，原生端就得真有一个 ``setVolume``。

    少一个方法的后果不是崩溃，而是 Capacitor 的 ``unimplemented`` 报错——在手机上表现为
    "点了没反应"，而这正是用户最容易误报成"这软件是假的"的那类故障。
    """
    speech = _plugin_methods(
        ANDROID / "java" / "com" / "xiaoye" / "assistant" / "XySpeechPlugin.java"
    )
    net = _plugin_methods(ANDROID / "java" / "com" / "xiaoye" / "assistant" / "XyNetPlugin.java")
    action = _plugin_methods(
        ANDROID / "java" / "com" / "xiaoye" / "assistant" / "XyActionPlugin.java"
    )
    assert _declared("XySpeechPlugin", MOBILE_TS / "speech.ts") <= speech
    assert _declared("XyNetPlugin", MOBILE_TS / "api.ts") <= net
    declared = _declared("XyActionPlugin", MOBILE_TS / "actions.ts")
    assert declared <= action, f"原生端缺：{sorted(declared - action)}"
    # 注册表里真正调用的那几个方法，也要在同一个文件里声明过。
    called = set(re.findall(r"XyAction\.(\w+)\(", read(MOBILE_TS / "actions.ts")))
    missing = sorted(called - declared)
    assert missing == [], f"注册表调了没声明的方法：{missing}"


def _action_blocks() -> dict[str, str]:
    """把 `ACTIONS` 里每一条动作的源码块切出来：``{名字: 原文}``。"""
    text = read(MOBILE_TS / "actions.ts")
    body = text.split("export const ACTIONS", 1)[1]
    blocks: dict[str, str] = {}
    for chunk in re.split(r"\n  \{\n", body)[1:]:
        name = re.search(r"name:\s*'(\w+)'", chunk)
        if name:
            blocks[name.group(1)] = chunk
    assert len(blocks) >= 8, f"只切出 {len(blocks)} 条动作，这份检查就没意义了"
    return blocks


def test_anything_that_leaves_the_phone_needs_a_human_nod() -> None:
    """`leavesDevice` 的动作必须同时 `needsConfirm`。

    拨号、发短信、写日历、开链接、定闹钟——这些会在别人的世界里留下痕迹或者花真钱。
    以后有人加一条"直接发出去"的动作而忘了确认闸，这条就是拦他的。
    """
    loose = sorted(
        name
        for name, block in _action_blocks().items()
        if "leavesDevice: true" in block and "needsConfirm: true" not in block
    )
    assert loose == [], f"这些动作会离开这台手机，却没设确认闸：{loose}"


def test_the_model_can_never_press_the_confirm_button() -> None:
    """确认这件事不许是一个**参数**——桌面那边定过的规矩，手机这边照办。

    一旦 `confirmed` 出现在工具签名里，模型自己填一个 true 就把确认闸绕过去了，
    而这种绕法在测试里看起来完全正常。查的是形状而不是这个词：注释里要讨论它，
    恰恰是因为不许它存在。
    """
    registry = read(MOBILE_TS / "actions.ts")
    assert not re.search(r"name:\s*'confirmed'", registry), "确认被写成了动作参数"
    assert not re.search(r'read\w*\(\s*"confirmed"', registry)
    assert '"confirmed"' not in read(
        ANDROID / "java" / "com" / "xiaoye" / "assistant" / "XyActionPlugin.java"
    ), "原生方法收了 confirmed, 等于把点头的权力交给调用方"
    # 出口只有一个, 而且点头的回调是必填的: 少传就是一个编译错误。
    signature = re.search(r"export async function execute\((.*?)\)\s*:\s*Promise", registry, re.S)
    assert signature, "找不到 execute() 的签名"
    assert "approve: Approve" in signature.group(1), "approve 变成了可选参数, 确认闸就成了一句劝告"


def test_the_tool_loop_is_the_only_way_to_act() -> None:
    """`execute()` 是唯一的出口：界面和模型都从它走，没人能绕过去直接调插件。"""
    callers = [
        path.name
        for path in mobile_sources()
        if path.name != "actions.ts" and re.search(r"\bXyAction\.\w+\(", read(path))
    ]
    assert callers == [], f"有人绕过注册表直接调原生动作：{callers}"
    assert "execute(" in read(MOBILE_TS / "agent.ts")


def test_the_phone_says_what_it_cannot_do() -> None:
    """独立模式的系统提示两头都要写：不能碰电脑, 但**这台手机她能动**。

    只写前一句, 模型会照着"只有对话能力"去拒绝真实的手机动作, 那个功能等于没做;
    只写后一句, 她会开始答应操作电脑。两头都不是我们要的。
    """
    api = read(MOBILE_TS / "api.ts")
    assert "不能操作任何电脑" in api
    assert "不要假装自己做了" in api
    assert "但这台手机本身她能动" in api
    assert "调工具" in api


# -- 语音通话与音色 ----------------------------------------------------------


def test_the_call_hands_the_computer_the_original_audio() -> None:
    """手机不本地识别。

    本地识别等于"手机用系统语音理解一遍、电脑再理解一遍"，同一个问题问两种方式
    会得到两种能力 —— 这是 `voice_graph` 里写下的那条教训，通话这条链路同样适用。
    所以录到的原声必须是**整个**交给电脑的 `call_turn`，而不是先把文字发出去。
    """
    call = read(MOBILE_TS / "call.ts")
    stage = read(MOBILE_TS / "CallStage.vue")
    assert "pcCallTurn" in stage, "通话页必须把原声交给电脑"
    assert "XySpeech" not in stage, "通话不该走 Android 的系统识别：那是另一个脑子"
    assert "TARGET_RATE = 16000" in call, "管线格式是 16kHz，写错就是慢放三倍的怪声"


def test_the_phone_resamples_instead_of_trusting_the_device_rate() -> None:
    """设备采样率常见 48kHz，直接当 16k 发过去**不会有任何报错**，只是识别不出。

    这是通话功能里最难从现象反推的一个错，所以它必须是一次显式换算。
    """
    call = read(MOBILE_TS / "call.ts")
    assert "function to16k" in call
    assert "context.sampleRate" in call


def test_the_recording_cannot_run_forever() -> None:
    """手机侧的上限必须和电脑侧的 MAX_HEARD_MS 对齐。

    对不齐的话，用户念满 40 秒才会收到一句"太长了"，而那 40 秒里麦克风一直在录。
    """
    from jarvis.app.voice_call import MAX_HEARD_MS

    call = read(MOBILE_TS / "call.ts")
    declared = re.search(r"MAX_RECORD_MS\s*=\s*(\d+)", call)
    assert declared, "手机侧没有声明录音时长上限"
    assert int(declared.group(1)) == MAX_HEARD_MS


def test_the_recording_bounds_match_what_the_computer_enforces() -> None:
    """界面上说的秒数来自电脑的常量，不是这里另写的一个数。

    两处各写各的，就会出现"界面上说至少 2 秒、电脑却说太短"这种对不上的提示。
    """
    panel = read(MOBILE_TS / "VoicePanel.vue")
    assert "rules.value.min_ms" in panel
    assert "rules.value.max_ms" in panel
    assert "comfortable_ms" in panel


def test_previewing_a_voice_does_not_select_it() -> None:
    """「让我听听另一个」不该变成「为什么她换声音了」。

    所以试听和选中是两个按钮、两条 RPC，而不是一个按钮的两种后果。
    """
    panel = read(MOBILE_TS / "VoicePanel.vue")
    assert "pcPreviewVoice" in panel
    assert "pcPickVoice" in panel
    api = read(MOBILE_TS / "api.ts")
    assert "'tts_preview_pcm'" in api, "试听要走「返回字节」那条，不是「推到电脑扬声器」那条"


def test_the_phone_never_sends_a_clone_recording_as_raw_bytes() -> None:
    """JSON 装不下裸字节，所以录音和合成音频都要走 base64。

    写错的表现是电脑那边 base64 解不开 —— 而错误信息会是"录音坏了"，
    让人以为是麦克风的问题。
    """
    call = read(MOBILE_TS / "call.ts")
    assert "function toBase64" in call
    assert "btoa" in call


def test_the_voice_entries_are_visible_without_a_computer() -> None:
    """语音通话和音色这两个入口**不许**带 ``v-if="link"``。

    2026-10-03 真机踩到的事故：它们当时都写成了 ``v-if="link"``，于是没连电脑的人
    打开 App **完全看不到**这两个功能。用户的结论不是"要先连电脑"，而是
    **"这个包没做语音"** —— 那是最坏的一种错法：功能在包里、代码是对的、
    而用户以为它不存在，所有测试还都是绿的。

    测的是**入口**，不是面板：``CallStage`` / ``VoicePanel`` 本身依然要
    ``link`` 才渲染（它们整块都依赖桥面方法，没有电脑时进去也是空白）。
    入口可见、点下去说清楚为什么现在不能用 —— 这两件事合起来才是对的。
    """
    app = read(MOBILE_TS / "MobileApp.vue")
    # 判据要连**开标签的属性**一起看，不能只扫按钮正文：
    # 「语音通话」是写在 `aria-label` 上的，正文里只有一段 SVG，
    # 只扫正文的话，把 `v-if="link"` 加回去这个测试照样绿（实测过）。
    for element in re.findall(r"<button\b.*?</button>", app, re.S):
        opening = element[: element.index(">")]
        if 'v-if="link"' not in opening:
            continue
        for marker in ("openCall", "openVoice", "语音通话", "音色"):
            assert (
                marker not in opening
            ), f"入口「{marker}」带了 v-if=link，没连电脑的人会以为没这功能"
    # 反过来也要成立：入口真的在（去掉守卫时最怕顺手删掉整块）。
    assert "openCall" in app, "通话入口的处理函数不见了"
    assert "openVoice" in app, "音色入口的处理函数不见了"


def test_the_voice_entries_explain_themselves_when_disconnected() -> None:
    """点了入口但没连电脑时，必须给一句人话，而不是静默或整屏空白。

    两个入口的守卫都写在处理函数里（``openCall`` / ``openVoice``），
    并且都要能走到 ``note(...)`` 那条分支 —— 有守卫但没提示，
    等于按钮点了没反应，比藏起来更难查。
    """
    app = read(MOBILE_TS / "MobileApp.vue")
    for name in ("openCall", "openVoice"):
        body = re.search(r"function " + name + r"\(\)[^{]*\{(.*?)\n\}", app, re.S)
        assert body, f"{name} 不在了"
        block = body.group(1)
        assert "!link.value" in block, f"{name} 少了未连电脑的分支"
        assert "note(" in block, f"{name} 未连电脑时只 return，不告诉用户为什么"
        assert "stage.value =" in block, f"{name} 连上之后没有真的打开面板"


def test_the_disconnected_banner_mentions_voice() -> None:
    """独立模式的横幅要说清语音也要连电脑。

    横幅原来只提"看屏幕、建提醒、查机器"，一个字没提语音和音色 ——
    而这两个入口当时还被藏着，于是"装上没有语音"这个结论完全合乎用户的观察。
    """
    app = read(MOBILE_TS / "MobileApp.vue")
    banner = re.search(r"class=\"banner\"[^>]*>(.*?)</button>", app, re.S)
    assert banner, "独立模式横幅不见了（或不再是个可点的 button）"
    text = banner.group(1)
    assert "语音" in text, "横幅没提语音：这正是用户找不到的那一项"
    assert "音色" in text, "横幅没提音色"
    assert "手机接入" in text, "横幅没说要先在电脑上打开「手机接入」"

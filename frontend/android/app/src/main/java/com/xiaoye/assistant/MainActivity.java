package com.xiaoye.assistant;

import android.os.Bundle;
import android.util.Log;

import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeActivity;

/**
 * 自家插件必须在 super.onCreate 之前注册：桥（Bridge）在 onCreate 里就把
 * 插件表建好了，晚一步 JS 那边拿到的是"插件不存在"，而界面显示的是"没声音"，
 * 排查方向会被带歪。
 *
 * <hr>
 *
 * 这里还多做了一件事：**把软键盘的高度报给网页**。
 *
 * 为什么非要在原生层做：Android 15+ 上目标 SDK 35 的应用被强制 edge-to-edge，
 * `adjustResize` 从此不再缩窗口，键盘变成一个盖在窗口上的悬浮层。于是
 * `dvh`、`visualViewport`、`adjustResize` 三条网页层的路**一起失灵**，
 * 输入框就还压在键盘底下（2026-10-03 在 Android 16 的 vivo X100 上实测到的就是这个）。
 *
 * ## 为什么是"报告"而不是"直接把 WebView 改矮"
 *
 * 第一版是直接把 IME 的高度写成 WebView 的下外边距 —— 聊天页确实好了，
 * 但在设置面板里点一个靠底部的输入框时**整屏变黑**：WebView 被压到没有高度，
 * 屏幕上只剩 Activity 的窗口背景（那张 Capacitor 启动图）。
 *
 * 让原生层去改 WebView 的几何尺寸，等于让两个层同时拥有布局的控制权，
 * 而这类"两边都以为自己说了算"的写法，出错时的表现就是整屏空白 —— 没有崩溃、
 * 没有日志、没有线索。现在改成只**报一个数字**：原生层写 `--xy-kb`（CSS 变量），
 * 页面拿它算自己的高度。布局始终只有网页一个主人，原生层只负责说环境是什么样。
 *
 * ## 为什么不用 `@capacitor/keyboard`
 *
 * 那个插件也是走"改 WebView 尺寸"那条路，代价一样，还多一个依赖。
 * 这个场景要的只是"键盘多高"这一个数，自己量比装一个插件便宜。
 */
public class MainActivity extends BridgeActivity {

    private static final String TAG = "XyInsets";

    /** 上一次报给网页的值，避免每帧都往 JS 里灌同一句话。 */
    private int reported = -1;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(XySpeechPlugin.class);
        registerPlugin(XyNetPlugin.class);
        registerPlugin(XyActionPlugin.class);
        super.onCreate(savedInstanceState);
        reportKeyboardHeight();
    }

    private void reportKeyboardHeight() {
        Bridge bridge = getBridge();
        final android.webkit.WebView web = bridge == null ? null : bridge.getWebView();
        if (web == null) {
            // 布局没加载出来时（no_webview 那条兜底路径）没有可报的东西，也不该崩。
            return;
        }
        android.view.ViewParent holder = web.getParent();
        if (!(holder instanceof android.view.ViewGroup)) {
            return;
        }
        final float density = getResources().getDisplayMetrics().density;
        // 监听器必须挂在**父布局**上，不能挂在 WebView 上：
        // View.dispatchApplyWindowInsets 只要发现设了 listener，就直接返回 listener 的结果，
        // **不再调用 onApplyWindowInsets** —— 而 WebView 正是靠那个方法记录安全区、
        // 让 CSS 的 env(safe-area-inset-*) 有值的。第一版挂在 WebView 上，
        // 键盘那条修好了，顶栏却整个贴到状态栏底下。挂父布局两头都对：
        // 父布局返回未消费的 insets，ViewGroup 会继续往下派发给子 View。
        ViewCompat.setOnApplyWindowInsetsListener(
                (android.view.View) holder,
                (view, windowInsets) -> {
                    Insets ime = windowInsets.getInsets(WindowInsetsCompat.Type.ime());
                    int css = Math.round(ime.bottom / density);
                    if (css != reported) {
                        reported = css;
                        // 单位是 CSS 像素：Java 这边拿到的是物理像素，
                        // 直接塞进去的话在 3.5 倍屏上会变成 3.5 倍高的一块空白。
                        web.evaluateJavascript(
                                "document.documentElement.style.setProperty('--xy-kb','"
                                        + css
                                        + "px')",
                                null);
                        Log.i(TAG, "键盘高度上报：" + css + "px（物理 " + ime.bottom + "）");
                    }
                    // 不消费：系统栏那部分还要往下传，状态栏/手势条由网页的
                    // env(safe-area-inset-*) 负责，两边不要抢同一块地方。
                    return windowInsets;
                });
        ViewCompat.requestApplyInsets((android.view.View) holder);
    }
}

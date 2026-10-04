package com.xiaoye.assistant;

import android.content.Context;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;

import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import java.util.ArrayList;
import java.util.Locale;

/**
 * 手机的耳朵：Android 系统语音识别（不是云端，也不是电脑那套 funasr）。
 *
 * 为什么必须有原生插件：WebView 里 `navigator.mediaDevices` 在 Capacitor 的
 * https://localhost 源上要么没有、要么每次都要重新授权，而系统识别引擎（Google / 厂商）
 * 本来就只在 Java 层暴露。用系统引擎还有一层现实考虑：手机上装 1.7 GB 的 funasr 模型
 * 是不现实的，识别必须借系统。
 *
 * 为什么拆成 listenStart / listenStop：用户是"按住说话、松开发送"。合成一个
 * `listen()` 的写法只能在松手后才开始录音，前半个字就丢了——这是真机上最容易做错、
 * 又最难在代码审查里看出来的一处。
 */
@CapacitorPlugin(
        name = "XySpeech",
        permissions = {
                @Permission(strings = {android.Manifest.permission.RECORD_AUDIO}, alias = XySpeechPlugin.AUDIO)
        }
)
public class XySpeechPlugin extends Plugin {

    static final String AUDIO = "audio";

    private SpeechRecognizer recognizer;
    private PluginCall pendingStop;
    private String transcript = "";
    private String listenError = "";
    private final Handler main = new Handler(Looper.getMainLooper());
    private Runnable watchdog;

    @PluginMethod
    public void capability(PluginCall call) {
        JSObject result = new JSObject();
        Context context = getContext();
        boolean available = SpeechRecognizer.isRecognitionAvailable(context);
        result.put("native", true);
        // 中文行不行只能"试出来"：系统没暴露按语言查询的接口，硬猜会在某些 ROM 上骗人。
        // 所以这里只报"有没有引擎"，真正的中文支持由第一次 listenStart 的结果说话。
        result.put("chinese", available);
        result.put("reason", available
                ? ""
                : "这台手机没装可用的系统语音识别引擎（部分定制 ROM 会砍掉），先用打字的方式问她");
        call.resolve(result);
    }

    @PluginMethod
    public void listenStart(final PluginCall call) {
        if (getPermissionState(AUDIO) != PermissionState.GRANTED) {
            requestPermissionForAlias(AUDIO, call, "audioPermissionCallback");
            return;
        }
        onMain(() -> begin(call));
    }

    @PermissionCallback
    private void audioPermissionCallback(final PluginCall call) {
        if (getPermissionState(AUDIO) != PermissionState.GRANTED) {
            call.reject("没有麦克风权限，说了也不会被听到");
            return;
        }
        onMain(() -> begin(call));
    }

    /**
     * 把真正碰系统语音的活挪到主线程上做。
     *
     * 不这么做就是**闪退**，而且是那种"聊着聊着突然没了"的闪退：Capacitor 把插件方法
     * 跑在自己的 `CapacitorPlugins` 工作线程上，而 `SpeechRecognizer.createSpeechRecognizer`
     * 和 `TextToSpeech` 的构造都只许在主线程调，违反就抛
     * `RuntimeException: SpeechRecognizer should be used only from the application's main thread`
     * ——FATAL，整个进程带走。（2026-10-03 在 vivo V2309A / Android 16 上就是这么崩的，
     * 三次同一处。模拟器上不复现，因为它根本没有语音引擎，走不到那一行。）
     */
    private void onMain(Runnable work) {
        main.post(work);
    }

    private void begin(PluginCall call) {
        if (!SpeechRecognizer.isRecognitionAvailable(getContext())) {
            call.reject("这台手机没有系统语音识别引擎");
            return;
        }
        transcript = "";
        listenError = "";
        releaseRecognizer();
        recognizer = SpeechRecognizer.createSpeechRecognizer(getContext());
        recognizer.setRecognitionListener(new Listener());

        Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        String wanted = call.getString("language", "zh-CN");
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, wanted);
        intent.putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1);
        // 不加这句，部分机型会先弹一个"请讲话"的系统对话框，把识别挡在门外。
        intent.putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, getContext().getPackageName());

        recognizer.startListening(intent);
        long maxMs = call.getInt("maxMs", 15000).longValue();
        watchdog = new Runnable() {
            @Override
            public void run() {
                finishListening();
            }
        };
        main.postDelayed(watchdog, maxMs);
        call.resolve(new JSObject().put("ok", true).put("error", ""));
    }

    @PluginMethod
    public void listenStop(PluginCall call) {
        if (recognizer == null) {
            JSObject empty = new JSObject();
            empty.put("ok", false);
            empty.put("transcript", "");
            empty.put("error", "没有正在进行的录音");
            call.resolve(empty);
            return;
        }
        // 结果可能比松手晚一点到：把这次调用挂住，等 onResults 来了再答。
        pendingStop = call;
        onMain(this::finishListening);
    }

    private void finishListening() {
        main.removeCallbacks(watchdog);
        if (recognizer != null) {
            recognizer.stopListening();
        }
    }

    private void releaseRecognizer() {
        if (watchdog != null) {
            main.removeCallbacks(watchdog);
            watchdog = null;
        }
        if (recognizer != null) {
            try {
                recognizer.cancel();
                recognizer.destroy();
            } catch (Exception ignored) {
                // 释放失败也不能把调用方卡住：这句是清理路径，没有值得上报的东西。
            }
            recognizer = null;
        }
    }

    private void deliver(String text, String error) {
        PluginCall call = pendingStop;
        pendingStop = null;
        if (call == null) {
            return;
        }
        JSObject result = new JSObject();
        boolean ok = error.isEmpty();
        result.put("ok", ok);
        result.put("transcript", text);
        result.put("error", error);
        call.resolve(result);
    }

    /** 错误码翻成人话。用户看到的必须是这一句，不是 error=-1 这种天书。 */
    private static String explain(int code) {
        switch (code) {
            case SpeechRecognizer.ERROR_AUDIO:
                return "录音失败：麦克风可能被别的应用占着";
            case SpeechRecognizer.ERROR_CLIENT:
                return "识别服务断开了（锁屏或系统把它回收了），再按一次";
            case SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS:
                return "没有麦克风权限，说了也不会被听到";
            case SpeechRecognizer.ERROR_NETWORK:
            case SpeechRecognizer.ERROR_NETWORK_TIMEOUT:
                return "系统识别要联网，这台手机此刻连不上网络";
            case SpeechRecognizer.ERROR_NO_MATCH:
                return "没听清，靠近一点再说一次";
            case SpeechRecognizer.ERROR_RECOGNIZER_BUSY:
                return "上一个识别还没结束，稍等一下再按";
            case SpeechRecognizer.ERROR_SPEECH_TIMEOUT:
                return "没听到声音";
            default:
                return "识别失败（错误码 " + code + "）";
        }
    }

    private class Listener implements RecognitionListener {
        @Override
        public void onReadyForSpeech(Bundle params) {
        }

        @Override
        public void onBeginningOfSpeech() {
        }

        @Override
        public void onRmsChanged(float rmsdB) {
        }

        @Override
        public void onBufferReceived(byte[] buffer) {
        }

        @Override
        public void onEndOfSpeech() {
        }

        @Override
        public void onError(int error) {
            listenError = explain(error);
            releaseRecognizer();
            deliver("", listenError);
        }

        @Override
        public void onResults(Bundle results) {
            ArrayList<String> rows =
                    results.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
            transcript = rows == null || rows.isEmpty() ? "" : rows.get(0);
            listenError = transcript.isEmpty() ? "没听到内容，再按一次说" : "";
            releaseRecognizer();
            deliver(transcript, listenError);
        }

        @Override
        public void onPartialResults(Bundle partialResults) {
        }

        @Override
        public void onEvent(int eventType, Bundle params) {
        }
    }

    @Override
    protected void handleOnDestroy() {
        releaseRecognizer();
        if (tts != null) {
            try {
                tts.stop();
                tts.shutdown();
            } catch (Exception ignored) {
                // 退出时引擎已经没了也没什么可报告的。
            }
            tts = null;
        }
        super.handleOnDestroy();
    }

    // -- 嘴：系统 TextToSpeech -------------------------------------------

    private TextToSpeech tts;
    private volatile boolean ttsReady;
    private String ttsError = "";
    private PluginCall pendingSpeak;
    private String pendingText = "";

    @PluginMethod
    public void speak(final PluginCall call) {
        final String text = call.getString("text", "");
        if (text.isEmpty()) {
            call.resolve(result(false, "没有要念的内容"));
            return;
        }
        // 和识别同一个理由：TextToSpeech 的构造也要主线程，在工作线程上建它同样会带走进程。
        onMain(() -> startSpeaking(call, text));
    }

    private void startSpeaking(final PluginCall call, final String text) {
        if (tts == null) {
            // 引擎初始化是异步的，第一次点"朗读"必须等它回来，否则按钮看起来就是坏的。
            pendingSpeak = call;
            pendingText = text;
            tts = new TextToSpeech(getContext(), new TextToSpeech.OnInitListener() {
                @Override
                public void onInit(int status) {
                    if (status == TextToSpeech.SUCCESS) {
                        int chinese = tts.setLanguage(Locale.CHINA);
                        ttsReady = chinese != TextToSpeech.LANG_MISSING_DATA
                                && chinese != TextToSpeech.LANG_NOT_SUPPORTED;
                        if (!ttsReady) {
                            ttsError = "这台手机的语音引擎没有中文包，她只能说不能读";
                        }
                    } else {
                        ttsError = "这台手机没有可用的语音合成引擎";
                    }
                    flushPending();
                }
            });
            return;
        }
        if (ttsReady) {
            utter(call, text);
            return;
        }
        call.resolve(result(false, ttsError.isEmpty() ? "语音引擎还没准备好，稍后再试" : ttsError));
    }

    private void flushPending() {
        PluginCall call = pendingSpeak;
        String text = pendingText;
        pendingSpeak = null;
        pendingText = "";
        if (call == null) {
            return;
        }
        if (ttsReady) {
            utter(call, text);
            return;
        }
        call.resolve(result(false, ttsError));
    }

    private static JSObject result(boolean ok, String error) {
        JSObject object = new JSObject();
        object.put("ok", ok);
        object.put("error", error);
        return object;
    }

    private void utter(PluginCall call, String text) {
        pendingSpeak = call;
        tts.setOnUtteranceProgressListener(new android.speech.tts.UtteranceProgressListener() {
            @Override
            public void onStart(String utteranceId) {
            }

            @Override
            public void onDone(String utteranceId) {
                PluginCall finished = pendingSpeak;
                pendingSpeak = null;
                if (finished != null) {
                    finished.resolve(result(true, ""));
                }
            }

            @Override
            public void onError(String utteranceId) {
                PluginCall failed = pendingSpeak;
                pendingSpeak = null;
                if (failed != null) {
                    failed.resolve(result(false, "念到一半断了（系统引擎的问题，字还在屏幕上）"));
                }
            }
        });
        int mode = tts.isSpeaking() ? TextToSpeech.QUEUE_ADD : TextToSpeech.QUEUE_FLUSH;
        tts.speak(text, mode, null, "xy-" + System.currentTimeMillis());
    }

    @PluginMethod
    public void stop(PluginCall call) {
        pendingSpeak = null;
        onMain(() -> {
            if (tts != null) {
                tts.stop();
            }
        });
        call.resolve(result(true, ""));
    }
}

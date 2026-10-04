package com.xiaoye.assistant;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;


import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.Iterator;
import java.util.Locale;
import java.util.Map;

import javax.net.ssl.HostnameVerifier;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLException;  // 握手失败会包成它抛出来，错误文案要用
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/**
 * 手机唯一的一条网络出口，两种信任方式：
 *
 * <ul>
 *   <li><b>没有 pin</b>（独立模式打模型 API）：走系统信任链，和任何 App 一样。
 *       端点有正经 CA 签发的证书，不需要也别去做任何特殊处理。</li>
 *   <li><b>带 pin</b>（遥控模式打电脑）：电脑给的是<b>自签证书</b>，系统一定不认。
 *       这里的做法是把对端证书的 SHA-256 和人在电脑屏幕上念给你那串比一下——
 *       <b>是指纹替代 CA，不是关掉校验</b>。关掉校验（一个信任所有证书的
 *       TrustManager）在"能连上就行"的诱惑下极易写成，而那条路等于把配对码和
 *       之后的每条命令都在网络上裸奔，任何人都能冒充你的电脑。</li>
 * </ul>
 *
 * 为什么自己写而不用现成的网络插件：CORS 之外还多一层——WebView 里的 {@code fetch}
 * 撞到自签证书只会得到一句 "Failed to fetch"，用户分不清是网不通还是证书不对，
 * 而这两种情况的处置完全不一样。这里把 HTTP 状态码和响应正文原样带回，错误码也翻译成人话。
 */
@CapacitorPlugin(name = "XyNet")
public class XyNetPlugin extends Plugin {

    private static final int DEFAULT_TIMEOUT_MS = 20_000;

    @PluginMethod
    public void request(PluginCall call) {
        String url = call.getString("url", "");
        String method = call.getString("method", "GET");
        String body = call.getString("body", "");
        String pin = normalize(call.getString("pin", ""));
        int timeout = value(call.getInt("timeoutMs", DEFAULT_TIMEOUT_MS));
        JSObject headers = call.getObject("headers");

        JSObject reply = new JSObject();
        if (url.isEmpty()) {
            reply.put("status", 0);
            reply.put("body", "");
            reply.put("error", "没有地址");
            call.resolve(reply);
            return;
        }
        if (!url.toLowerCase(Locale.ROOT).startsWith("https://")) {
            // 明文一律不发出去：小夜能做的事（执行命令、读磁盘）不该有任何一条走在没有加密的链路上。
            reply.put("status", 0);
            reply.put("body", "");
            reply.put("error", "只允许 https 地址");
            call.resolve(reply);
            return;
        }
        try {
            send(call, url, method, body, pin, timeout, headers);
        } catch (Exception exc) {
            reply.put("status", 0);
            reply.put("body", "");
            reply.put("error", explain(exc));
            call.resolve(reply);
        }
    }

    private static int value(Integer supplied) {
        return supplied == null || supplied <= 0 ? DEFAULT_TIMEOUT_MS : supplied;
    }

    // -- 流式：一段一段把正文吐给网页 --------------------------------------

    /** 事件名。JS 侧 `XyNet.addListener('xyNet', ...)` 收。 */
    private static final String STREAM_EVENT = "xyNet";

    /**
     * 当前这条流。`volatile` 是因为"停止"是从另一个线程打过来的。
     *
     * 为什么读循环必须自己起线程：Capacitor 把插件方法排在同一条工作线程上，
     * 如果 {@code stream()} 在那条线程上一路读到连接结束，
     * 用户在界面上按的"停止"就永远排在它后面——**按钮看着在，点了没反应**。
     * 这是"能中断"这件事最容易写错的一处：中断指令要能插进去，
     * 所以被中断的那个活不能占着处理指令的线程。
     */
    private volatile HttpURLConnection live;

    private volatile boolean cancelled;

    @PluginMethod
    public void stream(final PluginCall call) {
        final String url = call.getString("url", "");
        final String method = call.getString("method", "POST");
        final String body = call.getString("body", "");
        final String pin = normalize(call.getString("pin", ""));
        final int timeout = value(call.getInt("timeoutMs", DEFAULT_TIMEOUT_MS));
        final JSObject headers = call.getObject("headers");
        if (url.isEmpty() || !url.toLowerCase(Locale.ROOT).startsWith("https://")) {
            // 和 request() 同一条规矩：明文一律不发出去。
            call.resolve(wire(0, "", url.isEmpty() ? "没有地址" : "只允许 https 地址"));
            return;
        }
        cancelled = false;
        Thread worker = new Thread(new Runnable() {
            @Override
            public void run() {
                pump(call, url, method, body, pin, timeout, headers);
            }
        }, "xy-net-stream");
        worker.setDaemon(true);
        worker.start();
    }

    /** 用户按了"不听了"：断开连接，让 pump 那边带着"已停止"收尾。 */
    @PluginMethod
    public void cancelStream(PluginCall call) {
        cancelled = true;
        HttpURLConnection connection = live;
        if (connection != null) {
            try {
                connection.disconnect();
            } catch (Exception ignored) {
                // 已经断了。这里没有值得上报的东西，但也不能让"停止"这件事失败。
            }
        }
        JSObject result = new JSObject();
        result.put("ok", true);
        call.resolve(result);
    }

    private void pump(
            PluginCall call,
            String url,
            String method,
            String body,
            String pin,
            int timeout,
            JSObject headers
    ) {
        HttpURLConnection connection = null;
        try {
            connection = open(url, method, body, pin, timeout, headers);
            live = connection;
            int status = connection.getResponseCode();
            InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
            if (stream == null) {
                call.resolve(wire(status, "", status >= 400 ? "" : "对方一个字都没回"));
                return;
            }
            Utf8Stream decoder = new Utf8Stream();
            StringBuilder buffered = new StringBuilder();
            byte[] chunk = new byte[4096];
            int got;
            while (!cancelled && (got = stream.read(chunk)) > 0) {
                String text = decoder.feed(chunk, got);
                if (text.isEmpty()) {
                    continue;
                }
                if (status >= 400) {
                    // 错误响应不流式：整块攒着一次交回。半截错误页对排查没有任何帮助，
                    // 而上层要的就是"状态码 + 原文"这两样。
                    buffered.append(text);
                    continue;
                }
                notifyListeners(STREAM_EVENT, new JSObject().put("kind", "chunk").put("text", text));
            }
            stream.close();
            if (cancelled) {
                call.resolve(wire(status, "", ""));
                notifyListeners(STREAM_EVENT, new JSObject().put("kind", "end").put("stopped", true));
                return;
            }
            if (status >= 400) {
                call.resolve(wire(status, buffered.toString(), ""));
                return;
            }
            // 连接收尾时若还压着半个字（对方在字中间断线），把它吐出去。
            // 宁可露一个断字，也不要不吭声地少一句。
            String dangling = decoder.rest();
            if (!dangling.isEmpty()) {
                notifyListeners(STREAM_EVENT, new JSObject().put("kind", "chunk").put("text", dangling));
            }
            call.resolve(wire(status, "", ""));
            notifyListeners(STREAM_EVENT, new JSObject().put("kind", "end"));
        } catch (Exception exc) {
            String reason = cancelled ? "" : explain(exc);
            JSObject done = new JSObject().put("kind", "end");
            if (cancelled) {
                done.put("stopped", true);
            } else {
                done.put("error", reason);
            }
            notifyListeners(STREAM_EVENT, done);
            call.resolve(wire(0, "", reason));
        } finally {
            live = null;
            if (connection != null) {
                try {
                    connection.disconnect();
                } catch (Exception ignored) {
                    // 收尾用的，失败没有可报的。
                }
            }
        }
    }

    /**
     * 允许**一个汉字被切成两半**的 UTF-8 解码器。
     *
     * 中文在 UTF-8 里占三个字节，而网络块的边界不认字：一次 read 完全可能停在半个字上。
     * 直接 {@code new String(bytes, UTF_8)} 会把那半个字变成 ，
     * 更糟的是有些实现会把后续字节一起带偏——表现成"答案里偶发乱码，之后整段都歪"。
     * 所以不完整的尾巴留在这里，下一块接上再解。
     */
    private static final class Utf8Stream {
        private byte[] pending = new byte[0];

        String feed(byte[] chunk, int length) {
            byte[] joined = concat(pending, chunk, length);
            int usable = completeLength(joined);
            pending = usable == joined.length
                    ? new byte[0]
                    : java.util.Arrays.copyOfRange(joined, usable, joined.length);
            return new String(joined, 0, usable, java.nio.charset.StandardCharsets.UTF_8);
        }

        /** 流结束时还剩什么就交什么：宁可露出一个断字，也不要把内容吞掉。 */
        String rest() {
            String tail = new String(pending, java.nio.charset.StandardCharsets.UTF_8);
            pending = new byte[0];
            return tail;
        }

        private static byte[] concat(byte[] head, byte[] chunk, int length) {
            byte[] joined = new byte[head.length + length];
            System.arraycopy(head, 0, joined, 0, head.length);
            System.arraycopy(chunk, 0, joined, head.length, length);
            return joined;
        }

        /** 数出末尾**不是**半个字符的最长前缀。一个 UTF-8 序列最多四个字节，所以只看尾巴四个。 */
        private static int completeLength(byte[] bytes) {
            for (int back = 1; back <= Math.min(4, bytes.length); back++) {
                int probe = bytes.length - back;
                byte value = bytes[probe];
                boolean startsSequence = back == 1 || (bytes[probe + 1] & 0xC0) != 0x80;
                if (!startsSequence) {
                    continue;
                }
                int need = (value & 0x80) == 0 ? 1 : (value & 0xE0) == 0xC0 ? 2 : (value & 0xF0) == 0xE0 ? 3 : 4;
                return bytes.length - probe >= need ? bytes.length : probe;
            }
            return bytes.length;
        }
    }

    private static JSObject wire(int status, String body, String error) {
        JSObject reply = new JSObject();
        reply.put("status", status);
        reply.put("body", body);
        reply.put("error", error);
        return reply;
    }

    private HttpURLConnection open(
            String url,
            String method,
            String body,
            String pin,
            int timeout,
            JSObject headers
    ) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        if (connection instanceof HttpsURLConnection) {
            HttpsURLConnection secure = (HttpsURLConnection) connection;
            if (!pin.isEmpty()) {
                SSLContext context = SSLContext.getInstance("TLS");
                context.init(null, new TrustManager[]{new Pinned(pin)}, new java.security.SecureRandom());
                secure.setSSLSocketFactory(context.getSocketFactory());
                secure.setHostnameVerifier(anyHost());
            }
        }
        connection.setConnectTimeout(timeout);
        // 流式的读超时是"两帧之间最多等多久"，不是整条回答的总时长：
        // 模型想得久，但只要在持续吐字，就不该被这一项掐掉。
        connection.setReadTimeout(Math.max(timeout, 60_000));
        connection.setRequestMethod(method);
        connection.setRequestProperty("Accept", "text/event-stream, application/json");
        if (headers != null) {
            Iterator<String> keys = headers.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                connection.setRequestProperty(key, headers.optString(key, ""));
            }
        }
        if (!body.isEmpty()) {
            connection.setDoOutput(true);
            try (OutputStream out = connection.getOutputStream()) {
                out.write(body.getBytes("UTF-8"));
            }
        }
        return connection;
    }

    private void send(
            PluginCall call,
            String url,
            String method,
            String body,
            String pin,
            int timeout,
            JSObject headers
    ) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        if (connection instanceof HttpsURLConnection) {
            HttpsURLConnection secure = (HttpsURLConnection) connection;
            if (!pin.isEmpty()) {
                SSLContext context = SSLContext.getInstance("TLS");
                context.init(null, new TrustManager[]{new Pinned(pin)}, new java.security.SecureRandom());
                secure.setSSLSocketFactory(context.getSocketFactory());
                // 局域网地址基本没有反向 DNS，主机名核对不了；身份由上面那一步的指纹决定。
                // 这只影响带 pin 的自签链路，走系统信任链的那条（模型 API）仍然照常核对主机名。
                secure.setHostnameVerifier(anyHost());
            }
        }
        connection.setConnectTimeout(timeout);
        connection.setReadTimeout(timeout);
        connection.setRequestMethod(method);
        connection.setRequestProperty("Accept", "application/json");
        if (headers != null) {
            Iterator<String> keys = headers.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                connection.setRequestProperty(key, headers.optString(key, ""));
            }
        }
        if (!body.isEmpty()) {
            connection.setDoOutput(true);
            try (OutputStream out = connection.getOutputStream()) {
                out.write(body.getBytes("UTF-8"));
            }
        }

        int status = connection.getResponseCode();
        InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
        String text = stream == null ? "" : read(stream);
        connection.disconnect();

        JSObject reply = new JSObject();
        reply.put("status", status);
        reply.put("body", text);
        reply.put("error", "");
        call.resolve(reply);
    }

    private static String read(InputStream stream) throws java.io.IOException {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        byte[] chunk = new byte[8192];
        int got;
        while ((got = stream.read(chunk)) > 0) {
            buffer.write(chunk, 0, got);
        }
        stream.close();
        return buffer.toString("UTF-8");
    }

    private static String normalize(String pin) {
        String trimmed = pin == null ? "" : pin.trim().toLowerCase(Locale.ROOT);
        return trimmed.startsWith("sha256:") ? trimmed.substring("sha256:".length()) : trimmed;
    }

    private static HostnameVerifier anyHost() {
        return new HostnameVerifier() {
            @Override
            public boolean verify(String hostname, javax.net.ssl.SSLSession session) {
                return true;
            }
        };
    }

    private static String explain(Exception exc) {
        String message = exc.getMessage() == null ? exc.getClass().getSimpleName() : exc.getMessage();
        if (exc instanceof SSLException) {
            return "证书对不上：手机上填的指纹和电脑屏幕上的不是同一串（" + message + "）";
        }
        if (exc instanceof java.net.SocketTimeoutException) {
            return "连上了但没回话：可能被防火墙挡了，或电脑已经从那个 Wi-Fi 走开";
        }
        if (exc instanceof java.net.ConnectException || exc instanceof java.net.UnknownHostException) {
            return "连不上这台电脑：确认手机和它连的是同一个 Wi-Fi，且「手机接入」是开着的";
        }
        return message;
    }

    /**
     * 只认这一枚指纹的证书。
     *
     * 仍然是"链状"校验之外的独立判断：不做 CA 链、不做有效期之外的检查——
     * 有效期由电脑端那 824 天的自签证书决定，Android 对网络证书本来就要求不超过 825 天。
     */
    private static final class Pinned implements X509TrustManager {
        private final String expected;

        Pinned(String expected) {
            this.expected = expected;
        }

        @Override
        public void checkClientTrusted(X509Certificate[] chain, String authType)
                throws CertificateException {
            throw new CertificateException("这个客户端不接受双向证书");
        }

        @Override
        public void checkServerTrusted(X509Certificate[] chain, String authType)
                throws CertificateException {
            if (chain == null || chain.length == 0) {
                throw new CertificateException("对方没有交出证书");
            }
            try {
                byte[] digest = MessageDigest.getInstance("SHA-256").digest(chain[0].getEncoded());
                String actual = hex(digest);
                if (!actual.equals(expected)) {
                    throw new CertificateException("期望 sha256:" + expected + ", 实际 sha256:" + actual);
                }
            } catch (java.security.NoSuchAlgorithmException exc) {
                throw new CertificateException("这台手机没有 SHA-256: " + exc.getMessage());
            }
        }

        @Override
        public X509Certificate[] getAcceptedIssuers() {
            return new X509Certificate[0];
        }

        private static String hex(byte[] bytes) {
            StringBuilder builder = new StringBuilder(bytes.length * 2);
            for (byte each : bytes) {
                builder.append(String.format("%02x", each));
            }
            return builder.toString();
        }
    }
}

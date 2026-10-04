package com.xiaoye.assistant;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.ActivityNotFoundException;
import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraManager;
import android.media.AudioManager;
import android.net.Uri;
import android.os.BatteryManager;
import android.os.Build;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.VibratorManager;
import android.provider.CalendarContract;
import android.provider.MediaStore;
import android.provider.Settings;
import android.util.Base64;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 手机的手：AI 说"把手电筒打开"，真正去按那个开关的是这里。
 *
 * 一个能动的助手必须让人看得见它能动什么，所以这个插件里**没有一条静默通路**：
 * 1) 每个方法都只做一件小事，参数是标量，不接受"帮我做完这一串"这种复合指令；
 * 2) 要出这台手机、要花真钱的（拨号、发短信、写日历、打开链接、定闹钟），
 *    这里只把系统自带的界面**打开到用户手边**（ACTION_DIAL / ACTION_SENDTO /
 *    ACTION_INSERT / setAlarmClock 会显示在时钟 App 里），最后那一下永远是人按的。
 *    所以这些动作不需要"应用内确认框"来兜底 —— 系统确认框就是兜底；
 * 3) 反过来，只影响这台手机自己、随时可撤销的（手电筒、音量、震动、设置页），
 *    直接执行，但每一次都回一条结果给界面，界面把每一笔写进"最近动作"台账。
 *
 * 为什么没有"读短信/读通讯录/代指点别的 App"：那要无障碍服务或一批危险权限，
 * 装完等于把整台手机交出去，而用户不会为一次"帮我定个闹钟"预期这种代价。
 * 这条边界写在 `actions.ts` 的注册表里，也写在这里的注释里，两边都不许偷偷扩。
 */
@CapacitorPlugin(
        name = "XyAction",
        permissions = {
                @Permission(strings = {android.Manifest.permission.CAMERA}, alias = XyActionPlugin.CAMERA),
                @Permission(strings = {"android.permission.READ_MEDIA_IMAGES"}, alias = "mediaNew"),
                @Permission(strings = {"android.permission.READ_EXTERNAL_STORAGE"}, alias = "mediaOld"),
                @Permission(strings = {"android.permission.READ_MEDIA_VISUAL_USER_SELECTED"}, alias = "mediaSelected")
        }
)
public class XyActionPlugin extends Plugin {

    static final String CAMERA = "camera";

    private static final String SETTINGS_PAGES =
            "wlan|bluetooth|location|sound|display|app|notification|battery|airplane|date|accessibility|tts";

    @PluginMethod
    public void capability(PluginCall call) {
        Context context = getContext();
        JSObject result = new JSObject();
        result.put("torch", hasFlash(context));
        result.put("vibrate", context.getSystemService(Vibrator.class) != null);
        result.put("dial", canHandle(dialIntent("1")));
        result.put("sms", canHandle(sendToIntent("1")));
        result.put("calendar", canHandle(calendarIntent("探测", 0L)));
        result.put("browser", canHandle(viewIntent("https://example.com")));
        // 能不能列出发得起的应用，取决于 <queries> 有没有写；没写的话这里查到的是空表，
        // 界面就会老实说"这台设备上她叫不动别的应用"，而不是点了没反应。
        result.put("launcher", launchable().isEmpty() ? false : true);
        result.put("alarm", context.getSystemService(AlarmManager.class) != null);
        call.resolve(result);
    }

    // -- 只读，不需要人点头 ------------------------------------------------

    @PluginMethod
    public void battery(PluginCall call) {
        Context context = getContext();
        Intent status = registerStickyBattery(context);
        JSObject result = new JSObject();
        if (status == null) {
            result.put("ok", false);
            result.put("error", "这台设备读不到电量信息");
            call.resolve(result);
            return;
        }
        int level = status.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
        int scale = status.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
        int plugged = status.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0);
        result.put("ok", true);
        result.put("percent", scale > 0 ? Math.round(100f * level / scale) : -1);
        result.put("charging", plugged != 0);
        call.resolve(result);
    }

    @PluginMethod
    public void volume(PluginCall call) {
        AudioManager audio = audio();
        JSObject result = new JSObject();
        int max = audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
        int now = audio.getStreamVolume(AudioManager.STREAM_MUSIC);
        result.put("ok", true);
        result.put("percent", max <= 0 ? 0 : Math.round(100f * now / max));
        result.put("max", max);
        call.resolve(result);
    }

    @PluginMethod
    public void setVolume(PluginCall call) {
        Integer percent = readInt(call, "percent");
        if (percent == null) {
            call.resolve(fail("要说音量调到百分之多少"));
            return;
        }
        AudioManager audio = audio();
        int max = audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
        int target = Math.round(Math.max(0, Math.min(100, percent)) / 100f * max);
        audio.setStreamVolume(AudioManager.STREAM_MUSIC, target, 0);
        JSObject result = new JSObject();
        result.put("ok", true);
        result.put("percent", percent);
        call.resolve(result);
    }

    @PluginMethod
    public void vibrate(PluginCall call) {
        Integer ms = readInt(call, "ms");
        long duration = ms == null ? 220L : Math.max(20L, Math.min(2000L, ms.longValue()));
        Vibrator vibrator = vibrator();
        JSObject result = new JSObject();
        if (vibrator == null || !vibrator.hasVibrator()) {
            result.put("ok", false);
            result.put("error", "这台设备没有震动马达");
            call.resolve(result);
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            vibrator.vibrate(VibrationEffect.createOneShot(duration, VibrationEffect.DEFAULT_AMPLITUDE));
        } else {
            vibrator.vibrate(duration);
        }
        result.put("ok", true);
        result.put("ms", duration);
        call.resolve(result);
    }

    @PluginMethod
    public void torch(final PluginCall call) {
        Boolean on = call.getBoolean("on");
        if (on == null) {
            call.resolve(fail("要说开还是关"));
            return;
        }
        if (!hasFlash(getContext())) {
            call.resolve(fail("这台设备没有闪光灯，手电筒做不了"));
            return;
        }
        // CAMERA 是运行时权限：setTorchMode 名义上要的是它。先要，再干活，
        // 被拒了就把原因原样报回去，不静默失败。
        if (getPermissionState(CAMERA) != PermissionState.GRANTED) {
            pendingTorch = call;
            requestPermissionForAlias(CAMERA, call, "torchPermission");
            return;
        }
        applyTorch(call, on);
    }

    private PluginCall pendingTorch;

    @PermissionCallback
    void torchPermission(PluginCall call) {
        PluginCall stored = pendingTorch;
        pendingTorch = null;
        if (stored == null) {
            return;
        }
        if (getPermissionState(CAMERA) != PermissionState.GRANTED) {
            stored.resolve(fail("没给相机权限，手电筒开不了（手电筒用的是闪光灯那颗灯）"));
            return;
        }
        Boolean on = stored.getBoolean("on");
        applyTorch(stored, Boolean.TRUE.equals(on));
    }

    private void applyTorch(PluginCall call, boolean on) {
        JSObject result = new JSObject();
        try {
            CameraManager manager = (CameraManager) getContext().getSystemService(Context.CAMERA_SERVICE);
            String id = firstBackCamera(manager);
            if (id == null) {
                result.put("ok", false);
                result.put("error", "找不到能控制闪光灯的那个摄像头");
                call.resolve(result);
                return;
            }
            manager.setTorchMode(id, on);
            result.put("ok", true);
            result.put("on", on);
        } catch (Exception error) {
            result.put("ok", false);
            result.put("error", "闪光灯没响应：" + describe(error));
        }
        call.resolve(result);
    }

    // -- 把人送到那一下前面（系统界面接手最后一步） --------------------------

    @PluginMethod
    public void setAlarm(PluginCall call) {
        Integer hour = readInt(call, "hour");
        Integer minute = readInt(call, "minute");
        String label = call.getString("label", "小夜定的闹钟");
        if (hour == null || minute == null || hour < 0 || hour > 23 || minute < 0 || minute > 59) {
            call.resolve(fail("要说清楚几点几分（24 小时制）"));
            return;
        }
        AlarmManager manager = (AlarmManager) getContext().getSystemService(Context.ALARM_SERVICE);
        if (manager == null) {
            call.resolve(fail("这台设备没有闹钟服务"));
            return;
        }
        java.util.Calendar at = java.util.Calendar.getInstance();
        at.set(java.util.Calendar.HOUR_OF_DAY, hour);
        at.set(java.util.Calendar.MINUTE, minute);
        at.set(java.util.Calendar.SECOND, 0);
        if (at.getTimeInMillis() <= System.currentTimeMillis()) {
            at.add(java.util.Calendar.DAY_OF_YEAR, 1);
        }
        JSObject result = new JSObject();
        try {
            Intent show = new Intent(getContext(), MainActivity.class);
            PendingIntent visibility = PendingIntent.getActivity(
                    getContext(), 0, show, flagImmutable(PendingIntent.FLAG_CANCEL_CURRENT));
            Intent fire = new Intent(getContext(), MainActivity.class);
            PendingIntent trigger = PendingIntent.getActivity(
                    getContext(), 1, fire, flagImmutable(PendingIntent.FLAG_UPDATE_CURRENT));
            manager.setAlarmClock(new AlarmManager.AlarmClockInfo(at.getTimeInMillis(), visibility), trigger);
            result.put("ok", true);
            result.put("at", String.format(Locale.CHINA, "%02d:%02d", hour, minute));
            result.put("label", label);
            // 不再跳去时钟 App：setAlarmClock 定上之后状态栏就有那枚闹钟图标，
            // 那已经是"她真去按了"的凭据，多跳一次只是把人从对话里拽走。
        } catch (Exception error) {
            result.put("ok", false);
            result.put("error", "闹钟没定上：" + describe(error));
        }
        call.resolve(result);
    }

    @PluginMethod
    public void dial(PluginCall call) {
        String number = digits(call.getString("number", ""));
        if (number.isEmpty()) {
            call.resolve(fail("没有可拨的号码"));
            return;
        }
        JSObject result = new JSObject();
        boolean opened = startActivity(dialIntent(number), result);
        result.put("ok", opened);
        result.put("note", opened ? "拨号盘已经打开，按拨号键的那一下是你按的" : result.optString("error"));
        call.resolve(result);
    }

    @PluginMethod
    public void composeSms(PluginCall call) {
        String number = digits(call.getString("number", ""));
        String body = call.getString("body", "");
        JSObject result = new JSObject();
        boolean opened = startActivity(sendToIntent(number, body), result);
        result.put("ok", opened);
        result.put("note", opened ? "短信草稿已经填好，发送键在你手上" : result.optString("error"));
        call.resolve(result);
    }

    @PluginMethod
    public void addCalendar(PluginCall call) {
        String title = call.getString("title", "");
        Integer inMinutes = readInt(call, "inMinutes");
        Integer spanMinutes = readInt(call, "spanMinutes");
        JSObject result = new JSObject();
        if (title.trim().isEmpty()) {
            result.put("ok", false);
            result.put("error", "日程总得有件事");
            call.resolve(result);
            return;
        }
        long start = System.currentTimeMillis() + Math.max(0, inMinutes == null ? 60 : inMinutes) * 60_000L;
        long end = start + Math.max(5, spanMinutes == null ? 60 : spanMinutes) * 60_000L;
        boolean opened = startActivity(calendarIntent(title, start, end), result);
        result.put("ok", opened);
        result.put("note", opened ? "日历的新建页已经填好，存不存由你点" : result.optString("error"));
        call.resolve(result);
    }

    @PluginMethod
    public void openUrl(PluginCall call) {
        String url = call.getString("url", "").trim();
        JSObject result = new JSObject();
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            result.put("ok", false);
            result.put("error", "只开 http/https 的链接");
            call.resolve(result);
            return;
        }
        boolean opened = startActivity(viewIntent(url), result);
        result.put("ok", opened);
        call.resolve(result);
    }

    /**
     * 打开系统设置的某一页。
     *
     * 白名单是硬编码的：`openSettings("…")` 收的是**页名**不是 Intent 字符串，
     * 所以模型没法拼出一个它本不该去的地方（比如无障碍服务那一页的开关参数）。
     */
    @PluginMethod
    public void openSettings(PluginCall call) {
        String page = call.getString("page", "").trim().toLowerCase(Locale.ROOT);
        JSObject result = new JSObject();
        String action = settingsAction(page);
        if (action == null) {
            result.put("ok", false);
            result.put("error", "没有这一页设置。可选：" + SETTINGS_PAGES.replace('|', '、'));
            call.resolve(result);
            return;
        }
        Intent intent = new Intent(action);
        if (Settings.ACTION_APPLICATION_DETAILS_SETTINGS.equals(action)) {
            intent.setData(Uri.parse("package:" + getContext().getPackageName()));
        }
        if (Settings.ACTION_APP_NOTIFICATION_SETTINGS.equals(action)) {
            intent.putExtra(Settings.EXTRA_APP_PACKAGE, getContext().getPackageName());
        }
        result.put("ok", startActivity(intent, result));
        call.resolve(result);
    }

    /**
     * 按名字叫起一个已安装的应用。
     *
     * 名字是模糊匹配的（去掉空格、忽略大小写、包含即算），因为用户说的是"微信"而
     * 系统里的标签可能叫"微信"也可能带角标后缀。匹配不到时把候选报回去，
     * 而不是随便挑一个看起来像的 —— 开错应用比开不了更难解释。
     */
    @PluginMethod
    public void launchApp(PluginCall call) {
        String wanted = normalize(call.getString("name", ""));
        JSObject result = new JSObject();
        if (wanted.isEmpty()) {
            result.put("ok", false);
            result.put("error", "要说打开哪个应用");
            call.resolve(result);
            return;
        }
        List<ResolveInfo> all = launchable();
        List<String> exact = new ArrayList<>();
        List<String> partial = new ArrayList<>();
        for (ResolveInfo info : all) {
            String label = normalize(labelOf(info));
            if (label.isEmpty()) {
                continue;
            }
            if (label.equals(wanted)) {
                exact.add(label);
            } else if (label.contains(wanted) || wanted.contains(label)) {
                partial.add(label);
            }
        }
        String pick = exact.isEmpty() ? (partial.size() == 1 ? partial.get(0) : null) : exact.get(0);
        if (pick == null) {
            List<String> near = new ArrayList<>();
            int room = 0;
            for (String name : partial) {
                if (room++ >= 6) {
                    break;
                }
                near.add(name);
            }
            result.put("ok", false);
            JSArray candidates = new JSArray();
            for (String name : near) {
                candidates.put(name);
            }
            result.put("candidates", candidates);
            result.put("error", near.isEmpty()
                    ? "叫不动这个应用（这台设备可能没装，或者不允许被列出来）"
                    : "不止一个像的，说准一点：" + String.join("、", near));
            call.resolve(result);
            return;
        }
        for (ResolveInfo info : all) {
            if (!normalize(labelOf(info)).equals(pick)) {
                continue;
            }
            Intent launch = info.activityInfo == null
                    ? null
                    : getContext().getPackageManager().getLaunchIntentForPackage(info.activityInfo.packageName);
            if (launch != null && startActivity(launch, result)) {
                result.put("ok", true);
                result.put("name", labelOf(info));
                call.resolve(result);
                return;
            }
        }
        result.put("ok", false);
        result.put("error", "找到了 " + pick + " 但起不来");
        call.resolve(result);
    }

    @PluginMethod
    public void listApps(PluginCall call) {
        JSObject result = new JSObject();
        JSArray names = new JSArray();
        int room = 0;
        for (ResolveInfo info : launchable()) {
            if (room++ >= 80) {
                break;
            }
            String label = labelOf(info);
            if (!label.isEmpty()) {
                names.put(label);
            }
        }
        result.put("ok", true);
        result.put("apps", names);
        result.put("truncated", room >= 80);
        call.resolve(result);
    }

    // -- 相册：她得先看得见，才谈得上"这张是什么" --------------------------

    /**
     * 要媒体权限。Android 13 起是 `READ_MEDIA_IMAGES`，13 以下是老的
     * `READ_EXTERNAL_STORAGE`，14 还有个"只给选中的那些"的 `READ_MEDIA_VISUAL_USER_SELECTED`。
     * 三个都声明、按版本挑一个去要——只声明老的那个，在 Android 16 上会永远拿不到。
     */
    @PluginMethod
    public void requestMedia(PluginCall call) {
        if (hasMediaAccess()) {
            call.resolve(granted());
            return;
        }
        requestPermissionForAlias(mediaAlias(), call, "mediaGranted");
    }

    @PermissionCallback
    void mediaGranted(PluginCall call) {
        call.resolve(hasMediaAccess() ? granted() : fail("没给相册权限，她看不到你的照片"));
    }

    /** 最近的照片列表（倒序）。只回 uri + 时间 + 尺寸，不回内容。 */
    @PluginMethod
    public void listPhotos(PluginCall call) {
        JSObject result = new JSObject();
        if (!hasMediaAccess()) {
            result.put("ok", false);
            result.put("error", "还没给相册权限");
            result.put("granted", false);
            call.resolve(result);
            return;
        }
        int limit = Math.max(1, Math.min(60, call.getInt("limit", 20)));
        JSArray rows = new JSArray();
        ContentResolver resolver = getContext().getContentResolver();
        String[] columns = {
                MediaStore.Images.Media._ID,
                MediaStore.Images.Media.DISPLAY_NAME,
                MediaStore.Images.Media.WIDTH,
                MediaStore.Images.Media.HEIGHT,
                MediaStore.Images.Media.DATE_MODIFIED,
        };
        try (android.database.Cursor cursor = resolver.query(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                columns,
                null,
                null,
                MediaStore.Images.Media.DATE_MODIFIED + " DESC")) {
            if (cursor != null) {
                int idColumn = cursor.getColumnIndexOrThrow(MediaStore.Images.Media._ID);
                int nameColumn = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.DISPLAY_NAME);
                int widthColumn = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.WIDTH);
                int heightColumn = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.HEIGHT);
                int timeColumn = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.DATE_MODIFIED);
                // 条数在 Java 这边截，不写进排序串：MediaStore 的 query 会把
                // "… DESC LIMIT 20" 整段当 ORDER BY 交给 SQLite，报 `Invalid token LIMIT`
                // （2026-10-03 在 vivo V2309A 上真撞到，屏幕上就是这句错误）。
                int taken = 0;
                while (taken < limit && cursor.moveToNext()) {
                    long id = cursor.getLong(idColumn);
                    Uri uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id);
                    JSObject row = new JSObject();
                    row.put("uri", uri.toString());
                    row.put("name", cursor.getString(nameColumn));
                    row.put("width", cursor.getInt(widthColumn));
                    row.put("height", cursor.getInt(heightColumn));
                    row.put("modified", cursor.getLong(timeColumn) * 1000L);
                    rows.put(row);
                    taken += 1;
                }
            }
        } catch (Exception error) {
            result.put("ok", false);
            result.put("error", "翻不了相册：" + describe(error));
            call.resolve(result);
            return;
        }
        result.put("ok", true);
        result.put("photos", rows);
        result.put("granted", true);
        call.resolve(result);
    }

    /**
     * 读一张照片，压成能发给模型的 data URL。
     *
     * 必须压：相册里随手一张就是 4000×3000 / 6~10 MB，原样 base64 过桥会把 WebView
     * 直接撑爆（这是"聊着聊着闪退"的另一类来源），而模型也收不下那么大一个 body。
     * 降到 1280 长边 + JPEG 0.8，一般 200~400 KB，看得清字也不炸。
     */
    @PluginMethod
    public void readPhoto(PluginCall call) {
        JSObject result = new JSObject();
        if (!hasMediaAccess()) {
            result.put("ok", false);
            result.put("error", "还没给相册权限");
            call.resolve(result);
            return;
        }
        String raw = call.getString("uri", "");
        if (raw.isEmpty()) {
            result.put("ok", false);
            result.put("error", "要读哪一张？");
            call.resolve(result);
            return;
        }
        try {
            Uri uri = Uri.parse(raw);
            byte[] small = downscale(uri, Math.max(256, Math.min(1600, call.getInt("longest", 1280))));
            if (small == null) {
                result.put("ok", false);
                result.put("error", "这张读不出来（可能是云相册里还没下载到本地的图）");
                call.resolve(result);
                return;
            }
            result.put("ok", true);
            result.put("data", "data:image/jpeg;base64," + Base64.encodeToString(small, Base64.NO_WRAP));
            result.put("bytes", small.length);
        } catch (Exception error) {
            result.put("ok", false);
            result.put("error", "这张读不出来：" + describe(error));
        }
        call.resolve(result);
    }

    /** 两段式解码：先只量尺寸挑采样率，再按那个采样率真解，避免整张原图进内存。 */
    private byte[] downscale(Uri uri, int longest) throws Exception {
        android.graphics.BitmapFactory.Options bounds = new android.graphics.BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        try (java.io.InputStream probe = getContext().getContentResolver().openInputStream(uri)) {
            android.graphics.BitmapFactory.decodeStream(probe, null, bounds);
        }
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) {
            return null;
        }
        android.graphics.BitmapFactory.Options opts = new android.graphics.BitmapFactory.Options();
        opts.inSampleSize = 1;
        int side = Math.max(bounds.outWidth, bounds.outHeight);
        while (side / (opts.inSampleSize * 2) >= longest) {
            opts.inSampleSize *= 2;
        }
        android.graphics.Bitmap full;
        try (java.io.InputStream input = getContext().getContentResolver().openInputStream(uri)) {
            full = android.graphics.BitmapFactory.decodeStream(input, null, opts);
        }
        if (full == null) {
            return null;
        }
        android.graphics.Bitmap small = full;
        int max = Math.max(full.getWidth(), full.getHeight());
        if (max > longest) {
            float scale = (float) longest / max;
            small = android.graphics.Bitmap.createScaledBitmap(
                    full,
                    Math.max(1, Math.round(full.getWidth() * scale)),
                    Math.max(1, Math.round(full.getHeight() * scale)),
                    true);
        }
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        small.compress(android.graphics.Bitmap.CompressFormat.JPEG, 80, out);
        if (small != full) {
            small.recycle();
        }
        full.recycle();
        return out.toByteArray();
    }

    private boolean hasMediaAccess() {
        return getPermissionState(mediaAlias()) == PermissionState.GRANTED
                || getPermissionState("mediaSelected") == PermissionState.GRANTED;
    }

    private String mediaAlias() {
        return Build.VERSION.SDK_INT >= 33 ? "mediaNew" : "mediaOld";
    }

    private JSObject granted() {
        JSObject object = new JSObject();
        object.put("ok", true);
        object.put("granted", true);
        return object;
    }

    // -- 把话递到聊天窗口前面（微信这条路只到这里为止） ---------------------

    /**
     * 用系统的"分享"把一段文字交到微信（或任何指定应用）手上。
     *
     * 这是**能做的极限**，再往前一步就要无障碍服务：让本应用替你在微信里选人、
     * 把字敲进输入框、按下发送。那条路的代价不对等——无障碍服务能读能点**整台手机
     * 所有应用**的界面（银行、微信支付、12306 都在里面），而对面是一个会被一句话
     * 带动的模型。所以最后那一下必须是人按的。
     *
     * 走 `ACTION_SEND` + `setPackage`：微信会自己弹出"发送给…"的联系人选择，
     * 选谁、发不发都在那里由人决定。不需要任何危险权限，也不违反微信的使用条款。
     */
    @PluginMethod
    public void shareText(PluginCall call) {
        String text = call.getString("text", "");
        String target = call.getString("package", "");
        JSObject result = new JSObject();
        if (text.trim().isEmpty()) {
            result.put("ok", false);
            result.put("error", "要发出去的那句话是空的");
            call.resolve(result);
            return;
        }
        Intent send = new Intent(Intent.ACTION_SEND);
        send.setType("text/plain");
        send.putExtra(Intent.EXTRA_TEXT, text);
        if (!target.trim().isEmpty()) {
            send.setPackage(target.trim());
        }
        Intent chooser = target.trim().isEmpty() ? Intent.createChooser(send, "发给谁") : send;
        result.put("ok", startActivity(chooser, result));
        result.put("note", "选人和发送那两下在微信里，由你点");
        call.resolve(result);
    }

    /** 有哪些应用接得住一段文字。她要说"能发给谁"、或者要认微信的包名，靠这个。 */
    @PluginMethod
    public void shareTargets(PluginCall call) {
        Intent probe = new Intent(Intent.ACTION_SEND);
        probe.setType("text/plain");
        JSArray rows = new JSArray();
        JSObject result = new JSObject();
        for (ResolveInfo info : getContext().getPackageManager()
                .queryIntentActivities(probe, PackageManager.MATCH_DEFAULT_ONLY)) {
            if (info.activityInfo == null) {
                continue;
            }
            JSObject row = new JSObject();
            row.put("package", info.activityInfo.packageName);
            row.put("label", labelOf(info));
            rows.put(row);
        }
        result.put("ok", true);
        result.put("targets", rows);
        call.resolve(result);
    }

    /** 复制到剪贴板：接不住分享的应用，至少让人一粘就走。 */
    @PluginMethod
    public void copyText(PluginCall call) {
        String text = call.getString("text", "");
        JSObject result = new JSObject();
        try {
            android.content.ClipboardManager clip = (android.content.ClipboardManager)
                    getContext().getSystemService(Context.CLIPBOARD_SERVICE);
            if (clip == null) {
                result.put("ok", false);
                result.put("error", "这台设备没有剪贴板");
                call.resolve(result);
                return;
            }
            clip.setPrimaryClip(android.content.ClipData.newPlainText("小夜", text));
            result.put("ok", true);
            result.put("chars", text.length());
        } catch (Exception error) {
            result.put("ok", false);
            result.put("error", "复制失败：" + describe(error));
        }
        call.resolve(result);
    }

    // -- 工具方法 ----------------------------------------------------------

    private AudioManager audio() {
        return (AudioManager) getContext().getSystemService(Context.AUDIO_SERVICE);
    }

    @SuppressWarnings("deprecation")
    private Vibrator vibrator() {
        if (Build.VERSION.SDK_INT >= 31) {
            VibratorManager manager =
                    (VibratorManager) getContext().getSystemService(Context.VIBRATOR_MANAGER_SERVICE);
            return manager == null ? null : manager.getDefaultVibrator();
        }
        return getContext().getSystemService(Vibrator.class);
    }

    private boolean hasFlash(Context context) {
        return context.getPackageManager().hasSystemFeature(PackageManager.FEATURE_CAMERA_FLASH);
    }

    private String firstBackCamera(CameraManager manager) throws Exception {
        if (manager == null) {
            return null;
        }
        for (String id : manager.getCameraIdList()) {
            CameraCharacteristics chars = manager.getCameraCharacteristics(id);
            Integer facing = chars.get(CameraCharacteristics.LENS_FACING);
            Boolean flash = chars.get(CameraCharacteristics.FLASH_INFO_AVAILABLE);
            if (Boolean.TRUE.equals(flash) && (facing == null || facing == CameraCharacteristics.LENS_FACING_BACK)) {
                return id;
            }
        }
        return null;
    }

    private Intent registerStickyBattery(Context context) {
        try {
            // 电量是粘性广播：注册即拿到当前值，不需要 BATTERY_STATS 那种特权权限。
            return context.registerReceiver(
                    null, new android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        } catch (Exception error) {
            return null;
        }
    }

    private Intent dialIntent(String number) {
        return new Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + number));
    }

    private Intent sendToIntent(String number) {
        return sendToIntent(number, "");
    }

    private Intent sendToIntent(String number, String body) {
        Intent intent = new Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + number));
        if (body != null && !body.isEmpty()) {
            intent.putExtra("sms_body", body);
        }
        return intent;
    }

    private Intent calendarIntent(String title, long startMillis) {
        return calendarIntent(title, startMillis, startMillis + 60 * 60_000L);
    }

    private Intent calendarIntent(String title, long startMillis, long endMillis) {
        Intent intent = new Intent(Intent.ACTION_INSERT);
        intent.setData(CalendarContract.Events.CONTENT_URI);
        intent.putExtra(CalendarContract.Events.TITLE, title);
        if (startMillis > 0L) {
            intent.putExtra(CalendarContract.EXTRA_EVENT_BEGIN_TIME, startMillis);
            intent.putExtra(CalendarContract.EXTRA_EVENT_END_TIME, endMillis);
        }
        return intent;
    }

    private Intent viewIntent(String url) {
        return new Intent(Intent.ACTION_VIEW, Uri.parse(url));
    }

    private String settingsAction(String page) {
        switch (page) {
            case "wlan":
                return Settings.ACTION_WIFI_SETTINGS;
            case "bluetooth":
                return Settings.ACTION_BLUETOOTH_SETTINGS;
            case "location":
                return Settings.ACTION_LOCATION_SOURCE_SETTINGS;
            case "sound":
                return Settings.ACTION_SOUND_SETTINGS;
            case "display":
                return Settings.ACTION_DISPLAY_SETTINGS;
            case "notification":
                // 这一页要带包名才跳得对（开的是"本应用的通知设置"）。
                // 注意 `Settings.ACTION_NOTIFICATION_SETTINGS` 这个常量根本不存在，
                // 编译器报的是 cannot find symbol —— 别照着网页上的例子写。
                return Settings.ACTION_APP_NOTIFICATION_SETTINGS;
            case "battery":
                return Settings.ACTION_BATTERY_SAVER_SETTINGS;
            case "airplane":
                return Settings.ACTION_AIRPLANE_MODE_SETTINGS;
            case "date":
                return Settings.ACTION_DATE_SETTINGS;
            case "accessibility":
                return Settings.ACTION_ACCESSIBILITY_SETTINGS;
            case "tts":
                return "com.android.settings.TTS_SETTINGS";
            case "app":
                return Settings.ACTION_APPLICATION_DETAILS_SETTINGS;
            default:
                return null;
        }
    }

    private boolean canHandle(Intent intent) {
        try {
            return !getContext().getPackageManager()
                    .queryIntentActivities(intent, PackageManager.MATCH_DEFAULT_ONLY).isEmpty();
        } catch (Exception error) {
            return false;
        }
    }

    private List<ResolveInfo> launchable() {
        Intent probe = new Intent(Intent.ACTION_MAIN);
        probe.addCategory(Intent.CATEGORY_LAUNCHER);
        try {
            List<ResolveInfo> found = getContext().getPackageManager()
                    .queryIntentActivities(probe, PackageManager.MATCH_DEFAULT_ONLY);
            return found == null ? new ArrayList<ResolveInfo>() : found;
        } catch (Exception error) {
            return new ArrayList<>();
        }
    }

    private String labelOf(ResolveInfo info) {
        PackageManager pm = getContext().getPackageManager();
        try {
            CharSequence label = info.loadLabel(pm);
            return label == null ? "" : label.toString().trim();
        } catch (Exception error) {
            return "";
        }
    }

    /**
     * 把意图交出去。返回 false 时 `result.error` 已经写好了一句人话。
     *
     * `FLAG_ACTIVITY_NEW_TASK` 是必须的：从非 Activity 上下文 startActivity 会抛
     * `CallingActivity not set`，而这个异常长得像"没有应用能处理"，很容易查错方向。
     */
    private boolean startActivity(Intent intent, JSObject result) {
        try {
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(intent);
            return true;
        } catch (ActivityNotFoundException missing) {
            result.put("error", "这台手机上没有能接这个动作的应用");
            return false;
        } catch (Exception error) {
            result.put("error", "没打开成：" + describe(error));
            return false;
        }
    }

    private int flagImmutable(int base) {
        return Build.VERSION.SDK_INT >= 23 ? base | PendingIntent.FLAG_IMMUTABLE : base;
    }

    private Integer readInt(PluginCall call, String key) {
        Integer value = call.getInt(key);
        if (value != null) {
            return value;
        }
        String raw = call.getString(key);
        if (raw == null || raw.trim().isEmpty()) {
            return null;
        }
        try {
            return (int) Math.round(Double.parseDouble(raw.trim()));
        } catch (NumberFormatException error) {
            return null;
        }
    }

    private JSObject fail(String message) {
        JSObject result = new JSObject();
        result.put("ok", false);
        result.put("error", message);
        return result;
    }

    private String digits(String raw) {
        return raw == null ? "" : raw.replaceAll("[^0-9+*#]", "");
    }

    private String normalize(String raw) {
        return raw == null ? "" : raw.replaceAll("\\s+", "").toLowerCase(Locale.ROOT);
    }

    private String describe(Throwable error) {
        String message = error.getMessage();
        return message == null || message.isEmpty() ? error.getClass().getSimpleName() : message;
    }

    @Override
    protected void handleOnDestroy() {
        pendingTorch = null;
        super.handleOnDestroy();
    }
}

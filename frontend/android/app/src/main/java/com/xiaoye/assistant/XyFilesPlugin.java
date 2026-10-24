package com.xiaoye.assistant;

import android.app.Activity;
import android.content.Intent;
import androidx.activity.result.ActivityResult;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/** Exports only to the URI explicitly chosen by the person in the system picker. */
@CapacitorPlugin(name = "XyFiles")
public class XyFilesPlugin extends Plugin {
    @PluginMethod
    public void saveText(PluginCall call) {
        String text = call.getString("text", "");
        if (text.getBytes(StandardCharsets.UTF_8).length > 8 * 1024 * 1024) {
            call.reject("导出文件不能超过 8 MB");
            return;
        }
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType(call.getString("mime", "text/plain"));
        intent.putExtra(Intent.EXTRA_TITLE, call.getString("name", "小夜导出.txt"));
        startActivityForResult(call, intent, "documentCreated");
    }

    @ActivityCallback
    private void documentCreated(PluginCall call, ActivityResult result) {
        if (call == null) return;
        JSObject reply = new JSObject();
        if (result.getResultCode() != Activity.RESULT_OK || result.getData() == null || result.getData().getData() == null) {
            reply.put("saved", false);
            call.resolve(reply);
            return;
        }
        try (OutputStream stream = getContext().getContentResolver().openOutputStream(result.getData().getData(), "wt")) {
            if (stream == null) throw new IllegalStateException("无法打开目标文件");
            stream.write(call.getString("text", "").getBytes(StandardCharsets.UTF_8));
            stream.flush();
            reply.put("saved", true);
            call.resolve(reply);
        } catch (Exception error) {
            call.reject("文件没有保存成功，请重新选择位置", error);
        }
    }
}

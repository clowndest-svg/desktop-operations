package com.xiaoye.assistant;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Only encrypted model credentials and PC tokens live here. No key is exportable or logged. */
@CapacitorPlugin(name = "XySecure")
public class XySecurePlugin extends Plugin {
    private static final String ALIAS = "xiaoye.credentials.v1";
    private static final String CIPHER = "AES/GCM/NoPadding";

    private String checkedKey(PluginCall call) {
        String key = call.getString("key", "");
        if (!"xiaoye.model".equals(key) && !"xiaoye.pc".equals(key)) {
            throw new IllegalArgumentException("不是允许的安全配置项");
        }
        return key;
    }

    private SharedPreferences store() {
        return getContext().getSharedPreferences("xy_secure_v1", Context.MODE_PRIVATE);
    }

    private synchronized SecretKey secretKey() throws Exception {
        KeyStore keys = KeyStore.getInstance("AndroidKeyStore");
        keys.load(null);
        if (keys.containsAlias(ALIAS)) return (SecretKey) keys.getKey(ALIAS, null);
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256).build());
        return generator.generateKey();
    }

    @PluginMethod
    public void get(PluginCall call) {
        try {
            String key = checkedKey(call);
            String encoded = store().getString(key, null);
            JSObject result = new JSObject();
            if (encoded == null) {
                result.put("value", org.json.JSONObject.NULL);
            } else {
                String[] parts = encoded.split("\\.", -1);
                if (parts.length != 2) throw new IllegalStateException("安全配置格式不完整");
                byte[] iv = Base64.decode(parts[0], Base64.NO_WRAP);
                if (iv.length != 12) throw new IllegalStateException("安全配置校验失败");
                Cipher cipher = Cipher.getInstance(CIPHER);
                cipher.init(Cipher.DECRYPT_MODE, secretKey(), new GCMParameterSpec(128, iv));
                cipher.updateAAD(key.getBytes(StandardCharsets.UTF_8));
                byte[] plain = cipher.doFinal(Base64.decode(parts[1], Base64.NO_WRAP));
                result.put("value", new String(plain, StandardCharsets.UTF_8));
            }
            call.resolve(result);
        } catch (Exception error) {
            call.reject("安全配置无法读取；请重新填写模型配置或配对，不会退回明文保存", error);
        }
    }

    @PluginMethod
    public void set(PluginCall call) {
        try {
            String key = checkedKey(call);
            String value = call.getString("value", "");
            if (value.length() > 262144) throw new IllegalArgumentException("安全配置过大");
            Cipher cipher = Cipher.getInstance(CIPHER);
            cipher.init(Cipher.ENCRYPT_MODE, secretKey());
            cipher.updateAAD(key.getBytes(StandardCharsets.UTF_8));
            byte[] sealed = cipher.doFinal(value.getBytes(StandardCharsets.UTF_8));
            String encoded = Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP) + "."
                    + Base64.encodeToString(sealed, Base64.NO_WRAP);
            if (!store().edit().putString(key, encoded).commit()) throw new IllegalStateException("写入失败");
            call.resolve();
        } catch (Exception error) {
            call.reject("安全配置没有保存成功；原设置未删除", error);
        }
    }

    @PluginMethod
    public void remove(PluginCall call) {
        try {
            if (!store().edit().remove(checkedKey(call)).commit()) throw new IllegalStateException("删除失败");
            call.resolve();
        } catch (Exception error) {
            call.reject("安全配置没有清除成功", error);
        }
    }
}

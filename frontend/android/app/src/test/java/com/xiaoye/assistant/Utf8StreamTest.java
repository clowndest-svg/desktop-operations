package com.xiaoye.assistant;

import static org.junit.Assert.assertEquals;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import org.junit.Test;

/** Exercises the production stream decoder without a phone, microphone or network. */
public class Utf8StreamTest {
    @Test
    public void everyUtf8SplitPreservesChineseAndEmoji() throws Exception {
        Class<?> type = Class.forName("com.xiaoye.assistant.XyNetPlugin$Utf8Stream");
        Constructor<?> make = type.getDeclaredConstructor();
        make.setAccessible(true);
        Method feed = type.getDeclaredMethod("feed", byte[].class, int.class);
        Method rest = type.getDeclaredMethod("rest");
        feed.setAccessible(true);
        rest.setAccessible(true);
        String expected = "data: 小夜你好 😊\n\n";
        byte[] bytes = expected.getBytes(StandardCharsets.UTF_8);
        Object oneByteDecoder = make.newInstance();
        String oneByteResult = "";
        for (byte value : bytes) oneByteResult += (String) feed.invoke(oneByteDecoder, new byte[]{value}, 1);
        oneByteResult += (String) rest.invoke(oneByteDecoder);
        assertEquals("single-byte chunks", expected, oneByteResult);
        for (int split = 0; split <= bytes.length; split++) {
            Object decoder = make.newInstance();
            byte[] first = java.util.Arrays.copyOfRange(bytes, 0, split);
            byte[] second = java.util.Arrays.copyOfRange(bytes, split, bytes.length);
            String actual = (String) feed.invoke(decoder, first, first.length);
            actual += (String) feed.invoke(decoder, second, second.length);
            actual += (String) rest.invoke(decoder);
            assertEquals("split=" + split, expected, actual);
        }
    }
}

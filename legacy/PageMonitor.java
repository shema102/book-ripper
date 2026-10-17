import android.app.UiAutomation;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.SystemClock;
import android.graphics.Rect;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import java.io.*;
import java.lang.reflect.Constructor;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.regex.*;

/** Counter and normal page gestures, run as adb shell; no APK or app data access. */
public final class PageMonitor {
    private static final String PACKAGE = "com.readeat.app";
    private static final Pattern COUNTER = Pattern.compile(
        "\\s*(?:(?:Сторінка|Page)\\s+)?(\\d+)\\s*/\\s*(\\d+)\\s*", Pattern.CASE_INSENSITIVE);

    private static void collect(AccessibilityNodeInfo node, Set<String> found, int depth) {
        if (node == null || depth > 40) return;
        if (PACKAGE.contentEquals(node.getPackageName() == null ? "" : node.getPackageName())) {
            for (CharSequence value : new CharSequence[]{node.getText(), node.getContentDescription()}) {
                if (value == null) continue;
                Matcher match = COUNTER.matcher(value);
                if (match.matches()) {
                    int page = Integer.parseInt(match.group(1)), total = Integer.parseInt(match.group(2));
                    if (page >= 1 && page <= total) found.add(page + "/" + total);
                }
            }
            for (int i = 0; i < node.getChildCount(); i++) {
                AccessibilityNodeInfo child = node.getChild(i);
                try { collect(child, found, depth + 1); }
                finally { if (child != null) child.recycle(); }
            }
        }
    }

    private static void nextPage(UiAutomation automation, Rect bounds) throws Exception {
        // Use Android's tested input command; keep the page guard and counter in this process.
        Process gesture = new ProcessBuilder("input", "swipe",
            Integer.toString(bounds.left + Math.round(bounds.width()*.8f)),
            Integer.toString(bounds.top + Math.round(bounds.height()*.55f)),
            Integer.toString(bounds.left + Math.round(bounds.width()*.2f)),
            Integer.toString(bounds.top + Math.round(bounds.height()*.55f)), "300").start();
        if (gesture.waitFor() != 0) throw new Exception("Android page swipe command failed");
    }

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(args[0]);
        HandlerThread thread = new HandlerThread("ReadeatPageCounter");
        thread.start();
        Class<?> connectionClass = Class.forName("android.app.UiAutomationConnection");
        Object connection = connectionClass.getConstructor().newInstance();
        Constructor<?> constructor = UiAutomation.class.getConstructor(Looper.class,
            Class.forName("android.app.IUiAutomationConnection"));
        UiAutomation automation = (UiAutomation)constructor.newInstance(thread.getLooper(), connection);
        UiAutomation.class.getMethod("connect", int.class).invoke(automation,
            UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
        try (ServerSocket server = new ServerSocket(port, 8, InetAddress.getByName("127.0.0.1"))) {
            server.setSoTimeout(600000);
            System.out.println("Page counter ready on " + port); System.out.flush();
            boolean running = true;
            while (running) {
                try (Socket client = server.accept()) {
                    client.setSoTimeout(3000);
                    BufferedReader reader = new BufferedReader(new InputStreamReader(client.getInputStream(), StandardCharsets.UTF_8));
                    PrintWriter writer = new PrintWriter(new OutputStreamWriter(client.getOutputStream(), StandardCharsets.UTF_8), true);
                    String command = reader.readLine();
                    if ("QUIT".equals(command)) { writer.println("BYE"); running = false; continue; }
                    if (!"PAGE".equals(command) && !"NEXT".equals(command)) { writer.println("ERROR invalid request"); continue; }
                    automation.clearCache();
                    AccessibilityNodeInfo root = automation.getRootInActiveWindow();
                    try {
                        if (root == null || !PACKAGE.contentEquals(root.getPackageName() == null ? "" : root.getPackageName())) {
                            writer.println("ERROR Readeat is not the active window"); continue;
                        }
                        Set<String> found = new HashSet<>();
                        collect(root, found, 0);
                        if ("NEXT".equals(command)) {
                            if (found.size() != 1) { writer.println("ERROR cannot turn without a page counter"); continue; }
                            Rect bounds = new Rect(); root.getBoundsInScreen(bounds);
                            if (bounds.width() < 100 || bounds.height() < 100) { writer.println("ERROR invalid reader bounds"); continue; }
                            nextPage(automation, bounds);
                            writer.println("OK"); continue;
                        }
                        writer.println(found.size() == 1 ? found.iterator().next() : "ERROR page counter unavailable or ambiguous");
                    } finally { if (root != null) root.recycle(); }
                }
            }
        } finally {
            UiAutomation.class.getMethod("disconnect").invoke(automation);
            thread.quitSafely();
        }
    }
}

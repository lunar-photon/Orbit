package com.lunarphoton.pcauthenticator;

import android.app.Notification;
import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import org.json.JSONObject;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Shows the phone's notifications on the computer (a WhatsApp message, who is calling), as
 * desktop notifications, and takes them away there when they go on the phone. Only new,
 * visible ones: silent and ongoing ones (music, charging) and Orbit's own stay on the phone.
 * Needs Android's "Notification access" for Orbit, and the switch in Orbit's settings.
 */
public class PhoneNotificationListener extends NotificationListenerService {
    public static final String PREF_FORWARD = "forward_notifications";

    private final ExecutorService sender = Executors.newSingleThreadExecutor();
    // what was last sent per notification, so an update that changes nothing isn't sent again
    private final Map<String, String> lastSent = new LinkedHashMap<String, String>() {
        @Override protected boolean removeEldestEntry(Map.Entry<String, String> e) { return size() > 200; }
    };

    public static boolean isEnabled(Context c) {
        return c.getSharedPreferences("pc_auth_prefs", MODE_PRIVATE).getBoolean(PREF_FORWARD, false);
    }

    /** Has the user given Orbit notification access in Android's settings? */
    public static boolean hasAccess(Context c) {
        String flat = android.provider.Settings.Secure.getString(c.getContentResolver(), "enabled_notification_listeners");
        return flat != null && flat.contains(c.getPackageName() + "/");
    }

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        if (!isEnabled(this) || sbn == null || getPackageName().equals(sbn.getPackageName())) return;
        Notification n = sbn.getNotification();
        if (n == null || (n.flags & Notification.FLAG_GROUP_SUMMARY) != 0) return;
        boolean call = Notification.CATEGORY_CALL.equals(n.category);
        if (!call && (sbn.isOngoing() || isSilent(sbn))) return;

        Bundle x = n.extras;
        String title = text(x, Notification.EXTRA_TITLE);
        String body = text(x, Notification.EXTRA_BIG_TEXT);
        if (body.isEmpty()) body = text(x, Notification.EXTRA_TEXT);
        // a chat: the newest message, not the whole conversation
        CharSequence[] lines = x.getCharSequenceArray(Notification.EXTRA_TEXT_LINES);
        if (lines != null && lines.length > 0 && lines[lines.length - 1] != null) body = lines[lines.length - 1].toString();
        if (title.isEmpty() && body.isEmpty()) return;

        String key = sbn.getKey();
        String sig = title + "\n" + body;
        synchronized (lastSent) {
            if (sig.equals(lastSent.get(key))) return;
            lastSent.put(key, sig);
        }
        try {
            JSONObject json = new JSONObject();
            json.put("key", key);
            json.put("app", appName(sbn.getPackageName()));
            json.put("title", trim(title, 200));
            json.put("text", trim(body, 1000));
            json.put("category", call ? "call" : (n.category != null ? n.category : ""));
            send("/api/phone/notification", json);
        } catch (Exception ignored) { }
    }

    @Override
    public void onNotificationRemoved(StatusBarNotification sbn) {
        if (!isEnabled(this) || sbn == null) return;
        String key = sbn.getKey();
        synchronized (lastSent) {
            if (lastSent.remove(key) == null) return;      // never sent: nothing to take away
        }
        try {
            JSONObject json = new JSONObject();
            json.put("key", key);
            json.put("removed", true);
            send("/api/phone/notification", json);
        } catch (Exception ignored) { }
    }

    private boolean isSilent(StatusBarNotification sbn) {
        try {
            Ranking r = new Ranking();
            if (getCurrentRanking().getRanking(sbn.getKey(), r)) {
                return r.getImportance() <= android.app.NotificationManager.IMPORTANCE_LOW;
            }
        } catch (Exception ignored) { }
        return false;
    }

    private void send(String path, JSONObject json) {
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null || !active.isPaired()) return;
        String body = json.toString();
        sender.execute(() -> NetworkUtils.httpPostJsonWithAuth(active.getBaseUrl() + path, active.authToken, body, 4000));
    }

    private String appName(String pkg) {
        try {
            PackageManager pm = getPackageManager();
            ApplicationInfo ai = pm.getApplicationInfo(pkg, 0);
            return pm.getApplicationLabel(ai).toString();
        } catch (Exception e) {
            return pkg;
        }
    }

    private static String text(Bundle x, String k) {
        CharSequence c = x == null ? null : x.getCharSequence(k);
        return c == null ? "" : c.toString().trim();
    }

    private static String trim(String s, int max) {
        return s.length() <= max ? s : s.substring(0, max - 1) + "…";
    }
}

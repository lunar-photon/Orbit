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

    // the running listener, and the reply / mark-as-read buttons of the notifications it sent
    private static volatile PhoneNotificationListener instance;
    private static final Map<String, Notification.Action[]> buttons = new LinkedHashMap<String, Notification.Action[]>() {
        @Override protected boolean removeEldestEntry(Map.Entry<String, Notification.Action[]> e) { return size() > 200; }
    };
    // after a reply from the computer, the app's own update of that notification (your reply
    // shown in it) isn't sent back: key -> until when
    private static final Map<String, Long> quietUntil = new java.util.HashMap<>();

    @Override public void onListenerConnected() { instance = this; }
    @Override public void onListenerDisconnected() { instance = null; }

    /** A reply typed on the computer: sent through the app's own Reply button. */
    public static boolean reply(Context c, String key, String text) {
        Notification.Action[] b;
        synchronized (buttons) { b = buttons.get(key); }
        if (b == null || b[0] == null || text == null || text.isEmpty()) return false;
        Notification.Action a = b[0];
        try {
            android.app.RemoteInput[] inputs = a.getRemoteInputs();
            Bundle results = new Bundle();
            for (android.app.RemoteInput in : inputs) results.putCharSequence(in.getResultKey(), text);
            android.content.Intent fill = new android.content.Intent().addFlags(android.content.Intent.FLAG_RECEIVER_FOREGROUND);
            android.app.RemoteInput.addResultsToIntent(inputs, fill, results);
            a.actionIntent.send(c, 0, fill);
            synchronized (quietUntil) { quietUntil.put(key, System.currentTimeMillis() + 15000); }
            PhoneNotificationListener l = instance;
            if (l != null) l.dropFromComputer(key);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /** "Mark as read" clicked on the computer: the app's own button. */
    public static boolean markRead(Context c, String key) {
        Notification.Action[] b;
        synchronized (buttons) { b = buttons.get(key); }
        if (b == null || b[1] == null) return false;
        try {
            b[1].actionIntent.send();
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    private void dropFromComputer(String key) {
        synchronized (lastSent) { lastSent.remove(key); }
        try {
            JSONObject json = new JSONObject();
            json.put("key", key);
            json.put("removed", true);
            send("/api/phone/notification", json);
        } catch (Exception ignored) { }
    }

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
        synchronized (quietUntil) {
            Long until = quietUntil.get(key);
            if (until != null && until > System.currentTimeMillis()) return;    // our own reply, shown back
            quietUntil.remove(key);
        }
        // the app's Reply (a text input) and Mark as read buttons, to use from the computer
        Notification.Action replyBtn = null, readBtn = null;
        if (n.actions != null) for (Notification.Action a : n.actions) {
            if (a == null) continue;
            if (replyBtn == null && a.getRemoteInputs() != null && a.getRemoteInputs().length > 0) replyBtn = a;
            else if (readBtn == null && android.os.Build.VERSION.SDK_INT >= 28
                    && a.getSemanticAction() == Notification.Action.SEMANTIC_ACTION_MARK_AS_READ) readBtn = a;
        }
        synchronized (buttons) { buttons.put(key, new Notification.Action[]{replyBtn, readBtn}); }
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
            json.put("posted_at", sbn.getPostTime());       // so the computer can see a late arrival
            json.put("can_reply", replyBtn != null);
            json.put("can_mark_read", readBtn != null);
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

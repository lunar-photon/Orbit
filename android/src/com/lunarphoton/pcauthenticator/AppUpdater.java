package com.lunarphoton.pcauthenticator;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import android.util.Log;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Orbit updates itself: from the computer (it says so on the live link when it has a new build,
 * and the phone checks now and then) and from the GitHub repo (every 6 hours, which also covers
 * being away from the computer). Each build comes with a release note (version, SHA-256) signed
 * with a key that stays on the computer; this app carries the public half and installs only a
 * build whose note is validly signed, newer than itself, and whose download matches the note.
 * (The APK's own signature is the public Android debug key: on its own it proves nothing.)
 * The download goes to this app's own storage, never Downloads, and is deleted afterwards.
 * Android asks once (Orbit wasn't the app that installed Orbit); after that, on Android 12+,
 * updates install without asking.
 */
public final class AppUpdater {
    private static final String TAG = "AppUpdater";
    private static final String CHANNEL = "orbit_update";
    private static final String PROGRESS_CHANNEL = "orbit_update_progress";   // quiet: no sound per step
    private static final int NOTE_ID = 7408;
    private static final String PREF_OWN_SHA = "update_own_sha", PREF_OWN_AT = "update_own_at",
            PREF_TRIED = "update_tried_sha", PREF_TRIED_AT = "update_tried_at", PREF_CHECKED = "update_checked_at",
            PREF_GITHUB_AT = "update_github_at";
    private static final ExecutorService worker = Executors.newSingleThreadExecutor();
    private static volatile boolean busy;

    private AppUpdater() { }

    /** From the upkeep tick: ask the computer at most every 30 minutes. */
    static void maybeCheck(Context c) {
        SharedPreferences p = FindMy.prefs(c);
        if (System.currentTimeMillis() - p.getLong(PREF_CHECKED, 0) < 30 * 60_000L) return;
        p.edit().putLong(PREF_CHECKED, System.currentTimeMillis()).apply();
        check(c, null);
    }

    /** The computer has build `sha` (null: ask it). */
    static void check(Context ctx, String sha) {
        Context c = ctx.getApplicationContext();
        if (busy) return;
        busy = true;
        worker.execute(() -> {
            try {
                run(c, sha);
            } catch (Exception e) {
                Log.w(TAG, "Update failed: " + e.getMessage());
            } finally {
                busy = false;
            }
        });
    }

    // GitHub: the public repo carries each pushed build and its signed release note
    private static final String GITHUB = "https://raw.githubusercontent.com/lunar-photon/Orbit/main/";

    private static void run(Context c, String offered) throws Exception {
        // from the computer: its build, if its signed note says it's newer than this one
        PairedDevice active = DeviceManager.getActiveDevice(c);
        if (active != null && active.isPaired()) {
            String base = active.getBaseUrl();
            String res = NetworkUtils.httpGetWithAuth(base + "/api/app/latest", active.authToken, 10000);
            JSONObject note = res == null ? null : verified(c, new JSONObject(res).optJSONObject("note"));
            if (note != null && note.optLong("versionCode") > ownVersion(c)) {
                fetchAndInstall(c, base + "/apk", active.authToken, note, "your computer");
                return;
            }
        }
        // from GitHub (every 6 hours, or when the computer's out of reach)
        SharedPreferences p = FindMy.prefs(c);
        if (System.currentTimeMillis() - p.getLong(PREF_GITHUB_AT, 0) < 6 * 3600_000L) return;
        p.edit().putLong(PREF_GITHUB_AT, System.currentTimeMillis()).apply();
        JSONObject note = verified(c, fetchJson(GITHUB + "Orbit.apk.json"));
        if (note != null && note.optLong("versionCode") > ownVersion(c)) {
            fetchAndInstall(c, GITHUB + "Orbit.apk", null, note, "GitHub");
        }
    }

    private static void fetchAndInstall(Context c, String url, String token, JSONObject note, String from) throws Exception {
        String sha = note.optString("sha256");
        SharedPreferences p = FindMy.prefs(c);
        // the same build failed or was declined lately: not again for an hour
        if (sha.equals(p.getString(PREF_TRIED, "")) && System.currentTimeMillis() - p.getLong(PREF_TRIED_AT, 0) < 3600_000L) return;
        p.edit().putString(PREF_TRIED, sha).putLong(PREF_TRIED_AT, System.currentTimeMillis()).apply();

        if (Build.VERSION.SDK_INT >= 26 && !c.getPackageManager().canRequestPackageInstalls()) {
            Intent allow = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + c.getPackageName()))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            showNote(c, "Orbit can update itself", "Allow Orbit to install its updates: tap, then turn on \"Allow from this source\".", allow);
            p.edit().remove(PREF_TRIED).apply();     // ask again once allowed
            return;
        }
        File apk = new File(c.getCacheDir(), "orbit-update.apk");
        try {
            if (!download(c, url, token, apk, sha)) {
                cancelNote(c);
                return;
            }
            Log.i(TAG, "Installing " + note.optString("versionName") + " from " + from);
            progress(c, "Installing Orbit update…", -1);
            install(c, apk);
        } finally {
            apk.delete();
        }
    }

    /** The release note, if it's really signed with the update key this app carries; else null. */
    static JSONObject verified(Context c, JSONObject note) {
        if (note == null) return null;
        try {
            byte[] der = android.util.Base64.decode(c.getString(R.string.update_public_key), android.util.Base64.DEFAULT);
            java.security.PublicKey key = java.security.KeyFactory.getInstance("EC")
                    .generatePublic(new java.security.spec.X509EncodedKeySpec(der));
            java.security.Signature v = java.security.Signature.getInstance("SHA256withECDSA");
            v.initVerify(key);
            v.update(("orbit-update:" + note.getLong("versionCode") + ":" + note.getString("sha256").toLowerCase())
                    .getBytes("UTF-8"));
            if (v.verify(android.util.Base64.decode(note.getString("sig"), android.util.Base64.DEFAULT))) return note;
            Log.w(TAG, "Release note with a bad signature: ignored");
        } catch (Exception e) {
            Log.w(TAG, "Release note unreadable: " + e.getMessage());
        }
        return null;
    }

    static long ownVersion(Context c) {
        try {
            PackageInfo pi = c.getPackageManager().getPackageInfo(c.getPackageName(), 0);
            return Build.VERSION.SDK_INT >= 28 ? pi.getLongVersionCode() : pi.versionCode;
        } catch (Exception e) {
            return Long.MAX_VALUE;               // unknown: install nothing
        }
    }

    private static JSONObject fetchJson(String url) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(15000);
            conn.setUseCaches(false);
            if (conn.getResponseCode() != 200) return null;
            try (InputStream in = conn.getInputStream()) {
                java.io.ByteArrayOutputStream b = new java.io.ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) > 0) b.write(buf, 0, n);
                return new JSONObject(b.toString("UTF-8"));
            }
        } catch (Exception e) {
            return null;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    /** SHA-256 of the app running here (cached until it's updated). */
    static String ownSha(Context c) {
        try {
            PackageInfo pi = c.getPackageManager().getPackageInfo(c.getPackageName(), 0);
            SharedPreferences p = FindMy.prefs(c);
            if (p.getLong(PREF_OWN_AT, -1) == pi.lastUpdateTime) return p.getString(PREF_OWN_SHA, "");
            String sha = sha256(new File(c.getApplicationInfo().sourceDir));
            p.edit().putString(PREF_OWN_SHA, sha).putLong(PREF_OWN_AT, pi.lastUpdateTime).apply();
            return sha;
        } catch (Exception e) {
            return "";
        }
    }

    private static String sha256(File f) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        try (InputStream in = new FileInputStream(f)) {
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) md.update(buf, 0, n);
        }
        StringBuilder sb = new StringBuilder();
        for (byte b : md.digest()) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    private static boolean download(Context c, String url, String token, File to, String sha) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        try {
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(30000);
            if (token != null) {                     // the computer; GitHub gets no key of ours
                NetworkUtils.applyTunnelHeaders(conn);
                NetworkUtils.setAuth(conn, token);
            }
            if (conn.getResponseCode() != 200) return false;
            long total = conn.getContentLengthLong(), got = 0;
            int shown = -1;
            progress(c, "Downloading Orbit update", 0);
            try (InputStream in = conn.getInputStream(); OutputStream out = new FileOutputStream(to)) {
                byte[] buf = new byte[1 << 16];
                int n;
                while ((n = in.read(buf)) > 0) {
                    out.write(buf, 0, n);
                    got += n;
                    int pct = total > 0 ? (int) (got * 100 / total) : -1;
                    if (pct >= shown + 10) {
                        shown = pct;
                        progress(c, "Downloading Orbit update", pct);
                    }
                }
            }
        } finally {
            conn.disconnect();
        }
        boolean ok = sha.equalsIgnoreCase(sha256(to));      // exactly the build the signed note names
        if (!ok) Log.w(TAG, "Downloaded build doesn't match; not installing");
        return ok;
    }

    private static void install(Context c, File apk) throws Exception {
        PackageInstaller pi = c.getPackageManager().getPackageInstaller();
        PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        params.setAppPackageName(c.getPackageName());
        if (Build.VERSION.SDK_INT >= 31) params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);
        int id = pi.createSession(params);
        try (PackageInstaller.Session session = pi.openSession(id)) {
            try (InputStream in = new FileInputStream(apk); OutputStream out = session.openWrite("orbit.apk", 0, apk.length())) {
                byte[] buf = new byte[1 << 16];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                session.fsync(out);
            }
            Intent result = new Intent(c, Result.class);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0);
            session.commit(PendingIntent.getBroadcast(c, id, result, flags).getIntentSender());
        }
        Log.i(TAG, "Installing the computer's build");
    }

    /** The installer's answer: done (Orbit restarts as the new build), or "ask the user". */
    public static class Result extends BroadcastReceiver {
        @Override
        public void onReceive(Context c, Intent intent) {
            int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
            if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
                Intent confirm = intent.getParcelableExtra(Intent.EXTRA_INTENT);
                if (confirm == null) return;
                confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                showNote(c, "Orbit update ready", "Tap to install the new version from your computer. Orbit closes for a moment while it installs, then says when it's done.", confirm);
            } else if (status == PackageInstaller.STATUS_SUCCESS) {
                cancelNote(c);
            } else {
                String why = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
                Log.w(TAG, "Update not installed: " + why);
                if (status != PackageInstaller.STATUS_FAILURE_ABORTED) {
                    showNote(c, "Orbit update didn't install", why == null || why.isEmpty() ? "Android refused it." : why,
                            new Intent(c, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
                } else {
                    cancelNote(c);                    // you said no: it asks again in an hour
                }
            }
        }
    }

    /** A quiet, ongoing step: pct 0..100, or -1 for "working on it". */
    private static void progress(Context c, String title, int pct) {
        NotificationManager nm = (NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(PROGRESS_CHANNEL) == null) {
            nm.createNotificationChannel(new NotificationChannel(PROGRESS_CHANNEL, "App update progress", NotificationManager.IMPORTANCE_LOW));
        }
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(c, PROGRESS_CHANNEL) : new Notification.Builder(c);
        b.setSmallIcon(R.drawable.ic_o_notify).setContentTitle(title).setOngoing(true).setOnlyAlertOnce(true)
                .setProgress(100, Math.max(pct, 0), pct < 0);
        if (pct >= 0) b.setContentText(pct + "%");
        nm.notify(NOTE_ID, b.build());
    }

    private static void cancelNote(Context c) {
        ((NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE)).cancel(NOTE_ID);
    }

    /** From the new build's first start (BootReceiver: our package was replaced). */
    static void announceUpdated(Context c) {
        cancelNote(c);
        NotificationManager nm = (NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(PROGRESS_CHANNEL) == null) {
            nm.createNotificationChannel(new NotificationChannel(PROGRESS_CHANNEL, "App update progress", NotificationManager.IMPORTANCE_LOW));
        }
        PendingIntent open = PendingIntent.getActivity(c, NOTE_ID, new Intent(c, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(c, PROGRESS_CHANNEL) : new Notification.Builder(c);
        b.setSmallIcon(R.drawable.ic_o_notify).setContentTitle("Orbit updated")
                .setContentText("The new version from your computer is installed.").setContentIntent(open).setAutoCancel(true);
        if (Build.VERSION.SDK_INT >= 26) b.setTimeoutAfter(15_000);
        nm.notify(NOTE_ID + 1, b.build());
    }

    private static void showNote(Context c, String title, String text, Intent tap) {
        NotificationManager nm = (NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(new NotificationChannel(CHANNEL, "App updates", NotificationManager.IMPORTANCE_DEFAULT));
        }
        PendingIntent pi = PendingIntent.getActivity(c, NOTE_ID, tap,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(c, CHANNEL) : new Notification.Builder(c);
        b.setSmallIcon(R.drawable.ic_o_notify).setContentTitle(title).setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text)).setContentIntent(pi).setAutoCancel(true);
        nm.notify(NOTE_ID, b.build());
    }
}

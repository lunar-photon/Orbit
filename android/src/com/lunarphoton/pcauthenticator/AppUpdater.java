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
 * Orbit updates itself from the computer: when the computer has a different build than the
 * one running here (it says so on the live link, and the phone also checks now and then),
 * the phone downloads it into its own private storage, never Downloads, and installs it with
 * Android's package installer, then deletes the download. Android asks once (Orbit wasn't the
 * app that installed Orbit); after that, on Android 12+, updates install without asking.
 */
public final class AppUpdater {
    private static final String TAG = "AppUpdater";
    private static final String CHANNEL = "orbit_update";
    private static final String PROGRESS_CHANNEL = "orbit_update_progress";   // quiet: no sound per step
    private static final int NOTE_ID = 7408;
    private static final String PREF_OWN_SHA = "update_own_sha", PREF_OWN_AT = "update_own_at",
            PREF_TRIED = "update_tried_sha", PREF_TRIED_AT = "update_tried_at", PREF_CHECKED = "update_checked_at";
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

    private static void run(Context c, String offered) throws Exception {
        PairedDevice active = DeviceManager.getActiveDevice(c);
        if (active == null || !active.isPaired()) return;
        String base = active.getBaseUrl();
        String sha = offered;
        if (sha == null || sha.isEmpty()) {
            String res = NetworkUtils.httpGetWithAuth(base + "/api/app/latest", active.authToken, 10000);
            if (res == null) return;
            sha = new JSONObject(res).optString("sha256", "");
        }
        if (sha.isEmpty() || sha.equalsIgnoreCase(ownSha(c))) return;
        SharedPreferences p = FindMy.prefs(c);
        // the same build failed or was declined lately: not again for an hour
        if (sha.equals(p.getString(PREF_TRIED, "")) && System.currentTimeMillis() - p.getLong(PREF_TRIED_AT, 0) < 3600_000L) return;
        p.edit().putString(PREF_TRIED, sha).putLong(PREF_TRIED_AT, System.currentTimeMillis()).apply();

        if (Build.VERSION.SDK_INT >= 26 && !c.getPackageManager().canRequestPackageInstalls()) {
            Intent allow = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + c.getPackageName()))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            showNote(c, "Orbit can update itself", "Allow Orbit to install updates from your computer: tap, then turn on \"Allow from this source\".", allow);
            p.edit().remove(PREF_TRIED).apply();     // ask again once allowed
            return;
        }

        File apk = new File(c.getCacheDir(), "orbit-update.apk");
        try {
            if (!download(c, base + "/apk", active.authToken, apk, sha)) {
                cancelNote(c);
                return;
            }
            progress(c, "Installing Orbit update…", -1);
            install(c, apk);
        } finally {
            apk.delete();
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
            NetworkUtils.applyTunnelHeaders(conn);
            NetworkUtils.setAuth(conn, token);
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
        boolean ok = sha.equalsIgnoreCase(sha256(to));      // exactly the build the computer named
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

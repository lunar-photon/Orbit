package com.lunarphoton.pcauthenticator;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.admin.DeviceAdminReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.UserHandle;
import android.provider.Settings;
import android.util.Log;

/**
 * Orbit as a device admin, for one thing only: being told when someone types a wrong PIN or
 * password. Then (if "Photo on wrong unlock" is on) the front camera takes a photo for the
 * computer. Orbit can't lock, wipe or change anything with this.
 */
public class OrbitAdminReceiver extends DeviceAdminReceiver {
    private static final String TAG = "OrbitAdmin";
    private static final String CHANNEL = "findmy_capture";
    private static long lastPhoto;

    @Override
    public void onPasswordFailed(Context context, Intent intent, UserHandle user) {
        if (!FindMy.prefs(context).getBoolean(FindMy.PREF_PHOTO, false)) return;
        long now = System.currentTimeMillis();
        if (now - lastPhoto < 30_000L) return;          // one photo per try, not one per digit
        lastPhoto = now;
        Intent shot = new Intent(context, UnlockPhotoActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_NO_ANIMATION
                        | Intent.FLAG_ACTIVITY_EXCLUDE_FROM_RECENTS);
        try {
            if (Build.VERSION.SDK_INT < 29 || Settings.canDrawOverlays(context)) {
                context.startActivity(shot);             // allowed from here with "display over other apps"
                return;
            }
        } catch (Exception e) {
            Log.w(TAG, "Couldn't open the camera directly: " + e.getMessage());
        }
        // otherwise through a full-screen notification, which may open it over the lock screen
        NotificationManager nm = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL) == null) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "Find my: unlock attempts", NotificationManager.IMPORTANCE_HIGH);
            ch.setSound(null, null);
            nm.createNotificationChannel(ch);
        }
        PendingIntent pi = PendingIntent.getActivity(context, 7, shot, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(context, CHANNEL) : new Notification.Builder(context);
        b.setSmallIcon(R.drawable.ic_o_notify).setContentTitle("Orbit").setContentText("Checking…")
                .setCategory(Notification.CATEGORY_ALARM).setFullScreenIntent(pi, true).setAutoCancel(true);
        if (Build.VERSION.SDK_INT >= 26) b.setTimeoutAfter(10_000);
        nm.notify(7307, b.build());
    }

    @Override
    public void onDisabled(Context context, Intent intent) {
        FindMy.prefs(context).edit().putBoolean(FindMy.PREF_PHOTO, false).apply();
    }
}

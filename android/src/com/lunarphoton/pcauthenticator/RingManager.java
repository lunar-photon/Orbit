package com.lunarphoton.pcauthenticator;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.util.Log;

public class RingManager {
    private static final String TAG = "RingManager";
    public static final String ACTION_STOP_ALARM = "com.lunarphoton.pcauthenticator.STOP_ALARM";
    public static final String CHANNEL_RING = "pc_connect_ring";
    public static final int NOTIFICATION_ID_RING = 2001;
    public static final long ALARM_TIMEOUT_MS = 60000L; // Auto-stop after 60 seconds

    private static MediaPlayer mediaPlayer = null;
    private static Vibrator vibrator = null;
    private static boolean isRinging = false;
    private static final Handler timeoutHandler = new Handler(Looper.getMainLooper());
    private static Runnable timeoutRunnable = null;

    public static synchronized void startAlarm(Context context) {
        if (isRinging) return;
        isRinging = true;

        try {
            Uri alert = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM);
            if (alert == null) {
                alert = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE);
            }

            AudioManager am = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
            if (am != null) {
                int maxVol = am.getStreamMaxVolume(AudioManager.STREAM_ALARM);
                am.setStreamVolume(AudioManager.STREAM_ALARM, maxVol, 0);
            }

            mediaPlayer = new MediaPlayer();
            mediaPlayer.setDataSource(context, alert);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                AudioAttributes attrs = new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build();
                mediaPlayer.setAudioAttributes(attrs);
            } else {
                mediaPlayer.setAudioStreamType(AudioManager.STREAM_ALARM);
            }
            mediaPlayer.setLooping(true);
            mediaPlayer.prepare();
            mediaPlayer.start();

            vibrator = (Vibrator) context.getSystemService(Context.VIBRATOR_SERVICE);
            if (vibrator != null && vibrator.hasVibrator()) {
                long[] pattern = {0, 500, 200, 500, 200, 500, 800};
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    vibrator.vibrate(VibrationEffect.createWaveform(pattern, 0));
                } else {
                    vibrator.vibrate(pattern, 0);
                }
            }

            showRingNotification(context);

            try {
                Intent ringIntent = new Intent(context, RingActivity.class);
                ringIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
                context.startActivity(ringIntent);
            } catch (Exception ignored) {}

            if (timeoutRunnable != null) {
                timeoutHandler.removeCallbacks(timeoutRunnable);
            }
            timeoutRunnable = () -> {
                Log.i(TAG, "Alarm timed out after 60 seconds");
                stopAlarm(context);
            };
            timeoutHandler.postDelayed(timeoutRunnable, ALARM_TIMEOUT_MS);

        } catch (Exception e) {
            Log.e(TAG, "Error starting alarm", e);
        }
    }

    public static synchronized void stopAlarm(Context context) {
        isRinging = false;
        if (timeoutRunnable != null) {
            timeoutHandler.removeCallbacks(timeoutRunnable);
            timeoutRunnable = null;
        }

        if (mediaPlayer != null) {
            try {
                if (mediaPlayer.isPlaying()) mediaPlayer.stop();
                mediaPlayer.release();
            } catch (Exception ignored) {}
            mediaPlayer = null;
        }

        if (vibrator != null) {
            try {
                vibrator.cancel();
            } catch (Exception ignored) {}
            vibrator = null;
        }

        NotificationManager nm = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) {
            nm.cancel(NOTIFICATION_ID_RING);
        }
    }

    public static synchronized boolean isRinging() {
        return isRinging;
    }

    private static void showRingNotification(Context context) {
        NotificationManager nm = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel chan = new NotificationChannel(
                    CHANNEL_RING,
                    "Find My Phone Alert",
                    NotificationManager.IMPORTANCE_HIGH
            );
            chan.setDescription("Alert when computer requests Find My Phone");
            nm.createNotificationChannel(chan);
        }

        Intent stopIntent = new Intent(context, ActionReceiver.class);
        stopIntent.setAction(ACTION_STOP_ALARM);
        PendingIntent stopPending = PendingIntent.getBroadcast(
                context, 0, stopIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0)
        );

        Intent fullScreenIntent = new Intent(context, RingActivity.class);
        fullScreenIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent fullScreenPending = PendingIntent.getActivity(
                context, 0, fullScreenIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0)
        );

        android.app.Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            builder = new android.app.Notification.Builder(context, CHANNEL_RING);
        } else {
            builder = new android.app.Notification.Builder(context);
        }

        builder.setContentTitle("Find my phone")
                .setContentText("Tap Dismiss or unlock phone to stop.")
                .setSmallIcon(R.drawable.ic_o_notify)
                .setAutoCancel(false)
                .setOngoing(true)
                .setFullScreenIntent(fullScreenPending, true)
                .addAction(new android.app.Notification.Action.Builder(
                        R.mipmap.ic_launcher,
                        "Stop alarm",
                        stopPending
                ).build());

        nm.notify(NOTIFICATION_ID_RING, builder.build());
    }
}

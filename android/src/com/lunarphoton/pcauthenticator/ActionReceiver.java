package com.lunarphoton.pcauthenticator;

import android.app.NotificationManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.widget.Toast;

public class ActionReceiver extends BroadcastReceiver {
    public static final String ACTION_APPROVE = "com.lunarphoton.pcauthenticator.ACTION_APPROVE";
    public static final String ACTION_DENY = "com.lunarphoton.pcauthenticator.ACTION_DENY";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (RingManager.ACTION_STOP_ALARM.equals(action)) {
            RingManager.stopAlarm(context);
            return;
        }

        SharedPreferences prefs = context.getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        PairedDevice active = DeviceManager.getActiveDevice(context);
        String serverUrl = (active != null) ? active.getBaseUrl() : prefs.getString("server_url", "http://192.168.48.40:1760");
        String authToken = (active != null) ? active.authToken : null;
        String secretKey = (active != null) ? active.secretKey : null;
        String sessionId = intent.getStringExtra("session_id");

        if (ACTION_APPROVE.equals(action)) {
            boolean requireBiometric = prefs.getBoolean("require_biometric", true);
            if (requireBiometric) {
                // Biometrics required: forward to MainActivity for fingerprint scan
                Intent authIntent = new Intent(context, MainActivity.class);
                authIntent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
                authIntent.putExtra("from_challenge", true);
                authIntent.putExtra("auto_biometric", true);
                authIntent.putExtra("session_id", sessionId);
                context.startActivity(authIntent);
                return;
            }

            AuthService.cancelChallengeNotification(context);
            vibrate(context, 100);
            new Thread(() -> {
                boolean ok = NetworkUtils.httpPostWithAuth(serverUrl + "/api/approve_current", authToken, secretKey, sessionId, "approve", 5000);
                if (ok) {
                    Intent updateIntent = new Intent("com.lunarphoton.pcauthenticator.CHALLENGE_RESOLVED");
                    updateIntent.putExtra("status", "approved");
                    context.sendBroadcast(updateIntent);
                }
            }).start();
        } else if (ACTION_DENY.equals(action)) {
            AuthService.cancelChallengeNotification(context);
            vibrate(context, 200);
            new Thread(() -> {
                NetworkUtils.httpPostWithAuth(serverUrl + "/api/deny_current", authToken, secretKey, sessionId, "deny", 5000);
                Intent updateIntent = new Intent("com.lunarphoton.pcauthenticator.CHALLENGE_RESOLVED");
                updateIntent.putExtra("status", "denied");
                context.sendBroadcast(updateIntent);
            }).start();
        }
    }

    private void vibrate(Context context, long ms) {
        try {
            Vibrator v = (Vibrator) context.getSystemService(Context.VIBRATOR_SERVICE);
            if (v != null && v.hasVibrator()) {
                if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                    v.vibrate(VibrationEffect.createOneShot(ms, VibrationEffect.DEFAULT_AMPLITUDE));
                } else {
                    v.vibrate(ms);
                }
            }
        } catch (Exception ignored) {}
    }
}

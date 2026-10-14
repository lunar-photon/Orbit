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
    public static final String ACTION_COPY_CLIPBOARD = "com.lunarphoton.pcauthenticator.COPY_CLIPBOARD";
    public static final int CLIPBOARD_NOTIF_ID = 4100;
    public static final String ACTION_APPROVE = "com.lunarphoton.pcauthenticator.ACTION_APPROVE";
    public static final String ACTION_DENY = "com.lunarphoton.pcauthenticator.ACTION_DENY";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (ACTION_COPY_CLIPBOARD.equals(action)) {
            // the computer's clipboard, copied only now that you asked for it
            String text = intent.getStringExtra("text");
            if (text != null) {
                android.content.ClipboardManager cm = (android.content.ClipboardManager) context.getSystemService(Context.CLIPBOARD_SERVICE);
                if (cm != null) {
                    AuthService.lastSyncedClipboard = text;      // don't send it straight back
                    cm.setPrimaryClip(android.content.ClipData.newPlainText("Orbit", text));
                    android.widget.Toast.makeText(context, "Copied", android.widget.Toast.LENGTH_SHORT).show();
                }
            }
            android.app.NotificationManager nm = (android.app.NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) nm.cancel(CLIPBOARD_NOTIF_ID);
            return;
        }
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
                boolean ok = approveCurrent(active, sessionId);
                if (!ok) {
                    new android.os.Handler(android.os.Looper.getMainLooper()).post(() ->
                            android.widget.Toast.makeText(context, "Couldn't approve: the computer stopped asking", android.widget.Toast.LENGTH_SHORT).show());
                }
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

    /**
     * Approve the sign-in waiting on the computer. If the one we were asked about is gone (the
     * computer asked again, or its service restarted and lost it), approve the one it asks about
     * now, if there is one: you've just approved this unlock, it's the same unlock asked again.
     */
    static boolean approveCurrent(PairedDevice active, String sessionId) {
        if (active == null) return false;
        String url = active.getBaseUrl();
        if (NetworkUtils.httpPostWithAuth(url + "/api/approve_current", active.authToken, active.secretKey, sessionId, "approve", 5000)) {
            return true;
        }
        try {
            String res = NetworkUtils.httpGetWithAuth(url + "/api/current_challenge", active.authToken, 3000);
            if (res == null) return false;
            org.json.JSONObject json = new org.json.JSONObject(res);
            if (!json.optBoolean("has_challenge", false)) return false;
            org.json.JSONObject ch = json.getJSONObject("challenge");
            String now = ch.optString("session_id", ch.optString("id", ""));
            if (now.isEmpty() || now.equals(sessionId)) return false;
            return NetworkUtils.httpPostWithAuth(url + "/api/approve_current", active.authToken, active.secretKey, now, "approve", 5000);
        } catch (Exception e) {
            return false;
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

package com.lunarphoton.pcauthenticator;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.BroadcastReceiver;
import android.net.Uri;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.util.List;
import java.util.ArrayList;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.URL;
import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStream;
import android.os.BatteryManager;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.media.MediaScannerConnection;
import android.content.ContentValues;
import android.provider.MediaStore;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.telephony.TelephonyManager;
import android.telephony.TelephonyCallback;
import android.telephony.PhoneStateListener;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.os.StatFs;
import android.widget.Toast;

public class AuthService extends Service {
    private static final String TAG = "PCAuthService";
    public static final String CHANNEL_FOREGROUND = "pc_auth_status";
    public static final String CHANNEL_CHALLENGE = "pc_auth_challenge";

    public static final int NOTIFICATION_ID_FOREGROUND = 1001;
    public static final int NOTIFICATION_ID_CHALLENGE = 1002;

    public static final String ACTION_CHALLENGE = "com.lunarphoton.pcauthenticator.NEW_CHALLENGE";
    public static final String ACTION_STATUS = "com.lunarphoton.pcauthenticator.CONNECTION_STATUS";

    public static final String ACTION_RECONNECT = "com.lunarphoton.pcauthenticator.RECONNECT";

    public static volatile String lastSyncedClipboard = "";
    private ClipboardManager.OnPrimaryClipChangedListener clipListener = null;

    private volatile boolean isRunning = false;
    private volatile HttpURLConnection currentConn = null;
    private static volatile boolean isConnected = false;
    private static volatile String lastStatusText = "Connecting…";
    // When the user last unlocked the phone itself (face, fingerprint or PIN on the lock screen)
    private static volatile long lastPhoneUnlockMs = 0;

    public static long getLastPhoneUnlockMs() {
        return lastPhoneUnlockMs;
    }

    public static boolean isConnectedToLaptop() {
        return isConnected;
    }

    public static String getLastStatusText() {
        return lastStatusText;
    }

    private Thread workerThread;
    private PowerManager.WakeLock wakeLock;
    private FileServer fileServer;

    private ConnectivityManager.NetworkCallback defaultNetworkCallback = null;
    private Network lastKnownNetwork = null;
    private volatile boolean isSwitchingNetwork = false;

    private void abortCurrentConnectionAndWake() {
        isSwitchingNetwork = true;
        new Thread(() -> {
            try {
                HttpURLConnection c = currentConn;
                if (c != null) {
                    c.disconnect();
                }
            } catch (Exception ignored) {}
            if (workerThread != null) {
                workerThread.interrupt();
            }
        }).start();
    }

    private BroadcastReceiver phoneStateReceiver = null;
    private Object telephonyCallbackObj = null;
    private String lastSentCallState = "idle";

    @Override
    public void onCreate() {
        super.onCreate();
        try {
            android.os.StrictMode.VmPolicy.Builder builder = new android.os.StrictMode.VmPolicy.Builder();
            android.os.StrictMode.setVmPolicy(builder.build());
        } catch (Exception ignored) {}
        createNotificationChannels();
        try {
            java.net.URLConnection.setDefaultRequestProperty("ngrok-skip-browser-warning", "1");
        } catch (Exception ignored) {}

        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "PCAuth::ListeningLock");
            // Battery optimization: Keep socket event-driven, do not hold continuous wake lock
        }

        fileServer = new FileServer(this);
        fileServer.start();

        IntentFilter unlockFilter = new IntentFilter();
        unlockFilter.addAction(Intent.ACTION_USER_PRESENT);
        unlockFilter.addAction(Intent.ACTION_SCREEN_ON);
        registerReceiver(new BroadcastReceiver() {
            private long lastScreenCheckTime = 0;
            @Override
            public void onReceive(Context context, Intent intent) {
                if (intent == null) return;
                String action = intent.getAction();
                if (Intent.ACTION_USER_PRESENT.equals(action)) {
                    lastPhoneUnlockMs = System.currentTimeMillis();
                    if (RingManager.isRinging()) {
                        Log.i(TAG, "Device unlocked by user - stopping Find My Phone alarm");
                        RingManager.stopAlarm(context);
                    }
                }
                if (Intent.ACTION_SCREEN_ON.equals(action) || Intent.ACTION_USER_PRESENT.equals(action)) {
                    long now = System.currentTimeMillis();
                    if (now - lastScreenCheckTime >= 45000L) {
                        if (CaptivePortalManager.isEnabled(context) && CaptivePortalManager.isCheckOnScreenOn(context)
                                && !CaptivePortalManager.isExplicitlyLoggedOut(context)) {
                            if (DeviceManager.isWifiActive(context)) {
                                lastScreenCheckTime = now;
                                checkAndAutoLoginIfLoggedOut("Screen wake");
                            }
                        }
                    }
                }
            }
        }, unlockFilter);

        // Automatic battery & charging status reporting (throttled to preserve battery)
        IntentFilter batteryFilter = new IntentFilter(Intent.ACTION_BATTERY_CHANGED);
        registerReceiver(new BroadcastReceiver() {
            private int lastReportedLevel = -1;
            private boolean lastReportedCharging = false;
            private long lastReportTime = 0;
            @Override
            public void onReceive(Context context, Intent intent) {
                // If not actively connected to laptop, skip transmission entirely
                if (currentConn == null) return;

                int rawLevel = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                int scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
                int status = intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
                boolean isCharging = status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL;
                int level = (rawLevel >= 0 && scale > 0) ? (rawLevel * 100) / scale : -1;

                long now = System.currentTimeMillis();
                boolean chargingChanged = (isCharging != lastReportedCharging);
                boolean levelSignificant = (lastReportedLevel < 0 || Math.abs(level - lastReportedLevel) >= 5);
                boolean intervalElapsed = (now - lastReportTime > 900000L); // 15 min

                if (chargingChanged || (levelSignificant && (now - lastReportTime > 60000L)) || intervalElapsed) {
                    lastReportedLevel = level;
                    lastReportedCharging = isCharging;
                    lastReportTime = now;
                    PairedDevice active = DeviceManager.getActiveDevice(AuthService.this);
                    if (active != null) {
                        sendPhoneStatusToPc(active);
                    }
                }
            }
        }, batteryFilter);

        startUdpBeaconListener();
        initTelephonyListener();
        initClipboardListener();
        initNetworkMonitoring();
        initCaptivePortalWatchdog();
    }

    private Handler captiveWatchdogHandler;
    private Runnable captiveWatchdogRunnable;
    private long lastAutoLoginAttempt = 0;
    private static final long AUTO_LOGIN_COOLDOWN_MS = 35000L;

    private void initCaptivePortalWatchdog() {
        captiveWatchdogHandler = new Handler(Looper.getMainLooper());
        captiveWatchdogRunnable = new Runnable() {
            @Override
            public void run() {
                if (!isRunning) return;
                try {
                    if (CaptivePortalManager.isEnabled(AuthService.this)
                            && CaptivePortalManager.isAutoLoginOnWifi(AuthService.this)
                            && DeviceManager.isWifiActive(AuthService.this)
                            && !CaptivePortalManager.isExplicitlyLoggedOut(AuthService.this)) {
                        checkAndAutoLoginIfLoggedOut("Periodic background watchdog");
                    }
                } catch (Exception e) {
                    Log.w(TAG, "Captive watchdog error: " + e.getMessage());
                } finally {
                    if (isRunning && captiveWatchdogHandler != null) {
                        captiveWatchdogHandler.postDelayed(this, 5 * 60000L);   // Android reports captive Wi-Fi itself; this is a backstop
                    }
                }
            }
        };
        captiveWatchdogHandler.postDelayed(captiveWatchdogRunnable, 30000L);
    }

    private synchronized void checkAndAutoLoginIfLoggedOut(String reason) {
        if (!DeviceManager.isWifiActive(AuthService.this)) {
            return;
        }
        long now = System.currentTimeMillis();
        if (now - lastAutoLoginAttempt < AUTO_LOGIN_COOLDOWN_MS) {
            return;
        }
        lastAutoLoginAttempt = now;

        new Thread(() -> {
            try {
                if (!DeviceManager.isWifiActive(AuthService.this)) {
                    return;
                }
                if (CaptivePortalManager.isInternetConnected(AuthService.this)) {
                    return;
                }

                Log.i(TAG, "🌐 Network session dropped / internet blocked (" + reason + "). Re-authenticating automatically...");
                CaptivePortalManager.LoginResult res = CaptivePortalManager.login(AuthService.this);
                Log.i(TAG, "Auto-relogin result: " + res.status + " - " + res.message);

                if (res.success) {
                    new Handler(Looper.getMainLooper()).post(() -> {
                        Toast.makeText(AuthService.this, "Internet restored: " + res.message, Toast.LENGTH_SHORT).show();
                    });
                }
                sendBroadcast(new Intent("com.lunarphoton.pcauthenticator.CAPTIVE_STATE_CHANGED")
                        .setPackage(getPackageName()));
            } catch (Exception e) {
                Log.w(TAG, "Error in auto-relogin: " + e.getMessage());
            }
        }).start();
    }

    private void initNetworkMonitoring() {
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                defaultNetworkCallback = new ConnectivityManager.NetworkCallback() {
                    @Override
                    public void onAvailable(Network network) {
                        boolean networkChanged = (lastKnownNetwork != null && !network.equals(lastKnownNetwork));
                        boolean hadNoNetwork = (lastKnownNetwork == null);
                        lastKnownNetwork = network;
                        if (networkChanged || (hadNoNetwork && currentConn == null)) {
                            boolean wifi = DeviceManager.isWifiActive(AuthService.this);
                            Log.i(TAG, "Network route changed/available (Wi-Fi=" + wifi + "). Rapidly adapting active connection...");
                            NetworkUtils.forgetProofs();
                            DeviceManager.setActiveUrl(AuthService.this, null);
                            abortCurrentConnectionAndWake();

                            if (wifi && CaptivePortalManager.isEnabled(AuthService.this)
                                    && CaptivePortalManager.isAutoLoginOnWifi(AuthService.this)) {
                                CaptivePortalManager.setExplicitlyLoggedOut(AuthService.this, false);
                                checkAndAutoLoginIfLoggedOut("Wi-Fi network connected");
                            }
                        }
                    }

                    @Override
                    public void onCapabilitiesChanged(Network network, NetworkCapabilities networkCapabilities) {
                        if (CaptivePortalManager.isEnabled(AuthService.this)
                                && CaptivePortalManager.isAutoLoginOnWifi(AuthService.this)
                                && DeviceManager.isWifiActive(AuthService.this)
                                && !CaptivePortalManager.isExplicitlyLoggedOut(AuthService.this)) {
                            boolean hasCaptive = networkCapabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_CAPTIVE_PORTAL);
                            boolean isValidated = networkCapabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED);
                            if (hasCaptive || !isValidated) {
                                checkAndAutoLoginIfLoggedOut("Capabilities changed: captive=" + hasCaptive + ", validated=" + isValidated);
                            }
                        }
                    }

                    @Override
                    public void onLost(Network network) {
                        Log.i(TAG, "Active network interface lost. Adapting connection immediately...");
                        NetworkUtils.forgetProofs();
                        if (network.equals(lastKnownNetwork)) {
                            lastKnownNetwork = null;
                        }
                        DeviceManager.setActiveUrl(AuthService.this, null);
                        abortCurrentConnectionAndWake();
                    }
                };
                cm.registerDefaultNetworkCallback(defaultNetworkCallback);
            }
        } catch (Exception e) {
            Log.w(TAG, "Network monitoring registration error: " + e.getMessage());
        }
    }

    private boolean verifyLaptopAt(String ip, int port, PairedDevice active) {
        return NetworkUtils.proveComputer(ip, port, active);
    }

    private void startUdpBeaconListener() {
        new Thread(() -> {
            DatagramSocket udpSock = null;
            try {
                udpSock = new DatagramSocket(1760);
                udpSock.setReuseAddress(true);
                udpSock.setSoTimeout(30000);
                byte[] buf = new byte[1024];
                while (isRunning) {
                    try {
                        DatagramPacket packet = new DatagramPacket(buf, buf.length);
                        udpSock.receive(packet);
                        String msg = new String(packet.getData(), 0, packet.getLength(), "UTF-8");
                        JSONObject json = new JSONObject(msg);
                        String type = json.optString("type", "");
                        if ("pc_auth_discovery_reply".equals(type) || "pc_auth_beacon".equals(type)) {
                            String host = json.optString("hostname", "");
                            String newIp = packet.getAddress().getHostAddress();
                            int port = json.optInt("port", 1760);

                            PairedDevice active = DeviceManager.getActiveDevice(this);
                            String devId = json.optString("device_id", null);
                            if (active != null && ((devId != null && devId.equals(active.deviceId)) || host.equalsIgnoreCase(active.hostname))) {
                                // Beacons are unauthenticated broadcasts: anyone on the Wi-Fi can send one.
                                // Only move to a new address once it proves it holds our pairing secret,
                                // and never take an internet URL from a beacon (status calls supply it).
                                String expectedLocal = "http://" + newIp + ":" + port;
                                if (!active.ip.equals(newIp) || !expectedLocal.equals(active.localUrl)) {
                                    if (verifyLaptopAt(newIp, port, active)) {
                                        active.ip = newIp;
                                        active.port = port;
                                        active.localUrl = expectedLocal;
                                        Log.i(TAG, "Beacon: verified computer moved to " + newIp);
                                        DeviceManager.addOrUpdateDevice(this, active);
                                        abortCurrentConnectionAndWake();
                                    } else {
                                        Log.w(TAG, "Beacon: ignoring unverified computer address " + newIp);
                                    }
                                }
                            }
                        }
                    } catch (Exception ignored) {}
                }
            } catch (Exception e) {
                Log.w(TAG, "Cannot bind UDP listener: " + e.getMessage());
            } finally {
                if (udpSock != null) udpSock.close();
            }
        }).start();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // Started again whenever the app opens: keep showing the current state, not "Connecting"
        startForeground(NOTIFICATION_ID_FOREGROUND, buildForegroundNotification(
                lastForegroundStatus != null ? lastForegroundStatus : "Launching into orbit…"));

        if (intent != null && ACTION_RECONNECT.equals(intent.getAction())) {
            if (currentConn == null && workerThread != null) {
                workerThread.interrupt();
            }
        }

        if (phoneStateReceiver == null) {
            initTelephonyListener();
        }

        if (!isRunning) {
            isRunning = true;
            workerThread = new Thread(this::listenLoop);
            workerThread.start();
        }

        return START_STICKY;
    }

    private HttpURLConnection openStreamConnection(PairedDevice active, String baseUrl, int connectTimeoutMs) throws Exception {
        URL url = new URL(baseUrl + "/login/json");
        HttpURLConnection conn = (HttpURLConnection) url.openConnection();
        conn.setRequestMethod("GET");
        conn.setConnectTimeout(connectTimeoutMs);
        // The computer sends a keepalive every ~170 s on Wi-Fi (50 s through the tunnel), so the
        // radio isn't woken more than needed; allow a bit more than that before reconnecting
        conn.setReadTimeout(baseUrl.startsWith("https://") ? 100000 : 200000);
        conn.setRequestProperty("Accept", "application/x-ndjson");
        NetworkUtils.applyTunnelHeaders(conn);
        if (active.isPaired()) {
            NetworkUtils.setAuth(conn, active.authToken);
        }
        return conn;
    }

    private void handleTunnelUpdateEvent(JSONObject json) {
        String newUrl = json.optString("internet_url", "");
        if (!newUrl.isEmpty()) {
            Log.i(TAG, "Received updated internet tunnel URL from computer: " + newUrl);
            PairedDevice dev = DeviceManager.getActiveDevice(this);
            if (dev != null && !newUrl.equals(dev.internetUrl)) {
                dev.internetUrl = newUrl;
                DeviceManager.addOrUpdateDevice(this, dev);
            }
        }
    }

    private void listenLoop() {
        int consecutiveFails = 0;
        while (isRunning) {
            if (Thread.interrupted() || isSwitchingNetwork) {
                isSwitchingNetwork = false;
                consecutiveFails = 0;
            }

            PairedDevice active = DeviceManager.getActiveDevice(this);
            String localUrl = active.getLocalUrl();
            String internetUrl = active.getInternetUrl();
            boolean wifiActive = DeviceManager.isWifiActive(this);

            String targetUrl;
            boolean isInternet;

            // Pick the active reachable route immediately without waiting for timeouts
            if (!wifiActive && internetUrl != null && !internetUrl.isEmpty()) {
                targetUrl = internetUrl;
                isInternet = true;
            } else if (localUrl != null && !localUrl.isEmpty()) {
                targetUrl = localUrl;
                isInternet = false;
            } else if (internetUrl != null && !internetUrl.isEmpty()) {
                targetUrl = internetUrl;
                isInternet = true;
            } else {
                targetUrl = active.getBaseUrl();
                isInternet = targetUrl.startsWith("https://");
            }

            HttpURLConnection conn = null;
            try {
                int timeoutMs = isInternet ? 6000 : 2000;
                conn = openStreamConnection(active, targetUrl, timeoutMs);
                currentConn = conn;

                int code = -1;
                try {
                    code = conn.getResponseCode();
                } catch (Exception connectEx) {
                    if (conn != null) {
                        try { conn.disconnect(); } catch (Exception ignored) {}
                        conn = null;
                    }
                    currentConn = null;

                    // Immediate symmetric failover if chosen route fails
                    if (!isInternet && internetUrl != null && !internetUrl.isEmpty() && !internetUrl.equals(targetUrl)) {
                        Log.i(TAG, "Local connection unreachable (" + connectEx.getMessage() + "), failing over to Internet Tunnel immediately: " + internetUrl);
                        targetUrl = internetUrl;
                        isInternet = true;
                        conn = openStreamConnection(active, targetUrl, 6000);
                        currentConn = conn;
                        code = conn.getResponseCode();
                    } else if (isInternet && localUrl != null && !localUrl.isEmpty() && !localUrl.equals(targetUrl)) {
                        Log.i(TAG, "Internet Tunnel unreachable (" + connectEx.getMessage() + "), attempting local connection: " + localUrl);
                        targetUrl = localUrl;
                        isInternet = false;
                        conn = openStreamConnection(active, targetUrl, 2000);
                        currentConn = conn;
                        code = conn.getResponseCode();
                    } else {
                        throw connectEx;
                    }
                }

                currentConn = conn;
                if (code == 200) {
                    consecutiveFails = 0;
                    DeviceManager.setActiveUrl(AuthService.this, targetUrl);
                    active.activeUrl = targetUrl;

                    broadcastStatus(true, "Orbiting " + active.hostname, isInternet);
                    updateForegroundNotification("Orbiting " + active.hostname + (isInternet ? " · over the internet" : ""));
                    sendPhoneStatusToPc(active);

                    try (InputStream is = conn.getInputStream();
                         BufferedReader reader = new BufferedReader(new InputStreamReader(is, "UTF-8"))) {
                        String line;
                        while (isRunning && (line = reader.readLine()) != null) {
                            line = line.trim();
                            if (line.isEmpty()) continue;

                            try {
                                JSONObject json = new JSONObject(line);
                                String event = json.optString("event", "");
                                if ("message".equals(event)) {
                                    handleChallengeEvent(json);
                                } else if ("cancelled".equals(event)) {
                                    cancelChallengeNotification(AuthService.this);
                                    Intent updateIntent = new Intent("com.lunarphoton.pcauthenticator.CHALLENGE_RESOLVED");
                                    sendBroadcast(updateIntent);
                                } else if ("ring".equals(event)) {
                                    RingManager.startAlarm(AuthService.this);
                                } else if ("unring".equals(event) || "stop_alarm".equals(event)) {
                                    RingManager.stopAlarm(AuthService.this);
                                } else if ("clipboard".equals(event)) {
                                    handleClipboardEvent(json);
                                } else if ("incoming_file".equals(event)) {
                                    handleIncomingFileEvent(json);
                                } else if ("tunnel_update".equals(event)) {
                                    handleTunnelUpdateEvent(json);
                                } else if ("unpaired".equals(event)) {
                                    handleUnpairedEvent();
                                }
                            } catch (Exception e) {
                                Log.e(TAG, "JSON parse error", e);
                            }
                        }
                    }
                } else if (code == 401) {
                    consecutiveFails = 0;
                    broadcastStatus(false, "Not paired · open Computers to pair");
                    updateForegroundNotification("Not paired · tap to pair");
                } else {
                    consecutiveFails++;
                    broadcastStatus(false, "Reconnecting…");
                    updateForegroundNotification("Looking for your computer in the sky…");
                }
            } catch (Exception e) {
                if (isSwitchingNetwork) {
                    Log.i(TAG, "Stream connection closed for rapid network interface handoff.");
                } else {
                    consecutiveFails++;
                    Log.w(TAG, "Connection loop error: " + e.getMessage() + " (fail #" + consecutiveFails + ")");
                    broadcastStatus(false, "Reconnecting…");
                    updateForegroundNotification("Looking for your computer in the sky…");
                }

            } finally {
                if (conn != null) {
                    try { conn.disconnect(); } catch (Exception ignored) {}
                }
                currentConn = null;
            }

            if (isRunning) {
                if (isSwitchingNetwork) {
                    // Zero sleep delay on network handoff! Reconnect immediately!
                    isSwitchingNetwork = false;
                    consecutiveFails = 0;
                } else {
                    try {
                        // Adaptive battery-conserving backoff:
                        // Immediate recovery for transient network drops: 1s, 3s, 8s
                        // Extended low-power idle when PC is offline: 15s, 30s, 60s, capping at 90s max.
                        // Instant wakeup: Network changes (Wi-Fi, 4G) or UI foreground immediately wake with 0ms delay.
                        long backoff;
                        if (consecutiveFails <= 1) {
                            backoff = 1000L;
                        } else if (consecutiveFails == 2) {
                            backoff = 3000L;
                        } else if (consecutiveFails == 3) {
                            backoff = 8000L;
                        } else if (consecutiveFails == 4) {
                            backoff = 15000L;
                        } else if (consecutiveFails == 5) {
                            backoff = 30000L;
                        } else if (consecutiveFails < 10) {
                            backoff = 60000L;
                        } else {
                            backoff = 90000L;
                        }
                        Thread.sleep(backoff);
                    } catch (InterruptedException e) {
                        isSwitchingNetwork = false;
                        consecutiveFails = 0;
                    }
                }
            }
        }
    }

    private void handleUnpairedEvent() {
        Log.w(TAG, "Device was unpaired by computer");
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active != null) {
            active.authToken = "";
            active.secretKey = "";
            DeviceManager.addOrUpdateDevice(this, active);
        }
        cancelChallengeNotification(this);
        broadcastStatus(false, "Unpaired by the computer");
        updateForegroundNotification("Unpaired by the computer · tap to pair");
        Intent updateIntent = new Intent("com.lunarphoton.pcauthenticator.CHALLENGE_RESOLVED");
        sendBroadcast(updateIntent);
    }

    private String lastChallengeSessionId = null;
    private long lastChallengeTimestamp = 0;

    private void handleChallengeEvent(JSONObject json) {
        String title = json.optString("title", "Sign-in request");
        String message = json.optString("message", "Screen unlock requested on your computer. Tap Approve to unlock.");
        String sessionId = json.optString("id", "");

        long now = System.currentTimeMillis();
        if (sessionId != null && !sessionId.isEmpty() && sessionId.equals(lastChallengeSessionId) && (now - lastChallengeTimestamp < 25000L)) {
            Log.d(TAG, "Ignoring duplicate challenge event for session: " + sessionId);
            return;
        }
        lastChallengeSessionId = sessionId;
        lastChallengeTimestamp = now;

        // 0. Save active challenge into shared preferences so MainActivity renders it instantly
        SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        prefs.edit()
             .putString("active_challenge_id", sessionId)
             .putString("active_challenge_msg", message)
             .putLong("active_challenge_expiry", System.currentTimeMillis() + 35000L)
             .apply();

        // 1. Wake up the phone screen immediately (turn display ON)
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            try {
                PowerManager.WakeLock wl = pm.newWakeLock(
                        PowerManager.FULL_WAKE_LOCK | PowerManager.ACQUIRE_CAUSES_WAKEUP | PowerManager.ON_AFTER_RELEASE,
                        "PCAuth::AlertWakeScreen"
                );
                wl.acquire(15000); // Keep screen lit for 15s
            } catch (Exception ignored) {}
        }

        // 2. Trigger high-priority notification with FullScreenIntent & Lock Screen actions
        triggerChallengeNotification(title, message, sessionId);

        boolean requireBiometric = prefs.getBoolean("require_biometric", true);

        // 3. Launch MainActivity over keyguard
        Intent lockIntent = new Intent(this, MainActivity.class);
        lockIntent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        lockIntent.putExtra("from_challenge", true);
        lockIntent.putExtra("auto_biometric", requireBiometric);
        lockIntent.putExtra("message", message);
        lockIntent.putExtra("session_id", sessionId);
        try {
            startActivity(lockIntent);
        } catch (Exception ignored) {}

        Intent intent = new Intent(ACTION_CHALLENGE);
        intent.putExtra("title", title);
        intent.putExtra("message", message);
        intent.putExtra("session_id", sessionId);
        sendBroadcast(intent);

        vibrateStrong();
    }

    private void triggerChallengeNotification(String title, String message, String sessionId) {
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;

        SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        boolean requireBiometric = prefs.getBoolean("require_biometric", true);

        // Intent to launch MainActivity when tapped or as full-screen alert
        Intent mainIntent = new Intent(this, MainActivity.class);
        mainIntent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        mainIntent.putExtra("from_challenge", true);
        mainIntent.putExtra("auto_biometric", requireBiometric);
        mainIntent.putExtra("message", message);
        mainIntent.putExtra("session_id", sessionId);

        PendingIntent mainPending = PendingIntent.getActivity(
                this, 0, mainIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0)
        );

        PendingIntent fullScreenPending = PendingIntent.getActivity(
                this, 100, mainIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0)
        );

        // Approve Intent (Activity with biometric prompt OR direct Broadcast if disabled)
        PendingIntent approvePending;
        String approveTitle;
        if (requireBiometric) {
            approveTitle = "Approve";
            Intent approveActIntent = new Intent(this, MainActivity.class);
            approveActIntent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
            approveActIntent.putExtra("from_challenge", true);
            approveActIntent.putExtra("auto_biometric", true);
            approveActIntent.putExtra("message", message);
            approveActIntent.putExtra("session_id", sessionId);
            approvePending = PendingIntent.getActivity(
                    this, 1, approveActIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0)
            );
        } else {
            approveTitle = "Approve";
            Intent approveIntent = new Intent(this, ActionReceiver.class);
            approveIntent.setAction(ActionReceiver.ACTION_APPROVE);
            approveIntent.putExtra("session_id", sessionId);
            approvePending = PendingIntent.getBroadcast(
                    this, 1, approveIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0)
            );
        }

        // Deny Intent
        Intent denyIntent = new Intent(this, ActionReceiver.class);
        denyIntent.setAction(ActionReceiver.ACTION_DENY);
        denyIntent.putExtra("session_id", sessionId);
        PendingIntent denyPending = PendingIntent.getBroadcast(
                this, 2, denyIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0)
        );

        // Public version shown on lock screen with explicit action buttons
        Notification.Builder publicBuilder;
        Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            publicBuilder = new Notification.Builder(this, CHANNEL_CHALLENGE);
            builder = new Notification.Builder(this, CHANNEL_CHALLENGE);
        } else {
            publicBuilder = new Notification.Builder(this);
            builder = new Notification.Builder(this);
        }

        publicBuilder.setSmallIcon(R.drawable.ic_o_notify)
                     .setContentTitle(title)
                     .setContentText(message)
                     .setContentIntent(mainPending)
                     .setStyle(new Notification.BigTextStyle().bigText(message).setBigContentTitle(title))
                     .addAction(new Notification.Action.Builder(
                             android.R.drawable.checkbox_on_background, approveTitle, approvePending).build())
                     .addAction(new Notification.Action.Builder(
                             android.R.drawable.ic_delete, "Deny", denyPending).build());

        builder.setSmallIcon(R.drawable.ic_o_notify)
               .setContentTitle(title)
               .setContentText(message)
               .setContentIntent(mainPending)
               .setFullScreenIntent(fullScreenPending, true)
               .setPublicVersion(publicBuilder.setAutoCancel(true).build())
               .setStyle(new Notification.BigTextStyle().bigText(message).setBigContentTitle(title))
               .setPriority(Notification.PRIORITY_MAX)
               .setDefaults(Notification.DEFAULT_ALL)
               .setAutoCancel(true)
               .setOngoing(false)
               .addAction(new Notification.Action.Builder(
                       android.R.drawable.checkbox_on_background, approveTitle, approvePending).build())
               .addAction(new Notification.Action.Builder(
                       android.R.drawable.ic_delete, "Deny", denyPending).build());

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            builder.setCategory(Notification.CATEGORY_CALL);
            builder.setVisibility(Notification.VISIBILITY_PUBLIC);
        }

        nm.notify(NOTIFICATION_ID_CHALLENGE, builder.build());

        // Auto-cancel notification after 35 seconds when challenge expires
        new android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(() -> {
            cancelChallengeNotification(AuthService.this);
        }, 35000L);
    }

    public static void cancelChallengeNotification(Context context) {
        try {
            NotificationManager nm = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.cancel(NOTIFICATION_ID_CHALLENGE);
            }
            context.getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE)
                    .edit()
                    .remove("active_challenge_id")
                    .remove("active_challenge_msg")
                    .remove("active_challenge_expiry")
                    .apply();
        } catch (Exception ignored) {}
    }

    private Notification buildForegroundNotification(String status) {
        Intent mainIntent = new Intent(this, MainActivity.class);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this, 0, mainIntent,
                Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0
        );

        Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            builder = new Notification.Builder(this, CHANNEL_FOREGROUND);
        } else {
            builder = new Notification.Builder(this);
            builder.setPriority(Notification.PRIORITY_MIN);
        }

        return builder.setSmallIcon(R.drawable.ic_o_notify)
                .setContentTitle("Orbit")
                .setContentText(status)
                .setContentIntent(pendingIntent)
                .setOngoing(true)
                .build();
    }

    private String lastForegroundStatus = null;

    private void updateForegroundNotification(String status) {
        if (status == null || status.equals(lastForegroundStatus)) {
            return;
        }
        lastForegroundStatus = status;
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) {
            nm.notify(NOTIFICATION_ID_FOREGROUND, buildForegroundNotification(status));
        }
    }

    private void broadcastStatus(boolean connected, String statusText) {
        broadcastStatus(connected, statusText, DeviceManager.isInternetActive(this));
    }

    private void broadcastStatus(boolean connected, String statusText, boolean isInternet) {
        isConnected = connected;
        lastStatusText = statusText;
        Intent intent = new Intent(ACTION_STATUS);
        intent.putExtra("connected", connected);
        intent.putExtra("status_text", statusText);
        intent.putExtra("is_internet", isInternet);
        sendBroadcast(intent);
    }

    private void vibrateStrong() {
        try {
            Vibrator v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
            if (v != null && v.hasVibrator()) {
                long[] pattern = {0, 250, 150, 250};
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    v.vibrate(VibrationEffect.createWaveform(pattern, -1));
                } else {
                    v.vibrate(pattern, -1);
                }
            }
        } catch (Exception ignored) {}
    }

    private void createNotificationChannels() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) return;

            // 1. Foreground Status Channel (Silent)
            NotificationChannel fgChan = new NotificationChannel(
                    CHANNEL_FOREGROUND,
                    "Service Status",
                    NotificationManager.IMPORTANCE_LOW
            );
            fgChan.setDescription("Shows active connection state to your computer");
            fgChan.setShowBadge(false);
            nm.createNotificationChannel(fgChan);

            // 2. High-Priority Challenge Channel (Sound & Vibration)
            NotificationChannel chChan = new NotificationChannel(
                    CHANNEL_CHALLENGE,
                    "computer Unlock Requests",
                    NotificationManager.IMPORTANCE_HIGH
            );
            chChan.setDescription("Alerts for 2FA screen unlock challenges");
            chChan.enableVibration(true);
            chChan.enableLights(true);
            chChan.setLightColor(Color.CYAN);
            chChan.setBypassDnd(true);
            chChan.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
            nm.createNotificationChannel(chChan);
        }
    }

    private void sendPhoneStatusToPc(PairedDevice active) {
        if (active == null) return;
        new Thread(() -> {
            try {
                Intent batteryIntent = registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
                int level = -1;
                boolean isCharging = false;
                if (batteryIntent != null) {
                    int rawLevel = batteryIntent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                    int scale = batteryIntent.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
                    if (rawLevel >= 0 && scale > 0) {
                        level = (rawLevel * 100) / scale;
                    }
                    int status = batteryIntent.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
                    isCharging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
                                 status == BatteryManager.BATTERY_STATUS_FULL;
                }

                // Storage stats
                long storageFree = 0;
                long storageTotal = 0;
                try {
                    File extDir = Environment.getExternalStorageDirectory();
                    StatFs stat = new StatFs(extDir.getPath());
                    long blockSize = stat.getBlockSizeLong();
                    storageFree = stat.getAvailableBlocksLong() * blockSize;
                    storageTotal = stat.getBlockCountLong() * blockSize;
                } catch (Exception ignored) {}

                JSONObject body = new JSONObject();
                if (level >= 0) body.put("battery", level);
                body.put("charging", isCharging);
                body.put("port", FileServer.PORT);

                String manufacturer = Build.MANUFACTURER != null ? Build.MANUFACTURER : "";
                String model = Build.MODEL != null ? Build.MODEL : "Android";
                String fullName = model.toLowerCase().startsWith(manufacturer.toLowerCase()) ? model : (manufacturer + " " + model);
                body.put("device_name", fullName.trim());
                body.put("model", model);
                body.put("manufacturer", manufacturer);
                body.put("android_version", Build.VERSION.RELEASE);
                body.put("sdk_int", Build.VERSION.SDK_INT);
                body.put("storage_free", storageFree);
                body.put("storage_total", storageTotal);

                HttpURLConnection conn = (HttpURLConnection) new URL(active.getBaseUrl() + "/api/phone/status").openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("POST");
                conn.setRequestProperty("Content-Type", "application/json");
                if (active.isPaired()) {
                    NetworkUtils.setAuth(conn, active.authToken);
                }
                conn.setDoOutput(true);
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(body.toString().getBytes("UTF-8"));
                }
                conn.getResponseCode();
                conn.disconnect();
            } catch (Exception ignored) {}
        }).start();
    }

    /** The computer sent its clipboard: offer it, don't overwrite the phone's clipboard. */
    private void handleClipboardEvent(JSONObject json) {
        String text = json.optString("text", "");
        if (text.isEmpty()) return;
        try {
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) return;
            Intent copy = new Intent(this, ActionReceiver.class);
            copy.setAction(ActionReceiver.ACTION_COPY_CLIPBOARD);
            copy.putExtra("text", text);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
            PendingIntent pi = PendingIntent.getBroadcast(this, 4100, copy, flags);
            String preview = text.length() > 300 ? text.substring(0, 300) + "…" : text;
            Notification.Builder b = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                    ? new Notification.Builder(this, FileServer.CHANNEL_FILE) : new Notification.Builder(this);
            b.setSmallIcon(R.drawable.ic_o_clipboard)
                    .setContentTitle("Clipboard from computer")
                    .setContentText(preview.replace('\n', ' '))
                    .setStyle(new Notification.BigTextStyle().bigText(preview))
                    .setContentIntent(pi)                      // tapping it copies too
                    .addAction(new Notification.Action.Builder(null, "Copy", pi).build())
                    .setAutoCancel(true);
            nm.notify(ActionReceiver.CLIPBOARD_NOTIF_ID, b.build());
        } catch (Exception e) {
            Log.w(TAG, "Clipboard notification error: " + e.getMessage());
        }
    }

    private void handleIncomingFileEvent(JSONObject json) {
        String stagingPath = json.optString("staging_path", "");
        String downloadUrl = json.optString("download_url", "");
        String internetDl = json.optString("internet_download_url", "");
        String filename = json.optString("filename", "received_file");

        if (stagingPath.isEmpty() && !downloadUrl.isEmpty()) {
            if (downloadUrl.startsWith("/")) {
                stagingPath = downloadUrl;
            } else {
                try {
                    URL u = new URL(downloadUrl);
                    stagingPath = u.getFile();
                } catch (Exception ignored) {}
            }
        }

        PairedDevice active = DeviceManager.getActiveDevice(this);
        List<String> candidateUrls = new ArrayList<>();
        if (active != null && !active.getBaseUrl().isEmpty() && !stagingPath.isEmpty()) {
            String activeTarget = active.getBaseUrl() + stagingPath;
            if (!candidateUrls.contains(activeTarget)) candidateUrls.add(activeTarget);
        }
        if (!internetDl.isEmpty() && !candidateUrls.contains(internetDl)) {
            candidateUrls.add(internetDl);
        }
        if (!downloadUrl.isEmpty() && downloadUrl.startsWith("http") && !candidateUrls.contains(downloadUrl)) {
            candidateUrls.add(downloadUrl);
        }

        if (candidateUrls.isEmpty()) return;

        new Thread(() -> {
            boolean success = false;
            File dest = null;
            String mimeType = "";
            String lastFailure = null;

            for (String tryUrl : candidateUrls) {
                HttpURLConnection conn = null;
                OutputStream fos = null;
                try {
                    conn = (HttpURLConnection) new URL(tryUrl).openConnection();
                    NetworkUtils.applyTunnelHeaders(conn);
                    conn.setConnectTimeout(8000);
                    conn.setReadTimeout(60000);
                    if (active != null && active.isPaired()) {
                        NetworkUtils.setAuth(conn, active.authToken);
                    }

                    int responseCode = conn.getResponseCode();
                    if (responseCode != 200) {
                        Log.w(TAG, "Candidate URL " + tryUrl + " returned HTTP " + responseCode);
                        conn.disconnect();
                        continue;
                    }

                    File destDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
                    if (!destDir.exists()) destDir.mkdirs();
                    dest = new File(destDir, filename);
                    int c = 1;
                    String base = filename;
                    String ext = "";
                    int dot = filename.lastIndexOf(".");
                    if (dot > 0) {
                        base = filename.substring(0, dot);
                        ext = filename.substring(dot);
                    }
                    while (dest.exists()) {
                        dest = new File(destDir, base + " (" + c + ")" + ext);
                        c++;
                    }

                    mimeType = PCFileProvider.getMimeType(dest.getName());

                    try {
                        fos = new FileOutputStream(dest);
                    } catch (Exception directEx) {
                        Log.w(TAG, "Direct FileOutputStream failed, trying MediaStore: " + directEx.getMessage());
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                            try {
                                ContentValues values = new ContentValues();
                                values.put(MediaStore.MediaColumns.DISPLAY_NAME, dest.getName());
                                values.put(MediaStore.MediaColumns.MIME_TYPE, mimeType);
                                values.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
                                Uri insertedUri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                                if (insertedUri != null) {
                                    fos = getContentResolver().openOutputStream(insertedUri);
                                }
                            } catch (Exception mediaEx) {
                                Log.e(TAG, "MediaStore insert error", mediaEx);
                            }
                        }
                    }

                    if (fos != null) {
                        long expected = conn.getContentLengthLong(), written = 0;
                        try (InputStream in = conn.getInputStream();
                             OutputStream outStream = fos) {
                            byte[] buf = new byte[65536];
                            int r;
                            while ((r = in.read(buf)) != -1) {
                                outStream.write(buf, 0, r);
                                written += r;
                            }
                            outStream.flush();
                        }
                        if (expected > 0 && written != expected) {
                            lastFailure = "the transfer was cut off";
                            throw new java.io.IOException("got " + written + " of " + expected + " bytes");
                        }
                        success = true;
                        break;
                    }
                } catch (Exception e) {
                    Log.w(TAG, "Error trying URL " + tryUrl + ": " + e.getMessage());
                    if (dest != null && dest.exists()) {
                        dest.delete();          // never leave a partial file behind
                    }
                } finally {
                    if (conn != null) {
                        try { conn.disconnect(); } catch (Exception ignored) {}
                    }
                }
            }

            if (success && dest != null) {
                try {
                    MediaScannerConnection.scanFile(this, new String[]{dest.getAbsolutePath()}, new String[]{mimeType}, null);
                } catch (Exception ignored) {}

                showFileNotification(dest);
                new Handler(Looper.getMainLooper()).post(() -> {
                    Toast.makeText(AuthService.this, "Received: " + filename, Toast.LENGTH_SHORT).show();
                });
            } else {
                Log.e(TAG, "Failed to download incoming file from all candidate URLs: " + filename);
                FileServer.showTransferFailed(AuthService.this, filename,
                        lastFailure != null ? lastFailure : "the computer couldn't be reached");
            }
        }).start();
    }

    private void showFileNotification(File file) {
        try {
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) return;

            String mimeType = PCFileProvider.getMimeType(file.getAbsolutePath());
            Uri fileUri = PCFileProvider.getUriForFile(this, file);
            boolean isApk = file.getName().toLowerCase().endsWith(".apk") || "application/vnd.android.package-archive".equals(mimeType);
            if (isApk) {
                mimeType = "application/vnd.android.package-archive";
            }

            Intent viewIntent;
            if (isApk && Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !getPackageManager().canRequestPackageInstalls()) {
                viewIntent = new Intent(this, MainActivity.class);
                viewIntent.setAction("com.lunarphoton.pcauthenticator.INSTALL_APK");
                viewIntent.putExtra("apk_path", file.getAbsolutePath());
                viewIntent.setDataAndType(fileUri, "application/vnd.android.package-archive");
                viewIntent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                viewIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
            } else {
                viewIntent = new Intent(Intent.ACTION_VIEW);
                viewIntent.setDataAndType(fileUri, mimeType);
                viewIntent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                viewIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                if (isApk) {
                    viewIntent.putExtra(Intent.EXTRA_NOT_UNKNOWN_SOURCE, true);
                }
            }

            try {
                List<ResolveInfo> resInfoList = getPackageManager().queryIntentActivities(viewIntent, PackageManager.MATCH_DEFAULT_ONLY);
                for (ResolveInfo resolveInfo : resInfoList) {
                    grantUriPermission(resolveInfo.activityInfo.packageName, fileUri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
                }
            } catch (Exception ignored) {}

            if (isApk) {
                String[] commonInstallers = {"com.google.android.packageinstaller", "com.android.packageinstaller", "com.google.android.permissioncontroller"};
                for (String pkg : commonInstallers) {
                    try {
                        grantUriPermission(pkg, fileUri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    } catch (Exception ignored) {}
                }
            }

            PendingIntent pi = PendingIntent.getActivity(
                    this, (int) System.currentTimeMillis(), viewIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0)
            );

            Notification.Builder builder;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                builder = new Notification.Builder(this, FileServer.CHANNEL_FILE);
            } else {
                builder = new Notification.Builder(this);
            }

            String typeLabel = "File";
            if (isApk) typeLabel = "Android App (APK)";
            else if (mimeType.startsWith("image/")) typeLabel = "Image";
            else if (mimeType.startsWith("video/")) typeLabel = "Video";
            else if (mimeType.startsWith("audio/")) typeLabel = "Audio";
            else if (mimeType.equals("application/pdf")) typeLabel = "PDF Document";
            else if (mimeType.contains("word") || mimeType.contains("document")) typeLabel = "Document";
            else if (mimeType.contains("excel") || mimeType.contains("sheet")) typeLabel = "Spreadsheet";
            else if (mimeType.contains("zip") || mimeType.contains("compressed") || mimeType.contains("tar")) typeLabel = "Archive";

            if (isApk) {
                builder.setContentTitle("Android App Received")
                        .setContentText("Tap to install " + file.getName())
                        .setSubText("Install APK");
            } else {
                builder.setContentTitle("" + typeLabel + " Received from computer")
                        .setContentText(file.getName() + " (" + formatFileSize(file.length()) + ")")
                        .setSubText(typeLabel);
            }

            builder.setSmallIcon(R.drawable.ic_o_notify)
                    .setAutoCancel(true)
                    .setContentIntent(pi);

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                builder.setPriority(Notification.PRIORITY_HIGH)
                        .setVibrate(new long[]{0, 250, 150, 250});
            }

            nm.notify((int) (FileServer.NOTIF_BASE_ID + (System.currentTimeMillis() % 1000)), builder.build());
            Log.i(TAG, "Notification posted for incoming file: " + file.getName() + " (" + mimeType + ")");
        } catch (Throwable t) {
            Log.e(TAG, "Error posting file notification: " + t.getMessage(), t);
        }
    }

    public static String formatFileSize(long bytes) {
        if (bytes <= 0) return "0 B";
        final String[] units = new String[]{"B", "KB", "MB", "GB", "TB"};
        int digitGroups = (int) (Math.log10(bytes) / Math.log10(1024));
        digitGroups = Math.min(digitGroups, units.length - 1);
        return String.format(java.util.Locale.US, "%.1f %s", bytes / Math.pow(1024, digitGroups), units[digitGroups]);
    }

    private void initTelephonyListener() {
        if (checkSelfPermission(android.Manifest.permission.READ_PHONE_STATE) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "READ_PHONE_STATE not granted; waiting for user permission");
        }

        if (telephonyCallbackObj == null) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                telephonyCallbackObj = Api31TelephonyHelper.register(this, this);
            } else {
                telephonyCallbackObj = LegacyTelephonyHelper.register(this, this);
            }
        }

        try {
            if (phoneStateReceiver == null) {
                IntentFilter phoneFilter = new IntentFilter(TelephonyManager.ACTION_PHONE_STATE_CHANGED);
                phoneStateReceiver = new BroadcastReceiver() {
                    @Override
                    public void onReceive(Context context, Intent intent) {
                        if (TelephonyManager.ACTION_PHONE_STATE_CHANGED.equals(intent.getAction())) {
                            String stateStr = intent.getStringExtra(TelephonyManager.EXTRA_STATE);
                            if (stateStr != null) {
                                handlePhoneStateString(stateStr);
                            }
                        }
                    }
                };
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    registerReceiver(phoneStateReceiver, phoneFilter, Context.RECEIVER_EXPORTED);
                } else {
                    registerReceiver(phoneStateReceiver, phoneFilter);
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "phoneStateReceiver registration error: " + t.getMessage());
        }
    }

    void handlePhoneCallStateCode(int state) {
        String mappedState = "idle";
        if (state == TelephonyManager.CALL_STATE_RINGING) {
            mappedState = "ringing";
        } else if (state == TelephonyManager.CALL_STATE_OFFHOOK) {
            mappedState = "talking";
        } else if (state == TelephonyManager.CALL_STATE_IDLE) {
            mappedState = "idle";
        } else {
            return;
        }
        sendTelephonyCallState(mappedState);
    }

    void handlePhoneStateString(String stateStr) {
        String mappedState = "idle";
        if (TelephonyManager.EXTRA_STATE_RINGING.equalsIgnoreCase(stateStr)) {
            mappedState = "ringing";
        } else if (TelephonyManager.EXTRA_STATE_OFFHOOK.equalsIgnoreCase(stateStr)) {
            mappedState = "talking";
        } else if (TelephonyManager.EXTRA_STATE_IDLE.equalsIgnoreCase(stateStr)) {
            mappedState = "idle";
        } else {
            return;
        }
        sendTelephonyCallState(mappedState);
    }

    private void sendTelephonyCallState(String state) {
        if (state == null) return;
        synchronized (this) {
            if (state.equals(lastSentCallState)) {
                return;
            }
            lastSentCallState = state;
        }

        Log.i(TAG, "Dispatching phone call state to computer: " + state);
        new Thread(() -> {
            try {
                PairedDevice active = DeviceManager.getActiveDevice(AuthService.this);
                if (active == null) return;

                URL u = new URL(active.getBaseUrl() + "/api/telephony/call");
                HttpURLConnection conn = (HttpURLConnection) u.openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("POST");
                conn.setConnectTimeout(3000);
                conn.setReadTimeout(3000);
                conn.setDoOutput(true);
                conn.setRequestProperty("Content-Type", "application/json");
                if (active.isPaired() && active.authToken != null) {
                    NetworkUtils.setAuth(conn, active.authToken);
                }
                JSONObject payload = new JSONObject();
                payload.put("state", state);
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(payload.toString().getBytes("UTF-8"));
                }
                int code = conn.getResponseCode();
                Log.d(TAG, "Sent telephony state '" + state + "' to computer, response: " + code);
                conn.disconnect();
            } catch (Exception e) {
                Log.w(TAG, "Failed to send telephony state to computer: " + e.getMessage());
            }
        }).start();
    }

    private static class Api31TelephonyHelper {
        static Object register(Context context, AuthService service) {
            try {
                TelephonyManager tm = (TelephonyManager) context.getSystemService(Context.TELEPHONY_SERVICE);
                if (tm == null) return null;
                CallCallback cb = new CallCallback(service);
                tm.registerTelephonyCallback(context.getMainExecutor(), cb);
                return cb;
            } catch (Throwable t) {
                Log.w(TAG, "registerTelephonyCallback failed: " + t.getMessage());
                return null;
            }
        }

        static void unregister(Context context, Object cb) {
            try {
                if (cb instanceof TelephonyCallback) {
                    TelephonyManager tm = (TelephonyManager) context.getSystemService(Context.TELEPHONY_SERVICE);
                    if (tm != null) {
                        tm.unregisterTelephonyCallback((TelephonyCallback) cb);
                    }
                }
            } catch (Throwable ignored) {}
        }

        private static class CallCallback extends TelephonyCallback implements TelephonyCallback.CallStateListener {
            private final AuthService service;
            CallCallback(AuthService service) {
                this.service = service;
            }
            @Override
            public void onCallStateChanged(int state) {
                service.handlePhoneCallStateCode(state);
            }
        }
    }

    private static class LegacyTelephonyHelper {
        static Object register(Context context, AuthService service) {
            try {
                TelephonyManager tm = (TelephonyManager) context.getSystemService(Context.TELEPHONY_SERVICE);
                if (tm == null) return null;
                PhoneStateListener psl = new PhoneStateListener() {
                    @Override
                    public void onCallStateChanged(int state, String phoneNumber) {
                        service.handlePhoneCallStateCode(state);
                    }
                };
                tm.listen(psl, PhoneStateListener.LISTEN_CALL_STATE);
                return psl;
            } catch (Throwable t) {
                Log.w(TAG, "PhoneStateListener failed: " + t.getMessage());
                return null;
            }
        }

        static void unregister(Context context, Object psl) {
            try {
                if (psl instanceof PhoneStateListener) {
                    TelephonyManager tm = (TelephonyManager) context.getSystemService(Context.TELEPHONY_SERVICE);
                    if (tm != null) {
                        tm.listen((PhoneStateListener) psl, PhoneStateListener.LISTEN_NONE);
                    }
                }
            } catch (Throwable ignored) {}
        }
    }

    private void initClipboardListener() {
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm != null) {
                clipListener = () -> checkAndSyncPhoneClipboard(AuthService.this);
                cm.addPrimaryClipChangedListener(clipListener);
            }
        } catch (Exception e) {
            Log.w(TAG, "Clipboard listener init error: " + e.getMessage());
        }
    }

    public static void checkAndSyncPhoneClipboard(Context context) {
        try {
            ClipboardManager cm = (ClipboardManager) context.getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm == null || !cm.hasPrimaryClip() || cm.getPrimaryClip().getItemCount() == 0) return;
            ClipData.Item item = cm.getPrimaryClip().getItemAt(0);
            if (item == null) return;
            CharSequence seq = item.getText();
            if (seq == null) return;
            String text = seq.toString();
            if (text.isEmpty() || text.equals(lastSyncedClipboard)) return;

            lastSyncedClipboard = text;

            PairedDevice active = DeviceManager.getActiveDevice(context);
            if (active == null || !active.isPaired()) return;

            new Thread(() -> {
                try {
                    String url = active.getBaseUrl() + "/api/clipboard";
                    HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                    NetworkUtils.applyTunnelHeaders(conn);
                    conn.setRequestMethod("POST");
                    conn.setConnectTimeout(4000);
                    conn.setReadTimeout(4000);
                    conn.setRequestProperty("Content-Type", "application/json");
                    if (active.isPaired()) {
                        NetworkUtils.setAuth(conn, active.authToken);
                    }
                    conn.setDoOutput(true);
                    JSONObject body = new JSONObject().put("text", text);
                    try (OutputStream os = conn.getOutputStream()) {
                        os.write(body.toString().getBytes("UTF-8"));
                    }
                    int code = conn.getResponseCode();
                    conn.disconnect();
                    if (code == 200) {
                        Log.i("AuthService", "Auto-synced phone clipboard to computer (" + text.length() + " chars)");
                    }
                } catch (Exception e) {
                    Log.w("AuthService", "Auto clipboard sync failed: " + e.getMessage());
                }
            }).start();
        } catch (Exception ignored) {}
    }

    @Override
    public void onDestroy() {
        isRunning = false;
        if (captiveWatchdogHandler != null && captiveWatchdogRunnable != null) {
            captiveWatchdogHandler.removeCallbacks(captiveWatchdogRunnable);
            captiveWatchdogHandler = null;
        }
        if (clipListener != null) {
            try {
                ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                if (cm != null) {
                    cm.removePrimaryClipChangedListener(clipListener);
                }
            } catch (Exception ignored) {}
            clipListener = null;
        }
        if (fileServer != null) {
            fileServer.stop();
        }
        if (phoneStateReceiver != null) {
            try {
                unregisterReceiver(phoneStateReceiver);
            } catch (Exception ignored) {}
            phoneStateReceiver = null;
        }
        if (telephonyCallbackObj != null) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                Api31TelephonyHelper.unregister(this, telephonyCallbackObj);
            } else {
                LegacyTelephonyHelper.unregister(this, telephonyCallbackObj);
            }
            telephonyCallbackObj = null;
        }
        if (defaultNetworkCallback != null) {
            try {
                ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
                if (cm != null) {
                    cm.unregisterNetworkCallback(defaultNetworkCallback);
                }
            } catch (Exception ignored) {}
            defaultNetworkCallback = null;
        }
        abortCurrentConnectionAndWake();
        if (workerThread != null) workerThread.interrupt();
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}

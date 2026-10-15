package com.lunarphoton.pcauthenticator;

import android.Manifest;
import android.annotation.SuppressLint;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.net.wifi.ScanResult;
import android.net.wifi.WifiManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.SystemClock;
import android.telephony.SubscriptionInfo;
import android.telephony.SubscriptionManager;
import android.telephony.TelephonyManager;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * Find my phone: this phone tells the computer where it is, and nothing else hears it.
 * It reports when it moves, every 15 minutes, when the computer asks, and once more when the
 * battery is nearly flat. Each report lists the Wi-Fi networks around, so the computer learns
 * where they are and can place itself (it has no GPS) if it's ever taken somewhere this phone
 * has been. Reports that can't be sent wait here and go with the next one.
 * Also: an alert when the SIM card changes; the Wi-Fi it sees, for the nearby finder and the
 * computer's walk-away lock (no Bluetooth needed); and alerts from the computer.
 */
public final class FindMy {
    private static final String TAG = "FindMy";
    static final String PREFS = "pc_auth_prefs";
    static final String PREF_SHARE = "findmy_share";
    static final String PREF_PHOTO = "findmy_photo";
    static final String PREF_SIM = "findmy_sim";
    private static final String PREF_LAST = "findmy_last_report";
    private static final String PREF_QUEUE = "findmy_queue";
    private static final String PREF_SIM_PRINT = "findmy_sim_print";
    private static final String PREF_SIM_PENDING = "findmy_sim_pending", PREF_SIM_PENDING_AT = "findmy_sim_pending_at";
    private static final String PREF_LOW_SENT = "findmy_low_sent";
    static final String PREF_LAPTOP = "findmy_laptop";
    static final String PREF_WALK = "findmy_walk_away";          // the computer's setting, as last heard
    private static final String PREF_PRESENCE = "findmy_presence_at";
    private static final String PREF_ALERTS_AT = "findmy_alerts_checked";
    private static final String PREF_ALERT_SEEN = "findmy_alert_seen";
    private static final String ALERT_CHANNEL = "findmy_alerts";
    private static final String PREF_HISTORY = "findmy_history";   // the computer's alerts, kept here
    static final String PREF_LOST = "findmy_lost";               // the computer is marked lost

    static final String PREF_PHONE_LAST = "findmy_phone_last";

    private static final long EVERY_MS = 15 * 60_000L;
    private static final long MOVED_MIN_GAP_MS = 2 * 60_000L;
    private static final int LOW_BATTERY = 5;

    private static final ExecutorService worker = Executors.newSingleThreadExecutor();
    private static HandlerThread looperThread;
    private static LocationListener mover;
    private static long lastMovedReport;
    private static Location lastMovedAt;
    private static Runnable findPhoneLoop;
    static final java.util.UUID ORBIT_BLE_UUID = java.util.UUID.fromString("f1d0c0de-4f72-6269-7420-46696e644d79");
    private static android.bluetooth.le.AdvertiseCallback advertising;

    private FindMy() { }

    static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static boolean sharing(Context c) {
        return prefs(c).getBoolean(PREF_SHARE, true) && hasLocation(c);
    }

    static JSONObject phoneLast(Context c) {
        try {
            String s = prefs(c).getString(PREF_PHONE_LAST, null);
            return s == null ? null : new JSONObject(s);
        } catch (Exception e) {
            return null;
        }
    }

    static void rememberPhone(Context c, JSONObject fix) {
        if (fix != null) prefs(c).edit().putString(PREF_PHONE_LAST, fix.toString()).apply();
    }

    static boolean hasLocation(Context c) {
        return c.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    static boolean hasBackgroundLocation(Context c) {
        return Build.VERSION.SDK_INT < 29
                || c.checkSelfPermission(Manifest.permission.ACCESS_BACKGROUND_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    // ---- the upkeep tick (every 30 s, from AuthService) ----
    static void tick(Context ctx) {
        Context c = ctx.getApplicationContext();
        if (sharing(c)) {
            watchMovement(c);
            long last = prefs(c).getLong(PREF_LAST, 0);
            if (System.currentTimeMillis() - last > EVERY_MS) report(c, "periodic");
            checkBattery(c);
        }
        if (prefs(c).getBoolean(PREF_SIM, false)) checkSim(c);
        presence(c);
        checkLaptopAlerts(c);
    }

    private static void checkBattery(Context c) {
        Intent b = c.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        if (b == null) return;
        int level = b.getIntExtra(BatteryManager.EXTRA_LEVEL, -1), scale = b.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
        int status = b.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
        boolean charging = status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL;
        int pct = scale > 0 ? level * 100 / scale : level;
        boolean sent = prefs(c).getBoolean(PREF_LOW_SENT, false);
        if (!charging && pct >= 0 && pct <= LOW_BATTERY && !sent) {
            prefs(c).edit().putBoolean(PREF_LOW_SENT, true).apply();
            report(c, "low_battery");                // its last word before it dies
        } else if ((charging || pct > LOW_BATTERY + 5) && sent) {
            prefs(c).edit().putBoolean(PREF_LOW_SENT, false).apply();
        }
    }

    /** Report again whenever the phone has moved a few hundred metres. */
    @SuppressLint("MissingPermission")
    private static synchronized void watchMovement(Context c) {
        if (mover != null || !hasLocation(c)) return;
        LocationManager lm = (LocationManager) c.getSystemService(Context.LOCATION_SERVICE);
        if (lm == null) return;
        if (looperThread == null) {
            looperThread = new HandlerThread("findmy-location");
            looperThread.start();
        }
        mover = new LocationListener() {
            @Override public void onLocationChanged(Location l) {
                long now = System.currentTimeMillis();
                if (now - lastMovedReport < MOVED_MIN_GAP_MS) return;
                // indoors a fix wobbles by ~100 m: only real movement, beyond both fixes' accuracy
                if (lastMovedAt != null && l.distanceTo(lastMovedAt) < 250 + l.getAccuracy() + lastMovedAt.getAccuracy()) return;
                lastMovedAt = l;
                lastMovedReport = now;
                report(c, "moved");
            }
            @Override public void onStatusChanged(String p, int s, Bundle e) { }
            @Override public void onProviderEnabled(String p) { }
            @Override public void onProviderDisabled(String p) { }
        };
        try {
            // the network provider: Wi-Fi and cell towers, cheap on battery; GPS only when asked
            String provider = lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)
                    ? LocationManager.NETWORK_PROVIDER : LocationManager.PASSIVE_PROVIDER;
            lm.requestLocationUpdates(provider, 120_000L, 250f, mover, looperThread.getLooper());
        } catch (Exception e) {
            Log.w(TAG, "Can't watch for movement: " + e.getMessage());
            mover = null;
        }
    }

    static synchronized void stopWatching(Context c) {
        if (mover == null) return;
        LocationManager lm = (LocationManager) c.getSystemService(Context.LOCATION_SERVICE);
        try { if (lm != null) lm.removeUpdates(mover); } catch (Exception ignored) { }
        mover = null;
    }

    // ---- a report ----
    static void report(Context ctx, String reason) {
        Context c = ctx.getApplicationContext();
        if (!sharing(c)) return;
        prefs(c).edit().putLong(PREF_LAST, System.currentTimeMillis()).apply();
        worker.execute(() -> {
            try {
                sendReport(c, reason);
            } catch (Exception e) {
                Log.w(TAG, "Report failed: " + e.getMessage());
            }
        });
    }

    private static void sendReport(Context c, String reason) throws Exception {
        boolean fresh = "requested".equals(reason) || "low_battery".equals(reason) || "sim_changed".equals(reason);
        Location loc = currentLocation(c, fresh);
        JSONObject body = new JSONObject();
        JSONArray fixes = queued(c);
        if (loc != null) {
            JSONObject f = fix(loc, reason);
            rememberPhone(c, f);
            fixes.put(f);
            // the Wi-Fi around, placed by this fix when it's recent and good to 100 m
            JSONArray wifi = wifiAround(c);
            if (wifi.length() > 0 && loc.hasAccuracy() && loc.getAccuracy() <= 100
                    && (SystemClock.elapsedRealtimeNanos() - loc.getElapsedRealtimeNanos()) < 120_000_000_000L) {
                body.put("wifi", wifi);
                body.put("wifi_at", new JSONObject().put("lat", loc.getLatitude()).put("lng", loc.getLongitude())
                        .put("acc", loc.getAccuracy()));
            }
        }
        if (fixes.length() == 0) return;
        body.put("fixes", fixes);
        Intent b = c.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        if (b != null) {
            int level = b.getIntExtra(BatteryManager.EXTRA_LEVEL, -1), scale = b.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
            int status = b.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
            body.put("battery", scale > 0 ? level * 100 / scale : level);
            body.put("charging", status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL);
        }
        body.put("net", networkName(c));
        String res = post(c, "/api/findmy/report", body.toString());
        if (res != null && res.contains("\"ok\"")) {
            prefs(c).edit().remove(PREF_QUEUE).apply();
            try {
                JSONObject r = new JSONObject(res);
                JSONObject laptop = r.optJSONObject("laptop");
                if (laptop != null) prefs(c).edit().putString(PREF_LAPTOP, laptop.toString()).apply();
            } catch (Exception ignored) { }
        } else {
            // the computer is out of reach: keep the fixes (not the Wi-Fi) for next time
            JSONArray keep = new JSONArray();
            for (int i = Math.max(0, fixes.length() - 100); i < fixes.length(); i++) keep.put(fixes.get(i));
            prefs(c).edit().putString(PREF_QUEUE, keep.toString()).apply();
        }
    }

    private static JSONArray queued(Context c) {
        try {
            return new JSONArray(prefs(c).getString(PREF_QUEUE, "[]"));
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    private static JSONObject fix(Location l, String reason) throws Exception {
        JSONObject f = new JSONObject();
        f.put("t", l.getTime() / 1000.0);
        f.put("lat", l.getLatitude());
        f.put("lng", l.getLongitude());
        if (l.hasAccuracy()) f.put("acc", l.getAccuracy());
        if (l.hasAltitude()) f.put("alt", l.getAltitude());
        if (l.hasSpeed()) f.put("speed", l.getSpeed());
        f.put("provider", l.getProvider());
        f.put("reason", reason);
        return f;
    }

    /** The best recent location, or a fresh one (GPS included) when asked for. */
    @SuppressLint("MissingPermission")
    static Location currentLocation(Context c, boolean fresh) {
        LocationManager lm = (LocationManager) c.getSystemService(Context.LOCATION_SERVICE);
        if (lm == null || !hasLocation(c)) return null;
        Location best = null;
        for (String p : new String[]{"fused", LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER,
                LocationManager.PASSIVE_PROVIDER}) {
            try {
                Location l = lm.getLastKnownLocation(p);
                if (l != null && (best == null || l.getTime() > best.getTime() + 60_000
                        || (l.getTime() > best.getTime() - 60_000 && l.getAccuracy() < best.getAccuracy()))) best = l;
            } catch (Exception ignored) { }
        }
        long age = best == null ? Long.MAX_VALUE : System.currentTimeMillis() - best.getTime();
        if (!fresh && age < 5 * 60_000L) return best;
        // the fused provider (Android 12+: GPS, Wi-Fi and cells together) first; then GPS or the network
        Location now = Build.VERSION.SDK_INT >= 31 ? freshFix(lm, "fused", fresh ? 20 : 10) : null;
        if (now == null) now = freshFix(lm, fresh ? LocationManager.GPS_PROVIDER : LocationManager.NETWORK_PROVIDER, 25);
        if (now == null && fresh) now = freshFix(lm, LocationManager.NETWORK_PROVIDER, 10);
        return now != null ? now : best;
    }

    @SuppressLint("MissingPermission")
    private static Location freshFix(LocationManager lm, String provider, int seconds) {
        try {
            if (!lm.isProviderEnabled(provider)) return null;
        } catch (Exception e) {
            return null;
        }
        final Location[] got = new Location[1];
        CountDownLatch done = new CountDownLatch(1);
        if (looperThread == null) {
            looperThread = new HandlerThread("findmy-location");
            looperThread.start();
        }
        LocationListener once = new LocationListener() {
            @Override public void onLocationChanged(Location l) { got[0] = l; done.countDown(); }
            @Override public void onStatusChanged(String p, int s, Bundle e) { }
            @Override public void onProviderEnabled(String p) { }
            @Override public void onProviderDisabled(String p) { done.countDown(); }
        };
        try {
            lm.requestLocationUpdates(provider, 0L, 0f, once, looperThread.getLooper());
            done.await(seconds, TimeUnit.SECONDS);
        } catch (Exception ignored) {
        } finally {
            try { lm.removeUpdates(once); } catch (Exception ignored) { }
        }
        return got[0];
    }

    private static final java.util.Map<String, java.util.List<int[]>> scanHistory = new java.util.HashMap<>();
    private static final java.util.Map<String, String> scanSsids = new java.util.HashMap<>();

    @SuppressLint("MissingPermission")
    private static JSONArray wifiAround(Context c) {
        return wifiAround(c, 120_000L);
    }

    /** The Wi-Fi this phone sees: temporally smoothed across recent scans (averaging dBm over
     *  the last 3-5 looks and pruning 1-off transient networks), plus the connected network. */
    @SuppressLint("MissingPermission")
    static JSONArray wifiAround(Context c, long maxAgeMs) {
        JSONArray out = new JSONArray();
        try {
            WifiManager wm = (WifiManager) c.getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            if (wm == null) return out;
            long nowMs = System.currentTimeMillis();
            int nowSec = (int) (nowMs / 1000);
            String connected = null;
            android.net.wifi.WifiInfo info = wm.getConnectionInfo();
            if (info != null && info.getBSSID() != null && !"02:00:00:00:00:00".equals(info.getBSSID())) {
                connected = info.getBSSID().toLowerCase();
            }

            List<ScanResult> results = wm.getScanResults();
            long nowUs = SystemClock.elapsedRealtime() * 1000;

            synchronized (scanHistory) {
                // Prune entries older than 90 seconds
                java.util.Iterator<java.util.Map.Entry<String, java.util.List<int[]>>> it = scanHistory.entrySet().iterator();
                while (it.hasNext()) {
                    java.util.Map.Entry<String, java.util.List<int[]>> entry = it.next();
                    java.util.Iterator<int[]> sit = entry.getValue().iterator();
                    while (sit.hasNext()) {
                        if (nowSec - sit.next()[0] > 90) sit.remove();
                    }
                    if (entry.getValue().isEmpty()) {
                        it.remove();
                        scanSsids.remove(entry.getKey());
                    }
                }

                // Add current scan results to history
                if (connected != null && info != null) {
                    java.util.List<int[]> l = scanHistory.get(connected);
                    if (l == null) { l = new java.util.ArrayList<>(); scanHistory.put(connected, l); }
                    l.add(new int[]{nowSec, info.getRssi()});
                }
                if (results != null) {
                    for (ScanResult r : results) {
                        if (nowUs - r.timestamp > maxAgeMs * 1000) continue;
                        if (r.BSSID == null) continue;
                        String b = r.BSSID.toLowerCase();
                        java.util.List<int[]> l = scanHistory.get(b);
                        if (l == null) { l = new java.util.ArrayList<>(); scanHistory.put(b, l); }
                        l.add(new int[]{nowSec, r.level});
                        if (r.SSID != null && !r.SSID.isEmpty()) scanSsids.put(b, r.SSID);
                    }
                }

                // Output connected AP first
                if (connected != null && scanHistory.containsKey(connected)) {
                    java.util.List<int[]> vals = scanHistory.get(connected);
                    int sum = 0;
                    for (int[] v : vals) sum += v[1];
                    int avg = sum / vals.size();
                    out.put(new JSONObject().put("bssid", connected).put("rssi", avg));
                }

                // Output temporally averaged readings; prune 1-off transient networks if we have history
                for (java.util.Map.Entry<String, java.util.List<int[]>> e : scanHistory.entrySet()) {
                    String b = e.getKey();
                    if (b.equalsIgnoreCase(connected)) continue;
                    java.util.List<int[]> vals = e.getValue();
                    int count = vals.size();
                    int sum = 0;
                    for (int[] v : vals) sum += v[1];
                    int avg = sum / count;
                    // Ignore 1-off transient networks unless signal is strong (>= -65 dBm)
                    if (count >= 2 || avg >= -65) {
                        JSONObject apObj = new JSONObject().put("bssid", b).put("rssi", avg);
                        String s = scanSsids.get(b);
                        if (s != null) apObj.put("ssid", s);
                        out.put(apObj);
                    }
                    if (out.length() >= 60) break;
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "No Wi-Fi scan: " + e.getMessage());
        }
        return out;
    }

    private static String networkName(Context c) {
        try {
            ConnectivityManager cm = (ConnectivityManager) c.getSystemService(Context.CONNECTIVITY_SERVICE);
            NetworkCapabilities nc = cm.getNetworkCapabilities(cm.getActiveNetwork());
            if (nc == null) return "offline";
            if (nc.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return "Wi-Fi";
            if (nc.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) {
                TelephonyManager tm = (TelephonyManager) c.getSystemService(Context.TELEPHONY_SERVICE);
                String op = tm != null ? tm.getNetworkOperatorName() : "";
                return op == null || op.isEmpty() ? "Mobile data" : op;
            }
            return "Other";
        } catch (Exception e) {
            return "";
        }
    }

    // ---- to the computer: the route in use now, then the internet one ----
    static String post(Context c, String path, String json) {
        PairedDevice active = DeviceManager.getActiveDevice(c);
        if (active == null || !active.isPaired()) return null;
        String res = NetworkUtils.httpPostJsonWithAuth(active.getBaseUrl() + path, active.authToken, json, 15000);
        if (res == null) {
            String internet = active.getInternetUrl();
            if (internet != null && !internet.isEmpty() && !active.getBaseUrl().startsWith(internet)) {
                res = NetworkUtils.httpPostJsonWithAuth(internet.replaceAll("/+$", "") + path, active.authToken, json, 20000);
            }
        }
        return res;
    }

    /** A photo (JPEG) for the computer, e.g. after a wrong unlock attempt. */
    static void sendPhoto(Context ctx, byte[] jpeg, String reason) {
        Context c = ctx.getApplicationContext();
        worker.execute(() -> {
            PairedDevice active = DeviceManager.getActiveDevice(c);
            if (active == null || !active.isPaired()) return;
            String[] bases = {active.getBaseUrl(), active.getInternetUrl()};
            for (String base : bases) {
                if (base == null || base.isEmpty()) continue;
                HttpURLConnection conn = null;
                try {
                    conn = (HttpURLConnection) new URL(base.replaceAll("/+$", "") + "/api/findmy/photo").openConnection();
                    conn.setRequestMethod("POST");
                    conn.setConnectTimeout(15000);
                    conn.setReadTimeout(20000);
                    conn.setDoOutput(true);
                    conn.setFixedLengthStreamingMode(jpeg.length);
                    conn.setRequestProperty("Content-Type", "image/jpeg");
                    conn.setRequestProperty("X-Orbit-Reason", reason);
                    NetworkUtils.applyTunnelHeaders(conn);
                    NetworkUtils.setAuth(conn, active.authToken);
                    try (OutputStream os = conn.getOutputStream()) { os.write(jpeg); }
                    if (conn.getResponseCode() == 200) return;
                } catch (Exception e) {
                    Log.w(TAG, "Photo didn't reach the computer: " + e.getMessage());
                } finally {
                    if (conn != null) conn.disconnect();
                }
            }
        });
        report(c, "wrong_unlock");
    }

    // ---- the SIM card ----
    @SuppressLint({"MissingPermission", "HardwareIds"})
    /** What identifies the SIM card(s) in the phone, or null while the SIM is between states
     *  (starting up, locked, airplane mode): only a settled state is compared. Names (which a
     *  carrier can show differently) are left out: the slot, the network code and the phone's
     *  own subscription number for that card, which changes only with another card. */
    static String simPrint(Context c) {
        TelephonyManager tm = (TelephonyManager) c.getSystemService(Context.TELEPHONY_SERVICE);
        if (tm == null) return null;
        int state = tm.getSimState();
        if (state == TelephonyManager.SIM_STATE_ABSENT) return "v2|absent";
        if (state != TelephonyManager.SIM_STATE_READY) return null;
        List<String> cards = new java.util.ArrayList<>();
        try {
            if (c.checkSelfPermission(Manifest.permission.READ_PHONE_STATE) == PackageManager.PERMISSION_GRANTED) {
                SubscriptionManager sm = (SubscriptionManager) c.getSystemService(Context.TELEPHONY_SUBSCRIPTION_SERVICE);
                List<SubscriptionInfo> subs = sm != null ? sm.getActiveSubscriptionInfoList() : null;
                if (subs == null || subs.isEmpty()) return null;          // not readable just now
                for (SubscriptionInfo s : subs) {
                    String net = Build.VERSION.SDK_INT >= 29 ? s.getMccString() + s.getMncString() : s.getMcc() + "" + s.getMnc();
                    cards.add(s.getSimSlotIndex() + ":" + net + ":" + s.getSubscriptionId());
                }
            } else {
                String op = tm.getSimOperator();
                if (op == null || op.isEmpty()) return null;
                cards.add(op);
            }
        } catch (Exception e) {
            return null;
        }
        java.util.Collections.sort(cards);
        return "v2|" + android.text.TextUtils.join(",", cards);
    }

    /** A SIM change is reported once it has lasted a minute (two looks in a row), never for a
     *  passing state. */
    static void checkSim(Context c) {
        String now = simPrint(c);
        if (now == null) return;
        String was = prefs(c).getString(PREF_SIM_PRINT, null);
        if (was == null || !was.startsWith("v2|")) {   // first look (or an older kind of look): this is its own SIM
            prefs(c).edit().putString(PREF_SIM_PRINT, now).remove(PREF_SIM_PENDING).apply();
            return;
        }
        if (now.equals(was)) {
            prefs(c).edit().remove(PREF_SIM_PENDING).apply();
            return;
        }
        String pending = prefs(c).getString(PREF_SIM_PENDING, "");
        long since = prefs(c).getLong(PREF_SIM_PENDING_AT, 0);
        if (!now.equals(pending)) {
            prefs(c).edit().putString(PREF_SIM_PENDING, now).putLong(PREF_SIM_PENDING_AT, System.currentTimeMillis()).apply();
            return;
        }
        if (System.currentTimeMillis() - since < 60_000L) return;
        prefs(c).edit().putString(PREF_SIM_PRINT, now).remove(PREF_SIM_PENDING).apply();
        TelephonyManager tm = (TelephonyManager) c.getSystemService(Context.TELEPHONY_SERVICE);
        boolean absent = tm == null || tm.getSimState() == TelephonyManager.SIM_STATE_ABSENT;
        String operator = tm != null ? tm.getSimOperatorName() : "";
        worker.execute(() -> {
            try {
                JSONObject detail = new JSONObject().put("operator", operator).put("country", tm != null ? tm.getSimCountryIso() : "");
                post(c, "/api/findmy/event", new JSONObject().put("kind", absent ? "sim_removed" : "sim_changed")
                        .put("detail", detail).toString());
            } catch (Exception ignored) { }
        });
        report(c, "sim_changed");
    }

    // ---- what this phone can measure distances with (radio time-of-flight), for the computer ----
    /** Wi-Fi round-trip-time ranging (802.11mc), ultra-wideband, and how many access points around
     *  answer ranging requests: decides whether distances can be timed by radio, not just guessed. */
    @SuppressLint("MissingPermission")
    static void reportCapabilities(Context ctx) {
        Context c = ctx.getApplicationContext();
        worker.execute(() -> {
            try {
                PackageManager pm = c.getPackageManager();
                boolean rttFeature = Build.VERSION.SDK_INT >= 28 && pm.hasSystemFeature(PackageManager.FEATURE_WIFI_RTT);
                boolean rttOn = false;
                if (rttFeature) {
                    android.net.wifi.rtt.WifiRttManager rm = (android.net.wifi.rtt.WifiRttManager) c.getSystemService(Context.WIFI_RTT_RANGING_SERVICE);
                    rttOn = rm != null && rm.isAvailable();
                }
                boolean uwb = Build.VERSION.SDK_INT >= 31 && pm.hasSystemFeature("android.hardware.uwb");
                int responders = 0, seen = 0;
                if (hasLocation(c) && Build.VERSION.SDK_INT >= 28) {
                    WifiManager wm = (WifiManager) c.getApplicationContext().getSystemService(Context.WIFI_SERVICE);
                    for (ScanResult r : wm.getScanResults()) {
                        seen++;
                        if (r.is80211mcResponder()) responders++;
                    }
                }
                JSONObject body = new JSONObject().put("model", Build.MANUFACTURER + " " + Build.MODEL)
                        .put("android", Build.VERSION.SDK_INT).put("wifi_rtt", rttFeature).put("wifi_rtt_on", rttOn)
                        .put("uwb", uwb).put("rtt_access_points", responders).put("access_points_seen", seen);
                post(c, "/api/findmy/capabilities", body.toString());
            } catch (Exception e) {
                Log.w(TAG, "Capabilities: " + e.getMessage());
            }
        });
    }

    // ---- the nearby finder by Bluetooth (optional): Orbit's mark from this phone ----
    /** While the computer looks for this phone: advertise Orbit's mark (3 minutes at most) so it
     *  can tell how strongly it hears this phone. Nothing if Bluetooth is off. */
    @SuppressLint("MissingPermission")
    static synchronized void beacon(Context c, boolean on) {
        try {
            android.bluetooth.BluetoothManager bm = (android.bluetooth.BluetoothManager) c.getSystemService(Context.BLUETOOTH_SERVICE);
            android.bluetooth.BluetoothAdapter a = bm != null ? bm.getAdapter() : null;
            if (a == null || !a.isEnabled()) return;
            if (Build.VERSION.SDK_INT >= 31
                    && c.checkSelfPermission(Manifest.permission.BLUETOOTH_ADVERTISE) != PackageManager.PERMISSION_GRANTED) return;
            android.bluetooth.le.BluetoothLeAdvertiser adv = a.getBluetoothLeAdvertiser();
            if (adv == null) return;
            if (advertising != null) {
                adv.stopAdvertising(advertising);
                advertising = null;
            }
            if (!on) return;
            android.bluetooth.le.AdvertiseSettings settings = new android.bluetooth.le.AdvertiseSettings.Builder()
                    .setAdvertiseMode(android.bluetooth.le.AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
                    .setTxPowerLevel(android.bluetooth.le.AdvertiseSettings.ADVERTISE_TX_POWER_HIGH)
                    .setConnectable(false).setTimeout(180_000).build();
            android.bluetooth.le.AdvertiseData data = new android.bluetooth.le.AdvertiseData.Builder()
                    .addServiceUuid(new android.os.ParcelUuid(ORBIT_BLE_UUID)).setIncludeDeviceName(false).build();
            advertising = new android.bluetooth.le.AdvertiseCallback() {
                @Override public void onStartFailure(int errorCode) { Log.w(TAG, "Bluetooth mark failed: " + errorCode); }
            };
            adv.startAdvertising(settings, data, advertising);
        } catch (Exception e) {
            Log.w(TAG, "Bluetooth mark: " + e.getMessage());
        }
    }

    /** Signal strength (dBm) to a rough distance in metres: about -59 dBm at 1 m, falling off
     *  a little faster than in open air indoors. */
    static double bluetoothMetres(int dbm) {
        return Math.pow(10, (-59 - dbm) / 22.0);
    }

    // ---- near or far, by the Wi-Fi around (no Bluetooth) ----
    /** Send the Wi-Fi this phone sees to the computer, which compares it with its own:
     *  {score 0..1, word, zone}. `scanFirst` asks Android for a fresh scan (it allows a few a minute). */
    static JSONObject compareWifi(Context c, String purpose, boolean scanFirst) {
        return compareWifi(c, purpose, scanFirst, false);
    }

    @SuppressLint("MissingPermission")
    static JSONObject compareWifi(Context c, String purpose, boolean scanFirst, boolean calibratedRoom) {
        if (!hasLocation(c)) return null;
        try {
            if (scanFirst) {
                WifiManager wm = (WifiManager) c.getApplicationContext().getSystemService(Context.WIFI_SERVICE);
                if (wm != null && wm.startScan()) Thread.sleep(3500);
            }
            // what's seen now for the nearby finder; a presence check-in can use scans up to 10 min old
            JSONArray wifi = wifiAround(c, "presence".equals(purpose) ? 600_000L : 30_000L);
            if (wifi.length() == 0) return null;
            JSONObject req = new JSONObject().put("purpose", purpose).put("wifi", wifi);
            if (calibratedRoom) req.put("calibrated_room", true);
            String res = post(c, "/api/findmy/wifi", req.toString());
            return res == null ? null : new JSONObject(res);
        } catch (Exception e) {
            return null;
        }
    }

    /** For the walk-away lock: once a minute while on Wi-Fi, so the computer can tell if this
     *  phone is still around (when it isn't, the computer locks itself). */
    private static void presence(Context c) {
        if (!prefs(c).getBoolean(PREF_WALK, true) || !hasLocation(c) || !"Wi-Fi".equals(networkName(c))) return;
        if (System.currentTimeMillis() - prefs(c).getLong(PREF_PRESENCE, 0) < 55_000L) return;
        prefs(c).edit().putLong(PREF_PRESENCE, System.currentTimeMillis()).apply();
        worker.execute(() -> compareWifi(c, "presence", false));
    }

    /** The computer's Find my phone page asks: send the Wi-Fi every few seconds for 3 minutes. */
    static synchronized void findPhoneNearby(Context ctx, boolean on) {
        Context c = ctx.getApplicationContext();
        Handler h = mainHandler();
        if (findPhoneLoop != null) h.removeCallbacks(findPhoneLoop);
        findPhoneLoop = null;
        if (!on) return;
        long until = System.currentTimeMillis() + 180_000L;
        findPhoneLoop = new Runnable() {
            @Override public void run() {
                if (System.currentTimeMillis() > until) return;
                worker.execute(() -> compareWifi(c, "find_phone", true));
                h.postDelayed(this, 6000);
            }
        };
        h.post(findPhoneLoop);
    }

    // ---- alerts from the computer: a wrong password there (with a photo), the walk-away lock,
    //      and lost mode's reports ----
    static void laptopAlert(Context ctx, JSONObject ev) {
        Context c = ctx.getApplicationContext();
        worker.execute(() -> {
            try {
                if (!ev.has("t")) ev.put("t", System.currentTimeMillis() / 1000.0);
            } catch (Exception ignored) { }
            showLaptopAlert(c, ev);
        });
    }

    /** Alerts the phone missed while it wasn't connected: checked every 15 minutes. */
    private static void checkLaptopAlerts(Context c) {
        if (System.currentTimeMillis() - prefs(c).getLong(PREF_ALERTS_AT, 0) < 15 * 60_000L) return;
        prefs(c).edit().putLong(PREF_ALERTS_AT, System.currentTimeMillis()).apply();
        worker.execute(() -> {
            JSONObject r = getJson(c, "/api/findmy/laptop");
            if (r != null) absorbLaptop(c, r);
        });
    }

    /** What the computer said about itself: its settings, lost or not, and alerts not shown yet. */
    static void absorbLaptop(Context c, JSONObject r) {
        JSONObject settings = r.optJSONObject("settings");
        if (settings != null) prefs(c).edit().putBoolean(PREF_WALK, settings.optBoolean("walk_away_lock", true)).apply();
        JSONObject lost = r.optJSONObject("lost");
        if (lost != null) prefs(c).edit().putBoolean(PREF_LOST, lost.optBoolean("lost")).apply();
        JSONArray alerts = r.optJSONArray("alerts");
        if (alerts == null) return;
        double seen = Double.longBitsToDouble(prefs(c).getLong(PREF_ALERT_SEEN, 0));
        for (int i = alerts.length() - 1; i >= 0; i--) {
            JSONObject a = alerts.optJSONObject(i);
            if (a == null) continue;
            if (a.optDouble("t", 0) > seen + 0.001) showLaptopAlert(c, a);
            else remember(c, a);
        }
    }

    static String photoName(JSONObject a) {
        return a.isNull("name") ? null : a.optString("name", null);
    }

    /** Keep the newest photos (a wrong password, lost mode's reports) on this phone, with where the
     *  computer was, whether or not the computer can be reached later: the 3 latest only; older
     *  ones and their photo files go. */
    static final int KEEP_PHOTOS = 3;

    static synchronized void remember(Context c, JSONObject a) {
        try {
            String name = photoName(a);
            if (name == null) return;                         // only alerts with a photo are kept
            double t = a.optDouble("t");
            JSONArray old = history(c);
            java.util.List<JSONObject> all = new java.util.ArrayList<>();
            for (int i = 0; i < old.length(); i++) {
                JSONObject o = old.optJSONObject(i);
                if (o == null || photoName(o) == null) continue;
                if (Math.abs(o.optDouble("t") - t) < 0.001) return;                          // already kept
                all.add(o);
            }
            all.add(a);
            all.sort((x, y) -> Double.compare(y.optDouble("t"), x.optDouble("t")));        // newest first
            JSONArray out = new JSONArray();
            java.io.File dir = new java.io.File(c.getFilesDir(), "findmy");
            for (int i = 0; i < all.size(); i++) {
                if (i < KEEP_PHOTOS) out.put(all.get(i));
                else new java.io.File(dir, photoName(all.get(i))).delete();
            }
            prefs(c).edit().putString(PREF_HISTORY, out.toString()).apply();
            if (out.toString().contains(name)) laptopPhoto(c, name);   // download it now, while reachable
        } catch (Exception ignored) { }
    }

    /** Drop what's no longer kept (alerts without a photo, all but the newest 3 photos). */
    static synchronized void trimHistory(Context c) {
        JSONArray old = history(c);
        java.util.List<JSONObject> photos = new java.util.ArrayList<>();
        for (int i = 0; i < old.length(); i++) {
            JSONObject o = old.optJSONObject(i);
            if (o != null && photoName(o) != null) photos.add(o);
        }
        photos.sort((x, y) -> Double.compare(y.optDouble("t"), x.optDouble("t")));
        JSONArray out = new JSONArray();
        java.io.File dir = new java.io.File(c.getFilesDir(), "findmy");
        for (int i = 0; i < photos.size(); i++) {
            if (i < KEEP_PHOTOS) out.put(photos.get(i));
            else new java.io.File(dir, photoName(photos.get(i))).delete();
        }
        if (out.length() != old.length()) prefs(c).edit().putString(PREF_HISTORY, out.toString()).apply();
    }

    /** Where the computer was for an alert: "Jio, Kochi · Wi-Fi: …" or "On IISERTVM · ±25 m". */
    static String whereLine(JSONObject a) {
        if ("lost_report".equals(a.optString("kind"))) {
            String place = placeLine(a);
            return (place.isEmpty() ? "" : place + " · ") + "Wi-Fi: " + networksLine(a);
        }
        String ssid = a.isNull("connected") ? "" : a.optString("connected", "");
        JSONObject p = a.optJSONObject("place");
        if (p == null) return ssid.isEmpty() ? "Location unknown" : "On " + ssid;
        return (ssid.isEmpty() ? "Placed" : "On " + ssid) + " · ±" + Math.round(p.optDouble("acc", 30)) + " m";
    }

    static JSONArray history(Context c) {
        try {
            return new JSONArray(prefs(c).getString(PREF_HISTORY, "[]"));
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    /** "Jio, Kochi" from a lost-mode report. */
    static String placeLine(JSONObject a) {
        StringBuilder sb = new StringBuilder();
        for (String k : new String[]{"isp", "city", "region"}) {
            String v = a.isNull(k) ? "" : a.optString(k, "");
            if (!v.isEmpty() && sb.indexOf(v) < 0) sb.append(sb.length() > 0 ? ", " : "").append(v);
        }
        return sb.toString();
    }

    static String networksLine(JSONObject a) {
        JSONArray n = a.optJSONArray("networks");
        if (n == null || n.length() == 0) return "No Wi-Fi around";
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < Math.min(n.length(), 6); i++) sb.append(i > 0 ? ", " : "").append(n.optString(i));
        if (n.length() > 6) sb.append(" +").append(n.length() - 6);
        return sb.toString();
    }

    private static void showLaptopAlert(Context c, JSONObject a) {
        remember(c, a);
        String kind = a.optString("kind"), name = photoName(a);
        double t = a.optDouble("t");
        double seen = Double.longBitsToDouble(prefs(c).getLong(PREF_ALERT_SEEN, 0));
        if (t <= seen + 0.001) return;
        prefs(c).edit().putLong(PREF_ALERT_SEEN, Double.doubleToLongBits(t)).apply();
        android.app.NotificationManager nm = (android.app.NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(ALERT_CHANNEL) == null) {
            nm.createNotificationChannel(new android.app.NotificationChannel(ALERT_CHANNEL, "Find my: your computer",
                    android.app.NotificationManager.IMPORTANCE_HIGH));
        }
        // tapping opens the photo; the notification stays until you swipe it away
        Intent show = new Intent(c, FindMyActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        if (name != null) show.putExtra(FindMyActivity.EXTRA_PHOTO, name);
        android.app.PendingIntent open = android.app.PendingIntent.getActivity(c, (int) (t % 100000) + 7409, show,
                android.app.PendingIntent.FLAG_IMMUTABLE | android.app.PendingIntent.FLAG_UPDATE_CURRENT);
        android.app.Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new android.app.Notification.Builder(c, ALERT_CHANNEL) : new android.app.Notification.Builder(c);
        b.setSmallIcon(R.drawable.ic_o_notify).setContentIntent(open).setAutoCancel(false)
                .setWhen((long) (t * 1000)).setShowWhen(true);
        int id = (int) (t % 100000) + 8000;
        if ("walk_away_lock".equals(kind)) {
            b.setContentTitle("Your computer locked itself").setContentText("You walked away from it.");
        } else if ("lost_report".equals(kind)) {
            // one notification, updated with each report (they come every 10 minutes)
            id = 8001;
            String place = placeLine(a);
            String text = (place.isEmpty() ? "Online" : place) + " · Wi-Fi: " + networksLine(a);
            b.setContentTitle("Your lost computer reported in").setContentText(text).setOnlyAlertOnce(true);
            android.graphics.Bitmap photo = name == null ? null : laptopPhoto(c, name);
            if (photo != null) {
                b.setLargeIcon(photo).setStyle(new android.app.Notification.BigPictureStyle().bigPicture(photo).setSummaryText(text));
            } else {
                b.setStyle(new android.app.Notification.BigTextStyle().bigText(text));
            }
        } else {
            b.setContentTitle("Someone tried to unlock your computer").setContentText("A wrong password was typed.");
            android.graphics.Bitmap photo = name == null ? null : laptopPhoto(c, name);
            if (photo != null) {
                b.setLargeIcon(photo).setStyle(new android.app.Notification.BigPictureStyle().bigPicture(photo));
            }
        }
        nm.notify(id, b.build());
    }

    /** A photo taken on the computer after a wrong password (kept in this app's own storage). */
    static android.graphics.Bitmap laptopPhoto(Context c, String name) {
        try {
            java.io.File dir = new java.io.File(c.getFilesDir(), "findmy");
            dir.mkdirs();
            java.io.File f = new java.io.File(dir, name);
            if (!f.exists()) {
                PairedDevice active = DeviceManager.getActiveDevice(c);
                if (active == null) return null;
                for (String base : new String[]{active.getBaseUrl(), active.getInternetUrl()}) {
                    if (base == null || base.isEmpty()) continue;
                    HttpURLConnection conn = (HttpURLConnection) new URL(base.replaceAll("/+$", "")
                            + "/api/findmy/laptop/photo?name=" + java.net.URLEncoder.encode(name, "UTF-8")).openConnection();
                    try {
                        conn.setConnectTimeout(10000);
                        conn.setReadTimeout(15000);
                        NetworkUtils.applyTunnelHeaders(conn);
                        NetworkUtils.setAuth(conn, active.authToken);
                        if (conn.getResponseCode() != 200) continue;
                        try (java.io.InputStream in = conn.getInputStream(); java.io.OutputStream out = new java.io.FileOutputStream(f)) {
                            byte[] buf = new byte[1 << 15];
                            int n;
                            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                        }
                        break;
                    } finally {
                        conn.disconnect();
                    }
                }
            }
            return f.exists() ? android.graphics.BitmapFactory.decodeFile(f.getPath()) : null;
        } catch (Exception e) {
            return null;
        }
    }

    static JSONObject getJson(Context c, String path) {
        PairedDevice active = DeviceManager.getActiveDevice(c);
        if (active == null || !active.isPaired()) return null;
        String res = NetworkUtils.httpGetWithAuth(active.getBaseUrl() + path, active.authToken, 30000);
        String internet = active.getInternetUrl();
        if (res == null && internet != null && !internet.isEmpty() && !active.getBaseUrl().startsWith(internet)) {
            res = NetworkUtils.httpGetWithAuth(internet.replaceAll("/+$", "") + path, active.authToken, 30000);
        }
        try {
            return res == null ? null : new JSONObject(res);
        } catch (Exception e) {
            return null;
        }
    }

    // ---- this phone, about the computer ----
    /** Where the computer was last found (kept from the last report's answer). */
    static JSONObject laptopLast(Context c) {
        try {
            String s = prefs(c).getString(PREF_LAPTOP, null);
            return s == null ? null : new JSONObject(s);
        } catch (Exception e) {
            return null;
        }
    }

    static void rememberLaptop(Context c, JSONObject last) {
        if (last != null) prefs(c).edit().putString(PREF_LAPTOP, last.toString()).apply();
    }

    static Handler mainHandler() {
        return new Handler(android.os.Looper.getMainLooper());
    }
}

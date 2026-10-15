package com.lunarphoton.pcauthenticator;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.location.Location;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ImageButton;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Find my, in two tabs. "Your computer": where it is (map), find it (locate, sound, the Wi-Fi
 * nearby finder) and what happened there (wrong passwords, walk-away locks). "Settings": how
 * the computer and this phone look out for each other.
 */
public class FindMyActivity extends Activity {
    static final String EXTRA_PHOTO = "findmy_photo";
    private static final int REQ_LOCATION = 41, REQ_BACKGROUND = 42, REQ_NEARBY = 43, REQ_CAMERA = 44,
            REQ_PHONE = 45, REQ_ADMIN = 46;

    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private WebView map;
    private boolean mapReady;
    private JSONObject laptop;          // where the computer is (or was last)
    private JSONObject phoneFix;        // where this phone is, by its own location
    private JSONObject wifiPhone, wifiLaptop;   // both placed on the computer's Wi-Fi map (Nearby)
    private double apart = -1;          // how far apart the Wi-Fi map puts them
    private TextView tvMeterSub;
    private TextView tvSeen, tvMeter, tvRing, tvNearby, tabComputer, tabSettings;

    private ProgressBar pbMeter;
    private View meter;
    private ImageButton btnRing, btnNearby;
    private View rowLost;
    private TextView tvLost, tvLostDesc;
    private ImageView ivLost;
    private LinearLayout alerts;
    private Switch swShare, swPhoto, swSim, swWalk, swPcPhoto;
    private boolean ringing, nearby;
    private int nearbyRun;              // the current nearby loop (an older one stops)
    private boolean askedNearby;        // asked for location and the microphone once this time
    private android.bluetooth.le.BluetoothLeScanner bleScanner;
    private android.bluetooth.le.ScanCallback bleScan;
    private final java.util.ArrayDeque<long[]> bleReadings = new java.util.ArrayDeque<>();   // {time ms, dBm}

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_find_my);
        findViewById(R.id.btn_findmy_back).setOnClickListener(v -> finish());
        tvSeen = findViewById(R.id.tv_findmy_seen);
        tvMeter = findViewById(R.id.tv_findmy_meter);
        tvMeterSub = findViewById(R.id.tv_findmy_meter_sub);
        pbMeter = findViewById(R.id.pb_findmy_meter);
        meter = findViewById(R.id.ll_findmy_meter);
        btnRing = findViewById(R.id.btn_findmy_ring);
        btnNearby = findViewById(R.id.btn_findmy_nearby);
        tvRing = findViewById(R.id.tv_findmy_ring);
        tvNearby = findViewById(R.id.tv_findmy_nearby);
        alerts = findViewById(R.id.ll_findmy_alerts);
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active != null && active.hostname != null) ((TextView) findViewById(R.id.tv_findmy_name)).setText(active.hostname);

        tabComputer = findViewById(R.id.tab_findmy_computer);
        tabSettings = findViewById(R.id.tab_findmy_settings);
        tabComputer.setOnClickListener(v -> showTab(false));
        tabSettings.setOnClickListener(v -> showTab(true));

        setUpMap();
        findViewById(R.id.btn_findmy_locate).setOnClickListener(v -> locate(true));
        btnRing.setOnClickListener(v -> ring(!ringing));
        btnNearby.setOnClickListener(v -> setNearby(!nearby));
        View btnLab = findViewById(R.id.btn_findmy_wifilab);
        if (btnLab != null) {
            btnLab.setOnClickListener(v -> {
                PairedDevice d = DeviceManager.getActiveDevice(this);
                if (d != null && d.getBaseUrl() != null) {
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(d.getBaseUrl() + "/wifilab")));
                    } catch (Exception e) {
                        Toast.makeText(this, "Can't open Wi-Fi Lab: " + e.getMessage(), Toast.LENGTH_SHORT).show();
                    }
                } else {
                    Toast.makeText(this, "Computer not paired or offline", Toast.LENGTH_SHORT).show();
                }
            });
        }
        rowLost = findViewById(R.id.row_findmy_lost);
        tvLost = findViewById(R.id.tv_findmy_lost);
        tvLostDesc = findViewById(R.id.tv_findmy_lost_desc);
        ivLost = findViewById(R.id.iv_findmy_lost);
        showLost(FindMy.prefs(this).getBoolean(FindMy.PREF_LOST, false), 0);
        rowLost.setOnClickListener(v -> {
            if (FindMy.prefs(this).getBoolean(FindMy.PREF_LOST, false)) {
                setLost(false);
                return;
            }
            new AlertDialog.Builder(this)
                    .setTitle("Mark your computer as lost?")
                    .setMessage("Every 10 minutes while it's online, it sends this phone a webcam photo, the names of the "
                            + "Wi-Fi networks around it, and its internet provider and city. It keeps doing so after "
                            + "restarts, until you tap Found it.")
                    .setPositiveButton("Mark as lost", (d, w) -> setLost(true))
                    .setNegativeButton("Cancel", null)
                    .show();
        });

        swShare = findViewById(R.id.switch_findmy_share);
        swPhoto = findViewById(R.id.switch_findmy_photo);
        swSim = findViewById(R.id.switch_findmy_sim);
        swWalk = findViewById(R.id.switch_findmy_walk);
        swPcPhoto = findViewById(R.id.switch_findmy_pcphoto);
        showSwitches();
        swShare.setOnClickListener(v -> onShare(swShare.isChecked()));
        swPhoto.setOnClickListener(v -> onPhoto(swPhoto.isChecked()));
        swSim.setOnClickListener(v -> onSim(swSim.isChecked()));
        swWalk.setOnClickListener(v -> setComputer("walk_away_lock", swWalk.isChecked()));
        swPcPhoto.setOnClickListener(v -> setComputer("wrong_password_photo", swPcPhoto.isChecked()));

        laptop = FindMy.laptopLast(this);
        showLaptop(null);
        showAlerts();
        findMe();
        FindMy.reportCapabilities(this);
        locate(false);
        openPhotoFrom(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        openPhotoFrom(intent);
    }

    /** Opened from an alert's notification: show its photo straight away. */
    private void openPhotoFrom(Intent intent) {
        String name = intent != null ? intent.getStringExtra(EXTRA_PHOTO) : null;
        if (name == null) return;
        io.execute(() -> {
            Bitmap b = FindMy.laptopPhoto(this, name);
            if (b != null) ui.post(() -> showPhoto(b, name));
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        showSwitches();               // back from Android's settings pages
    }

    @Override
    protected void onPause() {
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (nearby) stopBluetooth();
        nearby = false;
        if (ringing) ring(false);
    }

    private void showTab(boolean settings) {
        findViewById(R.id.sv_findmy_computer).setVisibility(settings ? View.GONE : View.VISIBLE);
        findViewById(R.id.sv_findmy_settings).setVisibility(settings ? View.VISIBLE : View.GONE);
        tabComputer.setBackgroundResource(settings ? 0 : R.drawable.findmy_segment_on);
        tabSettings.setBackgroundResource(settings ? R.drawable.findmy_segment_on : 0);
        tabComputer.setTextColor(getColor(settings ? R.color.o_text_2 : R.color.o_text));
        tabSettings.setTextColor(getColor(settings ? R.color.o_text : R.color.o_text_2));
    }

    // ---- the map ----
    @SuppressLint("SetJavaScriptEnabled")
    private void setUpMap() {
        map = findViewById(R.id.wv_findmy_map);
        WebSettings s = map.getSettings();
        s.setJavaScriptEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        map.setBackgroundColor(0xFF1A1D22);
        map.setWebViewClient(new WebViewClient() {
            @Override public void onPageFinished(WebView view, String url) {
                mapReady = true;
                showOnMap();
            }
        });
        map.loadUrl("file:///android_asset/findmap.html");
    }

    private double calculateGpsDistance() {
        JSONObject me = wifiPhone != null ? wifiPhone : phoneFix;
        JSONObject lap = wifiLaptop != null ? wifiLaptop : (laptop != null && laptop.has("lat") ? laptop : null);
        if (me != null && lap != null && me.has("lat") && lap.has("lat")) {
            Location locMe = new Location("me");
            locMe.setLatitude(me.optDouble("lat"));
            locMe.setLongitude(me.optDouble("lng"));

            Location locLap = new Location("laptop");
            locLap.setLatitude(lap.optDouble("lat"));
            locLap.setLongitude(lap.optDouble("lng"));

            return locMe.distanceTo(locLap);
        }
        return -1.0;
    }

    /** Smooth continuous progress mapping across all distance tiers (0.1m to 1km+). */
    private static int distanceToProgress(double metres) {
        if (metres <= 0.5) return 100;
        if (metres <= 2.0) return (int) Math.round(90.0 + (2.0 - metres) * 6.67);
        if (metres <= 10.0) return (int) Math.round(65.0 + (10.0 - metres) * 3.125);
        if (metres <= 30.0) return (int) Math.round(40.0 + (30.0 - metres) * 1.25);
        if (metres <= 100.0) return (int) Math.round(20.0 + (100.0 - metres) * 0.286);
        return (int) Math.max(5, Math.min(20, Math.round(20.0 - (metres - 100.0) / 40.0)));
    }

    /** Both on the map, named: the computer and this phone (by the Wi-Fi map while Nearby runs,
     *  so both are placed the same way; else the computer's last place and this phone's own fix). */
    private void showOnMap() {
        if (!mapReady) return;
        try {
            JSONObject d = new JSONObject();
            String name = ((TextView) findViewById(R.id.tv_findmy_name)).getText().toString();
            JSONObject lap = wifiLaptop != null ? wifiLaptop : (laptop != null && laptop.has("lat") ? laptop : null);
            if (lap != null) {
                d.put("laptop", new JSONObject().put("lat", lap.getDouble("lat")).put("lng", lap.getDouble("lng"))
                        .put("acc", lap.optDouble("acc", 30)).put("label", name));
            }
            JSONObject me = wifiPhone != null ? wifiPhone : phoneFix;
            if (me != null) {
                d.put("phone", new JSONObject().put("lat", me.getDouble("lat")).put("lng", me.getDouble("lng"))
                        .put("acc", me.optDouble("acc", 20)).put("label", "You"));
            }
            if (wifiPhone != null && wifiLaptop != null && apart >= 0) d.put("apart", apart);
            map.evaluateJavascript("show(" + d + ")", null);
        } catch (Exception ignored) { }
    }

    /** This phone's own position, fresh (the fused provider, else GPS), in the background. */
    private void findMe() {
        if (!FindMy.hasLocation(this)) return;
        new Thread(() -> {
            Location l = FindMy.currentLocation(this, false);
            if (l == null) l = FindMy.currentLocation(this, true);
            Location got = l;
            if (got != null) ui.post(() -> {
                try {
                    phoneFix = new JSONObject().put("lat", got.getLatitude()).put("lng", got.getLongitude())
                            .put("acc", got.hasAccuracy() ? got.getAccuracy() : 20);
                    showOnMap();
                } catch (Exception ignored) { }
            });
        }, "find-me").start();
    }

    // ---- the computer ----
    private void locate(boolean fresh) {
        if (fresh) tvSeen.setText("Asking your computer…");
        io.execute(() -> {
            JSONObject r = FindMy.getJson(this, "/api/findmy/laptop" + (fresh ? "?fresh=1" : ""));
            if (r != null) FindMy.absorbLaptop(this, r);
            ui.post(() -> {
                if (r == null) {
                    showLaptop("Can't reach it right now");
                    return;
                }
                JSONObject now = r.optJSONObject("now"), last = r.optJSONObject("last");
                if (now != null) laptop = now;
                else if (last != null) laptop = last;
                FindMy.rememberLaptop(this, laptop);
                setRinging(r.optBoolean("ringing", ringing));
                JSONObject lost = r.optJSONObject("lost");
                if (lost != null) showLost(lost.optBoolean("lost"), lost.optDouble("since", 0));
                JSONObject settings = r.optJSONObject("settings");
                if (settings != null) {
                    swWalk.setChecked(settings.optBoolean("walk_away_lock", true));
                    swPcPhoto.setChecked(settings.optBoolean("wrong_password_photo", true));
                }
                showAlerts();                 // absorbLaptop kept the computer's alerts here
                String ssid = r.optString("ssid", "");
                showLaptop(now != null ? null : ("Online" + (ssid.isEmpty() ? "" : " on " + ssid) + ", somewhere this phone hasn't been"));
            });
        });
    }

    private void showLaptop(String note) {
        StringBuilder sb = new StringBuilder();
        if (laptop != null && laptop.has("lat")) {
            sb.append(laptop.has("t") ? "Found " + ago(laptop.optDouble("t")) : "Found");
            String ssid = laptop.optString("ssid", "");
            if (!ssid.isEmpty()) sb.append(" · ").append(ssid);
            sb.append(" · ±").append(Math.round(laptop.optDouble("acc", 30))).append(" m");
        } else {
            sb.append("Not located yet: it learns places from this phone's location, so turn on Share location in Settings");
        }
        if (note != null) sb.append("\n").append(note);
        tvSeen.setText(sb.toString());
        showOnMap();
    }

    private static String ago(double t) {
        long s = Math.max(0, System.currentTimeMillis() / 1000 - (long) t);
        if (s < 60) return "just now";
        if (s < 3600) return (s / 60) + " min ago";
        if (s < 86400) return (s / 3600) + " h ago";
        return (s / 86400) + " days ago";
    }

    private void showAlerts() {
        FindMy.trimHistory(this);
        JSONArray list = FindMy.history(this);
        alerts.removeAllViews();
        int n = Math.min(list.length(), FindMy.KEEP_PHOTOS);
        findViewById(R.id.tv_findmy_alerts_empty).setVisibility(n == 0 ? View.VISIBLE : View.GONE);
        float dp = getResources().getDisplayMetrics().density;
        for (int i = 0; i < n; i++) {
            JSONObject a = list.optJSONObject(i);
            if (a == null) continue;
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setGravity(Gravity.CENTER_VERTICAL);
            row.setPadding(0, (int) (8 * dp), 0, (int) (8 * dp));
            ImageView thumb = new ImageView(this);
            thumb.setScaleType(ImageView.ScaleType.CENTER_CROP);
            thumb.setBackgroundResource(R.drawable.bg_icon_tile);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams((int) (48 * dp), (int) (48 * dp));
            row.addView(thumb, lp);
            boolean lock = "walk_away_lock".equals(a.optString("kind"));
            if (lock) {
                thumb.setImageResource(R.drawable.ic_o_lock);
                thumb.setPadding((int) (12 * dp), (int) (12 * dp), (int) (12 * dp), (int) (12 * dp));
                thumb.setColorFilter(getColor(R.color.o_text_2));
            }
            TextView text = new TextView(this);
            text.setTextColor(getColor(R.color.o_text));
            text.setTextSize(14);
            String kind = a.optString("kind");
            text.setText(("lost_report".equals(kind) ? "Lost mode report" : "Wrong password typed")
                    + "\n" + FindMy.whereLine(a) + "\n" + when(a.optDouble("t")));
            JSONObject place = a.optJSONObject("place");
            if (place != null) {                       // tap the text: show that spot on the map
                text.setOnClickListener(v -> {
                    try {
                        wifiLaptop = null;
                        laptop = new JSONObject(place.toString());
                        showOnMap();
                        ((ScrollView) findViewById(R.id.sv_findmy_computer)).smoothScrollTo(0, 0);
                    } catch (Exception ignored) { }
                });
            }
            text.setLineSpacing(3 * dp, 1);
            LinearLayout.LayoutParams tp = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1);
            tp.setMarginStart((int) (12 * dp));
            row.addView(text, tp);
            alerts.addView(row);
            String name = a.isNull("name") ? null : a.optString("name", null);
            if (!lock && name != null) {
                io.execute(() -> {
                    Bitmap b = FindMy.laptopPhoto(this, name);
                    if (b != null) ui.post(() -> {
                        thumb.setImageBitmap(b);
                        thumb.setOnClickListener(v -> showPhoto(b, name));
                        row.setOnClickListener(v -> showPhoto(b, name));
                    });
                });
            }
        }
    }

    /** The photo, full screen, with a way to keep a copy in the gallery. */
    private void showPhoto(Bitmap b, String name) {
        android.app.Dialog d = new android.app.Dialog(this, android.R.style.Theme_Black_NoTitleBar_Fullscreen);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        box.setBackgroundColor(0xFF000000);
        ImageView big = new ImageView(this);
        big.setImageBitmap(b);
        big.setAdjustViewBounds(true);
        big.setScaleType(ImageView.ScaleType.FIT_CENTER);
        box.addView(big, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1));
        LinearLayout buttons = new LinearLayout(this);
        float dp = getResources().getDisplayMetrics().density;
        buttons.setPadding((int) (16 * dp), (int) (12 * dp), (int) (16 * dp), (int) (20 * dp));
        android.widget.Button save = new android.widget.Button(this, null, 0, R.style.Btn_Secondary);
        save.setText("Save to Photos");
        save.setOnClickListener(v -> saveToGallery(b, name));
        android.widget.Button close = new android.widget.Button(this, null, 0, R.style.Btn_Secondary);
        close.setText("Close");
        close.setOnClickListener(v -> d.dismiss());
        LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(0, (int) (48 * dp), 1);
        buttons.addView(save, bp);
        LinearLayout.LayoutParams cp = new LinearLayout.LayoutParams(0, (int) (48 * dp), 1);
        cp.setMarginStart((int) (8 * dp));
        buttons.addView(close, cp);
        box.addView(buttons, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        d.setContentView(box);
        d.show();
    }

    private void saveToGallery(Bitmap b, String name) {
        try {
            android.content.ContentValues v = new android.content.ContentValues();
            v.put(android.provider.MediaStore.Images.Media.DISPLAY_NAME, name);
            v.put(android.provider.MediaStore.Images.Media.MIME_TYPE, "image/jpeg");
            if (Build.VERSION.SDK_INT >= 29) v.put(android.provider.MediaStore.Images.Media.RELATIVE_PATH, "Pictures/Orbit");
            Uri uri = getContentResolver().insert(android.provider.MediaStore.Images.Media.EXTERNAL_CONTENT_URI, v);
            if (uri == null) throw new Exception("no room");
            try (java.io.OutputStream out = getContentResolver().openOutputStream(uri)) {
                b.compress(Bitmap.CompressFormat.JPEG, 95, out);
            }
            Toast.makeText(this, "Saved to Photos (Pictures/Orbit)", Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            Toast.makeText(this, "Couldn't save it: " + e.getMessage(), Toast.LENGTH_SHORT).show();
        }
    }

    /** "5 min ago" for recent ones, the date and time for older ones. */
    private static String when(double t) {
        long s = System.currentTimeMillis() / 1000 - (long) t;
        if (s < 86400) return ago(t);
        return java.text.DateFormat.getDateTimeInstance(java.text.DateFormat.MEDIUM, java.text.DateFormat.SHORT)
                .format(new java.util.Date((long) (t * 1000)));
    }

    private void showLost(boolean lost, double since) {
        tvLost.setText(lost ? "Lost mode is on" : "Mark as lost");
        tvLost.setTextColor(getColor(lost ? R.color.o_danger : R.color.o_text));
        ivLost.setColorFilter(getColor(lost ? R.color.o_danger : R.color.o_text_2));
        tvLostDesc.setText(lost
                ? (since > 0 ? "Since " + when(since) + " · " : "") + "tap when you've found it"
                : "A photo, the Wi-Fi around it and its city, every 10 minutes");
    }

    private void setLost(boolean on) {
        io.execute(() -> {
            String res = FindMy.post(this, "/api/findmy/lost", "{\"on\":" + on + "}");
            ui.post(() -> {
                if (res == null) {
                    Toast.makeText(this, "Can't reach your computer right now", Toast.LENGTH_SHORT).show();
                    return;
                }
                FindMy.prefs(this).edit().putBoolean(FindMy.PREF_LOST, on).apply();
                showLost(on, on ? System.currentTimeMillis() / 1000.0 : 0);
                Toast.makeText(this, on ? "Marked lost: the first report comes in a moment" : "Lost mode is off",
                        Toast.LENGTH_SHORT).show();
            });
        });
    }

    private void setRinging(boolean on) {
        ringing = on;
        btnRing.setBackgroundResource(on ? R.drawable.findmy_action_on : R.drawable.findmy_action);
        btnRing.setColorFilter(getColor(on ? R.color.o_ground : R.color.o_text));
        tvRing.setText(on ? "Stop sound" : "Play sound");
    }

    private void ring(boolean on) {
        setRinging(on);
        io.execute(() -> {
            String res = FindMy.post(this, "/api/findmy/laptop/ring", "{\"on\":" + on + "}");
            if (res == null) ui.post(() -> {
                setRinging(false);
                Toast.makeText(this, "Can't reach your computer right now", Toast.LENGTH_SHORT).show();
            });
        });
    }

    // ---- the nearby finder: how alike this phone's Wi-Fi is to the computer's ----
    private void setNearby(boolean on) {
        if (on && !askedNearby && (!FindMy.hasLocation(this) || !SoundRange.allowed(this) || !bluetoothAllowed())) {
            // the Wi-Fi around (Android shows it only with location access), the microphone (the
            // distance by sound) and Bluetooth (optional), only while this is open
            askedNearby = true;
            java.util.List<String> perms = new java.util.ArrayList<>(java.util.Arrays.asList(
                    Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION,
                    Manifest.permission.RECORD_AUDIO));
            if (Build.VERSION.SDK_INT >= 31) {
                perms.add(Manifest.permission.BLUETOOTH_SCAN);
                perms.add(Manifest.permission.BLUETOOTH_CONNECT);
                perms.add(Manifest.permission.BLUETOOTH_ADVERTISE);
            }
            requestPermissions(perms.toArray(new String[0]), REQ_NEARBY);
            return;
        }
        nearby = on;
        btnNearby.setBackgroundResource(on ? R.drawable.findmy_action_on : R.drawable.findmy_action);
        btnNearby.setColorFilter(getColor(on ? R.color.o_ground : R.color.o_text));
        tvNearby.setText(on ? "Stop" : "Nearby");
        meter.setVisibility(on ? View.VISIBLE : View.GONE);
        if (on) {
            tvMeter.setText("Comparing Wi-Fi…");
            pbMeter.setProgress(0);
            startBluetooth();
            nearbyStep(++nearbyRun);
        } else {
            nearbyRun++;
            stopBluetooth();
        }
    }

    // ---- Bluetooth, when it's on: how strongly this phone hears the computer's mark ----
    private boolean bluetoothAllowed() {
        return Build.VERSION.SDK_INT < 31
                || checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED;
    }

    private boolean bluetoothOn() {
        android.bluetooth.BluetoothManager bm = (android.bluetooth.BluetoothManager) getSystemService(BLUETOOTH_SERVICE);
        android.bluetooth.BluetoothAdapter a = bm != null ? bm.getAdapter() : null;
        return a != null && a.isEnabled() && bluetoothAllowed();
    }

    @SuppressLint("MissingPermission")
    private void startBluetooth() {
        if (!bluetoothOn()) return;
        io.execute(() -> FindMy.post(this, "/api/findmy/laptop/beacon", "{\"on\":true}"));
        try {
            android.bluetooth.BluetoothManager bm = (android.bluetooth.BluetoothManager) getSystemService(BLUETOOTH_SERVICE);
            bleScanner = bm.getAdapter().getBluetoothLeScanner();
            if (bleScanner == null) return;
            bleReadings.clear();
            bleScan = new android.bluetooth.le.ScanCallback() {
                @Override public void onScanResult(int type, android.bluetooth.le.ScanResult r) {
                    synchronized (bleReadings) {
                        bleReadings.addLast(new long[]{System.currentTimeMillis(), r.getRssi()});
                        while (bleReadings.size() > 15) bleReadings.removeFirst();
                    }
                }
            };
            bleScanner.startScan(java.util.Collections.singletonList(new android.bluetooth.le.ScanFilter.Builder()
                            .setServiceUuid(new android.os.ParcelUuid(FindMy.ORBIT_BLE_UUID)).build()),
                    new android.bluetooth.le.ScanSettings.Builder()
                            .setScanMode(android.bluetooth.le.ScanSettings.SCAN_MODE_LOW_LATENCY).build(), bleScan);
        } catch (Exception ignored) { }
    }

    @SuppressLint("MissingPermission")
    private void stopBluetooth() {
        try { if (bleScanner != null && bleScan != null) bleScanner.stopScan(bleScan); } catch (Exception ignored) { }
        bleScan = null;
        if (bluetoothOn()) io.execute(() -> FindMy.post(this, "/api/findmy/laptop/beacon", "{\"on\":false}"));
    }

    /** The computer's mark as heard over the last few seconds: the median dBm, or null. */
    private Integer bluetoothDbm() {
        java.util.List<Integer> recent = new java.util.ArrayList<>();
        long now = System.currentTimeMillis();
        synchronized (bleReadings) {
            for (long[] r : bleReadings) if (now - r[0] < 6000) recent.add((int) r[1]);
        }
        if (recent.isEmpty()) return null;
        java.util.Collections.sort(recent);
        return recent.get(recent.size() / 2);
    }

    /** Every few seconds while on: scan (Android allows a few a minute), compare, show. */
    private void nearbyStep(int run) {
        if (!nearby || run != nearbyRun) return;
        io.execute(() -> {
            // First measure acoustic chirp: sound confirmation guarantees same room
            JSONObject snd = nearby && run == nearbyRun ? SoundRange.measure(this) : null;
            boolean soundHeard = (snd != null && snd.optBoolean("heard"));

            // Send Wi-Fi scan to computer, passing calibratedRoom if sound confirms same room
            JSONObject r = FindMy.compareWifi(this, "nearby", true, soundHeard);
            ui.post(() -> {
                if (!nearby || run != nearbyRun) return;

                if (r != null) {                         // both on the Wi-Fi map, the same way
                    wifiPhone = r.optJSONObject("phone_at");
                    wifiLaptop = r.optJSONObject("laptop_at");
                    apart = r.has("apart") ? r.optDouble("apart") : -1;
                    showOnMap();
                }

                double calcGps = calculateGpsDistance();
                double serverApart = (r != null && r.has("apart") && !r.isNull("apart")) ? r.optDouble("apart") : -1;
                JSONObject identity = r != null ? r.optJSONObject("identity") : null;
                int commonCount = identity != null ? identity.optInt("common_count", 0) : 0;
                int strongCount = identity != null ? identity.optInt("strong_count", 0) : 0;
                int matchPercent = identity != null ? identity.optInt("match_percent", 0)
                        : (r != null && r.has("score") && !r.isNull("score") ? (int) Math.round(r.optDouble("score") * 100) : 0);
                Integer directDbm = bluetoothDbm();
                String zone = r != null ? r.optString("zone", "") : "";
                String zoneMethod = r != null ? r.optString("zone_method", "") : "";
                boolean isCalibrated = identity != null && identity.optBoolean("calibrated", false);

                if (snd != null && snd.optBoolean("heard")) {
                    // Tier 0: Ultrasonic acoustic ranging (centimetre-level accuracy in the room)
                    double d = snd.optDouble("distance");
                    apart = d;
                    showOnMap();
                    tvMeter.setText(d < 0.5 ? "Right here (<0.5m)" : String.format(java.util.Locale.US, "%.1f m away", d));
                    tvMeterSub.setText("Acoustic ranging: in this room · Antenna calibrated");
                    pbMeter.setProgress(distanceToProgress(d));
                } else if (directDbm != null) {
                    // Tier 1: Direct calibrated BLE beacon (immediate vicinity within ~1-5 m)
                    double d = FindMy.bluetoothMetres(directDbm);
                    apart = d;
                    showOnMap();
                    tvMeter.setText(d < 0.8 ? "Immediate proximity (<1m)" : String.format(java.util.Locale.US, "About %.0f m away", d));
                    tvMeterSub.setText(String.format(java.util.Locale.US, "Direct beacon signal · %d dBm", directDbm));
                    pbMeter.setProgress(distanceToProgress(d));
                } else if (!zone.isEmpty() && !"Elsewhere".equalsIgnoreCase(zone)) {
                    // Tier 2: Calibrated Wi-Fi Identity Recognition (Honest zones, not fake metres)
                    if (serverApart > 0) apart = serverApart;
                    showOnMap();
                    tvMeter.setText(zone);
                    String calibTag = isCalibrated ? " · Calibrated" : "";
                    if ("fingerprint_map".equals(zoneMethod)) {
                        tvMeterSub.setText(String.format(java.util.Locale.US, "Wi-Fi map fingerprint match · %d shared networks", commonCount));
                    } else {
                        double gap = identity != null ? identity.optDouble("signal_gap", 0) : 0;
                        tvMeterSub.setText(String.format(java.util.Locale.US,
                                "Wi-Fi: %d%% match · %d shared (%d strong) · Δ %.1f dBm%s",
                                matchPercent, commonCount, strongCount, gap, calibTag));
                    }
                    if ("Same room".equalsIgnoreCase(zone)) {
                        pbMeter.setProgress(85);
                    } else if ("Next room or two".equalsIgnoreCase(zone)) {
                        pbMeter.setProgress(55);
                    } else {
                        pbMeter.setProgress(30);
                    }
                } else if (serverApart > 0 || calcGps > 0) {
                    // Tier 3: Macro separation / campus scale (different locations)
                    double d = serverApart > 0 ? serverApart : calcGps;
                    apart = d;
                    showOnMap();
                    tvMeter.setText(d < 15 ? "Same building" : (d >= 1000 ? String.format(java.util.Locale.US, "Elsewhere (%.1f km away)", d / 1000.0) : String.format(java.util.Locale.US, "Elsewhere (%.0f m away)", d)));
                    tvMeterSub.setText(r != null ? r.optString("word", "Different Wi-Fi networks") : "Different Wi-Fi networks");
                    pbMeter.setProgress(distanceToProgress(d));
                } else if (r == null || r.isNull("score")) {
                    tvMeter.setText(r == null ? "Can't reach your computer" : r.optString("word", "No Wi-Fi to compare"));
                    tvMeterSub.setText("Scanning Wi-Fi environment…");
                    pbMeter.setProgress(0);
                } else {
                    tvMeter.setText("Elsewhere");
                    tvMeterSub.setText("Wi-Fi: No shared networks · Different location");
                    pbMeter.setProgress(5);
                }
                ui.postDelayed(() -> nearbyStep(run), 1500);
            });
        });
    }

    // ---- settings ----
    private void showSwitches() {
        swShare.setChecked(FindMy.sharing(this));
        TextView shareDesc = findViewById(R.id.tv_findmy_share_desc);
        shareDesc.setText(FindMy.sharing(this) && !FindMy.hasBackgroundLocation(this)
                ? "Only while Orbit is open: allow location \"all the time\""
                : "Only your computer gets it");
        swPhoto.setChecked(FindMy.prefs(this).getBoolean(FindMy.PREF_PHOTO, false) && adminActive());
        swSim.setChecked(FindMy.prefs(this).getBoolean(FindMy.PREF_SIM, false));
        swWalk.setChecked(FindMy.prefs(this).getBoolean(FindMy.PREF_WALK, true));
        TextView walkDesc = findViewById(R.id.tv_findmy_walk_desc);
        walkDesc.setText(FindMy.hasLocation(this)
                ? "When this phone leaves, the computer locks itself"
                : "When this phone leaves the computer's Wi-Fi, it locks itself (with location on, also on big networks like a campus)");
    }

    /** One of the computer's own settings. */
    private void setComputer(String key, boolean on) {
        if ("walk_away_lock".equals(key)) FindMy.prefs(this).edit().putBoolean(FindMy.PREF_WALK, on).apply();
        io.execute(() -> {
            String res = FindMy.post(this, "/api/findmy/settings", "{\"" + key + "\":" + on + "}");
            if (res == null) ui.post(() -> {
                Toast.makeText(this, "Can't reach your computer right now", Toast.LENGTH_SHORT).show();
                (key.equals("walk_away_lock") ? swWalk : swPcPhoto).setChecked(!on);
            });
        });
    }

    private void onShare(boolean on) {
        if (!on) {
            FindMy.prefs(this).edit().putBoolean(FindMy.PREF_SHARE, false).apply();
            FindMy.stopWatching(this);
            showSwitches();
            return;
        }
        FindMy.prefs(this).edit().putBoolean(FindMy.PREF_SHARE, true).apply();
        if (!FindMy.hasLocation(this)) {
            requestPermissions(new String[]{Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION}, REQ_LOCATION);
            return;
        }
        askBackground();
    }

    private void askBackground() {
        if (FindMy.hasBackgroundLocation(this)) {
            FindMy.report(this, "requested");
            showSwitches();
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle("Location all the time")
                .setMessage("To report where this phone is while Orbit isn't open, choose \"Allow all the time\" on the next screen.")
                .setPositiveButton("Continue", (d, w) -> requestPermissions(
                        new String[]{Manifest.permission.ACCESS_BACKGROUND_LOCATION}, REQ_BACKGROUND))
                .setNegativeButton("Not now", (d, w) -> showSwitches())
                .show();
    }

    private boolean adminActive() {
        DevicePolicyManager dpm = (DevicePolicyManager) getSystemService(DEVICE_POLICY_SERVICE);
        return dpm != null && dpm.isAdminActive(new ComponentName(this, OrbitAdminReceiver.class));
    }

    private void onPhoto(boolean on) {
        if (!on) {
            FindMy.prefs(this).edit().putBoolean(FindMy.PREF_PHOTO, false).apply();
            showSwitches();
            return;
        }
        FindMy.prefs(this).edit().putBoolean(FindMy.PREF_PHOTO, true).apply();
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, REQ_CAMERA);
            return;
        }
        continuePhoto();
    }

    /** Step by step: the camera, being told about wrong unlocks (device admin), opening over
     *  the lock screen (display over other apps). */
    private void continuePhoto() {
        if (!adminActive()) {
            Intent i = new Intent(DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN)
                    .putExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN, new ComponentName(this, OrbitAdminReceiver.class))
                    .putExtra(DevicePolicyManager.EXTRA_ADD_EXPLANATION,
                            "Orbit is only told when someone types a wrong PIN or password, to take a photo for your computer. It can't lock, wipe or change anything.");
            startActivityForResult(i, REQ_ADMIN);
            return;
        }
        if (Build.VERSION.SDK_INT >= 29 && !Settings.canDrawOverlays(this)) {
            new AlertDialog.Builder(this)
                    .setTitle("One more permission")
                    .setMessage("To take the photo while the phone is locked, Orbit needs \"Display over other apps\". Turn it on for Orbit on the next screen.")
                    .setPositiveButton("Continue", (d, w) -> {
                        try {
                            startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                    Uri.parse("package:" + getPackageName())));
                        } catch (Exception ignored) { }
                    })
                    .setNegativeButton("Skip", null)
                    .show();
        }
        showSwitches();
    }

    private void onSim(boolean on) {
        FindMy.prefs(this).edit().putBoolean(FindMy.PREF_SIM, on).apply();
        if (on && checkSelfPermission(Manifest.permission.READ_PHONE_STATE) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.READ_PHONE_STATE}, REQ_PHONE);
            return;
        }
        if (on) FindMy.checkSim(this);       // the SIM in it now is the phone's own
        showSwitches();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_ADMIN) {
            if (adminActive()) continuePhoto();
            else {
                FindMy.prefs(this).edit().putBoolean(FindMy.PREF_PHOTO, false).apply();
                showSwitches();
            }
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        boolean granted = results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED;
        if (code == REQ_LOCATION) {
            if (granted) askBackground();
            else {
                FindMy.prefs(this).edit().putBoolean(FindMy.PREF_SHARE, false).apply();
                showSwitches();
            }
        } else if (code == REQ_BACKGROUND) {
            FindMy.report(this, "requested");
            showSwitches();
        } else if (code == REQ_NEARBY) {
            if (FindMy.hasLocation(this)) setNearby(true);
            askedNearby = false;
        } else if (code == REQ_CAMERA) {
            if (granted) continuePhoto();
            else {
                FindMy.prefs(this).edit().putBoolean(FindMy.PREF_PHOTO, false).apply();
                showSwitches();
            }
        } else if (code == REQ_PHONE) {
            if (granted) FindMy.checkSim(this);
            showSwitches();
        }
    }
}

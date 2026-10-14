package com.lunarphoton.pcauthenticator;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.Dialog;
import android.app.KeyguardManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.graphics.Color;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.CancellationSignal;
import android.os.CountDownTimer;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.provider.Settings;
import android.util.Log;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.view.GestureDetector;
import android.view.HapticFeedbackConstants;
import android.view.MotionEvent;
import android.view.inputmethod.EditorInfo;
import android.text.Editable;
import android.text.InputType;
import android.text.TextWatcher;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;
import android.widget.ViewFlipper;

import org.json.JSONArray;
import org.json.JSONObject;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.media.MediaScannerConnection;
import android.provider.MediaStore;
import android.provider.OpenableColumns;
import android.widget.ImageView;
import android.widget.ProgressBar;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private TextView tvHostname;
    private TextView tvActiveIp;
    private TextView tvStatusDot;
    private TextView tvStatusText;
    private Button btnTestConnection;

    // KDE Connect Feature UI references
    private static final int REQUEST_PICK_FILE = 4001;
    private Button btnSendFileToPc;
    private Button btnSendClipToPc;
    private Button btnGetClipFromPc;
    private Button btnRingPc;
    private Button btnRemoteTrackpad;
    private Button btnCameraView;

    private TextView tvMediaStatus;
    private TextView tvMediaTitle;
    private TextView tvMediaArtist;
    private Button btnMediaPrev;
    private Button btnMediaPlayPause;
    private Button btnMediaNext;

    // Laptop Live Status UI
    private TextView tvPcLockBadge;
    private TextView tvPcBattery;
    private TextView tvPcUptime;
    private TextView tvPcCpu;
    private TextView tvPcMemory;
    private TextView tvPcStorage;
    private TextView tvPcWeather;
    private Button btnLockLaptopAction;
    private Button btnScreenshotAction;
    private Button btnBrowseLaptopAction;
    private Button btnSearchFilesAction;
    private boolean isPcCurrentlyLocked = false;
    private Bitmap currentScreenshotBitmap = null;
    private String currentBrowsePath = "shortcuts";
    private String parentBrowsePath = null;

    private LinearLayout layoutChallenge;
    private TextView tvChallengeMessage;
    private TextView tvTimer;
    private Button btnApproveChallenge;
    private Button btnDenyChallenge;

    private Button btnSettings;
    private View layoutCaptivePortal;
    private TextView tvCaptiveStatusBadge;
    private TextView tvCaptiveActionsLink;
    private Button btnCaptivePortalToggle;
    private volatile boolean isCaptiveLive = false;

    private CancellationSignal cancellationSignal = null;
    private boolean isPromptShowing = false;

    private LinearLayout layoutIdle;

    private TextView tvActiveModeChip;
    private View layoutLanFeatures;
    private View layoutLanNotice;
    private TextView tvNetworkModeBadge;
    private TextView tvDeviceCount;
    private Button btnScanNetwork;
    private Button btnAddManual;
    private TextView tvScanStatus;
    private LinearLayout containerDevices;

    // --- Live Terminals Panel ---
    private ViewFlipper mainViewFlipper;
    private TextView tabBtnDashboard;
    private TextView tabBtnTerminals;
    private TextView tabBtnControl;
    private TextView tabBtnFiles;
    private View layoutDevicesSheet;

    private View layoutTopTabs;
    private View panelTerminals;
    private View layoutTerminalsListHeader;
    private HorizontalScrollView scrollTermChips;
    private Button btnTerminalsRefresh;
    private Button btnTerminalsNew;
    private LinearLayout layoutTermChips;
    private ScrollView scrollTermList;
    private LinearLayout containerTerminalsList;
    private View layoutTerminalsEmpty;

    private View layoutTerminalInteractive;
    private Button btnInteractiveBack;
    private TextView tvInteractiveTitle;
    private TextView tvInteractiveStatus;
    private Button btnInteractiveRefresh;
    private Button btnInteractiveRotate;
    private TextView tvInteractiveCwd;
    private ScrollView scrollTermScreen;
    private HorizontalScrollView hscrollTermScreen;
    private TextView tvTermScreen;
    private Button btnTermLoadEarlier;

    private Button btnKeyMode;
    private Button btnKeyCtrl;
    private Button btnKeyEsc;
    private Button btnKeyTab;
    private Button btnKeyLeft;
    private Button btnKeyRight;
    private Button btnKeyUp;
    private Button btnKeyDown;
    private Button btnKeyClear;
    private boolean isCtrlActive = false;

    private EditText etTermInput;
    private Button btnTermSend;

    // Typing is always live: edits in the box are mirrored to the computer as they happen
    private final boolean isLiveTypeEnabled = true;
    // Off by default: lines keep the computer's width (tables stay intact) and the text is
    // scaled to fit; "wrap" in the key bar switches to wrapped lines for reading prose.
    private boolean isTerminalWrapEnabled = false;
    private int terminalColumns = 0;
    private boolean isInternalTextUpdate = false;
    private final ExecutorService terminalSendExecutor = Executors.newSingleThreadExecutor();
    private volatile boolean isPollingTerminal = false;
    private volatile boolean hasPendingTerminalPoll = false;
    private static final int TERMINAL_STREAM_INTERVAL_MS = 350;
    private static final int TERMINAL_REMOTE_INTERVAL_MS = 1500;
    // Terminal polling: the computer answers "unchanged" for a screen we already show,
    // and we poll less often while nothing changes.
    private volatile String lastTerminalVersion = "";
    /** The lines on screen right now, exactly as the computer sent them (null: unknown, so the
     *  next reply must be a full one). Changes arrive as deltas against these. */
    private volatile java.util.List<String> shownTermLines = null;
    /** The coloured text on screen (goes with shownTermLines); deltas are applied to a copy
     *  of it off the main thread. */
    private volatile CharSequence shownTermColored = null;
    /** The computer holds a read until the screen changes (pushed updates, like tmux's control
     *  mode), so the phone keeps one request open instead of asking every 350 ms. */
    private volatile boolean termServerWaits = false;

    /** What each recently viewed terminal showed, so going back and opening it again is instant:
     *  it reappears as it was and only what changed since is fetched (kept while the app runs). */
    private static class TermSnapshot {
        String version, text; java.util.List<String> lines; CharSequence colored;
        boolean wrap; int linesToFetch;
    }
    private final java.util.Map<String, TermSnapshot> termSnapshots =
            new java.util.LinkedHashMap<String, TermSnapshot>(16, 0.75f, true) {
                @Override protected boolean removeEldestEntry(java.util.Map.Entry<String, TermSnapshot> e) { return size() > 8; }
            };

    private void saveTermSnapshot() {
        if (activeTerminalId == null || shownTermColored == null || shownTermLines == null || lastTerminalVersion.isEmpty()) return;
        TermSnapshot snap = new TermSnapshot();
        snap.version = lastTerminalVersion;
        snap.text = lastLoadedTerminalText;
        snap.lines = shownTermLines;
        snap.colored = shownTermColored;
        snap.wrap = isTerminalWrapEnabled;
        snap.linesToFetch = currentTerminalLinesToFetch;
        synchronized (termSnapshots) { termSnapshots.put(activeTerminalId, snap); }
    }

    // --- Terminals you just left keep syncing (while the app is open), so reopening one shows
    // what's on the computer now. Each is one held request: a quiet terminal costs a request
    // every ~20 s.
    private final java.util.Map<String, Thread> bgTermSync = new java.util.LinkedHashMap<>();
    private String lastClosedTerminalId = null;

    private void startBackgroundTermSync(String termId) {
        if (termId == null) return;
        stopBackgroundTermSync(termId);
        while (bgTermSync.size() >= 2) stopBackgroundTermSync(bgTermSync.keySet().iterator().next());
        Thread t = new Thread(() -> {
            while (!Thread.currentThread().isInterrupted()) {
                TermSnapshot snap;
                synchronized (termSnapshots) { snap = termSnapshots.get(termId); }
                PairedDevice active = DeviceManager.getActiveDevice(this);
                String base = active != null ? getTerminalReadUrl(active) : null;
                if (base == null) return;
                try {
                    if (snap == null) {
                        // left before its first load finished: load it here
                        boolean wrap = isTerminalWrapEnabled;
                        String url = base + "/api/terminals/read?id=" + java.net.URLEncoder.encode(termId, "UTF-8")
                                + "&lines=300" + (wrap ? "&join=1" : "");
                        String res = NetworkUtils.httpGetWithAuth(url, active.authToken, 15000);
                        if (Thread.currentThread().isInterrupted()) return;
                        if (res == null) { Thread.sleep(3000); continue; }
                        TermSnapshot seed = new TermSnapshot();
                        seed.version = ""; seed.wrap = wrap; seed.linesToFetch = 300;
                        seed.lines = new java.util.ArrayList<>();
                        TermSnapshot first = advanceSnapshot(seed, new JSONObject(res));
                        if (first == null) { Thread.sleep(3000); continue; }
                        synchronized (termSnapshots) {
                            if (!termSnapshots.containsKey(termId)) termSnapshots.put(termId, first);
                        }
                        continue;
                    }
                    String url = base + "/api/terminals/read?id=" + java.net.URLEncoder.encode(termId, "UTF-8")
                            + "&lines=" + snap.linesToFetch + "&v=" + snap.version + "&delta=1&wait=1"
                            + (snap.wrap ? "&join=1" : "");
                    String res = NetworkUtils.httpGetWithAuth(url, active.authToken, 30000);
                    if (Thread.currentThread().isInterrupted()) return;
                    if (res == null) { Thread.sleep(5000); continue; }
                    JSONObject reply = new JSONObject(res);
                    if (!"ok".equals(reply.optString("status"))) {
                        synchronized (termSnapshots) { termSnapshots.remove(termId); }   // the tab closed
                        return;
                    }
                    TermSnapshot next = advanceSnapshot(snap, reply);
                    if (next != null) {
                        synchronized (termSnapshots) {
                            if (termSnapshots.get(termId) == snap) termSnapshots.put(termId, next);
                        }
                    }
                    Thread.sleep(400);            // gentle: a few updates a second at most
                } catch (InterruptedException e) {
                    return;
                } catch (Exception e) {
                    try { Thread.sleep(5000); } catch (InterruptedException ie) { return; }
                }
            }
        }, "term-bg-sync");
        t.setDaemon(true);
        bgTermSync.put(termId, t);
        t.start();
    }

    private void stopBackgroundTermSync(String termId) {
        Thread t = bgTermSync.remove(termId);
        if (t != null) t.interrupt();     // a held request finishes on its own, then the thread stops
    }

    private void stopAllBackgroundTermSync() {
        for (String id : new java.util.ArrayList<>(bgTermSync.keySet())) stopBackgroundTermSync(id);
    }

    /** The snapshot after one reply from the computer (null: nothing changed). */
    private TermSnapshot advanceSnapshot(TermSnapshot snap, JSONObject obj) {
        if (!"ok".equals(obj.optString("status")) || obj.optBoolean("unchanged", false)) return null;
        TermSnapshot n = new TermSnapshot();
        n.version = obj.optString("version", "");
        n.wrap = snap.wrap;
        n.linesToFetch = snap.linesToFetch;
        JSONObject delta = obj.optJSONObject("delta");
        if (delta != null) {
            if (!snap.version.equals(delta.optString("base")) || snap.lines.size() != delta.optInt("old_count", -1)) return null;
            int drop = delta.optInt("drop", 0), keep = delta.optInt("keep", 0);
            if (drop + keep > snap.lines.size()) return null;
            org.json.JSONArray arr = delta.optJSONArray("tail");
            java.util.List<String> tail = new java.util.ArrayList<>();
            if (arr != null) for (int i = 0; i < arr.length(); i++) tail.add(arr.optString(i, ""));
            java.util.List<String> lines = new java.util.ArrayList<>(snap.lines.subList(drop, drop + keep));
            lines.addAll(tail);
            CharSequence coloredTail = tail.isEmpty() ? "" : TerminalColorizer.colorize(android.text.TextUtils.join("\n", tail), snap.wrap, cachedTerminalCols, obj.optBoolean("ansi", false));
            CharSequence colored = spliceTerminalText(snap.colored, drop, keep, tail.size(), coloredTail);
            if (colored == null) return null;
            n.lines = lines;
            n.colored = colored;
            n.text = android.text.TextUtils.join("\n", lines);
        } else {
            String text = obj.optString("text", "");
            if (text.isEmpty()) return null;
            n.text = text;
            n.lines = java.util.Arrays.asList(text.split("\n", -1));
            n.colored = TerminalColorizer.colorize(text, snap.wrap, cachedTerminalCols, obj.optBoolean("ansi", false));
        }
        return n;
    }
    private volatile int unchangedTerminalPolls = 0;
    private volatile int cachedTerminalCols = 45;
    // Several can be in flight: a held (up to 20 s) read for a terminal just closed must not
    // hold up the next one. Each poll belongs to a generation; older ones are ignored.
    private final java.util.concurrent.ExecutorService terminalPollExecutor = java.util.concurrent.Executors.newCachedThreadPool();
    private final java.util.concurrent.atomic.AtomicInteger termPollGen = new java.util.concurrent.atomic.AtomicInteger();

    /** Forget any read in flight (switching or closing a terminal, loading earlier lines). */
    private void newTermPollGeneration() {
        termPollGen.incrementAndGet();
        isPollingTerminal = false;
        hasPendingTerminalPoll = false;
    }

    private String activeTerminalId = null;
    private String activeTerminalTitle = "";
    private String activeTerminalCwd = "";
    private int currentTerminalLinesToFetch = 300;
    private boolean isTerminalInputEnabled = true;
    private boolean hasMoreTerminalHistory = false;
    private boolean isLoadingEarlier = false;
    private String lastLoadedTerminalText = "";
    private int preserveDistanceFromBottom = -1;
    private boolean isFirstTerminalLoad = true;
    private Handler terminalHandler = new Handler(Looper.getMainLooper());
    private Runnable terminalPollRunnable = null;

    private CountDownTimer countDownTimer;
    private Handler pollHandler = new Handler(Looper.getMainLooper());
    private Runnable pollRunnable;
    private File pendingInstallApk = null;
    private ClipboardManager.OnPrimaryClipChangedListener mainClipListener = null;

    private final BroadcastReceiver serviceReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            String action = intent.getAction();
            if (AuthService.ACTION_CHALLENGE.equals(action)) {
                String message = intent.getStringExtra("message");
                showChallenge(message);
                if (isBiometricRequiredAndSupported()) {
                    btnApproveChallenge.postDelayed(MainActivity.this::promptBiometricAuthentication, 350);
                }
            } else if (AuthService.ACTION_STATUS.equals(action)) {
                boolean connected = intent.getBooleanExtra("connected", false);
                String statusText = intent.getStringExtra("status_text");
                boolean isInternet = intent.hasExtra("is_internet") ?
                        intent.getBooleanExtra("is_internet", false) :
                        ((statusText != null && statusText.contains("Internet")) || DeviceManager.isInternetActive(MainActivity.this));
                updateConnectionStatus(connected, statusText, isInternet);
            } else if ("com.lunarphoton.pcauthenticator.CHALLENGE_RESOLVED".equals(action)) {
                hideChallenge();
            } else if ("com.lunarphoton.pcauthenticator.CAPTIVE_STATE_CHANGED".equals(action)) {
                updateCaptivePortalButton();
            }
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        try {
            java.net.URLConnection.setDefaultRequestProperty("ngrok-skip-browser-warning", "1");
        } catch (Exception ignored) {}

        // 1. Allow Activity to show and turn on screen over lock screen
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
        }
        getWindow().addFlags(
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED |
                WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
        );

        setContentView(R.layout.activity_main);

        // UI references
        tvHostname = findViewById(R.id.tv_hostname);
        tvActiveIp = findViewById(R.id.tv_active_ip);
        tvStatusDot = findViewById(R.id.tv_status_dot);
        tvStatusText = findViewById(R.id.tv_status_text);
        btnTestConnection = findViewById(R.id.btn_test_connection);

        layoutChallenge = findViewById(R.id.layout_challenge);
        tvChallengeMessage = findViewById(R.id.tv_challenge_message);
        tvTimer = findViewById(R.id.tv_timer);
        btnApproveChallenge = findViewById(R.id.btn_approve_challenge);
        btnDenyChallenge = findViewById(R.id.btn_deny_challenge);

        layoutIdle = findViewById(R.id.layout_idle);

        tvDeviceCount = findViewById(R.id.tv_device_count);
        btnScanNetwork = findViewById(R.id.btn_scan_network);
        btnAddManual = findViewById(R.id.btn_add_manual);
        tvScanStatus = findViewById(R.id.tv_scan_status);
        containerDevices = findViewById(R.id.container_devices);

        // Bind KDE Connect UI
        btnSendFileToPc = findViewById(R.id.btn_send_file_to_pc);
        btnSendClipToPc = findViewById(R.id.btn_send_clip_to_pc);
        btnGetClipFromPc = findViewById(R.id.btn_get_clip_from_pc);
        btnRingPc = findViewById(R.id.btn_ring_pc);
        btnRemoteTrackpad = findViewById(R.id.btn_remote_trackpad);
        btnCameraView = findViewById(R.id.btn_camera_view);
        layoutLanFeatures = findViewById(R.id.layout_lan_features);
        layoutLanNotice = findViewById(R.id.layout_lan_notice);
        tvNetworkModeBadge = findViewById(R.id.tv_network_mode_badge);
        tvActiveModeChip = findViewById(R.id.tv_active_mode_chip);

        tvMediaStatus = findViewById(R.id.tv_media_status);
        tvMediaTitle = findViewById(R.id.tv_media_title);
        tvMediaArtist = findViewById(R.id.tv_media_artist);
        btnMediaPrev = findViewById(R.id.btn_media_prev);
        btnMediaPlayPause = findViewById(R.id.btn_media_play_pause);
        btnMediaNext = findViewById(R.id.btn_media_next);

        setupKdeConnectListeners();

        // Laptop Live Status UI bindings
        tvPcLockBadge = findViewById(R.id.tv_pc_lock_badge);
        tvPcBattery = findViewById(R.id.tv_pc_battery);
        tvPcUptime = findViewById(R.id.tv_pc_uptime);
        tvPcCpu = findViewById(R.id.tv_pc_cpu);
        tvPcMemory = findViewById(R.id.tv_pc_memory);
        tvPcStorage = findViewById(R.id.tv_pc_storage);
        tvPcWeather = findViewById(R.id.tv_pc_weather);
        btnLockLaptopAction = findViewById(R.id.btn_lock_laptop_action);
        btnScreenshotAction = findViewById(R.id.btn_screenshot_action);
        btnBrowseLaptopAction = findViewById(R.id.btn_browse_laptop_action);
        btnSearchFilesAction = findViewById(R.id.btn_search_files_action);

        if (btnLockLaptopAction != null) {
            btnLockLaptopAction.setOnClickListener(v -> {
                if (!isPcConnected) {
                    Toast.makeText(this, "Not connected to the computer", Toast.LENGTH_SHORT).show();
                    return;
                }
                if (isPcCurrentlyLocked) {
                    onUnlockButtonClicked();
                } else {
                    lockPc();
                }
            });
            btnLockLaptopAction.setOnLongClickListener(v -> {
                if (!isPcConnected) {
                    Toast.makeText(this, "Not connected to the computer", Toast.LENGTH_SHORT).show();
                    return true;
                }
                showChangePasswordDialog(null);
                return true;
            });
        }
        if (btnScreenshotAction != null) {
            btnScreenshotAction.setOnClickListener(v -> showScreenshotDialog());
        }
        if (btnBrowseLaptopAction != null) {
            btnBrowseLaptopAction.setOnClickListener(v -> showFileBrowseDialog());
        }
        if (btnSearchFilesAction != null) {
            btnSearchFilesAction.setOnClickListener(v -> showFileSearchDialog());
        }

        initTerminalsPanel();

        layoutDevicesSheet = findViewById(R.id.layout_devices_sheet);
        View btnLaptops = findViewById(R.id.btn_laptops);
        if (btnLaptops != null) btnLaptops.setOnClickListener(v -> setDevicesSheetVisible(true));
        View btnCloseDevices = findViewById(R.id.btn_close_devices);
        if (btnCloseDevices != null) btnCloseDevices.setOnClickListener(v -> setDevicesSheetVisible(false));
        if (layoutDevicesSheet != null) layoutDevicesSheet.setOnClickListener(v -> setDevicesSheetVisible(false));
        int[] folderButtons = {R.id.btn_folder_downloads, R.id.btn_folder_documents, R.id.btn_folder_desktop, R.id.btn_folder_pictures, R.id.btn_folder_home};
        String[] folderPaths = {"~/Downloads", "~/Documents", "~/Desktop", "~/Pictures", "~"};
        for (int i = 0; i < folderButtons.length; i++) {
            View b = findViewById(folderButtons[i]);
            String path = folderPaths[i];
            if (b != null) b.setOnClickListener(v -> showFileBrowseDialog(path));
        }
        initControlExtras();

        // Header Settings Button
        btnSettings = findViewById(R.id.btn_settings);
        if (btnSettings != null) {
            btnSettings.setOnClickListener(v -> showSettingsDialog());
        }
        layoutCaptivePortal = findViewById(R.id.layout_captive_portal);
        tvCaptiveStatusBadge = findViewById(R.id.tv_captive_status_badge);
        btnCaptivePortalToggle = findViewById(R.id.btn_captive_portal_toggle);

        if (btnCaptivePortalToggle != null) {
            btnCaptivePortalToggle.setOnClickListener(v -> {
                if (isCaptiveLive) {
                    handleCaptiveLogoutClick();
                } else {
                    handleCaptiveLoginClick();
                }
            });
        }
        if (tvCaptiveActionsLink != null) {
            tvCaptiveActionsLink.setOnClickListener(v -> showCaptivePortalConfigDialog(null));
        }
        if (layoutCaptivePortal != null) {
            // the button logs in and out; the card opens the setup (login page, details, auto login)
            layoutCaptivePortal.setOnClickListener(v -> showCaptivePortalConfigDialog(null));
        }
        updateCaptivePortalButton();
        updateConnectionStatus(AuthService.isConnectedToLaptop(), AuthService.getLastStatusText());
        updateApproveButtonText();

        // Listeners
        btnTestConnection.setOnClickListener(v -> testActiveConnection());
        btnScanNetwork.setOnClickListener(v -> {
            if (DeviceManager.isInternetActive(MainActivity.this)) {
                Toast.makeText(MainActivity.this, "Auto-Scan Wi-Fi is not available over Internet.", Toast.LENGTH_SHORT).show();
                return;
            }
            startNetworkScan();
        });
        btnAddManual.setOnClickListener(v -> showAddDeviceDialog());

        btnApproveChallenge.setOnClickListener(v -> {
            SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
            long expiry = prefs.getLong("active_challenge_expiry", 0);
            String activeId = prefs.getString("active_challenge_id", null);
            if (activeId == null || System.currentTimeMillis() >= expiry) {
                Toast.makeText(MainActivity.this, "Challenge expired or already handled", Toast.LENGTH_SHORT).show();
                hideChallenge();
                AuthService.cancelChallengeNotification(MainActivity.this);
                return;
            }
            if (isBiometricRequiredAndSupported()) {
                promptBiometricAuthentication();
            } else {
                executeApprove();
            }
        });

        btnDenyChallenge.setOnClickListener(v -> {
            vibrate(200);
            PairedDevice active = DeviceManager.getActiveDevice(this);
            String url = active.getBaseUrl();
            SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
            String activeChallengeId = prefs.getString("active_challenge_id", "");

            AuthService.cancelChallengeNotification(MainActivity.this);
            hideChallenge();

            new Thread(() -> {
                NetworkUtils.httpPostWithAuth(url + "/api/deny_current", active.authToken, active.secretKey, activeChallengeId, "deny", 5000);
                runOnUiThread(() -> {
                    Toast.makeText(MainActivity.this, "Unlock Denied", Toast.LENGTH_SHORT).show();
                });
            }).start();
        });

        // Request notification permission on Android 13+
        if (Build.VERSION.SDK_INT >= 33) {
            if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 101);
            }
        }

        // Request battery optimization exemption for uninterrupted background connection
        checkBatteryOptimization();
        checkStoragePermissions();
        checkPhonePermission();

        // Register receiver for background challenge alerts
        IntentFilter filter = new IntentFilter();
        filter.addAction(AuthService.ACTION_CHALLENGE);
        filter.addAction(AuthService.ACTION_STATUS);
        filter.addAction("com.lunarphoton.pcauthenticator.CHALLENGE_RESOLVED");
        filter.addAction("com.lunarphoton.pcauthenticator.CAPTIVE_STATE_CHANGED");
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(serviceReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(serviceReceiver, filter);
        }

        // Refresh device list and start background service
        refreshDeviceList();
        startAuthService();

        // Check active challenge immediately from persistent store and intent
        updateChallengeUIFromStore();
        handleIntent(getIntent());
    }

    @Override
    protected void onStart() {
        super.onStart();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
        }
        updateChallengeUIFromStore();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
        }
        updateChallengeUIFromStore();
        handleIntent(intent);
    }

    private void handleIntent(Intent intent) {
        if (intent == null) {
            return;
        }

        if ("com.lunarphoton.pcauthenticator.INSTALL_APK".equals(intent.getAction()) || intent.hasExtra("apk_path")) {
            String apkPath = intent.getStringExtra("apk_path");
            intent.removeExtra("apk_path");
            if (apkPath != null) {
                File apkFile = new File(apkPath);
                if (apkFile.exists()) {
                    openDownloadedFile(apkFile);
                }
            }
        }

        if (!intent.getBooleanExtra("from_challenge", false)) {
            return;
        }

        // Consume and strip extras immediately to prevent repeat triggers
        boolean autoBio = intent.getBooleanExtra("auto_biometric", false);
        String msg = intent.getStringExtra("message");
        intent.removeExtra("from_challenge");
        intent.removeExtra("auto_biometric");
        intent.removeExtra("message");
        intent.removeExtra("session_id");
        setIntent(new Intent(this, MainActivity.class));

        // Verify active challenge status from local store
        SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        long expiry = prefs.getLong("active_challenge_expiry", 0);
        String activeId = prefs.getString("active_challenge_id", null);
        long now = System.currentTimeMillis();

        if (activeId == null || now >= expiry) {
            Log.d("MainActivity", "handleIntent: Stale or expired challenge ignored");
            hideChallenge();
            cancelBiometricPrompt();
            AuthService.cancelChallengeNotification(this);
            return;
        }

        int remainingSec = Math.max(1, (int) ((expiry - now) / 1000L));
        if (msg == null || msg.isEmpty()) {
            msg = prefs.getString("active_challenge_msg", "Screen unlock requested on your computer. Tap Approve to unlock.");
        }
        showChallengeWithDuration(msg, remainingSec);

        if (autoBio && isBiometricRequiredAndSupported()) {
            btnApproveChallenge.postDelayed(this::promptBiometricAuthentication, 350);
        }
    }

    private void updateChallengeUIFromStore() {
        SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        long expiry = prefs.getLong("active_challenge_expiry", 0);
        String activeId = prefs.getString("active_challenge_id", null);
        long now = System.currentTimeMillis();
        if (activeId != null && now < expiry) {
            String msg = prefs.getString("active_challenge_msg", "Screen unlock requested on your computer. Tap Approve to unlock.");
            int remainingSec = (int) ((expiry - now) / 1000L);
            showChallengeWithDuration(msg, Math.max(1, remainingSec));
        } else {
            hideChallenge();
            AuthService.cancelChallengeNotification(this);
        }
    }

    private void checkBatteryOptimization() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
                try {
                    Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                    intent.setData(Uri.parse("package:" + getPackageName()));
                    startActivity(intent);
                } catch (Exception ignored) {}
            }
        }
    }

    private void checkStoragePermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (!Environment.isExternalStorageManager()) {
                new AlertDialog.Builder(this)
                        .setTitle("Allow Storage Access")
                        .setMessage("To allow browsing, downloading, and sharing files between your computer and phone, please enable 'Allow access to manage all files'.")
                        .setPositiveButton("Grant Access", (dialog, which) -> {
                            try {
                                Intent intent = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION);
                                intent.setData(Uri.parse("package:" + getPackageName()));
                                startActivity(intent);
                            } catch (Exception e) {
                                try {
                                    Intent intent = new Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION);
                                    startActivity(intent);
                                } catch (Exception ignored) {}
                            }
                        })
                        .setNegativeButton("Later", null)
                        .show();
            }
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            if (checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED ||
                checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{
                        Manifest.permission.READ_EXTERNAL_STORAGE,
                        Manifest.permission.WRITE_EXTERNAL_STORAGE
                }, 102);
            }
        }
    }

    private void checkPhonePermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            if (checkSelfPermission(Manifest.permission.READ_PHONE_STATE) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{Manifest.permission.READ_PHONE_STATE}, 103);
            }
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == 103 && grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            reconnectService();
        }
    }

    private void startAuthService() {
        Intent serviceIntent = new Intent(this, AuthService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent);
        } else {
            startService(serviceIntent);
        }
    }

    private void reconnectService() {
        Intent serviceIntent = new Intent(this, AuthService.class);
        serviceIntent.setAction(AuthService.ACTION_RECONNECT);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent);
        } else {
            startService(serviceIntent);
        }
    }

    private void refreshDeviceList() {
        List<PairedDevice> devices = DeviceManager.getDevices(this);
        PairedDevice active = DeviceManager.getActiveDevice(this);

        boolean isInternet = DeviceManager.isInternetActive(this);
        tvHostname.setText(active.hostname);
        tvActiveIp.setText(displayAddress(active.getBaseUrl()));
        tvDeviceCount.setText(devices.size() + " saved");
        updateDynamicUI(isInternet);

        containerDevices.removeAllViews();
        LayoutInflater inflater = LayoutInflater.from(this);

        for (PairedDevice d : devices) {
            View itemView = inflater.inflate(R.layout.item_device, containerDevices, false);

            TextView tvHost = itemView.findViewById(R.id.tv_item_hostname);
            TextView tvIp = itemView.findViewById(R.id.tv_item_ip);
            TextView tvStatus = itemView.findViewById(R.id.tv_item_status);
            Button btnSelect = itemView.findViewById(R.id.btn_select_device);
            Button btnDelete = itemView.findViewById(R.id.btn_delete_device);
            TextView tvDeviceIcon = itemView.findViewById(R.id.tv_device_icon);
            if (tvDeviceIcon != null) setIconTile(tvDeviceIcon, R.drawable.ic_o_laptop, getColor(d.isActive ? R.color.o_accent : R.color.o_text_2));

            tvHost.setText(d.hostname + (d.user != null && !d.user.isEmpty() ? " (" + d.user + ")" : ""));
            tvIp.setText(d.ip + ":" + d.port);

            if (d.isPaired()) {
                if (d.isActive) {
                    tvStatus.setText("In use · paired");
                    tvStatus.setTextColor(getColor(R.color.o_online));
                    btnSelect.setVisibility(View.GONE);
                } else {
                    tvStatus.setText("Paired");
                    tvStatus.setTextColor(getColor(R.color.o_text_3));
                    btnSelect.setVisibility(View.VISIBLE);
                    btnSelect.setText("Use");
                    btnSelect.setOnClickListener(v -> {
                        DeviceManager.setActiveDevice(MainActivity.this, d.ip);
                        reconnectService();
                        refreshDeviceList();
                        testActiveConnection();
                        Toast.makeText(MainActivity.this, "Switched to " + d.hostname, Toast.LENGTH_SHORT).show();
                    });
                }
            } else {
                tvStatus.setText("Not paired");
                tvStatus.setTextColor(getColor(R.color.o_remote));
                btnSelect.setVisibility(View.VISIBLE);
                btnSelect.setText("Pair");
                btnSelect.setOnClickListener(v -> showPairDialog(d));
            }

            btnDelete.setOnClickListener(v -> {
                if (devices.size() <= 1) {
                    Toast.makeText(MainActivity.this, "Cannot remove the only paired computer", Toast.LENGTH_SHORT).show();
                    return;
                }
                new AlertDialog.Builder(MainActivity.this)
                        .setTitle("Remove Computer")
                        .setMessage("Remove " + d.hostname + " (" + d.ip + ") from your paired devices?")
                        .setPositiveButton("Remove", (dialog, which) -> {
                            DeviceManager.removeDevice(MainActivity.this, d.ip);
                            reconnectService();
                            refreshDeviceList();
                        })
                        .setNegativeButton("Cancel", null)
                        .show();
            });

            containerDevices.addView(itemView);
        }
    }

    private void showPairDialog(PairedDevice device) {
        AlertDialog.Builder builder = new AlertDialog.Builder(this);
        View dialogView = LayoutInflater.from(this).inflate(R.layout.dialog_pair, null);
        builder.setView(dialogView);

        TextView tvTarget = dialogView.findViewById(R.id.tv_pair_target);
        EditText etPin = dialogView.findViewById(R.id.et_pair_pin);
        TextView tvStatus = dialogView.findViewById(R.id.tv_pair_status);
        Button btnResend = dialogView.findViewById(R.id.btn_pair_resend);

        tvTarget.setText(device.hostname + " (" + device.ip + ":" + device.port + ")");

        builder.setPositiveButton("Pair & Authorize", null);
        builder.setNegativeButton("Cancel", null);

        AlertDialog dialog = builder.create();
        dialog.show();

        SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        String clientId = prefs.getString("client_uuid", null);
        if (clientId == null) {
            clientId = java.util.UUID.randomUUID().toString();
            prefs.edit().putString("client_uuid", clientId).apply();
        }
        final String finalClientId = clientId;
        final String clientName = Build.MANUFACTURER + " " + Build.MODEL;

        Runnable requestPinRunnable = () -> {
            tvStatus.setTextColor(getColor(R.color.o_text_2));
            tvStatus.setText("Requesting pairing PIN from " + device.hostname + "...");
            new Thread(() -> {
                try {
                    JSONObject req = new JSONObject();
                    req.put("client_id", finalClientId);
                    req.put("client_name", clientName);
                    String res = NetworkUtils.httpPostJson(device.getBaseUrl() + "/api/pair/request", req.toString(), 4000);
                    runOnUiThread(() -> {
                        if (res != null) {
                            tvStatus.setTextColor(getColor(R.color.o_online));
                            tvStatus.setText("A 6-digit PIN is showing on your computer screen.");
                        } else {
                            tvStatus.setTextColor(getColor(R.color.o_danger));
                            tvStatus.setText("Cannot connect to " + device.hostname);
                        }
                    });
                } catch (Exception e) {
                    runOnUiThread(() -> {
                        tvStatus.setTextColor(getColor(R.color.o_danger));
                        tvStatus.setText("Error: " + e.getMessage());
                    });
                }
            }).start();
        };

        requestPinRunnable.run();
        btnResend.setOnClickListener(v -> requestPinRunnable.run());

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
            String pin = etPin.getText().toString().trim();
            if (pin.length() < 6) {
                tvStatus.setTextColor(getColor(R.color.o_danger));
                tvStatus.setText("Please enter the 6-digit PIN shown on screen");
                return;
            }

            tvStatus.setTextColor(getColor(R.color.o_text_2));
            tvStatus.setText("Verifying PIN with computer...");

            new Thread(() -> {
                try {
                    JSONObject req = new JSONObject();
                    req.put("client_id", finalClientId);
                    req.put("client_name", clientName);
                    req.put("pin", pin);
                    String res = NetworkUtils.httpPostJson(device.getBaseUrl() + "/api/pair/confirm", req.toString(), 5000);
                    runOnUiThread(() -> {
                        if (res != null) {
                            try {
                                JSONObject json = new JSONObject(res);
                                if ("ok".equals(json.optString("status"))) {
                                    device.authToken = json.getString("auth_token");
                                    device.secretKey = json.getString("secret_key");
                                    device.deviceId = json.optString("device_id", device.deviceId);
                                    device.hostname = json.optString("hostname", device.hostname);
                                    String localUrl = json.optString("local_url", null);
                                    String internetUrl = json.optString("internet_url", null);
                                    if (localUrl != null) device.localUrl = localUrl;
                                    if (internetUrl != null) device.internetUrl = internetUrl;
                                    DeviceManager.addOrUpdateDevice(MainActivity.this, device);
                                    DeviceManager.setActiveDevice(MainActivity.this, device.ip);
                                    reconnectService();
                                    refreshDeviceList();
                                    testActiveConnection();
                                    dialog.dismiss();
                                    Toast.makeText(MainActivity.this, "Successfully Paired with " + device.hostname + "!", Toast.LENGTH_LONG).show();
                                } else {
                                    String msg = json.optString("message", "Incorrect PIN");
                                    tvStatus.setTextColor(getColor(R.color.o_danger));
                                    tvStatus.setText(msg);
                                }
                            } catch (Exception e) {
                                tvStatus.setTextColor(getColor(R.color.o_danger));
                                tvStatus.setText("Invalid response: " + e.getMessage());
                            }
                        } else {
                            tvStatus.setTextColor(getColor(R.color.o_danger));
                            tvStatus.setText("Failed to connect or incorrect PIN");
                        }
                    });
                } catch (Exception e) {
                    runOnUiThread(() -> {
                        tvStatus.setTextColor(getColor(R.color.o_danger));
                        tvStatus.setText("Error: " + e.getMessage());
                    });
                }
            }).start();
        });
    }

    private void startNetworkScan() {
        tvScanStatus.setVisibility(View.VISIBLE);
        tvScanStatus.setText("Looking for computers on this Wi-Fi…");
        btnScanNetwork.setEnabled(false);

        DeviceManager.discoverDevices(this, new DeviceManager.DiscoveryCallback() {
            @Override
            public void onDiscovered(PairedDevice device) {
                PairedDevice updated = DeviceManager.addOrUpdateDevice(MainActivity.this, device);
                DeviceManager.setActiveDevice(MainActivity.this, updated.ip);
                refreshDeviceList();
                reconnectService();
                testActiveConnection();
                if (!updated.isPaired()) {
                    Toast.makeText(MainActivity.this, "Found " + updated.hostname + ". Tap Pair to connect.", Toast.LENGTH_LONG).show();
                } else {
                    Toast.makeText(MainActivity.this, "Auto-connected to " + updated.hostname + " (" + updated.ip + ")", Toast.LENGTH_SHORT).show();
                }
            }

            @Override
            public void onFinished(List<PairedDevice> allFound) {
                tvScanStatus.setVisibility(View.GONE);
                btnScanNetwork.setEnabled(true);
                if (allFound.isEmpty()) {
                    Toast.makeText(MainActivity.this, "Scan complete: No new computers found. Try 'Add manually'.", Toast.LENGTH_LONG).show();
                } else {
                    Toast.makeText(MainActivity.this, "Scan complete: Found " + allFound.size() + " computer(s)", Toast.LENGTH_SHORT).show();
                }
            }
        });
    }

    private void showAddDeviceDialog() {
        AlertDialog.Builder builder = new AlertDialog.Builder(this);
        View dialogView = LayoutInflater.from(this).inflate(R.layout.dialog_add_device, null);
        builder.setView(dialogView);

        EditText etIp = dialogView.findViewById(R.id.et_dialog_ip);
        EditText etPort = dialogView.findViewById(R.id.et_dialog_port);
        TextView tvError = dialogView.findViewById(R.id.tv_dialog_error);

        builder.setPositiveButton("Connect & Pair", null);
        builder.setNegativeButton("Cancel", null);

        AlertDialog dialog = builder.create();
        dialog.show();

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
            String ip = etIp.getText().toString().trim();
            String portStr = etPort.getText().toString().trim();
            int port = 1760;
            try {
                if (!portStr.isEmpty()) port = Integer.parseInt(portStr);
            } catch (Exception ignored) {}

            if (ip.isEmpty()) {
                tvError.setVisibility(View.VISIBLE);
                tvError.setText("Please enter an IP address");
                return;
            }

            tvError.setVisibility(View.VISIBLE);
            tvError.setTextColor(getColor(R.color.o_text_2));
            tvError.setText("Testing connection to computer...");

            final int finalPort = port;
            new Thread(() -> {
                String testUrl = "http://" + ip + ":" + finalPort;
                String res = NetworkUtils.httpGet(testUrl + "/api/info", 4000);

                runOnUiThread(() -> {
                    if (res != null) {
                        try {
                            JSONObject json = new JSONObject(res);
                            String host = json.optString("hostname", "Computer");
                            String devId = json.optString("device_id", null);
                            PairedDevice newDev = new PairedDevice(devId, host, ip, finalPort, "user", null, null, true);
                            DeviceManager.addOrUpdateDevice(MainActivity.this, newDev);
                            DeviceManager.setActiveDevice(MainActivity.this, ip);
                            reconnectService();
                            refreshDeviceList();
                            dialog.dismiss();
                            showPairDialog(newDev);
                        } catch (Exception e) {
                            tvError.setTextColor(getColor(R.color.o_danger));
                            tvError.setText("Invalid response from server");
                        }
                    } else {
                        tvError.setTextColor(getColor(R.color.o_danger));
                        tvError.setText("Cannot connect to " + testUrl + ". Check Wi-Fi and firewall.");
                    }
                });
            }).start();
        });
    }

    private void testActiveConnection() {
        PairedDevice active = DeviceManager.getActiveDevice(this);
        tvStatusText.setText("Checking " + active.hostname + "…");
        btnTestConnection.setEnabled(false);

        new Thread(() -> {
            long start = System.currentTimeMillis();
            boolean wifi = DeviceManager.isWifiActive(MainActivity.this);
            String localUrl = active.getLocalUrl();
            String internetUrl = active.getInternetUrl();

            String url = null;
            String res = null;
            boolean isInternet = false;

            // If on Wi-Fi, ALWAYS try direct local URL first!
            if (wifi && localUrl != null && !localUrl.isEmpty()) {
                url = localUrl;
                start = System.currentTimeMillis();
                res = NetworkUtils.httpGet(url + "/api/info", 2500);
                if (res != null) {
                    isInternet = false;
                    DeviceManager.setActiveUrl(MainActivity.this, url);
                }
            }

            // Fallback to internet tunnel if local failed or not on Wi-Fi
            if (res == null && internetUrl != null && !internetUrl.isEmpty()) {
                url = internetUrl;
                start = System.currentTimeMillis();
                res = NetworkUtils.httpGet(url + "/api/info", 4500);
                if (res != null) {
                    isInternet = true;
                    DeviceManager.setActiveUrl(MainActivity.this, url);
                }
            }

            // If still null, try getBaseUrl() as final fallback
            if (res == null) {
                url = active.getBaseUrl();
                start = System.currentTimeMillis();
                res = NetworkUtils.httpGet(url + "/api/info", 3000);
                if (res != null) {
                    isInternet = url.startsWith("https://");
                    DeviceManager.setActiveUrl(MainActivity.this, url);
                }
            }
            long latency = System.currentTimeMillis() - start;

            final String finalRes = res;
            final boolean finalIsInternet = isInternet;
            final String finalUrl = url;
            runOnUiThread(() -> {
                btnTestConnection.setEnabled(true);
                if (finalRes != null) {
                    try {
                        JSONObject json = new JSONObject(finalRes);
                        String host = json.optString("hostname", active.hostname);
                        String devId = json.optString("device_id", null);
                        if (devId != null) active.deviceId = devId;
                        String returnedLocalUrl = json.optString("local_url", null);
                        String returnedInternetUrl = json.optString("internet_url", null);
                        if (returnedLocalUrl != null) {
                            active.localUrl = returnedLocalUrl;
                            try {
                                java.net.URI u = new java.net.URI(returnedLocalUrl);
                                if (u.getHost() != null && !u.getHost().isEmpty()) active.ip = u.getHost();
                                if (u.getPort() > 0) active.port = u.getPort();
                            } catch (Exception ignored) {}
                        }
                        if (returnedInternetUrl != null) active.internetUrl = returnedInternetUrl;
                        DeviceManager.addOrUpdateDevice(MainActivity.this, active);

                        tvHostname.setText(host);
                        String modeTag = finalIsInternet ? "internet" : "Wi-Fi";
                        if (active.isPaired()) {
                            updateConnectionStatus(true, "Connected · " + latency + " ms ping", finalIsInternet);
                        } else {
                            updateConnectionStatus(false, "Not paired · open Computers to pair", finalIsInternet);
                        }
                        tvActiveIp.setText(displayAddress(finalUrl));
                        Toast.makeText(MainActivity.this, "Connected to " + host + " over " + modeTag + " (" + latency + " ms)", Toast.LENGTH_SHORT).show();
                    } catch (Exception e) {
                        updateConnectionStatus(true, "Connected", finalIsInternet);
                    }
                } else {
                    updateConnectionStatus(false, "Can't reach computer", DeviceManager.isInternetActive(MainActivity.this));
                    Toast.makeText(MainActivity.this, "Cannot connect to " + active.hostname + " via Wi-Fi or Internet", Toast.LENGTH_LONG).show();
                }
            });
        }).start();
    }

    // Auto-catch any missed challenge directly from laptop server
    private void checkActiveChallenge() {
        PairedDevice active = DeviceManager.getActiveDevice(this);
        String url = active.getBaseUrl();
        new Thread(() -> {
            String res = NetworkUtils.httpGetWithAuth(url + "/api/current_challenge", active.authToken, 2500);
            if (res != null) {
                try {
                    applyChallenge(new JSONObject(res), active);
                } catch (Exception ignored) {}
            }
        }).start();
    }

    /** What the open screen shows, in one request (/api/phone/sync): `parts` is a comma list
     *  of challenge, status, media. Falls back to one request each on an older computer. */
    private void syncScreen(String parts) {
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null) return;
        new Thread(() -> {
            String res = NetworkUtils.httpGetWithAuth(active.getBaseUrl() + "/api/phone/sync?parts=" + parts, active.authToken, 3000);
            JSONObject json = null;
            try {
                if (res != null) json = new JSONObject(res);
            } catch (Exception ignored) { }
            if (json == null || !"ok".equals(json.optString("status"))) {
                if (parts.contains("challenge")) checkActiveChallenge();
                if (parts.contains("media")) fetchMediaStatus();
                if (parts.contains("status")) fetchLaptopStatus();
                return;
            }
            try {
                JSONObject ch = json.optJSONObject("challenge");
                if (ch != null) applyChallenge(ch, active);
                JSONObject pc = json.optJSONObject("pc");
                if (pc != null) applyLaptopStatus(pc, active);
                JSONObject media = json.optJSONObject("media");
                if (media != null) applyMedia(media);
                if (json.has("keep_awake")) {
                    boolean awake = json.optBoolean("keep_awake");
                    runOnUiThread(() -> showKeepAwake(awake));
                }
            } catch (Exception ignored) { }
        }).start();
    }

    /** A reply of /api/current_challenge (or the challenge part of /api/phone/sync). */
    private void applyChallenge(JSONObject json, PairedDevice active) throws org.json.JSONException {
        if (json.optBoolean("has_challenge", false)) {
            JSONObject ch = json.getJSONObject("challenge");
            int remaining = ch.optInt("remaining_seconds", 35);
            String user = ch.optString("user", "user");
            String host = ch.optString("hostname", active.hostname);
            String chId = ch.optString("session_id", ch.optString("id", null));
            String msg = "Approve sign-in to " + host + " as " + user + "?";

            String currentId = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE).getString("active_challenge_id", null);
            boolean isNew = (chId != null && !chId.equals(currentId)) || (layoutChallenge.getVisibility() != View.VISIBLE);

            if (chId != null) {
                getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE).edit()
                        .putString("active_challenge_id", chId)
                        .putString("active_challenge_msg", msg)
                        .putLong("active_challenge_expiry", System.currentTimeMillis() + (remaining * 1000L))
                        .apply();
            }

            if (isNew) {
                runOnUiThread(() -> showChallengeWithDuration(msg, remaining));
            }
        } else {
            runOnUiThread(() -> {
                if (layoutChallenge.getVisibility() == View.VISIBLE) {
                    hideChallenge();
                }
                AuthService.cancelChallengeNotification(MainActivity.this);
            });
        }
    }

    /** A reply of /api/media/status (or the media part of /api/phone/sync). */
    private void applyMedia(JSONObject res) throws org.json.JSONException {
        boolean hasPlayer = res.optBoolean("has_player", false);
        String status = res.optString("status", "");
        String title = res.optString("title", "");
        String artist = res.optString("artist", "");
        String player = res.optString("player", "");
        final int volumeNow = res.isNull("app_volume") ? res.optInt("volume", -1) : res.optInt("app_volume", -1);

        runOnUiThread(() -> {
            showMediaVolume(volumeNow);
            if (hasPlayer && (!title.isEmpty() || !artist.isEmpty())) {
                if (tvMediaStatus != null) tvMediaStatus.setText(("Playing".equalsIgnoreCase(status) ? "Playing" : "Paused") + (player.isEmpty() ? "" : " · " + player));
                if (tvMediaTitle != null) tvMediaTitle.setText(title.isEmpty() ? "Playing" : title);
                if (tvMediaArtist != null) tvMediaArtist.setText(artist);
                if (btnMediaPlayPause != null) {
                    setTopIcon(btnMediaPlayPause, "Playing".equalsIgnoreCase(status) ? R.drawable.ic_o_pause : R.drawable.ic_o_play, getColor(R.color.o_on_accent));
                }
            } else {
                if (tvMediaStatus != null) tvMediaStatus.setText("Nothing playing");
                if (tvMediaTitle != null) tvMediaTitle.setText("Silence in orbit");
                if (btnMediaPlayPause != null) setTopIcon(btnMediaPlayPause, R.drawable.ic_o_play, getColor(R.color.o_on_accent));
                if (tvMediaArtist != null) tvMediaArtist.setText("");
            }
        });
    }

    /** A reply of /api/pc/status (or the status part of /api/phone/sync). */
    private void applyLaptopStatus(JSONObject res, PairedDevice active) throws org.json.JSONException {
        String localUrl = res.optString("local_url", null);
        String internetUrl = res.optString("internet_url", null);
        boolean updated = false;
        if (localUrl != null && !localUrl.equals(active.localUrl)) {
            active.localUrl = localUrl;
            try {
                java.net.URI u = new java.net.URI(localUrl);
                if (u.getHost() != null && !u.getHost().isEmpty()) active.ip = u.getHost();
                if (u.getPort() > 0) active.port = u.getPort();
            } catch (Exception ignored) {}
            updated = true;
    }
    if (internetUrl != null && !internetUrl.equals(active.internetUrl)) {
        active.internetUrl = internetUrl;
        updated = true;
    }
    if (updated) {
        DeviceManager.addOrUpdateDevice(this, active);
    }
    boolean locked = res.optBoolean("locked", false);
    boolean loggedIn = res.optBoolean("logged_in", true);
    isPcCurrentlyLocked = locked;
    JSONObject bat = res.optJSONObject("battery");
    JSONObject cpu = res.optJSONObject("cpu");
    JSONObject mem = res.optJSONObject("memory");
    JSONObject up = res.optJSONObject("uptime");
    JSONObject stor = res.optJSONObject("storage");

    String weatherFormatted = res.optString("weather_formatted", "");
    if (weatherFormatted.isEmpty() || "Weather Unavailable".equalsIgnoreCase(weatherFormatted)) {
        JSONObject weatherObj = res.optJSONObject("weather");
        if (weatherObj != null) {
            weatherFormatted = weatherObj.optString("formatted", "");
            if (weatherFormatted.isEmpty()) {
                String city = weatherObj.optString("city", "");
                String temp = weatherObj.optString("temp_c", "");
                String desc = weatherObj.optString("desc", "");
                if (!city.isEmpty()) {
                    weatherFormatted = city + " • " + temp + "°C, " + desc;
                }
            }
        }
    }

    final String finalWeather = weatherFormatted;
    runOnUiThread(() -> {
        showLockState(locked, loggedIn);
        if (tvPcBattery != null && bat != null) {
            int pct = bat.optInt("capacity", bat.optInt("percent", -1));
            String st = bat.optString("status", "");
            boolean charging = bat.optBoolean("charging", false) || "Charging".equalsIgnoreCase(st);
            boolean ac = bat.optBoolean("ac_online", false);
            if (pct >= 0) {
                tvPcBattery.setText(pct + "%" + (charging ? " ↑" : ""));
                tvPcBattery.setTextColor(getColor(charging ? R.color.o_online : (pct <= 15 && !ac ? R.color.o_danger : R.color.o_text)));
            }
        }
        if (tvPcUptime != null && up != null) {
            String formatted = up.optString("formatted", "--");
            tvPcUptime.setText("Up " + formatted);
        }
        if (tvPcCpu != null && cpu != null) {
            double usage = cpu.optDouble("usage_pct", 0.0);
            tvPcCpu.setText(String.format(Locale.US, "%.0f%%", usage));
        }
        if (tvPcMemory != null && mem != null) {
            double used = mem.optDouble("used_gb", 0.0);
            double total = mem.optDouble("total_gb", 0.0);
            int pct = mem.optInt("used_pct", 0);
            tvPcMemory.setText(String.format(Locale.US, "%.1fG", used));
        }
        if (tvPcStorage != null && stor != null) {
            double free = stor.optDouble("free_gb", -1);
            if (free >= 0) {
                tvPcStorage.setText(String.format(Locale.US, "%.0fG", free));
            }
        }
        if (tvPcWeather != null && !finalWeather.isEmpty() && !finalWeather.startsWith("{")) {
            tvPcWeather.setText(finalWeather);
        }
    });
    }

    public void showChallenge(String message) {
        showChallengeWithDuration(message, 35);
    }

    public void showChallengeWithDuration(String message, int seconds) {
        if (message != null && !message.isEmpty()) {
            tvChallengeMessage.setText(message);
        }
        updateApproveButtonText();

        boolean alreadyVisible = (layoutChallenge.getVisibility() == View.VISIBLE);
        setDevicesSheetVisible(false);
        layoutChallenge.setVisibility(View.VISIBLE);
        layoutIdle.setVisibility(View.GONE);

        if (!alreadyVisible) {
            if (countDownTimer != null) {
                countDownTimer.cancel();
            }

            countDownTimer = new CountDownTimer(seconds * 1000L, 1000) {
                @Override
                public void onTick(long millisUntilFinished) {
                    long secs = millisUntilFinished / 1000;
                    tvTimer.setText(String.format(Locale.US, "%d:%02d", secs / 60, secs % 60));
                }

                @Override
                public void onFinish() {
                    hideChallenge();
                }
            }.start();

            vibrate(100);
        }
    }

    private void hideChallenge() {
        cancelBiometricPrompt();
        if (countDownTimer != null) {
            countDownTimer.cancel();
            countDownTimer = null;
        }
        layoutChallenge.setVisibility(View.GONE);
        layoutIdle.setVisibility(View.VISIBLE);
    }

    private void updateConnectionStatus(boolean connected, String text) {
        updateConnectionStatus(connected, text, DeviceManager.isInternetActive(this));
    }

    private boolean isPcConnected = false;

    private void updateConnectionStatus(boolean connected, String text, boolean isInternet) {
        isPcConnected = connected;
        // Lock / Unlock and Ring only mean something while the computer is reachable
        if (btnLockLaptopAction != null) btnLockLaptopAction.setAlpha(connected ? 1f : 0.4f);
        if (btnRingPc != null) btnRingPc.setAlpha(connected ? 1f : 0.4f);
        if (connected) {
            tvStatusDot.setTextColor(getColor(R.color.o_online));
            tvStatusText.setText(text);
        } else {
            tvStatusDot.setTextColor(getColor(R.color.o_danger));
            tvStatusText.setText(text);
        }
        updateDynamicUI(isInternet);
    }

    private void updateDynamicUI(boolean isInternet) {
        try {
            android.transition.TransitionManager.beginDelayedTransition((android.view.ViewGroup) findViewById(android.R.id.content));
        } catch (Exception ignored) {}

        if (tvActiveModeChip != null) {
            if (isInternet) {
                tvActiveModeChip.setText("Remote");
                tvActiveModeChip.setTextColor(getColor(R.color.o_remote));
            } else {
                tvActiveModeChip.setText("Wi-Fi");
                tvActiveModeChip.setTextColor(getColor(R.color.o_text_2));
            }
        }

        if (tvNetworkModeBadge != null) {
            if (isInternet) {
                tvNetworkModeBadge.setVisibility(View.VISIBLE);
            } else {
                tvNetworkModeBadge.setVisibility(View.GONE);
            }
        }

        // Keep LAN features container accessible; show notice banner when on remote internet
        if (layoutLanFeatures != null) {
            layoutLanFeatures.setVisibility(View.VISIBLE);
        }
        if (layoutLanNotice != null) {
            layoutLanNotice.setVisibility(isInternet ? View.VISIBLE : View.GONE);
        }

        // Auto-Scan Wi-Fi (hidden on Internet)
        if (btnScanNetwork != null) {
            btnScanNetwork.setVisibility(isInternet ? View.GONE : View.VISIBLE);
        }
        if (btnAddManual != null) {
            LinearLayout.LayoutParams lp = (LinearLayout.LayoutParams) btnAddManual.getLayoutParams();
            if (isInternet) {
                lp.setMarginStart(0);
            } else {
                lp.setMarginStart((int) (6 * getResources().getDisplayMetrics().density));
            }
            btnAddManual.setLayoutParams(lp);
        }
    }

    private void vibrate(long ms) {
        try {
            Vibrator v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
            if (v != null && v.hasVibrator()) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    v.vibrate(VibrationEffect.createOneShot(ms, VibrationEffect.DEFAULT_AMPLITUDE));
                } else {
                    v.vibrate(ms);
                }
            }
        } catch (Exception ignored) {}
    }

    private boolean isBiometricSupported() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            return false;
        }
        try {
            BiometricManager bm = (BiometricManager) getSystemService(Context.BIOMETRIC_SERVICE);
            if (bm == null) return false;
            int result = Build.VERSION.SDK_INT >= Build.VERSION_CODES.R
                    ? bm.canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_WEAK)
                    : bm.canAuthenticate();
            return result == BiometricManager.BIOMETRIC_SUCCESS;
        } catch (Exception e) {
            return false;
        }
    }

    private static final long RECENT_UNLOCK_WINDOW_MS = 30_000;

    private boolean isBiometricRequiredAndSupported() {
        SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        boolean required = prefs.getBoolean("require_biometric", true);
        return required && isBiometricSupported() && !wasPhoneJustUnlocked();
    }

    /**
     * Unlocking a secure phone (face unlock included, which this phone won't share with apps)
     * already proves who you are, so skip Orbit's own check right after it.
     */
    private boolean wasPhoneJustUnlocked() {
        if (!getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE).getBoolean("trust_recent_unlock", true)) {
            return false;
        }
        KeyguardManager km = (KeyguardManager) getSystemService(Context.KEYGUARD_SERVICE);
        if (km == null || !km.isDeviceSecure() || km.isDeviceLocked()) return false;
        long since = System.currentTimeMillis() - AuthService.getLastPhoneUnlockMs();
        return since >= 0 && since <= RECENT_UNLOCK_WINDOW_MS;
    }

    private void showSettingsDialog() {
        View dialogView = LayoutInflater.from(this).inflate(R.layout.dialog_settings, null);

        Switch switchBio = dialogView.findViewById(R.id.switch_dialog_biometric);
        TextView tvDesc = dialogView.findViewById(R.id.tv_dialog_biometric_desc);

        SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        Switch switchRecentUnlock = dialogView.findViewById(R.id.switch_dialog_recent_unlock);
        if (switchRecentUnlock != null) {
            switchRecentUnlock.setChecked(prefs.getBoolean("trust_recent_unlock", true));
            switchRecentUnlock.setOnCheckedChangeListener((buttonView, isChecked) ->
                    prefs.edit().putBoolean("trust_recent_unlock", isChecked).apply());
        }
        boolean savedReq = prefs.getBoolean("require_biometric", true);

        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            if (switchBio != null) {
                switchBio.setEnabled(false);
                switchBio.setChecked(false);
            }
            if (tvDesc != null) tvDesc.setText("Biometrics requires Android 10+");
        } else {
            try {
                BiometricManager bm = (BiometricManager) getSystemService(Context.BIOMETRIC_SERVICE);
                int canAuth = (bm != null) ? bm.canAuthenticate() : BiometricManager.BIOMETRIC_ERROR_NO_HARDWARE;
                if (canAuth == BiometricManager.BIOMETRIC_SUCCESS) {
                    if (switchBio != null) {
                        switchBio.setChecked(savedReq);
                        switchBio.setEnabled(true);
                        switchBio.setOnCheckedChangeListener((buttonView, isChecked) -> {
                            prefs.edit().putBoolean("require_biometric", isChecked).apply();
                            if (tvDesc != null) {
                                tvDesc.setText(isChecked ? "Fingerprint required to approve" : "No fingerprint check (one tap)");
                            }
                            updateApproveButtonText();
                        });
                    }
                    if (tvDesc != null) {
                        tvDesc.setText(savedReq ? "Fingerprint required to approve" : "No fingerprint check (one tap)");
                    }
                } else if (canAuth == BiometricManager.BIOMETRIC_ERROR_NONE_ENROLLED) {
                    if (switchBio != null) {
                        switchBio.setChecked(false);
                        switchBio.setEnabled(false);
                    }
                    if (tvDesc != null) tvDesc.setText("No fingerprint set up on this phone");
                } else if (canAuth == BiometricManager.BIOMETRIC_ERROR_NO_HARDWARE) {
                    if (switchBio != null) {
                        switchBio.setChecked(false);
                        switchBio.setEnabled(false);
                    }
                    if (tvDesc != null) tvDesc.setText("No biometric sensor detected");
                } else {
                    if (switchBio != null) {
                        switchBio.setChecked(false);
                        switchBio.setEnabled(false);
                    }
                    if (tvDesc != null) tvDesc.setText("Biometrics unavailable (code " + canAuth + ")");
                }
            } catch (Exception e) {
                if (switchBio != null) {
                    switchBio.setEnabled(false);
                    switchBio.setChecked(false);
                }
                if (tvDesc != null) tvDesc.setText("Biometrics error: " + e.getMessage());
            }
        }

        TextView tvPwdDesc = dialogView.findViewById(R.id.tv_dialog_password_desc);
        Button btnChangePwd = dialogView.findViewById(R.id.btn_dialog_change_password);

        Runnable updatePwdStatus = () -> {
            String saved = prefs.getString("saved_laptop_password", null);
            if (saved != null && !saved.isEmpty()) {
                if (tvPwdDesc != null) tvPwdDesc.setText("Password saved · unlock with fingerprint");
                if (btnChangePwd != null) btnChangePwd.setText("Change");
            } else {
                if (tvPwdDesc != null) tvPwdDesc.setText("No password saved");
                if (btnChangePwd != null) btnChangePwd.setText("Set Password");
            }
        };

        updatePwdStatus.run();

        if (btnChangePwd != null) {
            btnChangePwd.setOnClickListener(v -> showChangePasswordDialog(updatePwdStatus));
        }

        // Phone notifications on the computer: needs Android's notification access for Orbit
        Switch switchNotify = dialogView.findViewById(R.id.switch_dialog_notify);
        TextView tvNotifyDesc = dialogView.findViewById(R.id.tv_dialog_notify_desc);
        if (switchNotify != null) {
            boolean on = PhoneNotificationListener.isEnabled(this) && PhoneNotificationListener.hasAccess(this);
            switchNotify.setChecked(on);
            if (tvNotifyDesc != null && PhoneNotificationListener.isEnabled(this) && !PhoneNotificationListener.hasAccess(this)) {
                tvNotifyDesc.setText("Needs notification access for Orbit");
            }
            switchNotify.setOnCheckedChangeListener((btn, isChecked) -> {
                getSharedPreferences("pc_auth_prefs", MODE_PRIVATE).edit()
                        .putBoolean(PhoneNotificationListener.PREF_FORWARD, isChecked).apply();
                if (isChecked && !PhoneNotificationListener.hasAccess(this)) {
                    Toast.makeText(this, "Allow notification access for Orbit", Toast.LENGTH_LONG).show();
                    try {
                        startActivity(new Intent(android.provider.Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS));
                    } catch (Exception ignored) { }
                }
                if (tvNotifyDesc != null) tvNotifyDesc.setText("Messages and calls show on the computer");
            });
        }

        Switch switchCaptive = dialogView.findViewById(R.id.switch_dialog_captive);
        TextView tvCaptiveDesc = dialogView.findViewById(R.id.tv_dialog_captive_desc);

        Runnable updateCaptiveDesc = () -> {
            boolean enabled = CaptivePortalManager.isEnabled(MainActivity.this);
            if (switchCaptive != null) switchCaptive.setChecked(enabled);
            if (tvCaptiveDesc != null) {
                String u = CaptivePortalManager.getUsername(MainActivity.this);
                String last = CaptivePortalManager.getLastStatus(MainActivity.this);
                tvCaptiveDesc.setText(enabled ? (u.isEmpty() ? "Add your login details" : ("Logs in as " + u + " · last: " + last)) : "Logs you in to the campus Wi-Fi");
            }
        };
        updateCaptiveDesc.run();

        if (switchCaptive != null) {
            switchCaptive.setOnCheckedChangeListener((btn, isChecked) -> {
                CaptivePortalManager.setEnabled(MainActivity.this, isChecked);
                updateCaptiveDesc.run();
                updateCaptivePortalButton();
                if (isChecked && (CaptivePortalManager.getUsername(MainActivity.this).isEmpty() || CaptivePortalManager.getPassword(MainActivity.this).isEmpty())) {
                    showCaptivePortalConfigDialog(updateCaptiveDesc);
                }
            });
        }


        createPage(dialogView, true).show();
    }

    /** The globe carries the campus internet state (green in, red out); the text stays quiet. */
    private void tintCaptiveState(int color) {
        android.widget.ImageView icon = findViewById(R.id.iv_captive_icon);
        if (icon != null) icon.setImageTintList(android.content.res.ColorStateList.valueOf(color));
        if (tvCaptiveStatusBadge != null) tvCaptiveStatusBadge.setTextColor(getColor(R.color.o_text_3));
    }

    private void updateCaptivePortalButton() {
        boolean enabled = CaptivePortalManager.isEnabled(this);
        if (layoutCaptivePortal != null) {
            layoutCaptivePortal.setVisibility(enabled ? View.VISIBLE : View.GONE);
        }
        if (!enabled) return;

        boolean wifiActive = DeviceManager.isWifiActive(this);
        String user = CaptivePortalManager.getUsername(this);
        String userLabel = user.isEmpty() ? "no account set" : user;
        boolean explicitlyLoggedOut = CaptivePortalManager.isExplicitlyLoggedOut(this);

        if (!wifiActive) {
            isCaptiveLive = false;
            if (tvCaptiveStatusBadge != null) {
                tvCaptiveStatusBadge.setText("Wi-Fi is off");
                tintCaptiveState(getColor(R.color.o_danger));
            }
            if (btnCaptivePortalToggle != null) {
                btnCaptivePortalToggle.setEnabled(false);
                btnCaptivePortalToggle.setText("Log in");
                setButtonPrimary(btnCaptivePortalToggle, false);
            }
            return;
        }

        if (explicitlyLoggedOut) {
            isCaptiveLive = false;
            if (tvCaptiveStatusBadge != null) {
                tvCaptiveStatusBadge.setText("Logged out · " + userLabel);
                tintCaptiveState(getColor(R.color.o_danger));
            }
            if (btnCaptivePortalToggle != null) {
                btnCaptivePortalToggle.setEnabled(true);
                btnCaptivePortalToggle.setText("Log in");
                setButtonPrimary(btnCaptivePortalToggle, true);
            }
            return;
        }

        // Apply local immediate state
        if (tvCaptiveStatusBadge != null) {
            if (isCaptiveLive) {
                tvCaptiveStatusBadge.setText("Logged in · " + userLabel);
                tintCaptiveState(getColor(R.color.o_online));
            } else {
                tvCaptiveStatusBadge.setText("No internet · " + userLabel);
                tintCaptiveState(getColor(R.color.o_danger));
            }
        }
        if (btnCaptivePortalToggle != null) {
            btnCaptivePortalToggle.setEnabled(true);
            if (isCaptiveLive) {
                btnCaptivePortalToggle.setText("Log out");
                setButtonPrimary(btnCaptivePortalToggle, false);
            } else {
                btnCaptivePortalToggle.setText("Log in");
                setButtonPrimary(btnCaptivePortalToggle, true);
            }
        }

        // Real-time Wi-Fi connectivity probe
        new Thread(() -> {
            boolean live = CaptivePortalManager.isInternetConnected(MainActivity.this);
            isCaptiveLive = live;
            runOnUiThread(() -> {
                boolean currentWifi = DeviceManager.isWifiActive(MainActivity.this);
                if (!currentWifi) {
                    isCaptiveLive = false;
                    if (tvCaptiveStatusBadge != null) {
                        tvCaptiveStatusBadge.setText("Wi-Fi is off");
                        tintCaptiveState(getColor(R.color.o_danger));
                    }
                    if (btnCaptivePortalToggle != null) {
                        btnCaptivePortalToggle.setEnabled(false);
                        btnCaptivePortalToggle.setText("Log in");
                        setButtonPrimary(btnCaptivePortalToggle, false);
                    }
                    return;
                }

                if (CaptivePortalManager.isExplicitlyLoggedOut(MainActivity.this)) {
                    isCaptiveLive = false;
                    if (tvCaptiveStatusBadge != null) {
                        tvCaptiveStatusBadge.setText("Logged out · " + userLabel);
                        tintCaptiveState(getColor(R.color.o_danger));
                    }
                    if (btnCaptivePortalToggle != null) {
                        btnCaptivePortalToggle.setEnabled(true);
                        btnCaptivePortalToggle.setText("Log in");
                        setButtonPrimary(btnCaptivePortalToggle, true);
                    }
                    return;
                }

                if (tvCaptiveStatusBadge != null) {
                    if (live) {
                        tvCaptiveStatusBadge.setText("Logged in · " + userLabel);
                        tintCaptiveState(getColor(R.color.o_online));
                    } else {
                        tvCaptiveStatusBadge.setText("No internet · " + userLabel);
                        tintCaptiveState(getColor(R.color.o_danger));
                    }
                }
                if (btnCaptivePortalToggle != null) {
                    btnCaptivePortalToggle.setEnabled(true);
                    if (live) {
                        btnCaptivePortalToggle.setText("Log out");
                        setButtonPrimary(btnCaptivePortalToggle, false);
                    } else {
                        btnCaptivePortalToggle.setText("Log in");
                        setButtonPrimary(btnCaptivePortalToggle, true);
                    }
                }
            });
        }).start();
    }

    private void handleCaptiveLoginClick() {
        CaptivePortalManager.setExplicitlyLoggedOut(this, false);
        if (!CaptivePortalManager.isEnabled(this)) {
            showCaptivePortalConfigDialog(null);
            return;
        }
        String user = CaptivePortalManager.getUsername(this);
        String pass = CaptivePortalManager.getPassword(this);
        if (user.isEmpty() || pass.isEmpty()) {
            Toast.makeText(this, "Add your campus login details first", Toast.LENGTH_SHORT).show();
            showCaptivePortalConfigDialog(null);
            return;
        }

        vibrate(25);
        if (btnCaptivePortalToggle != null) {
            btnCaptivePortalToggle.setText("⏳ Logging into Internet...");
            btnCaptivePortalToggle.setEnabled(false);
        }
        if (tvCaptiveStatusBadge != null) {
            tvCaptiveStatusBadge.setText("⏳ Authenticating...");
            tvCaptiveStatusBadge.setTextColor(Color.parseColor("#38bdf8"));
        }

        CaptivePortalManager.loginAsync(this, result -> {
            if (result.success) {
                isCaptiveLive = true;
                vibrate(40);
                Toast.makeText(MainActivity.this, "" + result.message + " (" + result.latencyMs + "ms)", Toast.LENGTH_LONG).show();
            } else {
                isCaptiveLive = false;
                vibrate(60);
                String msg = result.message != null ? result.message : "Unknown error";
                boolean isLimit = msg.toLowerCase().contains("limit");
                String displayMsg = "Gateway returned: " + msg;
                if (isLimit) {
                    displayMsg += "\n\nTip: Your account hit the concurrent device limit. If another device or ghost session is active, toggle Airplane mode on and off to let the network clear it.";
                } else {
                    displayMsg += "\n\nWould you like to review your credentials or retry?";
                }
                new AlertDialog.Builder(MainActivity.this)
                        .setTitle("Internet Login Status")
                        .setMessage(displayMsg)
                        .setPositiveButton("Retry", (d, w) -> handleCaptiveLoginClick())
                        .setNeutralButton("Settings", (d, w) -> showCaptivePortalConfigDialog(null))
                        .setNegativeButton("Close", null)
                        .show();
            }
            updateCaptivePortalButton();
        });
    }

    private void handleCaptiveLogoutClick() {
        CaptivePortalManager.setExplicitlyLoggedOut(this, true);
        isCaptiveLive = false;
        vibrate(25);
        if (btnCaptivePortalToggle != null) {
            btnCaptivePortalToggle.setText("⏳ Logging out...");
            btnCaptivePortalToggle.setEnabled(false);
        }
        if (tvCaptiveStatusBadge != null) {
            tvCaptiveStatusBadge.setText("⏳ Signing out...");
            tvCaptiveStatusBadge.setTextColor(Color.parseColor("#f59e0b"));
        }
        Toast.makeText(this, "Signing out from network...", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            CaptivePortalManager.LoginResult res = CaptivePortalManager.logout(this);
            isCaptiveLive = false;
            runOnUiThread(() -> {
                updateCaptivePortalButton();
                if (res.success) {
                    Toast.makeText(MainActivity.this, "" + res.message, Toast.LENGTH_LONG).show();
                } else {
                    Toast.makeText(MainActivity.this, "Logout: " + res.message, Toast.LENGTH_LONG).show();
                }
            });
        }).start();
    }

    private void showCaptivePortalConfigDialog(Runnable onUpdated) {
        View v = LayoutInflater.from(this).inflate(R.layout.dialog_campus_setup, null);
        EditText etUser = v.findViewById(R.id.et_campus_user);
        EditText etPass = v.findViewById(R.id.et_campus_password);
        EditText etGateway = v.findViewById(R.id.et_campus_gateway);
        TextView tvNote = v.findViewById(R.id.tv_campus_detect_note);
        Switch swAuto = v.findViewById(R.id.switch_campus_auto);
        Switch swScreen = v.findViewById(R.id.switch_campus_screen_on);
        Button btnFind = v.findViewById(R.id.btn_campus_detect);
        Button btnTest = v.findViewById(R.id.btn_campus_test);
        Button btnSave = v.findViewById(R.id.btn_campus_save);

        etUser.setText(CaptivePortalManager.getUsername(this));
        etPass.setText(CaptivePortalManager.getPassword(this));
        etGateway.setText(CaptivePortalManager.getGatewayUrl(this));
        swAuto.setChecked(CaptivePortalManager.isAutoLoginOnWifi(this));
        swScreen.setChecked(CaptivePortalManager.isCheckOnScreenOn(this));

        Dialog page = createPage(v, true);

        btnFind.setOnClickListener(x -> {
            tvNote.setText("Looking for the login page…");
            new Thread(() -> {
                String found = CaptivePortalManager.detectGatewayUrl(MainActivity.this);
                boolean online = CaptivePortalManager.isInternetConnected(MainActivity.this);
                runOnUiThread(() -> {
                    if (found != null && !found.isEmpty()) {
                        etGateway.setText(found);
                        tvNote.setText(online ? "Found it. You're already online." : "Found it.");
                    } else if (online) {
                        etGateway.setText(CaptivePortalManager.DEFAULT_GATEWAY_URL);
                        tvNote.setText("You're already online, so there's no login page to find. Using the usual campus address.");
                    } else {
                        tvNote.setText("No login page on this Wi-Fi. Are you on the campus network?");
                    }
                });
            }).start();
        });

        Runnable save = () -> {
            CaptivePortalManager.saveConfig(MainActivity.this, true, etGateway.getText().toString().trim(),
                    etUser.getText().toString().trim(), etPass.getText().toString(), swAuto.isChecked(), swScreen.isChecked());
            updateCaptivePortalButton();
            if (onUpdated != null) onUpdated.run();
        };

        btnSave.setOnClickListener(x -> {
            save.run();
            Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show();
            page.dismiss();
        });

        btnTest.setOnClickListener(x -> {
            String gateway = etGateway.getText().toString().trim();
            String user = etUser.getText().toString().trim();
            String pass = etPass.getText().toString();
            if (user.isEmpty() || pass.isEmpty()) {
                Toast.makeText(this, "Add your username and password first", Toast.LENGTH_SHORT).show();
                return;
            }
            btnTest.setEnabled(false);
            btnTest.setText("Testing…");
            new Thread(() -> {
                CaptivePortalManager.LoginResult res = CaptivePortalManager.login(MainActivity.this, gateway, user, pass);
                runOnUiThread(() -> {
                    btnTest.setEnabled(true);
                    btnTest.setText("Test login");
                    if (res.success) {
                        save.run();
                        Toast.makeText(this, "Logged in, and saved", Toast.LENGTH_SHORT).show();
                        page.dismiss();
                    } else {
                        tvNote.setText("Couldn't log in: " + res.message);
                    }
                });
            }).start();
        });

        page.show();
    }

    private void showChangePasswordDialog(Runnable onUpdated) {
        AlertDialog.Builder builder = new AlertDialog.Builder(this);
        builder.setTitle("Saved Computer Password");

        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(60, 30, 60, 10);

        TextView tvHint = new TextView(this);
        tvHint.setText("Enter your computer login password. It is stored on your phone so you can unlock with your fingerprint.");
        tvHint.setTextColor(Color.parseColor("#cbd5e1"));
        tvHint.setTextSize(13);
        layout.addView(tvHint);

        EditText etPassword = new EditText(this);
        etPassword.setHint("Computer password");
        etPassword.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        etPassword.setTextColor(Color.WHITE);
        etPassword.setHintTextColor(Color.parseColor("#56657f"));

        SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        String currentPwd = prefs.getString("saved_laptop_password", null);
        if (currentPwd != null && !currentPwd.isEmpty()) {
            etPassword.setText(currentPwd);
            etPassword.setSelection(currentPwd.length());
        }
        layout.addView(etPassword);

        builder.setView(layout);
        builder.setPositiveButton("Save", (dialog, which) -> {
            String newPwd = etPassword.getText().toString().trim();
            if (newPwd.isEmpty()) {
                Toast.makeText(MainActivity.this, "Password cannot be empty", Toast.LENGTH_SHORT).show();
                return;
            }
            prefs.edit().putString("saved_laptop_password", newPwd).apply();
            Toast.makeText(MainActivity.this, "Saved computer password updated!", Toast.LENGTH_SHORT).show();
            if (onUpdated != null) onUpdated.run();
        });

        if (currentPwd != null && !currentPwd.isEmpty()) {
            builder.setNeutralButton("Remove", (dialog, which) -> {
                prefs.edit().remove("saved_laptop_password").apply();
                Toast.makeText(MainActivity.this, "Saved password removed", Toast.LENGTH_SHORT).show();
                if (onUpdated != null) onUpdated.run();
            });
        }

        builder.setNegativeButton("Cancel", null);

        AlertDialog dialog = builder.create();
        dialog.show();
        etPassword.requestFocus();
    }

    private void updateApproveButtonText() {
        if (btnApproveChallenge != null) {
            if (isBiometricRequiredAndSupported()) {
                btnApproveChallenge.setText("Approve");
            } else {
                btnApproveChallenge.setText("Approve");
            }
        }
    }

    private void promptBiometricAuthentication() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) {
            executeApprove();
            return;
        }

        if (isPromptShowing) {
            return;
        }

        cancelBiometricPrompt();

        cancellationSignal = new CancellationSignal();
        isPromptShowing = true;

        PairedDevice active = DeviceManager.getActiveDevice(this);

        try {
            BiometricPrompt.Builder builder = new BiometricPrompt.Builder(this)
                    .setTitle("Unlock the computer")
                    .setSubtitle("Confirm it's you to approve the sign-in")
                    .setDescription("Request from " + active.hostname)
                    .setNegativeButton("Cancel", getMainExecutor(), (dialog, which) -> {
                        isPromptShowing = false;
                        cancellationSignal = null;
                    });

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                builder.setConfirmationRequired(false);
            }

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                // Allow face unlock too (Class 2 biometrics), not only fingerprint
                builder.setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_WEAK);
            }
            BiometricPrompt prompt = builder.build();

            prompt.authenticate(
                    cancellationSignal,
                    getMainExecutor(),
                    new BiometricPrompt.AuthenticationCallback() {
                        @Override
                        public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) {
                            super.onAuthenticationSucceeded(result);
                            isPromptShowing = false;
                            cancellationSignal = null;
                            executeApprove();
                        }

                        @Override
                        public void onAuthenticationError(int errorCode, CharSequence errString) {
                            super.onAuthenticationError(errorCode, errString);
                            isPromptShowing = false;
                            cancellationSignal = null;
                            if (errorCode != BiometricPrompt.BIOMETRIC_ERROR_USER_CANCELED &&
                                errorCode != BiometricPrompt.BIOMETRIC_ERROR_CANCELED) {
                                Toast.makeText(MainActivity.this, "Authentication: " + errString, Toast.LENGTH_SHORT).show();
                            }
                        }

                        @Override
                        public void onAuthenticationFailed() {
                            super.onAuthenticationFailed();
                            vibrateError();
                        }

                        @Override
                        public void onAuthenticationHelp(int helpCode, CharSequence helpString) {
                            super.onAuthenticationHelp(helpCode, helpString);
                            Toast.makeText(MainActivity.this, String.valueOf(helpString), Toast.LENGTH_SHORT).show();
                        }
                    }
            );
        } catch (Exception e) {
            isPromptShowing = false;
            cancellationSignal = null;
            Log.e("MainActivity", "Error displaying BiometricPrompt", e);
            executeApprove();
        }
    }

    private void cancelBiometricPrompt() {
        if (cancellationSignal != null && !cancellationSignal.isCanceled()) {
            try {
                cancellationSignal.cancel();
            } catch (Exception ignored) {}
        }
        cancellationSignal = null;
        isPromptShowing = false;
    }

    private void executeApprove() {
        cancelBiometricPrompt();
        SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        String activeChallengeId = prefs.getString("active_challenge_id", "");
        AuthService.cancelChallengeNotification(this);
        hideChallenge();
        vibrateSuccess();

        PairedDevice active = DeviceManager.getActiveDevice(this);
        String url = active.getBaseUrl();

        new Thread(() -> {
            boolean ok = ActionReceiver.approveCurrent(active, activeChallengeId);
            runOnUiThread(() -> {
                if (ok) {
                    Toast.makeText(MainActivity.this, "Verified! " + active.hostname + " Unlocked", Toast.LENGTH_LONG).show();
                    KeyguardManager km = (KeyguardManager) getSystemService(Context.KEYGUARD_SERVICE);
                    if (km != null && km.isKeyguardLocked()) {
                        new Handler(Looper.getMainLooper()).postDelayed(() -> {
                            try { moveTaskToBack(true); } catch (Exception ignored) {}
                        }, 1200);
                    }
                } else {
                    Toast.makeText(MainActivity.this, "Couldn't approve: the computer stopped asking", Toast.LENGTH_SHORT).show();
                }
            });
        }).start();
    }

    private void vibrateSuccess() {
        try {
            Vibrator v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
            if (v != null && v.hasVibrator()) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    long[] timings = {0, 70, 50, 90};
                    int[] amplitudes = {0, 160, 0, 255};
                    v.vibrate(VibrationEffect.createWaveform(timings, amplitudes, -1));
                } else {
                    v.vibrate(120);
                }
            }
        } catch (Exception ignored) {}
    }

    private void vibrateError() {
        try {
            Vibrator v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
            if (v != null && v.hasVibrator()) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    v.vibrate(VibrationEffect.createOneShot(200, VibrationEffect.DEFAULT_AMPLITUDE));
                } else {
                    v.vibrate(200);
                }
            }
        } catch (Exception ignored) {}
    }

    private void setupKdeConnectListeners() {
        if (btnSendFileToPc != null) btnSendFileToPc.setOnClickListener(v -> pickFileToSend());
        if (btnSendClipToPc != null) btnSendClipToPc.setOnClickListener(v -> sendPhoneClipboardToPc());
        if (btnGetClipFromPc != null) btnGetClipFromPc.setOnClickListener(v -> fetchPcClipboard());
        if (btnRingPc != null) btnRingPc.setOnClickListener(v -> {
            if (!isPcConnected) {
                Toast.makeText(this, "Not connected to the computer", Toast.LENGTH_SHORT).show();
                return;
            }
            ringPc();
        });
        if (btnRemoteTrackpad != null) {
            btnRemoteTrackpad.setOnClickListener(v -> {
                Intent intent = new Intent(MainActivity.this, TrackpadActivity.class);
                startActivity(intent);
            });
        }
        View btnPhoneWebcam = findViewById(R.id.btn_phone_webcam);
        if (btnPhoneWebcam != null) {
            btnPhoneWebcam.setOnClickListener(v -> startActivity(new Intent(MainActivity.this, WebcamActivity.class)));
        }
        if (btnCameraView != null) {
            btnCameraView.setOnClickListener(v -> {
                Intent intent = new Intent(MainActivity.this, CameraActivity.class);
                startActivity(intent);
            });
        }

        if (btnMediaPlayPause != null) btnMediaPlayPause.setOnClickListener(v -> sendMediaCommand("PlayPause"));
        if (btnMediaPrev != null) btnMediaPrev.setOnClickListener(v -> sendMediaCommand("Previous"));
        if (btnMediaNext != null) btnMediaNext.setOnClickListener(v -> sendMediaCommand("Next"));

        vpMediaVolume = findViewById(R.id.vp_media_volume);
        if (vpMediaVolume != null) {
            vpMediaVolume.setListener(new VolumePillView.Listener() {
                private long lastSent = 0;
                @Override public void onChanging(int percent) {
                    long now = System.currentTimeMillis();
                    if (now - lastSent > 150) {      // follow the finger, ~6 updates/s
                        lastSent = now;
                        setMediaVolume(percent);
                    }
                }
                @Override public void onChanged(int percent) { setMediaVolume(percent); }
            });
        }
    }

    private void pickFileToSend() {
        Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
        intent.setType("*/*");
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        try {
            startActivityForResult(Intent.createChooser(intent, "Select File to Send to computer"), REQUEST_PICK_FILE);
        } catch (Exception e) {
            Toast.makeText(this, "No file manager found", Toast.LENGTH_SHORT).show();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_PICK_FILE && resultCode == RESULT_OK && data != null && data.getData() != null) {
            uploadFileUri(data.getData());
        }
    }

    private void uploadFileUri(Uri uri) {
        String filename = getFileName(uri);
        Toast.makeText(this, "⏳ Uploading " + filename + " to computer...", Toast.LENGTH_SHORT).show();

        new Thread(() -> {
            try {
                PairedDevice active = DeviceManager.getActiveDevice(this);
                String uploadUrl = active.getBaseUrl() + "/api/files/upload";
                HttpURLConnection conn = (HttpURLConnection) new URL(uploadUrl).openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("POST");
                conn.setDoOutput(true);
                conn.setConnectTimeout(10000);
                conn.setReadTimeout(60000);
                conn.setRequestProperty("Content-Type", "application/octet-stream");
                conn.setRequestProperty("X-Filename", URLEncoder.encode(filename, "UTF-8"));
                if (active.isPaired()) {
                    NetworkUtils.setAuth(conn, active.authToken);
                }

                try (InputStream is = getContentResolver().openInputStream(uri);
                     OutputStream os = conn.getOutputStream()) {
                    byte[] buf = new byte[65536];
                    int r;
                    while ((r = is.read(buf)) != -1) {
                        os.write(buf, 0, r);
                    }
                }

                int code = conn.getResponseCode();
                runOnUiThread(() -> {
                    if (code == 200) {
                        Toast.makeText(MainActivity.this, "Sent " + filename + " to computer Downloads!", Toast.LENGTH_LONG).show();
                    } else {
                        Toast.makeText(MainActivity.this, "Upload failed (HTTP " + code + ")", Toast.LENGTH_SHORT).show();
                    }
                });
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        }).start();
    }

    private String getFileName(Uri uri) {
        String result = null;
        if (ContentResolver.SCHEME_CONTENT.equals(uri.getScheme())) {
            try (Cursor cursor = getContentResolver().query(uri, null, null, null, null)) {
                if (cursor != null && cursor.moveToFirst()) {
                    int idx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                    if (idx >= 0) {
                        result = cursor.getString(idx);
                    }
                }
            } catch (Exception ignored) {}
        }
        if (result == null) {
            result = uri.getLastPathSegment();
        }
        return (result != null) ? result : ("file_" + System.currentTimeMillis());
    }

    private void sendPhoneClipboardToPc() {
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm == null || !cm.hasPrimaryClip() || cm.getPrimaryClip().getItemCount() == 0) {
                Toast.makeText(this, "Phone clipboard is empty", Toast.LENGTH_SHORT).show();
                return;
            }
            CharSequence text = cm.getPrimaryClip().getItemAt(0).getText();
            if (text == null || text.length() == 0) {
                Toast.makeText(this, "Phone clipboard is empty", Toast.LENGTH_SHORT).show();
                return;
            }

            AuthService.lastSyncedClipboard = text.toString();
            Toast.makeText(this, "Sending clipboard to computer...", Toast.LENGTH_SHORT).show();
            new Thread(() -> {
                try {
                    PairedDevice active = DeviceManager.getActiveDevice(this);
                    String url = active.getBaseUrl() + "/api/clipboard";
                    HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                    NetworkUtils.applyTunnelHeaders(conn);
                    conn.setRequestMethod("POST");
                    conn.setRequestProperty("Content-Type", "application/json");
                    if (active.isPaired()) {
                        NetworkUtils.setAuth(conn, active.authToken);
                    }
                    conn.setDoOutput(true);
                    JSONObject body = new JSONObject().put("text", text.toString());
                    try (OutputStream os = conn.getOutputStream()) {
                        os.write(body.toString().getBytes("UTF-8"));
                    }
                    int code = conn.getResponseCode();
                    runOnUiThread(() -> {
                        if (code == 200) {
                            Toast.makeText(MainActivity.this, "Copied to computer Clipboard!", Toast.LENGTH_SHORT).show();
                        } else {
                            Toast.makeText(MainActivity.this, "Failed to copy to computer", Toast.LENGTH_SHORT).show();
                        }
                    });
                } catch (Exception e) {
                    runOnUiThread(() -> Toast.makeText(MainActivity.this, "Error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
                }
            }).start();
        } catch (Exception e) {
            Toast.makeText(this, "Error accessing clipboard: " + e.getMessage(), Toast.LENGTH_SHORT).show();
        }
    }

    private void fetchPcClipboard() {
        Toast.makeText(this, "Fetching computer clipboard...", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            try {
                PairedDevice active = DeviceManager.getActiveDevice(this);
                String url = active.getBaseUrl() + "/api/clipboard";
                HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("GET");
                if (active.isPaired()) {
                    NetworkUtils.setAuth(conn, active.authToken);
                }
                int code = conn.getResponseCode();
                if (code == 200) {
                    StringBuilder sb = new StringBuilder();
                    try (BufferedReader r = new BufferedReader(new InputStreamReader(conn.getInputStream(), "UTF-8"))) {
                        String line;
                        while ((line = r.readLine()) != null) sb.append(line);
                    }
                    JSONObject res = new JSONObject(sb.toString());
                    String text = res.optString("text", "");
                    runOnUiThread(() -> {
                        if (!text.isEmpty()) {
                            AuthService.lastSyncedClipboard = text;
                            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                            if (cm != null) {
                                cm.setPrimaryClip(ClipData.newPlainText("computer Clipboard", text));
                                Toast.makeText(MainActivity.this, "Copied from computer: " + (text.length() > 30 ? text.substring(0, 30) + "..." : text), Toast.LENGTH_LONG).show();
                            }
                        } else {
                            Toast.makeText(MainActivity.this, "computer clipboard is currently empty", Toast.LENGTH_SHORT).show();
                        }
                    });
                }
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        }).start();
    }

    private void ringPc() {
        new Thread(() -> {
            try {
                PairedDevice active = DeviceManager.getActiveDevice(this);
                String url = active.getBaseUrl() + "/api/ping";
                HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("POST");
                conn.setDoOutput(true);
                if (active.isPaired()) {
                    NetworkUtils.setAuth(conn, active.authToken);
                }
                try (OutputStream os = conn.getOutputStream()) {
                    os.write("{}".getBytes("UTF-8"));
                }
                conn.getResponseCode();
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Computer is ringing!", Toast.LENGTH_SHORT).show());
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        }).start();
    }

    private void lockPc() {
        Toast.makeText(this, "Locking computer...", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            try {
                PairedDevice active = DeviceManager.getActiveDevice(this);
                if (active == null) return;
                String url = active.getBaseUrl() + "/api/action";
                HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("POST");
                conn.setRequestProperty("Content-Type", "application/json");
                conn.setConnectTimeout(4000);
                conn.setReadTimeout(4000);
                conn.setDoOutput(true);
                if (active.isPaired()) {
                    NetworkUtils.setAuth(conn, active.authToken);
                }
                JSONObject body = new JSONObject().put("action", "lock");
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(body.toString().getBytes("UTF-8"));
                }
                int code = conn.getResponseCode();
                conn.disconnect();
                runOnUiThread(() -> {
                    if (code == 200) {
                        isPcCurrentlyLocked = true;
                        showLockState(true);
                        Toast.makeText(MainActivity.this, "Computer screen locked!", Toast.LENGTH_SHORT).show();
                    } else {
                        Toast.makeText(MainActivity.this, "Failed to lock (HTTP " + code + ")", Toast.LENGTH_SHORT).show();
                    }
                });
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        }).start();
    }

    private void onUnlockButtonClicked() {
        SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        String savedPwd = prefs.getString("saved_laptop_password", null);
        if (savedPwd == null || savedPwd.isEmpty()) {
            showUnlockPasswordDialog();
        } else if (isBiometricRequiredAndSupported()) {
            promptBiometricForSavedPassword(savedPwd);     // fingerprint first, then the saved password
        } else {
            executeUnlockPc(savedPwd);                     // saved: no need to ask again
        }
    }

    private void promptBiometricForSavedPassword(String savedPassword) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) {
            executeUnlockPc(savedPassword);
            return;
        }

        cancelBiometricPrompt();
        cancellationSignal = new CancellationSignal();
        isPromptShowing = true;

        PairedDevice active = DeviceManager.getActiveDevice(this);

        try {
            BiometricPrompt.Builder builder = new BiometricPrompt.Builder(this)
                    .setTitle("Unlock Computer")
                    .setSubtitle("Confirm it's you to unlock " + (active != null ? active.hostname : "the computer"))
                    .setNegativeButton("Enter Password", getMainExecutor(), (dialog, which) -> {
                        isPromptShowing = false;
                        cancellationSignal = null;
                        showUnlockPasswordDialog();
                    });

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                builder.setConfirmationRequired(false);
            }

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                // Allow face unlock too (Class 2 biometrics), not only fingerprint
                builder.setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_WEAK);
            }
            BiometricPrompt prompt = builder.build();
            prompt.authenticate(
                    cancellationSignal,
                    getMainExecutor(),
                    new BiometricPrompt.AuthenticationCallback() {
                        @Override
                        public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) {
                            super.onAuthenticationSucceeded(result);
                            isPromptShowing = false;
                            cancellationSignal = null;
                            executeUnlockPc(savedPassword);
                        }

                        @Override
                        public void onAuthenticationError(int errorCode, CharSequence errString) {
                            super.onAuthenticationError(errorCode, errString);
                            isPromptShowing = false;
                            cancellationSignal = null;
                            if (errorCode != BiometricPrompt.BIOMETRIC_ERROR_CANCELED &&
                                errorCode != BiometricPrompt.BIOMETRIC_ERROR_USER_CANCELED) {
                                showUnlockPasswordDialog();
                            }
                        }

                        @Override
                        public void onAuthenticationFailed() {
                            super.onAuthenticationFailed();
                            vibrate(100);
                        }
                    }
            );
        } catch (Exception e) {
            isPromptShowing = false;
            cancellationSignal = null;
            showUnlockPasswordDialog();
        }
    }

    private void showUnlockPasswordDialog() {
        AlertDialog.Builder builder = new AlertDialog.Builder(this);
        builder.setTitle("Unlock Computer Screen");

        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(60, 30, 60, 10);

        TextView tvHint = new TextView(this);
        tvHint.setText("Enter your computer password to unlock:");
        tvHint.setTextColor(Color.parseColor("#cbd5e1"));
        tvHint.setTextSize(13);
        layout.addView(tvHint);

        EditText etPassword = new EditText(this);
        etPassword.setHint("Computer password");
        etPassword.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        etPassword.setTextColor(Color.WHITE);
        etPassword.setHintTextColor(Color.parseColor("#56657f"));
        layout.addView(etPassword);

        SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        String savedPwd = prefs.getString("saved_laptop_password", null);

        CheckBox cbRemember = new CheckBox(this);
        cbRemember.setText(savedPwd != null && !savedPwd.isEmpty() ? "Update saved password for fingerprint unlock" : "Save password for fingerprint unlock");
        cbRemember.setTextColor(Color.parseColor("#cbd5e1"));
        cbRemember.setTextSize(12);
        cbRemember.setChecked(true);
        if (isBiometricRequiredAndSupported()) {
            layout.addView(cbRemember);
        }

        TextView tvManage = null;
        if (savedPwd != null && !savedPwd.isEmpty()) {
            tvManage = new TextView(this);
            tvManage.setText("Change saved password");
            tvManage.setTextColor(Color.parseColor("#22d3ee"));
            tvManage.setTextSize(12);
            tvManage.setPadding(0, 16, 0, 4);
            layout.addView(tvManage);
        }

        builder.setView(layout);
        builder.setPositiveButton("Unlock", (dialog, which) -> {
            String pwd = etPassword.getText().toString().trim();
            if (pwd.isEmpty()) {
                Toast.makeText(MainActivity.this, "Please enter your password", Toast.LENGTH_SHORT).show();
                return;
            }
            if (isBiometricRequiredAndSupported()) {
                promptBiometricForUnlock(pwd, cbRemember.isChecked());
            } else {
                if (cbRemember.isChecked()) {
                    prefs.edit().putString("saved_laptop_password", pwd).apply();
                }
                executeUnlockPc(pwd);
            }
        });
        builder.setNegativeButton("Cancel", null);

        AlertDialog dialog = builder.create();
        if (tvManage != null) {
            tvManage.setOnClickListener(v -> {
                dialog.dismiss();
                showChangePasswordDialog(null);
            });
        }
        dialog.show();
        etPassword.requestFocus();
    }

    private void promptBiometricForUnlock(String password, boolean rememberChecked) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) {
            if (rememberChecked) {
                getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE)
                        .edit().putString("saved_laptop_password", password).apply();
            }
            executeUnlockPc(password);
            return;
        }

        cancelBiometricPrompt();
        cancellationSignal = new CancellationSignal();
        isPromptShowing = true;

        PairedDevice active = DeviceManager.getActiveDevice(this);

        try {
            BiometricPrompt.Builder builder = new BiometricPrompt.Builder(this)
                    .setTitle("Authorize Computer Unlock")
                    .setSubtitle("Confirm it's you to unlock the computer")
                    .setDescription("Authorize unlock for " + (active != null ? active.hostname : "Computer"))
                    .setNegativeButton("Cancel", getMainExecutor(), (dialog, which) -> {
                        isPromptShowing = false;
                        cancellationSignal = null;
                    });

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                builder.setConfirmationRequired(false);
            }

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                // Allow face unlock too (Class 2 biometrics), not only fingerprint
                builder.setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_WEAK);
            }
            BiometricPrompt prompt = builder.build();
            prompt.authenticate(
                    cancellationSignal,
                    getMainExecutor(),
                    new BiometricPrompt.AuthenticationCallback() {
                        @Override
                        public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) {
                            super.onAuthenticationSucceeded(result);
                            isPromptShowing = false;
                            cancellationSignal = null;
                            if (rememberChecked) {
                                getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE)
                                        .edit().putString("saved_laptop_password", password).apply();
                            }
                            executeUnlockPc(password);
                        }

                        @Override
                        public void onAuthenticationError(int errorCode, CharSequence errString) {
                            super.onAuthenticationError(errorCode, errString);
                            isPromptShowing = false;
                            cancellationSignal = null;
                            if (errorCode != BiometricPrompt.BIOMETRIC_ERROR_CANCELED &&
                                errorCode != BiometricPrompt.BIOMETRIC_ERROR_USER_CANCELED) {
                                Toast.makeText(MainActivity.this, "Authentication failed: " + errString, Toast.LENGTH_SHORT).show();
                            }
                        }

                        @Override
                        public void onAuthenticationFailed() {
                            super.onAuthenticationFailed();
                            vibrate(100);
                        }
                    }
            );
        } catch (Exception e) {
            isPromptShowing = false;
            cancellationSignal = null;
            if (rememberChecked) {
                getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE)
                        .edit().putString("saved_laptop_password", password).apply();
            }
            executeUnlockPc(password);
        }
    }

    private void executeUnlockPc(String password) {
        if (!isPcConnected) {
            Toast.makeText(this, "Not connected to the computer", Toast.LENGTH_SHORT).show();
            return;
        }
        Toast.makeText(this, "Unlocking computer...", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            try {
                PairedDevice active = DeviceManager.getActiveDevice(this);
                if (active == null) return;
                String url = active.getBaseUrl() + "/api/action";
                HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("POST");
                conn.setRequestProperty("Content-Type", "application/json");
                conn.setConnectTimeout(6000);
                conn.setReadTimeout(6000);
                if (active.isPaired()) {
                    NetworkUtils.setAuth(conn, active.authToken);
                }
                conn.setDoOutput(true);
                JSONObject body = new JSONObject();
                body.put("action", "unlock");
                if (password != null && !password.isEmpty()) {
                    body.put("password", password);
                }
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(body.toString().getBytes("UTF-8"));
                }
                int code = conn.getResponseCode();
                String serverMessage = "";
                if (code != 200 && conn.getErrorStream() != null) {
                    try (BufferedReader r = new BufferedReader(new InputStreamReader(conn.getErrorStream(), "UTF-8"))) {
                        StringBuilder sb = new StringBuilder();
                        String line;
                        while ((line = r.readLine()) != null) sb.append(line);
                        serverMessage = new JSONObject(sb.toString()).optString("message", "");
                    } catch (Exception ignored) {}
                }
                final String reason = serverMessage;
                conn.disconnect();

                runOnUiThread(() -> {
                    if (code == 200) {
                        isPcCurrentlyLocked = false;
                        showLockState(false);
                        hideChallenge();
                        AuthService.cancelChallengeNotification(MainActivity.this);
                        Toast.makeText(MainActivity.this, "Computer screen unlocked!", Toast.LENGTH_SHORT).show();
                    } else if (code == 401) {
                        Toast.makeText(MainActivity.this, "Incorrect password. Please try again.", Toast.LENGTH_LONG).show();
                        getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE)
                                .edit().remove("saved_laptop_password").apply();
                        showUnlockPasswordDialog();
                    } else {
                        Toast.makeText(MainActivity.this, reason.isEmpty() ? "Couldn't unlock (HTTP " + code + ")" : reason, Toast.LENGTH_LONG).show();
                    }
                });
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        }).start();
    }

    private void fetchLaptopStatus() {
        new Thread(() -> {
            try {
                PairedDevice active = DeviceManager.getActiveDevice(this);
                if (active == null) return;
                String url = active.getBaseUrl() + "/api/pc/status";
                HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("GET");
                conn.setConnectTimeout(3000);
                conn.setReadTimeout(3000);
                if (active.isPaired()) {
                    NetworkUtils.setAuth(conn, active.authToken);
                }
                int code = conn.getResponseCode();
                if (code == 200) {
                    StringBuilder sb = new StringBuilder();
                    try (BufferedReader r = new BufferedReader(new InputStreamReader(conn.getInputStream(), "UTF-8"))) {
                        String line;
                        while ((line = r.readLine()) != null) sb.append(line);
                    }
                    JSONObject res = new JSONObject(sb.toString());
                    applyLaptopStatus(res, active);
                }
            } catch (Exception ignored) {}
        }).start();
    }

    private void showScreenshotDialog() {
        View dialogView = LayoutInflater.from(this).inflate(R.layout.dialog_screenshot, null);

        ImageView ivPreview = dialogView.findViewById(R.id.iv_screenshot_preview);
        ProgressBar pbLoading = dialogView.findViewById(R.id.pb_screenshot_loading);
        TextView tvTime = dialogView.findViewById(R.id.tv_screenshot_time);
        Button btnRefresh = dialogView.findViewById(R.id.btn_screenshot_refresh);
        Button btnSave = dialogView.findViewById(R.id.btn_screenshot_save);

        // A popup card the size of the picture (a landscape screen leaves most of a full page empty);
        // tap outside or swipe back to close
        Dialog dialog = new Dialog(this);
        dialog.requestWindowFeature(android.view.Window.FEATURE_NO_TITLE);
        dialog.setContentView(dialogView);
        dialog.setCanceledOnTouchOutside(true);
        android.view.Window w = dialog.getWindow();
        if (w != null) {
            w.setBackgroundDrawable(new android.graphics.drawable.InsetDrawable(
                    new android.graphics.drawable.ColorDrawable(android.graphics.Color.TRANSPARENT), dpToPx(12)));
            w.setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            w.setDimAmount(0.6f);
        }

        Runnable fetchScreenshot = () -> {
            pbLoading.setVisibility(View.VISIBLE);
            tvTime.setText("Capturing screen...");
            new Thread(() -> {
                try {
                    PairedDevice active = DeviceManager.getActiveDevice(this);
                    String baseUrl = (DeviceManager.isWifiActive(this) && active.getLocalUrl() != null && !active.getLocalUrl().isEmpty())
                            ? active.getLocalUrl() : active.getBaseUrl();
                    String url = baseUrl + "/api/screen/screenshot";
                    HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                    NetworkUtils.applyTunnelHeaders(conn);
                    conn.setRequestMethod("GET");
                    conn.setConnectTimeout(8000);
                    conn.setReadTimeout(10000);
                    if (active.isPaired()) {
                        NetworkUtils.setAuth(conn, active.authToken);
                    }
                    int code = conn.getResponseCode();
                    if (code == 200) {
                        try (InputStream is = conn.getInputStream()) {
                            Bitmap bm = BitmapFactory.decodeStream(is);
                            currentScreenshotBitmap = bm;
                            String timeStr = new SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(new Date());
                            runOnUiThread(() -> {
                                pbLoading.setVisibility(View.GONE);
                                if (bm != null) {
                                    ivPreview.setImageBitmap(bm);
                                    tvTime.setText("Captured at " + timeStr);
                                } else {
                                    tvTime.setText("Failed to decode screenshot");
                                }
                            });
                        }
                    } else {
                        runOnUiThread(() -> {
                            pbLoading.setVisibility(View.GONE);
                            tvTime.setText(code == 409 ? "Nobody is logged in on the computer yet" : "Capture error (HTTP " + code + ")");
                        });
                    }
                } catch (Exception e) {
                    runOnUiThread(() -> {
                        pbLoading.setVisibility(View.GONE);
                        tvTime.setText("Error: " + e.getMessage());
                    });
                }
            }).start();
        };

        btnRefresh.setOnClickListener(v -> fetchScreenshot.run());

        btnSave.setOnClickListener(v -> {
            if (currentScreenshotBitmap == null) {
                Toast.makeText(this, "No screenshot available to save", Toast.LENGTH_SHORT).show();
                return;
            }
            saveScreenshotToGallery(currentScreenshotBitmap);
        });

        fetchScreenshot.run();
        dialog.show();
    }

    private void saveScreenshotToGallery(Bitmap bitmap) {
        String filename = "PC_Screenshot_" + new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date()) + ".jpg";
        try {
            OutputStream fos = null;
            File imageFile = null;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ContentValues values = new ContentValues();
                values.put(MediaStore.Images.Media.DISPLAY_NAME, filename);
                values.put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg");
                values.put(MediaStore.Images.Media.RELATIVE_PATH, Environment.DIRECTORY_PICTURES + "/PC_Screenshots");
                Uri uri = getContentResolver().insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values);
                if (uri != null) {
                    fos = getContentResolver().openOutputStream(uri);
                }
            } else {
                File dir = new File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_PICTURES), "PC_Screenshots");
                if (!dir.exists()) dir.mkdirs();
                imageFile = new File(dir, filename);
                fos = new FileOutputStream(imageFile);
            }

            if (fos != null) {
                bitmap.compress(Bitmap.CompressFormat.JPEG, 95, fos);
                fos.flush();
                fos.close();
                if (imageFile != null) {
                    MediaScannerConnection.scanFile(this, new String[]{imageFile.getAbsolutePath()}, new String[]{"image/jpeg"}, null);
                }
                Toast.makeText(this, "Saved to Pictures/PC_Screenshots!", Toast.LENGTH_SHORT).show();
            } else {
                Toast.makeText(this, "Could not open storage to save screenshot", Toast.LENGTH_SHORT).show();
            }
        } catch (Exception e) {
            Toast.makeText(this, "Failed to save: " + e.getMessage(), Toast.LENGTH_SHORT).show();
        }
    }

    private void showFileSearchDialog() {
        View dialogView = LayoutInflater.from(this).inflate(R.layout.dialog_file_search, null);

        EditText etQuery = dialogView.findViewById(R.id.et_search_query);
        Button btnSearch = dialogView.findViewById(R.id.btn_search_submit);
        TextView tvStatus = dialogView.findViewById(R.id.tv_search_status);
        LinearLayout containerResults = dialogView.findViewById(R.id.container_search_results);

        Dialog dialog = createPage(dialogView, false);

        Runnable runSearch = () -> {
            String q = etQuery.getText().toString().trim();
            if (q.isEmpty()) {
                tvStatus.setText("Please enter a search term");
                return;
            }
            tvStatus.setText("Searching for \u201c" + q + "\u201d…");
            containerResults.removeAllViews();

            new Thread(() -> {
                try {
                    PairedDevice active = DeviceManager.getActiveDevice(this);
                    String url = active.getBaseUrl() + "/api/files/search?q=" + URLEncoder.encode(q, "UTF-8");
                    HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                    NetworkUtils.applyTunnelHeaders(conn);
                    conn.setRequestMethod("GET");
                    conn.setConnectTimeout(8000);
                    conn.setReadTimeout(12000);
                    if (active.isPaired()) {
                        NetworkUtils.setAuth(conn, active.authToken);
                    }
                    int code = conn.getResponseCode();
                    if (code == 200) {
                        StringBuilder sb = new StringBuilder();
                        try (BufferedReader r = new BufferedReader(new InputStreamReader(conn.getInputStream(), "UTF-8"))) {
                            String line;
                            while ((line = r.readLine()) != null) sb.append(line);
                        }
                        // The laptop returns {"status","results":[...]}; older versions sent a bare array
                        String body = sb.toString().trim();
                        JSONArray parsed = body.startsWith("[")
                                ? new JSONArray(body)
                                : new JSONObject(body).optJSONArray("results");
                        final JSONArray finalResults = parsed != null ? parsed : new JSONArray();
                        runOnUiThread(() -> {
                            JSONArray results = finalResults;
                            int count = results.length();
                            if (count == 0) {
                                tvStatus.setText("No matching files found on computer.");
                                return;
                            }
                            tvStatus.setText("Found " + count + " file" + (count == 1 ? "" : "s") + " on computer:");
                            LayoutInflater inflater = LayoutInflater.from(this);
                            for (int i = 0; i < count; i++) {
                                JSONObject item = results.optJSONObject(i);
                                if (item == null) continue;
                                String name = item.optString("name", "Unknown");
                                String path = item.optString("path", "");
                                String sizeStr = item.optString("size_formatted", "");
                                boolean isDir = item.optBoolean("is_dir", false);

                                View row = inflater.inflate(R.layout.item_file_search, containerResults, false);
                                TextView tvIcon = row.findViewById(R.id.tv_file_icon);
                                TextView tvName = row.findViewById(R.id.tv_file_name);
                                TextView tvDetails = row.findViewById(R.id.tv_file_details);
                                Button btnGet = row.findViewById(R.id.btn_file_download);

                                tvName.setText(name);
                                String lower = name.toLowerCase();
                                setIconTile(tvIcon, fileIconFor(lower), getColor(R.color.o_text_2));

                                String shortDir = path;
                                int lastSlash = path.lastIndexOf('/');
                                if (lastSlash > 0) {
                                    shortDir = path.substring(0, lastSlash);
                                    if (shortDir.length() > 28) {
                                        shortDir = "..." + shortDir.substring(shortDir.length() - 25);
                                    }
                                }
                                tvDetails.setText((isDir ? "Folder" : sizeStr) + " · " + shortDir);

                                if (isDir) {
                                    setIconTile(tvIcon, R.drawable.ic_o_folder, getColor(R.color.o_accent));
                                    btnGet.setText("Open");
                                    btnGet.setOnClickListener(v -> {
                                        dialog.dismiss();
                                        showFileBrowseDialog(path);
                                    });
                                } else {
                                    btnGet.setOnClickListener(v -> downloadFileFromLaptop(path, name, dialog));
                                }
                                containerResults.addView(row);
                            }
                        });
                    } else {
                        runOnUiThread(() -> tvStatus.setText("Search failed (HTTP " + code + ")"));
                    }
                } catch (Exception e) {
                    runOnUiThread(() -> tvStatus.setText("Error: " + e.getMessage()));
                }
            }).start();
        };

        btnSearch.setOnClickListener(v -> runSearch.run());
        etQuery.setOnEditorActionListener((v, actionId, event) -> {
            runSearch.run();
            return true;
        });

        dialog.show();
    }

    private void downloadFileFromLaptop(String pcPath, String filename, Dialog parentDialog) {
        Toast.makeText(this, "⏳ Downloading " + filename + "...", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            try {
                PairedDevice active = DeviceManager.getActiveDevice(this);
                String url = active.getBaseUrl() + "/api/files/download_pc?path=" + URLEncoder.encode(pcPath, "UTF-8");
                HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("GET");
                conn.setConnectTimeout(10000);
                conn.setReadTimeout(60000);
                if (active.isPaired()) {
                    NetworkUtils.setAuth(conn, active.authToken);
                }

                File destDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
                if (!destDir.exists()) destDir.mkdirs();
                File dest = new File(destDir, filename);
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

                String mimeType = PCFileProvider.getMimeType(dest.getName());
                OutputStream fos = null;
                try {
                    fos = new FileOutputStream(dest);
                } catch (Exception directEx) {
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
                        } catch (Exception ignored) {}
                    }
                }

                if (fos != null) {
                    try (InputStream in = conn.getInputStream();
                         OutputStream outStream = fos) {
                        byte[] buf = new byte[65536];
                        int r;
                        while ((r = in.read(buf)) != -1) {
                            outStream.write(buf, 0, r);
                        }
                        outStream.flush();
                    }

                    try {
                        MediaScannerConnection.scanFile(this, new String[]{dest.getAbsolutePath()}, new String[]{mimeType}, null);
                    } catch (Exception ignored) {}

                    final File finalFile = dest;
                    runOnUiThread(() -> {
                        Toast.makeText(MainActivity.this, "Saved: " + finalFile.getName() + "! Opening...", Toast.LENGTH_LONG).show();
                        openDownloadedFile(finalFile);
                    });
                }
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Download error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        }).start();
    }

    private void openDownloadedFile(File file) {
        try {
            String mimeType = PCFileProvider.getMimeType(file.getAbsolutePath());
            Uri fileUri = PCFileProvider.getUriForFile(this, file);
            boolean isApk = file.getName().toLowerCase().endsWith(".apk") || "application/vnd.android.package-archive".equals(mimeType);
            if (isApk) {
                mimeType = "application/vnd.android.package-archive";
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !getPackageManager().canRequestPackageInstalls()) {
                    pendingInstallApk = file;
                    new AlertDialog.Builder(this)
                        .setTitle("Install Permission Required")
                        .setMessage("To install " + file.getName() + ", please enable 'Allow from this source' for Orbit in Settings.")
                        .setPositiveButton("Open Settings", (d, w) -> {
                            try {
                                Intent sIntent = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                                        Uri.parse("package:" + getPackageName()));
                                startActivity(sIntent);
                            } catch (Exception ex) {
                                try {
                                    startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES));
                                } catch (Exception ignored) {}
                            }
                        })
                        .setNegativeButton("Cancel", null)
                        .show();
                    return;
                }
            }

            Intent viewIntent = new Intent(Intent.ACTION_VIEW);
            viewIntent.setDataAndType(fileUri, mimeType);
            viewIntent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            viewIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            if (isApk) {
                viewIntent.putExtra(Intent.EXTRA_NOT_UNKNOWN_SOURCE, true);
            }

            try {
                List<android.content.pm.ResolveInfo> resInfoList = getPackageManager().queryIntentActivities(viewIntent, PackageManager.MATCH_DEFAULT_ONLY);
                for (android.content.pm.ResolveInfo resolveInfo : resInfoList) {
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

            startActivity(viewIntent);
        } catch (Exception e) {
            Toast.makeText(this, "Saved to Downloads: " + file.getName(), Toast.LENGTH_SHORT).show();
        }
    }

    private void streamFileFromLaptop(String pcPath, String filename) {
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null) {
            Toast.makeText(this, "No active computer connected", Toast.LENGTH_SHORT).show();
            return;
        }
        // The player app gets a link for this one file only (a ticket), never the pairing token
        final String baseUrl = active.getBaseUrl();
        new Thread(() -> {
            String ticket = null;
            try {
                JSONObject req = new JSONObject();
                req.put("path", pcPath);
                String res = NetworkUtils.httpPostJsonWithAuth(baseUrl + "/api/files/stream_ticket", active.authToken, req.toString(), 5000);
                if (res != null) ticket = new JSONObject(res).optString("ticket", null);
            } catch (Exception ignored) { }
            final String t = ticket;
            runOnUiThread(() -> {
                if (t == null || t.isEmpty()) {
                    Toast.makeText(this, "Couldn't open " + filename, Toast.LENGTH_SHORT).show();
                } else {
                    openStreamUrl(baseUrl + "/api/files/download_pc?ticket=" + Uri.encode(t), filename);
                }
            });
        }).start();
    }

    private void openStreamUrl(String streamUrl, String filename) {
        try {
            String mimeType = PCFileProvider.getMimeType(filename);
            String lower = filename.toLowerCase();
            if (mimeType == null || "application/octet-stream".equals(mimeType)) {
                if (lower.endsWith(".mp4")) mimeType = "video/mp4";
                else if (lower.endsWith(".mkv")) mimeType = "video/x-matroska";
                else if (lower.endsWith(".webm")) mimeType = "video/webm";
                else if (lower.endsWith(".avi")) mimeType = "video/x-msvideo";
                else if (lower.endsWith(".mov")) mimeType = "video/quicktime";
                else if (lower.endsWith(".mp3")) mimeType = "audio/mpeg";
                else if (lower.endsWith(".flac")) mimeType = "audio/flac";
                else if (lower.endsWith(".wav")) mimeType = "audio/wav";
                else if (lower.endsWith(".m4a")) mimeType = "audio/mp4";
                else if (lower.endsWith(".ogg")) mimeType = "audio/ogg";
                else if (lower.endsWith(".pdf")) mimeType = "application/pdf";
                else if (lower.endsWith(".png")) mimeType = "image/png";
                else if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) mimeType = "image/jpeg";
                else if (lower.endsWith(".webp")) mimeType = "image/webp";
                else if (lower.endsWith(".gif")) mimeType = "image/gif";
                else if (lower.endsWith(".txt") || lower.endsWith(".log") || lower.endsWith(".md") || lower.endsWith(".py") || lower.endsWith(".java") || lower.endsWith(".c") || lower.endsWith(".sh")) mimeType = "text/plain";
                else mimeType = "*/*";
            }

            Intent viewIntent = new Intent(Intent.ACTION_VIEW);
            viewIntent.setDataAndType(Uri.parse(streamUrl), mimeType);
            viewIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
            try {
                startActivity(Intent.createChooser(viewIntent, "Stream / Open " + filename));
            } catch (Exception ex) {
                Intent browserIntent = new Intent(Intent.ACTION_VIEW, Uri.parse(streamUrl));
                browserIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(browserIntent);
            }
        } catch (Exception e) {
            Toast.makeText(this, "Stream error: " + e.getMessage(), Toast.LENGTH_SHORT).show();
        }
    }

    private void showFileBrowseDialog() {
        showFileBrowseDialog("shortcuts");
    }

    private void showFileBrowseDialog(String startPath) {
        View dialogView = LayoutInflater.from(this).inflate(R.layout.dialog_file_browse, null);

        TextView tvStorage = dialogView.findViewById(R.id.tv_browser_storage);
        Button btnHome = dialogView.findViewById(R.id.btn_browse_home);
        Button btnUp = dialogView.findViewById(R.id.btn_browse_up);
        TextView tvPath = dialogView.findViewById(R.id.tv_browse_path);
        Button btnRefresh = dialogView.findViewById(R.id.btn_browse_refresh);
        Button btnHidden = dialogView.findViewById(R.id.btn_browse_hidden);

        final boolean[] showHidden = new boolean[] {
            getSharedPreferences("pc_auth_prefs", MODE_PRIVATE).getBoolean("browse_show_hidden", false)
        };

        Button btnShortcutDrives = dialogView.findViewById(R.id.btn_shortcut_drives);
        Button btnQuickDownloads = dialogView.findViewById(R.id.btn_shortcut_downloads);
        Button btnQuickDocs = dialogView.findViewById(R.id.btn_shortcut_documents);
        Button btnQuickPics = dialogView.findViewById(R.id.btn_shortcut_pictures);
        Button btnQuickVids = dialogView.findViewById(R.id.btn_shortcut_videos);
        Button btnQuickDesk = dialogView.findViewById(R.id.btn_shortcut_desktop);

        ProgressBar pbLoading = dialogView.findViewById(R.id.pb_browse_loading);
        TextView tvEmpty = dialogView.findViewById(R.id.tv_browse_empty);
        LinearLayout containerItems = dialogView.findViewById(R.id.container_browse_items);

        Dialog dialog = createPage(dialogView, false);

        // The folders you came through, newest last: back returns to the one you came from
        // (a shortcut, the drives list, a folder further away), not always the parent
        java.util.ArrayDeque<String> backStack = new java.util.ArrayDeque<>();

        class BrowseLoader {
            /** Go to a folder, remembering this one so back returns here. */
            void open(String targetPath) {
                if (currentBrowsePath != null && !currentBrowsePath.equals(targetPath)) backStack.push(currentBrowsePath);
                load(targetPath);
            }

            void load(String targetPath) {
                currentBrowsePath = targetPath;
                pbLoading.setVisibility(View.VISIBLE);
                tvEmpty.setVisibility(View.GONE);
                containerItems.removeAllViews();

                new Thread(() -> {
                    try {
                        PairedDevice active = DeviceManager.getActiveDevice(MainActivity.this);
                        String url = active.getBaseUrl() + "/api/laptop/files/list?path=" + URLEncoder.encode(targetPath, "UTF-8") + "&hidden=" + (showHidden[0] ? "1" : "0");
                        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                        NetworkUtils.applyTunnelHeaders(conn);
                        conn.setRequestMethod("GET");
                        conn.setConnectTimeout(8000);
                        conn.setReadTimeout(12000);
                        if (active.isPaired()) {
                            NetworkUtils.setAuth(conn, active.authToken);
                        }
                        int code = conn.getResponseCode();
                        if (code == 200) {
                            StringBuilder sb = new StringBuilder();
                            try (BufferedReader r = new BufferedReader(new InputStreamReader(conn.getInputStream(), "UTF-8"))) {
                                String line;
                                while ((line = r.readLine()) != null) sb.append(line);
                            }
                            JSONObject res = new JSONObject(sb.toString());
                            String curPath = res.optString("current_path", targetPath);
                            parentBrowsePath = res.isNull("parent_path") ? null : res.optString("parent_path", null);
                            JSONObject stor = res.optJSONObject("storage");
                            String storStr = (stor != null) ? stor.optString("formatted", "") : "";
                            JSONArray items = res.optJSONArray("items");

                            runOnUiThread(() -> {
                                pbLoading.setVisibility(View.GONE);
                                if (!storStr.isEmpty()) {
                                    tvStorage.setText(storStr);
                                }
                                String dispPath = curPath;
                                if (dispPath.startsWith("/home/")) {
                                    int nextSlash = dispPath.indexOf('/', 6);
                                    if (nextSlash > 0) dispPath = "~" + dispPath.substring(nextSlash);
                                    else dispPath = "~";
                                }
                                tvPath.setText("shortcuts".equals(dispPath) ? "Shortcuts / Drives" : dispPath);
                                btnUp.setEnabled(parentBrowsePath != null);
                                btnUp.setAlpha(parentBrowsePath != null ? 1.0f : 0.4f);

                                if (items == null || items.length() == 0) {
                                    tvEmpty.setVisibility(View.VISIBLE);
                                    return;
                                }

                                LayoutInflater inflater = LayoutInflater.from(MainActivity.this);
                                for (int i = 0; i < items.length(); i++) {
                                    JSONObject it = items.optJSONObject(i);
                                    if (it == null) continue;
                                    String name = it.optString("name", "Unknown");
                                    String itemPath = it.optString("path", "");
                                    boolean isDir = it.optBoolean("is_dir", false);
                                    String sizeFormatted = it.optString("size_formatted", "");
                                    String customIcon = it.optString("icon", "");

                                    View row = inflater.inflate(R.layout.item_file_browse, containerItems, false);
                                    TextView tvIcon = row.findViewById(R.id.tv_browse_item_icon);
                                    TextView tvName = row.findViewById(R.id.tv_browse_item_name);
                                    TextView tvDetails = row.findViewById(R.id.tv_browse_item_details);
                                    Button btnAction = row.findViewById(R.id.btn_browse_item_action);
                                    Button btnDownload = row.findViewById(R.id.btn_browse_item_download);

                                    boolean isHidden = it.optBoolean("is_hidden", false) || name.startsWith(".");
                                    tvName.setText(isHidden ? "· " + name : name);
                                    tvName.setAlpha(isHidden ? 0.65f : 1.0f);
                                    if (isDir) {
                                        setIconTile(tvIcon, R.drawable.ic_o_folder, getColor(R.color.o_accent));
                                        tvDetails.setText("Directory / Folder");
                                        btnAction.setVisibility(View.VISIBLE);
                                        btnAction.setText("Open");
                                        btnAction.setOnClickListener(v -> open(itemPath));
                                        if (btnDownload != null) btnDownload.setVisibility(View.GONE);
                                        row.setOnClickListener(v -> open(itemPath));
                                    } else {
                                        String lower = name.toLowerCase();
                                        setIconTile(tvIcon, fileIconFor(lower), getColor(R.color.o_text_2));

                                        tvDetails.setText(sizeFormatted);
                                        btnAction.setVisibility(View.GONE);    // tapping the file streams it
                                        if (btnDownload != null) {
                                            btnDownload.setVisibility(View.VISIBLE);
                                            btnDownload.setOnClickListener(v -> downloadFileFromLaptop(itemPath, name, null));
                                        }
                                        row.setOnClickListener(v -> streamFileFromLaptop(itemPath, name));
                                    }

                                    containerItems.addView(row);
                                }
                            });
                        } else {
                            runOnUiThread(() -> {
                                pbLoading.setVisibility(View.GONE);
                                tvEmpty.setText("Failed to load folder (HTTP " + code + ")");
                                tvEmpty.setVisibility(View.VISIBLE);
                            });
                        }
                    } catch (Exception e) {
                        runOnUiThread(() -> {
                            pbLoading.setVisibility(View.GONE);
                            tvEmpty.setText("Error: " + e.getMessage());
                            tvEmpty.setVisibility(View.VISIBLE);
                        });
                    }
                }).start();
            }
        }

        BrowseLoader loader = new BrowseLoader();
        btnHome.setOnClickListener(v -> loader.open("shortcuts"));
        if (btnShortcutDrives != null) btnShortcutDrives.setOnClickListener(v -> loader.open("shortcuts"));
        btnUp.setOnClickListener(v -> {
            if (parentBrowsePath != null) {
                loader.open(parentBrowsePath);
            }
        });
        // Swiping back retraces your steps, like a web browser; from where the explorer
        // opened, it closes the explorer
        dialog.setOnKeyListener((d, keyCode, event) -> {
            if (keyCode != android.view.KeyEvent.KEYCODE_BACK || backStack.isEmpty()) return false;
            if (event.getAction() == android.view.KeyEvent.ACTION_UP) loader.load(backStack.pop());
            return true;
        });
        btnRefresh.setOnClickListener(v -> loader.load(currentBrowsePath != null ? currentBrowsePath : "shortcuts"));

        if (btnHidden != null) {
            btnHidden.setText("");
            setTopIcon(btnHidden, showHidden[0] ? R.drawable.ic_o_eye : R.drawable.ic_o_eye_off, getColor(showHidden[0] ? R.color.o_accent : R.color.o_text_2));
            btnHidden.setOnClickListener(v -> {
                showHidden[0] = !showHidden[0];
                getSharedPreferences("pc_auth_prefs", MODE_PRIVATE).edit().putBoolean("browse_show_hidden", showHidden[0]).apply();
                btnHidden.setText("");
                setTopIcon(btnHidden, showHidden[0] ? R.drawable.ic_o_eye : R.drawable.ic_o_eye_off, getColor(showHidden[0] ? R.color.o_accent : R.color.o_text_2));
                loader.load(currentBrowsePath != null ? currentBrowsePath : "shortcuts");
            });
        }

        btnQuickDownloads.setOnClickListener(v -> loader.open("~/Downloads"));
        btnQuickDocs.setOnClickListener(v -> loader.open("~/Documents"));
        btnQuickPics.setOnClickListener(v -> loader.open("~/Pictures"));
        btnQuickVids.setOnClickListener(v -> loader.open("~/Videos"));
        btnQuickDesk.setOnClickListener(v -> loader.open("~/Desktop"));

        loader.load(startPath);
        dialog.show();
    }

    private void sendMediaCommand(String command) {
        new Thread(() -> {
            try {
                PairedDevice active = DeviceManager.getActiveDevice(this);
                String url = active.getBaseUrl() + "/api/media/command";
                HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("POST");
                conn.setRequestProperty("Content-Type", "application/json");
                conn.setDoOutput(true);
                if (active.isPaired()) {
                    NetworkUtils.setAuth(conn, active.authToken);
                }
                JSONObject body = new JSONObject().put("command", command);
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(body.toString().getBytes("UTF-8"));
                }
                conn.getResponseCode();
                runOnUiThread(() -> pollHandler.postDelayed(this::fetchMediaStatus, 300));
            } catch (Exception ignored) {}
        }).start();
    }

    private void fetchMediaStatus() {
        new Thread(() -> {
            try {
                PairedDevice active = DeviceManager.getActiveDevice(this);
                String url = active.getBaseUrl() + "/api/media/status";
                HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("GET");
                conn.setConnectTimeout(2000);
                conn.setReadTimeout(2000);
                if (active.isPaired()) {
                    NetworkUtils.setAuth(conn, active.authToken);
                }
                int code = conn.getResponseCode();
                if (code == 200) {
                    StringBuilder sb = new StringBuilder();
                    try (BufferedReader r = new BufferedReader(new InputStreamReader(conn.getInputStream(), "UTF-8"))) {
                        String line;
                        while ((line = r.readLine()) != null) sb.append(line);
                    }
                    JSONObject res = new JSONObject(sb.toString());
                    applyMedia(res);
                }
            } catch (Exception ignored) {}
        }).start();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (activeTerminalId == null && lastClosedTerminalId != null) startBackgroundTermSync(lastClosedTerminalId);
        if (activeTerminalId != null && layoutTerminalInteractive != null && layoutTerminalInteractive.getVisibility() == View.VISIBLE) {
            // back from another app or the lock screen: pick the live view up again
            newTermPollGeneration();
            pollActiveTerminalOutput();
            startTerminalStreamTimer();
        }
        updateCaptivePortalButton();
        updateConnectionStatus(AuthService.isConnectedToLaptop(), AuthService.getLastStatusText());
        if (RingManager.isRinging()) {
            RingManager.stopAlarm(this);
            Toast.makeText(this, "Alarm stopped", Toast.LENGTH_SHORT).show();
        }

        // Auto-install pending APK if permission was just granted
        if (pendingInstallApk != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            if (getPackageManager().canRequestPackageInstalls()) {
                File apkToInstall = pendingInstallApk;
                pendingInstallApk = null;
                openDownloadedFile(apkToInstall);
            }
        }

        // Auto-sync clipboard if changed
        AuthService.checkAndSyncPhoneClipboard(this);

        // Register clipboard listener while in foreground
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm != null) {
                if (mainClipListener == null) {
                    mainClipListener = () -> AuthService.checkAndSyncPhoneClipboard(MainActivity.this);
                }
                cm.removePrimaryClipChangedListener(mainClipListener);
                cm.addPrimaryClipChangedListener(mainClipListener);
            }
        } catch (Exception ignored) {}

        refreshDeviceList();
        updateChallengeUIFromStore();
        checkActiveChallenge();
        fetchMediaStatus();
        fetchLaptopStatus();
        refreshKeepAwake();

        // Start battery-efficient periodic check while activity is in foreground (4.5s)
        pollRunnable = new Runnable() {
            private int statusCounter = 0;
            @Override
            public void run() {
                statusCounter++;
                // only what the screen shows: media on Control, computer status on Home;
                // approval requests arrive on the live link, this is just a backstop
                int panel = mainViewFlipper != null ? mainViewFlipper.getDisplayedChild() : PANEL_HOME;
                StringBuilder parts = new StringBuilder();
                if (statusCounter % 3 == 0) parts.append("challenge,");
                if (panel == 1) parts.append("media,");
                if (panel == PANEL_HOME && statusCounter % 2 == 0) parts.append("status,");
                if (parts.length() > 0) syncScreen(parts.toString());     // one request for all of it
                pollHandler.postDelayed(this, 4500);
            }
        };
        pollHandler.postDelayed(pollRunnable, 4500);
    }

    @Override
    protected void onPause() {
        super.onPause();
        cancelBiometricPrompt();
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm != null && mainClipListener != null) {
                cm.removePrimaryClipChangedListener(mainClipListener);
            }
        } catch (Exception ignored) {}
        if (pollRunnable != null) {
            pollHandler.removeCallbacks(pollRunnable);
        }
        stopTerminalStreamTimer();
        stopAllBackgroundTermSync();          // only while the app is on screen
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            AuthService.checkAndSyncPhoneClipboard(this);
        }
    }

    @Override
    protected void onDestroy() {
        cancelBiometricPrompt();
        if (countDownTimer != null) {
            countDownTimer.cancel();
        }
        stopTerminalStreamTimer();
        try {
            terminalSendExecutor.shutdownNow();
        } catch (Exception ignored) {}
        try {
            terminalPollExecutor.shutdownNow();
        } catch (Exception ignored) {}
        try {
            unregisterReceiver(serviceReceiver);
        } catch (Exception ignored) {}
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        if (activeTerminalId != null && layoutTerminalInteractive != null && layoutTerminalInteractive.getVisibility() == View.VISIBLE) {
            closeTerminalInteractive();
            return;
        }
        if (layoutDevicesSheet != null && layoutDevicesSheet.getVisibility() == View.VISIBLE) {
            setDevicesSheetVisible(false);
            return;
        }
        if (mainViewFlipper != null && mainViewFlipper.getDisplayedChild() != PANEL_HOME) {
            switchToPanel(PANEL_HOME);
            return;
        }
        super.onBackPressed();
    }

    // --- Live Terminals Panel Implementation ---
    private void initTerminalsPanel() {
        mainViewFlipper = findViewById(R.id.main_view_flipper);
        tabBtnDashboard = findViewById(R.id.tab_btn_dashboard);
        tabBtnTerminals = findViewById(R.id.tab_btn_terminals);
        tabBtnControl = findViewById(R.id.tab_btn_control);
        tabBtnFiles = findViewById(R.id.tab_btn_files);

        TextView[] tabs = {tabBtnDashboard, tabBtnControl, tabBtnFiles, tabBtnTerminals};
        for (int i = 0; i < tabs.length; i++) {
            final int panel = i;
            if (tabs[i] != null) tabs[i].setOnClickListener(v -> switchToPanel(panel));
        }
        updateTabBar(PANEL_HOME);

        // Terminals Panel Views
        layoutTopTabs = findViewById(R.id.layout_top_tabs);
        panelTerminals = findViewById(R.id.panel_terminals);
        layoutTerminalsListHeader = findViewById(R.id.layout_terminals_list_header);
        scrollTermChips = findViewById(R.id.scroll_term_chips);
        btnTerminalsRefresh = findViewById(R.id.btn_terminals_refresh);
        btnTerminalsNew = findViewById(R.id.btn_terminals_new);
        layoutTermChips = findViewById(R.id.layout_term_chips);
        scrollTermList = findViewById(R.id.scroll_term_list);
        containerTerminalsList = findViewById(R.id.container_terminals_list);
        layoutTerminalsEmpty = findViewById(R.id.layout_terminals_empty);

        layoutTerminalInteractive = findViewById(R.id.layout_terminal_interactive);
        btnInteractiveBack = findViewById(R.id.btn_interactive_back);
        tvInteractiveTitle = findViewById(R.id.tv_interactive_title);
        tvInteractiveStatus = findViewById(R.id.tv_interactive_status);
        tvInteractiveCwd = findViewById(R.id.tv_interactive_cwd);
        scrollTermScreen = findViewById(R.id.scroll_term_screen);
        hscrollTermScreen = findViewById(R.id.hscroll_term_screen);
        tvTermScreen = findViewById(R.id.tv_term_screen);
        btnTermLoadEarlier = findViewById(R.id.btn_term_load_earlier);

        if (tvTermScreen != null) {
            // Plain text normally: selectable text makes every update far more expensive.
            // Long-press selects the word under your finger (updates pause); typing, or tapping
            // the status pill, resumes.
            tvTermScreen.setTextIsSelectable(false);
            tvTermScreen.setOnTouchListener((v, e) -> {
                if (e.getActionMasked() == android.view.MotionEvent.ACTION_DOWN) {
                    lastTermTouchX = e.getX();
                    lastTermTouchY = e.getY();
                }
                return false;
            });
            termLongClick = v -> {
                if (terminalSelectionMode) return false;          // already selectable: Android's own long-press
                setTerminalSelectionMode(true);
                // put the cursor under the finger, then let Android's long-press select that word
                // and show the handles and Copy, all from this one press
                int off = tvTermScreen.getOffsetForPosition(lastTermTouchX, lastTermTouchY);
                CharSequence t = tvTermScreen.getText();
                if (off >= 0 && t instanceof android.text.Spannable) {
                    android.text.Selection.setSelection((android.text.Spannable) t, Math.min(off, t.length()));
                }
                tvTermScreen.post(() -> {
                    tvTermScreen.setOnLongClickListener(null);
                    tvTermScreen.performLongClick();
                    tvTermScreen.setOnLongClickListener(termLongClick);
                });
                return true;
            };
            tvTermScreen.setOnLongClickListener(termLongClick);
        }
        if (tvInteractiveStatus != null) {
            tvInteractiveStatus.setOnClickListener(v -> setTerminalSelectionMode(false));
        }

        if (scrollTermScreen != null) {
            scrollTermScreen.setOnScrollChangeListener((v, x, y, ox, oy) -> updateLoadEarlierPill());
            scrollTermScreen.addOnLayoutChangeListener((v, l, t, r, b, ol, ot, or, ob) -> {
                if (terminalFullscreen && (b - t) != (ob - ot)) v.post(() -> setTerminalFullscreen(true));
            });
        }
        if (btnTermLoadEarlier != null) {
            btnTermLoadEarlier.setOnClickListener(v -> loadEarlierTerminalHistory());
        }

        btnKeyCtrl = findViewById(R.id.btn_key_ctrl);
        btnKeyEsc = findViewById(R.id.btn_key_esc);
        btnKeyTab = findViewById(R.id.btn_key_tab);
        btnKeyLeft = findViewById(R.id.btn_key_left);
        btnKeyRight = findViewById(R.id.btn_key_right);
        btnKeyUp = findViewById(R.id.btn_key_up);
        btnKeyDown = findViewById(R.id.btn_key_down);

        etTermInput = findViewById(R.id.et_term_input);
        btnTermSend = findViewById(R.id.btn_term_send);

        if (btnTerminalsRefresh != null) {
            btnTerminalsRefresh.setOnClickListener(v -> loadTerminalsList());
        }
        if (btnTerminalsNew != null) {
            btnTerminalsNew.setOnClickListener(v -> spawnNewTerminal());
        }
        if (btnInteractiveBack != null) {
            btnInteractiveBack.setOnClickListener(v -> closeTerminalInteractive());
        }
        if (btnInteractiveRefresh != null) {
            btnInteractiveRefresh.setOnClickListener(v -> pollActiveTerminalOutput());
        }
        if (btnInteractiveRotate != null) {
            btnInteractiveRotate.setOnClickListener(v -> toggleTerminalOrientation());
        }

        if (scrollTermScreen != null) {
            scrollTermScreen.addOnLayoutChangeListener((v, left, top, right, bottom, oldLeft, oldTop, oldRight, oldBottom) -> {
                if ((right - left) != (oldRight - oldLeft)) {
                    if (isTerminalWrapEnabled) applyTerminalWrapMode();
                    v.post(this::fitTerminalText);   // width changed (rotation): refit the text
                }
            });
        }

        if (btnKeyCtrl != null) {
            btnKeyCtrl.setOnClickListener(v -> {
                isCtrlActive = !isCtrlActive;
                updateCtrlButtonUI();
                vibrate(15);
            });
        }
        if (btnKeyEsc != null) {
            btnKeyEsc.setOnClickListener(v -> {
                sendTerminalKey("escape");
                vibrate(15);
            });
        }
        if (btnKeyTab != null) {
            btnKeyTab.setOnClickListener(v -> {
                if (isLiveTypeEnabled) {
                    sendTerminalKey("tab");
                } else {
                    String current = etTermInput != null && etTermInput.getText() != null ? etTermInput.getText().toString() : "";
                    if (!current.isEmpty()) {
                        // the line now lives on the computer (with the completion); empty box +
                        // backspace twice brings it back here to edit
                        sendTerminalRawText(current + "\t", termReplaceCount);
                        termReplaceCount = 0;
                        isInternalTextUpdate = true;
                        etTermInput.setText("");
                        isInternalTextUpdate = false;
                    } else {
                        sendTerminalKey("tab");
                    }
                }
                vibrate(15);
            });
        }
        if (btnKeyLeft != null) {
            btnKeyLeft.setOnClickListener(v -> {
                sendTerminalKey("left");
                vibrate(15);
            });
        }
        if (btnKeyRight != null) {
            btnKeyRight.setOnClickListener(v -> {
                sendTerminalKey("right");
                vibrate(15);
            });
        }
        if (btnKeyUp != null) {
            btnKeyUp.setOnClickListener(v -> {
                sendTerminalKey("up");
                vibrate(15);
            });
        }
        if (btnKeyDown != null) {
            btnKeyDown.setOnClickListener(v -> {
                sendTerminalKey("down");
                vibrate(15);
            });
        }
        if (btnKeyClear != null) {
            btnKeyClear.setOnClickListener(v -> {
                if (tvTermScreen != null) tvTermScreen.setText("");
                vibrate(15);
            });
        }

        isTerminalWrapEnabled = getSharedPreferences("pc_auth_prefs", MODE_PRIVATE).getBoolean("terminal_wrap", false);
        btnKeyWrap = findViewById(R.id.btn_term_wrap);
        if (btnKeyWrap != null) {
            updateWrapKey(btnKeyWrap);
            btnKeyWrap.setOnClickListener(v -> toggleTerminalWrap());
        }
        View btnTermRotate = findViewById(R.id.btn_term_rotate);
        if (btnTermRotate != null) {
            btnTermRotate.setOnClickListener(v -> toggleTerminalOrientation());
        }

        if (btnTermSend != null) {
            btnTermSend.setOnClickListener(v -> handleSendTerminalInput());
        }
        if (etTermInput != null) {
            etTermInput.addTextChangedListener(new TextWatcher() {
                @Override
                public void beforeTextChanged(CharSequence s, int start, int count, int after) {
                    textBeforeEdit = s.toString();
                }

                @Override
                public void onTextChanged(CharSequence s, int start, int before, int count) {
                    if (isInternalTextUpdate || activeTerminalId == null) {
                        liveCursor = s.length();      // set by the app (cleared, or loaded from the computer)
                        return;
                    }
                    if (terminalSelectionMode) setTerminalSelectionMode(false);   // typing resumes live updates
                    if (isCtrlActive && count > 0) {
                        // ctrl stays on until tapped again: each key typed (the last character
                        // added; some keyboards resend the whole word) goes as ctrl+key
                        int at = start + count - 1;
                        char ch = s.charAt(at);
                        if ((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z')) {
                            // the box clears (sendTerminalKey): after ctrl+c / ctrl+u the computer's
                            // line isn't what the box shows any more
                            sendTerminalKey("ctrl_" + Character.toLowerCase(ch));
                            vibrate(15);
                            return;
                        }
                    }
                    if (!isLiveTypeEnabled) {
                        return;
                    }
                    // Mirror the edit on the computer: move its cursor to the end of the changed
                    // part (edits can be mid-line), delete what was replaced, type what's new.
                    String added = count > 0 ? s.subSequence(start, start + count).toString().replace("\r", "").replace("\n", "") : "";
                    // The computer counts characters (an emoji is one); Android counts UTF-16
                    // units (an emoji is two). Convert, measuring on the text before the edit.
                    String old = textBeforeEdit != null ? textBeforeEdit : "";
                    int cur = Math.min(liveCursor, old.length()), target = Math.min(start + before, old.length());
                    int move = cur <= target ? old.codePointCount(cur, target) : -old.codePointCount(target, cur);
                    int deleted = old.codePointCount(Math.min(start, old.length()), target);
                    if (move != 0 || deleted > 0 || !added.isEmpty()) {
                        sendTerminalEdit(move, deleted, added);
                    }
                    liveCursor = start + count;
                }

                @Override
                public void afterTextChanged(Editable s) {}
            });

            View prompt = findViewById(R.id.tv_term_prompt);
            if (prompt != null) {
                prompt.setOnClickListener(v -> {
                    if (!etTermInput.isEnabled()) return;
                    if (etTermInput.length() > 0) {
                        Toast.makeText(this, "Clear the box first", Toast.LENGTH_SHORT).show();
                        return;
                    }
                    loadTerminalInputLine();
                });
            }
            if (etTermInput instanceof TermInputEditText) {
                // Backspace with nothing in the box deletes on the computer, like a real terminal
                // (tap ❯ to bring the computer's line here instead)
                ((TermInputEditText) etTermInput).setOnEmptyBackspace(() -> sendTerminalKey("backspace"));
            }

            if (etTermInput instanceof TermInputEditText) {
                ((TermInputEditText) etTermInput).setOnEnter(this::handleSendTerminalInput);
            }

            etTermInput.setOnEditorActionListener((v, actionId, event) -> {
                if (actionId == EditorInfo.IME_ACTION_SEND || actionId == EditorInfo.IME_ACTION_DONE ||
                    (event != null && event.getKeyCode() == android.view.KeyEvent.KEYCODE_ENTER && event.getAction() == android.view.KeyEvent.ACTION_DOWN)) {
                    handleSendTerminalInput();
                    return true;
                }
                return false;
            });
        }
        updateLiveModeUI();
        updateCtrlButtonUI();
    }

    private static final int PANEL_HOME = 0;
    private static final int PANEL_TERMINAL = 3;

    private void switchToPanel(int panelIndex) {
        if (mainViewFlipper == null || mainViewFlipper.getDisplayedChild() == panelIndex) return;
        int current = mainViewFlipper.getDisplayedChild();
        boolean forward = panelIndex > current;
        mainViewFlipper.setInAnimation(this, forward ? R.anim.slide_in_right : R.anim.slide_in_left);
        mainViewFlipper.setOutAnimation(this, forward ? R.anim.slide_out_left : R.anim.slide_out_right);
        mainViewFlipper.setDisplayedChild(panelIndex);
        updateTabBar(panelIndex);
        if (panelIndex == PANEL_TERMINAL) {
            loadTerminalsList();
        } else {
            if (layoutTopTabs != null) layoutTopTabs.setVisibility(View.VISIBLE);
            stopTerminalStreamTimer();
        }
    }

    private void updateTabBar(int selected) {
        TextView[] tabs = {tabBtnDashboard, tabBtnControl, tabBtnFiles, tabBtnTerminals};
        for (int i = 0; i < tabs.length; i++) {
            if (tabs[i] == null) continue;
            boolean on = (i == selected);
            tabs[i].setTextColor(getColor(on ? R.color.o_text : R.color.o_text_3));
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                tabs[i].setTypeface(getResources().getFont(on ? R.font.geist_medium : R.font.geist_regular));
            }
            tabs[i].setCompoundDrawableTintList(android.content.res.ColorStateList.valueOf(getColor(on ? R.color.o_accent : R.color.o_text_3)));
        }
    }

    private void setDevicesSheetVisible(boolean visible) {
        if (layoutDevicesSheet == null) return;
        layoutDevicesSheet.setVisibility(visible ? View.VISIBLE : View.GONE);
    }

    /** "http://192.168.1.5:1760" -> "192.168.1.5:1760"; tunnels show their host. */
    private static String displayAddress(String url) {
        if (url == null) return "";
        return url.replaceFirst("^https?://", "").replaceAll("/$", "");
    }

    private void setIconTile(TextView tv, int iconRes, int tint) {
        android.graphics.drawable.Drawable icon = getDrawable(iconRes).mutate();
        icon.setTint(tint);
        android.graphics.drawable.LayerDrawable tile = new android.graphics.drawable.LayerDrawable(
                new android.graphics.drawable.Drawable[]{getDrawable(R.drawable.bg_icon_tile), icon});
        tile.setLayerGravity(1, android.view.Gravity.CENTER);
        tile.setLayerSize(1, dpToPx(20), dpToPx(20));
        tv.setText("");
        tv.setBackground(tile);
    }

    private static int fileIconFor(String lowerName) {
        if (lowerName.matches(".*\\.(png|jpe?g|webp|gif|heic|svg)$")) return R.drawable.ic_o_camera;
        if (lowerName.matches(".*\\.(mp4|mkv|mov|avi|webm)$")) return R.drawable.ic_o_video;
        if (lowerName.matches(".*\\.(mp3|wav|flac|m4a|ogg|opus)$")) return R.drawable.ic_o_music;
        if (lowerName.endsWith(".apk")) return R.drawable.ic_o_phone;
        if (lowerName.matches(".*\\.(zip|tar|gz|xz|7z|rar)$")) return R.drawable.ic_o_download;
        return R.drawable.ic_o_file;
    }

    private void setStartIcon(TextView tv, int iconRes, int tint, int sizeDp) {
        android.graphics.drawable.Drawable icon = getDrawable(iconRes).mutate();
        icon.setTint(tint);
        icon.setBounds(0, 0, dpToPx(sizeDp), dpToPx(sizeDp));
        tv.setCompoundDrawablesRelative(icon, null, null, null);
    }

    private void setTopIcon(TextView tv, int iconRes, int tint) {
        android.graphics.drawable.Drawable icon = getDrawable(iconRes).mutate();
        icon.setTint(tint);
        icon.setBounds(0, 0, dpToPx(24), dpToPx(24));
        tv.setCompoundDrawablesRelative(null, icon, null, null);
    }

    /** A full-screen page that slides in over the app, with a back arrow (replaces pop-up dialogs). */
    private Dialog createPage(View content, boolean scrollable) {
        Dialog page = new Dialog(this, R.style.PageTheme);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(getColor(R.color.o_ground));

        Button back = new Button(this, null, 0, R.style.Btn_Icon);
        back.setText("");
        back.setContentDescription("Back");
        back.setPadding(0, dpToPx(10), 0, 0);
        setTopIcon(back, R.drawable.ic_o_chevron_left, getColor(R.color.o_text));
        back.setOnClickListener(v -> page.dismiss());
        LinearLayout.LayoutParams backLp = new LinearLayout.LayoutParams(dpToPx(44), dpToPx(44));
        backLp.setMargins(dpToPx(6), dpToPx(8), 0, 0);
        root.addView(back, backLp);

        View body = content;
        if (scrollable) {
            ScrollView scroll = new ScrollView(this);
            scroll.setOverScrollMode(View.OVER_SCROLL_NEVER);
            scroll.addView(content, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
            body = scroll;
        }
        root.addView(body, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        page.setContentView(root);
        if (page.getWindow() != null) {
            page.getWindow().setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
        }
        return page;
    }

    // --- Control: keep awake, power, running apps ---
    private Button btnKeepAwake;
    private boolean keepAwakeOn = false;

    private void initControlExtras() {
        btnKeepAwake = findViewById(R.id.btn_keep_awake);
        if (btnKeepAwake != null) {
            btnKeepAwake.setOnClickListener(v -> {
                boolean on = !keepAwakeOn;
                showKeepAwake(on);
                postToComputer("/api/keep_awake", "{\"enabled\":" + on + "}",
                        on ? "Keeping the computer awake" : "Computer can sleep again");
            });
        }
        View sleep = findViewById(R.id.btn_power_sleep);
        if (sleep != null) sleep.setOnClickListener(v -> postAction("suspend", "Computer is going to sleep"));
        View screenOff = findViewById(R.id.btn_power_screen_off);
        if (screenOff != null) screenOff.setOnClickListener(v -> postAction("screen_off", "Screen turned off"));
        View restart = findViewById(R.id.btn_power_restart);
        if (restart != null) restart.setOnClickListener(v -> confirmPower("reboot", "Restart the computer?", "Restart", "Restarting…"));
        View shutdown = findViewById(R.id.btn_power_shutdown);
        if (shutdown != null) shutdown.setOnClickListener(v -> confirmPower("shutdown", "Shut down the computer?", "Shut down", "Shutting down…"));
        View tasks = findViewById(R.id.btn_task_manager);
        if (tasks != null) tasks.setOnClickListener(v -> showTaskManager());
    }

    /** The keep-awake tile lights up (amber edge, "On") while sleep is blocked. */
    private void showKeepAwake(boolean on) {
        keepAwakeOn = on;
        if (btnKeepAwake != null) {
            // the label stays short; "ON" sits beside the cup, so the tile isn't crowded
            btnKeepAwake.setText("Keep awake");
            btnKeepAwake.setContentDescription(on ? "Keep awake, on" : "Keep awake, off");
            btnKeepAwake.setBackgroundResource(on ? R.drawable.btn_tint_amber_on : R.drawable.btn_tint_amber);
            android.graphics.drawable.Drawable cup = getDrawable(R.drawable.ic_o_coffee).mutate();
            cup.setTint(getColor(R.color.o_amber));
            android.graphics.drawable.Drawable top = on ? new OnBadgeDrawable(cup, getColor(R.color.o_amber),
                    getColor(R.color.o_on_accent), getResources().getDisplayMetrics().density) : cup;
            int size = dpToPx(24);
            top.setBounds(0, 0, on ? top.getIntrinsicWidth() : size, size);
            btnKeepAwake.setCompoundDrawablesRelative(null, top, null, null);
        }
    }

    /** An icon with a small "ON" pill to its right. */
    private static class OnBadgeDrawable extends android.graphics.drawable.Drawable {
        private final android.graphics.drawable.Drawable icon;
        private final android.graphics.Paint pill = new android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG);
        private final android.graphics.Paint text = new android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG);
        private final float d;

        OnBadgeDrawable(android.graphics.drawable.Drawable icon, int pillColor, int textColor, float density) {
            this.icon = icon;
            this.d = density;
            pill.setColor(pillColor);
            text.setColor(textColor);
            text.setTextSize(9 * density);
            text.setFakeBoldText(true);
            text.setTextAlign(android.graphics.Paint.Align.CENTER);
        }

        @Override public int getIntrinsicWidth() { return Math.round(24 * d + 5 * d + 24 * d); }
        @Override public int getIntrinsicHeight() { return Math.round(24 * d); }

        @Override
        public void draw(android.graphics.Canvas c) {
            android.graphics.Rect b = getBounds();
            int s = Math.round(24 * d);
            icon.setBounds(b.left, b.top, b.left + s, b.top + s);
            icon.draw(c);
            float left = b.left + s + 5 * d, right = left + 24 * d;
            float cy = b.top + s / 2f, h = 14 * d;
            c.drawRoundRect(left, cy - h / 2, right, cy + h / 2, h / 2, h / 2, pill);
            android.graphics.Paint.FontMetrics fm = text.getFontMetrics();
            c.drawText("ON", (left + right) / 2, cy - (fm.ascent + fm.descent) / 2, text);
        }

        @Override public void setAlpha(int a) { pill.setAlpha(a); text.setAlpha(a); icon.setAlpha(a); }
        @Override public void setColorFilter(android.graphics.ColorFilter f) { icon.setColorFilter(f); }
        // the tile tints its icon: only the cup, or the pill's "ON" would vanish into it
        @Override public void setTintList(android.content.res.ColorStateList t) { icon.setTintList(t); }
        @Override public int getOpacity() { return android.graphics.PixelFormat.TRANSLUCENT; }
    }

    private void refreshKeepAwake() {
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null || btnKeepAwake == null) return;
        new Thread(() -> {
            String res = NetworkUtils.httpGetWithAuth(active.getBaseUrl() + "/api/keep_awake", active.authToken, 3000);
            if (res == null) return;
            try {
                boolean on = new JSONObject(res).optBoolean("enabled", false);
                runOnUiThread(() -> showKeepAwake(on));
            } catch (Exception ignored) {}
        }).start();
    }

    private void confirmPower(String action, String question, String verb, String doneMsg) {
        PairedDevice active = DeviceManager.getActiveDevice(this);
        new AlertDialog.Builder(this)
                .setTitle(question)
                .setMessage("Unsaved work on " + (active != null ? active.hostname : "the computer") + " may be lost.")
                .setPositiveButton(verb, (d, w) -> postAction(action, doneMsg))
                .setNegativeButton("Cancel", null)
                .show();
    }

    private void postAction(String action, String doneMsg) {
        postToComputer("/api/action", "{\"action\":\"" + action + "\"}", doneMsg);
    }

    private void postToComputer(String path, String json, String doneMsg) {
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null) return;
        new Thread(() -> {
            String res = NetworkUtils.httpPostJsonWithAuth(active.getBaseUrl() + path, active.authToken, json, 4000);
            String reason = responseError(res);
            runOnUiThread(() -> Toast.makeText(MainActivity.this,
                    reason == null ? doneMsg : reason, Toast.LENGTH_SHORT).show());
        }).start();
    }

    /** null when the computer answered {"status":"ok"}, otherwise a message to show. */
    private static String responseError(String res) {
        if (res == null) return "Couldn't reach the computer";
        try {
            JSONObject obj = new JSONObject(res);
            if ("ok".equals(obj.optString("status"))) return null;
            String msg = obj.optString("message", "");
            return msg.isEmpty() ? "The computer refused that" : msg;
        } catch (Exception e) {
            return "Unexpected reply from the computer";
        }
    }

    private static final int[] APP_TINT_BG = {R.drawable.bg_tint_sky, R.drawable.bg_tint_violet, R.drawable.bg_tint_mint,
            R.drawable.bg_tint_amber, R.drawable.bg_tint_pink, R.drawable.bg_tint_coral};
    private static final int[] APP_TINT_FG = {R.color.o_sky, R.color.o_violet, R.color.o_mint,
            R.color.o_amber, R.color.o_pink, R.color.o_coral};

    private static String formatMb(double mb) {
        return mb >= 1024 ? String.format(Locale.US, "%.1f GB", mb / 1024) : String.format(Locale.US, "%.0f MB", mb);
    }

    private void showTaskManager() {
        View content = LayoutInflater.from(this).inflate(R.layout.page_task_manager, null);
        Dialog page = createPage(content, false);
        TextView summary = content.findViewById(R.id.tv_tm_summary);
        LinearLayout list = content.findViewById(R.id.container_tm_apps);
        Handler handler = new Handler(Looper.getMainLooper());
        Runnable[] tick = new Runnable[1];
        tick[0] = () -> {
            loadRunningApps(summary, list);
            handler.postDelayed(tick[0], 4000);
        };
        page.setOnDismissListener(d -> handler.removeCallbacksAndMessages(null));
        content.findViewById(R.id.btn_tm_refresh).setOnClickListener(v -> loadRunningApps(summary, list));
        content.findViewById(R.id.btn_tm_restart_desktop).setOnClickListener(v -> new AlertDialog.Builder(this)
                .setTitle("Restart the desktop?")
                .setMessage("Restarts the panel and desktop shell if they have frozen. Open windows stay open.")
                .setPositiveButton("Restart", (d, w) -> postAction("restart_desktop", "Restarting the desktop…"))
                .setNegativeButton("Cancel", null)
                .show());
        page.show();
        tick[0].run();
    }

    private void loadRunningApps(TextView summary, LinearLayout list) {
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null) return;
        new Thread(() -> {
            String res = NetworkUtils.httpGetWithAuth(active.getBaseUrl() + "/api/processes", active.authToken, 5000);
            runOnUiThread(() -> {
                if (res == null) {
                    summary.setText("Couldn't reach the computer");
                    return;
                }
                try {
                    JSONObject obj = new JSONObject(res);
                    JSONArray apps = obj.optJSONArray("apps");
                    double total = obj.optDouble("memory_total_mb", 0), free = obj.optDouble("memory_available_mb", 0);
                    summary.setText((apps != null ? apps.length() : 0) + " programs · " + formatMb(total - free) + " of " + formatMb(total) + " memory in use");
                    list.removeAllViews();
                    if (apps == null) return;
                    LayoutInflater inflater = LayoutInflater.from(this);
                    for (int i = 0; i < apps.length(); i++) {
                        JSONObject a = apps.getJSONObject(i);
                        String name = a.optString("name", "?");
                        JSONArray pidArr = a.optJSONArray("pids");
                        int count = pidArr != null ? pidArr.length() : 0;
                        double cpu = a.optDouble("cpu", 0), mem = a.optDouble("memory_mb", 0);
                        boolean stuck = a.optInt("stuck", 0) > 0;

                        View row = inflater.inflate(R.layout.item_app, list, false);
                        TextView letter = row.findViewById(R.id.tv_app_letter);
                        int tint = Math.abs(name.hashCode()) % APP_TINT_BG.length;
                        letter.setText(name.isEmpty() ? "?" : name.substring(0, 1).toUpperCase(Locale.US));
                        letter.setBackgroundResource(APP_TINT_BG[tint]);
                        letter.setTextColor(getColor(APP_TINT_FG[tint]));
                        ((TextView) row.findViewById(R.id.tv_app_name)).setText(name);
                        TextView details = row.findViewById(R.id.tv_app_details);
                        details.setText(formatMb(mem) + " · " + count + (count == 1 ? " process" : " processes") + (stuck ? " · not responding" : ""));
                        if (stuck) details.setTextColor(getColor(R.color.o_danger));
                        TextView cpuView = row.findViewById(R.id.tv_app_cpu);
                        cpuView.setText(String.format(Locale.US, "%.1f%%", cpu));
                        cpuView.setTextColor(getColor(cpu >= 50 ? R.color.o_coral : cpu >= 15 ? R.color.o_amber : R.color.o_text_2));
                        row.setOnClickListener(v -> confirmEndApp(name, pidArr, count, mem, summary, list));
                        list.addView(row);
                    }
                } catch (Exception e) {
                    summary.setText("Couldn't read the app list");
                }
            });
        }).start();
    }

    private void confirmEndApp(String name, JSONArray pids, int count, double mem, TextView summary, LinearLayout list) {
        boolean core = name.equals("kwin_wayland") || name.equals("plasmashell") || name.equals("Xwayland");
        String msg = count + (count == 1 ? " process" : " processes") + " · " + formatMb(mem)
                + (core ? "\n\nThis is part of your desktop. Ending it may close your session." : "");
        new AlertDialog.Builder(this)
                .setTitle("End " + name + "?")
                .setMessage(msg)
                .setPositiveButton("End", (d, w) -> endApp(pids, false, summary, list))
                .setNeutralButton("Force quit", (d, w) -> endApp(pids, true, summary, list))
                .setNegativeButton("Cancel", null)
                .show();
    }

    private void endApp(JSONArray pids, boolean force, TextView summary, LinearLayout list) {
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null || pids == null) return;
        new Thread(() -> {
            String res = null;
            try {
                JSONObject req = new JSONObject();
                req.put("pids", pids);
                req.put("force", force);
                res = NetworkUtils.httpPostJsonWithAuth(active.getBaseUrl() + "/api/processes/kill", active.authToken, req.toString(), 4000);
            } catch (Exception ignored) {}
            final boolean ok = responseError(res) == null;
            runOnUiThread(() -> {
                Toast.makeText(MainActivity.this, ok ? (force ? "Force quit sent" : "Asked the app to close") : "Couldn't end the app", Toast.LENGTH_SHORT).show();
                summary.postDelayed(() -> loadRunningApps(summary, list), 800);
            });
        }).start();
    }

    private volatile String appliedPaletteKey = null;

    /** Parse the computer's terminal colours and hand them to the colouriser (any thread).
     *  Returns {fg, bg} when they changed, so the UI can recolour the screen; else null. */
    private int[] prepareTerminalPalette(JSONObject palette) {
        if (palette == null) return null;
        String key = palette.toString();
        if (key.equals(appliedPaletteKey)) return null;
        try {
            JSONArray normal = palette.getJSONArray("normal"), bright = palette.getJSONArray("bright");
            int[] n = new int[8], b = new int[8];
            for (int i = 0; i < 8; i++) {
                n[i] = Color.parseColor(normal.getString(i));
                b[i] = Color.parseColor(bright.getString(i));
            }
            int fg = Color.parseColor(palette.getString("fg")), bg = Color.parseColor(palette.getString("bg"));
            TerminalColorizer.setPalette(fg, bg, n, b);
            appliedPaletteKey = key;
            return new int[]{fg, bg};
        } catch (Exception e) {
            return null;
        }
    }

    private void applyTerminalPaletteViews(int[] colors) {
        if (colors == null) return;
        if (scrollTermScreen != null) {
            android.graphics.drawable.GradientDrawable screen = new android.graphics.drawable.GradientDrawable();
            screen.setColor(colors[1]);
            screen.setCornerRadius(dpToPx(18));
            screen.setStroke(dpToPx(1), getColor(R.color.o_raised));
            scrollTermScreen.setBackground(screen);
        }
        if (tvTermScreen != null) tvTermScreen.setTextColor(colors[0]);
    }

    private Button btnKeyWrap;
    // Landscape: the keys move into the input row so the screen keeps most of the height
    private boolean termKeysInInputRow = false;
    private final java.util.List<Object[]> termKeyHomes = new java.util.ArrayList<>();   // {key, parent, index, params}

    private void placeTerminalKeys(boolean inInputRow) {
        if (inInputRow == termKeysInInputRow) return;
        LinearLayout row = findViewById(R.id.layout_term_input_row);
        View panel = findViewById(R.id.layout_term_keys);
        if (row == null || panel == null) return;
        if (inInputRow) {
            Button[] keys = {btnKeyEsc, btnKeyTab, btnKeyCtrl, btnKeyLeft, btnKeyUp, btnKeyDown, btnKeyRight};
            termKeyHomes.clear();
            int at = 0;
            for (Button key : keys) {
                if (key == null || !(key.getParent() instanceof ViewGroup)) continue;
                ViewGroup home = (ViewGroup) key.getParent();
                termKeyHomes.add(new Object[]{key, home, home.indexOfChild(key), key.getLayoutParams()});
                home.removeView(key);
                LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dpToPx(40));
                lp.setMarginEnd(dpToPx(4));
                key.setMinWidth(dpToPx(40));
                row.addView(key, at++, lp);
            }
            panel.setVisibility(View.GONE);
        } else {
            // put each key back where it was, lowest index first so positions line up
            termKeyHomes.sort((a, b) -> Integer.compare((int) a[2], (int) b[2]));
            for (Object[] h : termKeyHomes) {
                View key = (View) h[0];
                row.removeView(key);
                ViewGroup home = (ViewGroup) h[1];
                home.addView(key, Math.min((int) h[2], home.getChildCount()), (ViewGroup.LayoutParams) h[3]);
            }
            termKeyHomes.clear();
            panel.setVisibility(View.VISIBLE);
        }
        termKeysInInputRow = inInputRow;
    }

    private volatile boolean terminalSelectionMode = false;
    private float lastTermTouchX, lastTermTouchY;
    private View.OnLongClickListener termLongClick;

    private void setTerminalSelectionMode(boolean on) {
        if (tvTermScreen == null || on == terminalSelectionMode) return;
        terminalSelectionMode = on;
        // A plain copy: the pre-measured text the terminal normally shows can't be made
        // selectable (Android throws), which used to crash here
        CharSequence current = new android.text.SpannableString(tvTermScreen.getText());
        tvTermScreen.setTextIsSelectable(on);
        tvTermScreen.setText(current, on ? TextView.BufferType.SPANNABLE : TextView.BufferType.NORMAL);
        if (!on) {
            // selectable text takes focus; hand it back so the input box gets the keyboard again
            tvTermScreen.setFocusable(false);
            tvTermScreen.setFocusableInTouchMode(false);
            tvTermScreen.clearFocus();
        }
        if (tvInteractiveStatus != null) {
            tvInteractiveStatus.setText(on ? "Paused · tap to resume" : "Live");
            tvInteractiveStatus.setTextColor(getColor(on ? R.color.o_remote : R.color.o_text_2));
        }
        if (!on) {
            lastLoadedTerminalText = "";        // show the latest output right away
            lastTerminalVersion = ""; shownTermLines = null;
            triggerTerminalImmediatePoll();
        }
    }

    private int earlierLinesAvailable = 0;

    /** "Load earlier" floats over the top of the terminal, only when you've scrolled to the top
     *  and there's more history; otherwise it stays out of the way. */
    private void updateLoadEarlierPill() {
        if (btnTermLoadEarlier == null || scrollTermScreen == null) return;
        boolean atTop = scrollTermScreen.getScrollY() < dpToPx(48);
        boolean show = earlierLinesAvailable > 0 && atTop && !isLoadingEarlier;
        if (show) {
            btnTermLoadEarlier.setText("Load earlier · " + earlierLinesAvailable + " lines");
            btnTermLoadEarlier.setEnabled(true);
        }
        btnTermLoadEarlier.setVisibility(show ? View.VISIBLE : View.GONE);
    }

    private void updateWrapKey(Button key) {
        // lit while wrapping is on
        key.setCompoundDrawableTintList(android.content.res.ColorStateList.valueOf(
                getColor(isTerminalWrapEnabled ? R.color.o_accent : R.color.o_text_2)));
    }

    private void toggleTerminalWrap() {
        isTerminalWrapEnabled = !isTerminalWrapEnabled;
        getSharedPreferences("pc_auth_prefs", MODE_PRIVATE).edit().putBoolean("terminal_wrap", isTerminalWrapEnabled).apply();
        if (btnKeyWrap != null) updateWrapKey(btnKeyWrap);
        applyTerminalWrapMode();
        fitTerminalText();
        if (lastLoadedTerminalText != null && !lastLoadedTerminalText.isEmpty()) {
            renderColorizedTerminalText(lastLoadedTerminalText);
        }
        vibrate(15);
    }


    /** Unwrapped view: scale the text so the computer's terminal width fits the screen, like
     *  zooming out on a wide terminal (never below a readable size; scroll sideways beyond that). */
    private void fitTerminalText() {
        if (tvTermScreen == null || scrollTermScreen == null) return;
        float sp = getResources().getDisplayMetrics().scaledDensity;
        float target = 12 * sp;
        if (!isTerminalWrapEnabled && terminalColumns > 0 && scrollTermScreen.getWidth() > 0) {
            float avail = scrollTermScreen.getWidth() - tvTermScreen.getPaddingLeft() - tvTermScreen.getPaddingRight();
            android.graphics.Paint probe = new android.graphics.Paint(tvTermScreen.getPaint());
            probe.setTextSize(100f);
            float perPx = probe.measureText("M") / 100f;           // monospace: every cell is this wide
            target = Math.max(7 * sp, Math.min(13 * sp, avail / (terminalColumns * perPx)));
        }
        if (Math.abs(tvTermScreen.getTextSize() - target) > 0.3f) {
            tvTermScreen.setTextSize(android.util.TypedValue.COMPLEX_UNIT_PX, target);
        }
    }

    private VolumePillView vpMediaVolume;

    private void setMediaVolume(int percent) {
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null) return;
        terminalSendExecutor.execute(() -> NetworkUtils.httpPostJsonWithAuth(active.getBaseUrl() + "/api/media/volume",
                active.authToken, "{\"percent\":" + percent + "}", 3000));
    }

    private void showMediaVolume(int percent) {
        if (vpMediaVolume != null && percent >= 0) vpMediaVolume.setPercent(percent);
    }

    private void showLockState(boolean locked) {
        showLockState(locked, true);
    }

    /** At the login screen nobody is logged in: neither locked nor unlocked, nothing to lock. */
    private void showLockState(boolean locked, boolean loggedIn) {
        if (tvPcLockBadge != null) {
            tvPcLockBadge.setText(!loggedIn ? "Not logged in" : locked ? "Locked" : "Unlocked");
            setStartIcon(tvPcLockBadge, locked || !loggedIn ? R.drawable.ic_o_lock : R.drawable.ic_o_unlock, getColor(R.color.o_text_2), 14);
        }
        if (btnLockLaptopAction != null) {
            btnLockLaptopAction.setEnabled(loggedIn);
            btnLockLaptopAction.setAlpha(loggedIn ? 1f : 0.4f);
            btnLockLaptopAction.setText(locked ? "Unlock" : "Lock");
            setStartIcon(btnLockLaptopAction, locked ? R.drawable.ic_o_unlock : R.drawable.ic_o_lock, getColor(R.color.o_on_accent), 18);
        }
    }

    private void setButtonPrimary(Button b, boolean primary) {
        b.setBackgroundResource(primary ? R.drawable.btn_approve : R.drawable.btn_tonal);
        b.setTextColor(getColor(primary ? R.color.o_on_accent : R.color.o_text));
    }

    private String getTerminalBaseUrl(PairedDevice active) {
        if (active == null) return null;
        if (!DeviceManager.isWifiActive(this)) {
            return null;
        }
        return active.getLocalUrl();
    }

    // Viewing works over the internet tunnel too; typing stays LAN-only (the PC enforces this as well)
    private String getTerminalReadUrl(PairedDevice active) {
        if (active == null) return null;
        if (DeviceManager.isWifiActive(this)) return active.getLocalUrl();
        String url = active.getBaseUrl();
        String internet = active.getInternetUrl();
        if (!url.startsWith("https://") && internet != null) url = internet;
        return url;
    }

    private void setTerminalInputEnabled(boolean enabled) {
        if (enabled == isTerminalInputEnabled) return;
        isTerminalInputEnabled = enabled;
        View[] inputs = {etTermInput, btnTermSend, btnKeyMode, btnKeyCtrl, btnKeyEsc, btnKeyTab,
                btnKeyLeft, btnKeyRight, btnKeyUp, btnKeyDown, btnKeyClear};
        for (View v : inputs) {
            if (v == null) continue;
            v.setEnabled(enabled);
            v.setAlpha(enabled ? 1f : 0.4f);
        }
        if (etTermInput != null) {
            if (enabled) {
                updateLiveModeUI();
            } else {
                etTermInput.setHint("View only");
            }
        }
    }

    private void loadTerminalsList() {
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null) {
            Toast.makeText(this, "No active computer connected", Toast.LENGTH_SHORT).show();
            return;
        }
        String baseUrl = getTerminalReadUrl(active);
        if (baseUrl == null) {
            Toast.makeText(this, "computer is not reachable.", Toast.LENGTH_SHORT).show();
            if (containerTerminalsList != null) containerTerminalsList.removeAllViews();
            if (layoutTerminalsEmpty != null) layoutTerminalsEmpty.setVisibility(View.VISIBLE);
            return;
        }

        new Thread(() -> {
            try {
                String url = baseUrl + "/api/terminals/list";
                String res = NetworkUtils.httpGetWithAuth(url, active.authToken, 4000);
                if (res != null) {
                    JSONObject obj = new JSONObject(res);
                    if ("ok".equals(obj.optString("status"))) {
                        JSONArray terms = obj.optJSONArray("terminals");
                        runOnUiThread(() -> renderTerminalsList(terms));
                        return;
                    }
                }
                runOnUiThread(() -> {
                    if (containerTerminalsList != null) containerTerminalsList.removeAllViews();
                    if (layoutTerminalsEmpty != null) layoutTerminalsEmpty.setVisibility(View.VISIBLE);
                });
            } catch (Exception e) {
                runOnUiThread(() -> {
                    Toast.makeText(MainActivity.this, "Could not fetch terminals: " + e.getMessage(), Toast.LENGTH_SHORT).show();
                });
            }
        }).start();
    }

    private void renderTerminalsList(JSONArray terms) {
        if (terms == null || terms.length() == 0) {
            if (containerTerminalsList != null) containerTerminalsList.removeAllViews();
            if (layoutTermChips != null) layoutTermChips.removeAllViews();
            if (layoutTerminalsEmpty != null) layoutTerminalsEmpty.setVisibility(View.VISIBLE);
            return;
        }

        if (layoutTerminalsEmpty != null) layoutTerminalsEmpty.setVisibility(View.GONE);
        if (containerTerminalsList != null) containerTerminalsList.removeAllViews();
        if (layoutTermChips != null) layoutTermChips.removeAllViews();

        LayoutInflater inflater = LayoutInflater.from(this);

        for (int i = 0; i < terms.length(); i++) {
            JSONObject t = terms.optJSONObject(i);
            if (t == null) continue;

            final String id = t.optString("id");
            final String title = t.optString("title", "Terminal " + (i + 1));
            final String command = t.optString("command", "shell");
            final String cwd = t.optString("cwd", "");
            final String tty = t.optString("tty", "");
            final int pid = t.optInt("pid", 0);
            final String preview = t.optString("preview", "");
            // Tabs that record their output (or run in tmux) have their full scrollback
            final boolean fullHistory = t.optBoolean("recorded", false) || !t.optString("tmux", "").isEmpty();

            // 2. Add Terminal Card to container
            View itemView = inflater.inflate(R.layout.item_terminal, containerTerminalsList, false);
            TextView tvTitle = itemView.findViewById(R.id.tv_term_title);
            TextView tvBadge = itemView.findViewById(R.id.tv_term_badge);
            TextView tvCwd = itemView.findViewById(R.id.tv_term_cwd);
            TextView tvPrev = itemView.findViewById(R.id.tv_term_preview);
            Button btnOpen = itemView.findViewById(R.id.btn_term_open);

            if (tvTitle != null) tvTitle.setText(title);
            if (tvBadge != null) tvBadge.setText(command + (fullHistory ? " · full history" : " · recent screens only"));
            if (tvCwd != null) {
                if (cwd.isEmpty()) {
                    tvCwd.setVisibility(View.GONE);
                } else {
                    tvCwd.setVisibility(View.VISIBLE);
                    tvCwd.setText(cwd.replaceFirst("^/home/[^/]+", "~"));
                }
            }
            if (tvPrev != null) {
                if (preview.isEmpty()) {
                    tvPrev.setVisibility(View.GONE);
                } else {
                    tvPrev.setVisibility(View.VISIBLE);
                    tvPrev.setText(preview);
                }
            }
            if (btnOpen != null) {
                btnOpen.setOnClickListener(v -> openTerminalInteractive(id, title, cwd));
            }

            containerTerminalsList.addView(itemView);
        }
    }

    private void openTerminalInteractive(String termId, String title, String cwd) {
        if (activeTerminalId != null && !activeTerminalId.equals(termId)) saveTermSnapshot();
        newTermPollGeneration();
        activeTerminalId = termId;
        activeTerminalTitle = title;
        activeTerminalCwd = cwd;
        currentTerminalLinesToFetch = 300;
        hasMoreTerminalHistory = false;
        lastTerminalVersion = ""; shownTermLines = null;
        isLoadingEarlier = false;
        lastLoadedTerminalText = "";
        preserveDistanceFromBottom = -1;
        isFirstTerminalLoad = true;

        if (btnTermLoadEarlier != null) {
            btnTermLoadEarlier.setVisibility(View.GONE);
            btnTermLoadEarlier.setEnabled(true);
        }
        if (layoutTerminalsListHeader != null) layoutTerminalsListHeader.setVisibility(View.GONE);
        if (scrollTermChips != null) scrollTermChips.setVisibility(View.GONE);
        if (scrollTermList != null) scrollTermList.setVisibility(View.GONE);
        if (layoutTerminalInteractive != null) layoutTerminalInteractive.setVisibility(View.VISIBLE);

        applyOrientationLayout(getResources().getConfiguration().orientation);
        applyTerminalWrapMode();

        if (tvInteractiveTitle != null) tvInteractiveTitle.setText(title);
        if (tvInteractiveCwd != null) {
            tvInteractiveCwd.setVisibility(cwd.isEmpty() ? View.GONE : View.VISIBLE);
            tvInteractiveCwd.setText(cwd.replaceFirst("^/home/[^/]+", "~"));
        }
        stopBackgroundTermSync(termId);       // the live view takes over
        Boolean wasFullscreen = fullscreenById.get(termId);
        setTerminalFullscreen(wasFullscreen != null && wasFullscreen);
        TermSnapshot snap;
        synchronized (termSnapshots) { snap = termSnapshots.get(termId); }
        if (snap != null && snap.wrap == isTerminalWrapEnabled && tvTermScreen != null) {
            // as it was when you left; the next read brings only what changed since
            tvTermScreen.setText(snap.colored);
            lastTerminalVersion = snap.version;
            shownTermLines = snap.lines;
            shownTermColored = snap.colored;
            lastLoadedTerminalText = snap.text;
            currentTerminalLinesToFetch = snap.linesToFetch;
            isFirstTerminalLoad = false;
            if (scrollTermScreen != null) scrollTermScreen.post(() -> scrollTermScreen.fullScroll(View.FOCUS_DOWN));
        } else {
            renderColorizedTerminalText("Connecting to " + title + "…");
            if (scrollTermScreen != null) scrollTermScreen.scrollTo(0, 0);
        }

        isInternalTextUpdate = true;
        if (etTermInput != null) etTermInput.setText("");
        isInternalTextUpdate = false;
        termReplaceCount = 0;
        updateLiveModeUI();

        pollActiveTerminalOutput();
        startTerminalStreamTimer();
    }

    private void loadEarlierTerminalHistory() {
        if (!hasMoreTerminalHistory || isLoadingEarlier) return;
        newTermPollGeneration();          // don't wait for a held read to come back
        isLoadingEarlier = true;
        if (tvTermScreen != null && scrollTermScreen != null) {
            preserveDistanceFromBottom = Math.max(0, tvTermScreen.getHeight() - scrollTermScreen.getScrollY());
        } else {
            preserveDistanceFromBottom = -1;
        }
        currentTerminalLinesToFetch += 1000;
        lastTerminalVersion = ""; shownTermLines = null;
        if (btnTermLoadEarlier != null) {
            btnTermLoadEarlier.setText("Loading earlier output…");
            btnTermLoadEarlier.setEnabled(false);
        }
        pollActiveTerminalOutput();
    }

    private void toggleTerminalOrientation() {
        int currentOrientation = getResources().getConfiguration().orientation;
        if (currentOrientation == Configuration.ORIENTATION_PORTRAIT) {
            setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE);
            applyOrientationLayout(Configuration.ORIENTATION_LANDSCAPE);
        } else {
            setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_PORTRAIT);
            applyOrientationLayout(Configuration.ORIENTATION_PORTRAIT);
        }
    }

    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        applyOrientationLayout(newConfig.orientation);
    }

    private void applyOrientationLayout(int orientation) {
        boolean isLandscape = (orientation == Configuration.ORIENTATION_LANDSCAPE);
        if (btnInteractiveRotate != null) {
        }

        boolean isInteractiveActive = (activeTerminalId != null && layoutTerminalInteractive != null && layoutTerminalInteractive.getVisibility() == View.VISIBLE);

        // While a terminal is open, give the screen to it: no tab bar (back returns to the list)
        if (layoutTopTabs != null) {
            if (isInteractiveActive) {
                layoutTopTabs.setVisibility(View.GONE);
            } else {
                layoutTopTabs.setVisibility(View.VISIBLE);
            }
        }

        placeTerminalKeys(isLandscape && isInteractiveActive);
        if (tvInteractiveCwd != null && isInteractiveActive) {
            tvInteractiveCwd.setVisibility(isLandscape || activeTerminalCwd == null || activeTerminalCwd.isEmpty() ? View.GONE : View.VISIBLE);
        }

        // Adjust padding on panelTerminals for landscape vs portrait
        if (panelTerminals != null) {
            if (isInteractiveActive) {
                int padH = dpToPx(isLandscape ? 8 : 6);
                int padV = dpToPx(4);
                panelTerminals.setPadding(padH, padV, padH, padV);
            } else {
                int pad = dpToPx(12);
                panelTerminals.setPadding(pad, pad, pad, pad);
            }
        }

        applyTerminalWrapMode();
        if (tvTermScreen != null && lastLoadedTerminalText != null && !lastLoadedTerminalText.isEmpty()) {
            renderColorizedTerminalText(lastLoadedTerminalText);
        }
    }

    private int dpToPx(int dp) {
        return (int) (dp * getResources().getDisplayMetrics().density);
    }

    private void closeTerminalInteractive() {
        newTermPollGeneration();
        saveTermSnapshot();
        lastClosedTerminalId = activeTerminalId;
        startBackgroundTermSync(activeTerminalId);
        dismissTermEditTip(false);
        stopTerminalStreamTimer();
        if (terminalSelectionMode && tvTermScreen != null) {
            terminalSelectionMode = false;
            tvTermScreen.setTextIsSelectable(false);
            if (tvInteractiveStatus != null) {
                tvInteractiveStatus.setText("Live");
                tvInteractiveStatus.setTextColor(getColor(R.color.o_text_2));
            }
        }
        lastTerminalVersion = ""; shownTermLines = null;
        activeTerminalId = null;
        lastLoadedTerminalText = "";
        preserveDistanceFromBottom = -1;
        isFirstTerminalLoad = true;
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED);
        if (layoutTerminalInteractive != null) layoutTerminalInteractive.setVisibility(View.GONE);
        if (layoutTerminalsListHeader != null) layoutTerminalsListHeader.setVisibility(View.VISIBLE);
        if (scrollTermChips != null) scrollTermChips.setVisibility(View.GONE);
        if (scrollTermList != null) scrollTermList.setVisibility(View.VISIBLE);
        if (layoutTopTabs != null) layoutTopTabs.setVisibility(View.VISIBLE);
        isCtrlActive = false;
        updateCtrlButtonUI();
        applyOrientationLayout(getResources().getConfiguration().orientation);
        loadTerminalsList();
    }

    private void updateLiveModeUI() {
        if (btnKeyMode != null) {
            if (isLiveTypeEnabled) {
                btnKeyMode.setText("live");
                btnKeyMode.setTextColor(getColor(R.color.o_accent));
            } else {
                btnKeyMode.setText("buffer");
                btnKeyMode.setTextColor(getColor(R.color.o_text_3));
            }
        }
        if (etTermInput != null) {
            etTermInput.setHint("");
        }
    }

    private void updateCtrlButtonUI() {
        if (btnKeyCtrl == null) return;
        // a background change resets padding: keep the key's own
        int l = btnKeyCtrl.getPaddingLeft(), t = btnKeyCtrl.getPaddingTop(), r = btnKeyCtrl.getPaddingRight(), b = btnKeyCtrl.getPaddingBottom();
        btnKeyCtrl.setBackgroundResource(isCtrlActive ? R.drawable.bg_key_on : R.drawable.card_bg);
        btnKeyCtrl.setTextColor(getColor(isCtrlActive ? R.color.o_on_accent : R.color.o_text));
        btnKeyCtrl.setTypeface(btnKeyCtrl.getTypeface(), isCtrlActive ? android.graphics.Typeface.BOLD : android.graphics.Typeface.NORMAL);
        btnKeyCtrl.setPadding(l, t, r, b);
    }

    private void applyTerminalWrapMode() {
        if (tvTermScreen == null) return;
        if (isTerminalWrapEnabled) {
            int viewportWidth = 0;
            if (scrollTermScreen != null && scrollTermScreen.getWidth() > 0) {
                viewportWidth = scrollTermScreen.getWidth();
            } else if (hscrollTermScreen != null && hscrollTermScreen.getWidth() > 0) {
                viewportWidth = hscrollTermScreen.getWidth();
            } else {
                int screenW = getResources().getDisplayMetrics().widthPixels;
                int pad = dpToPx(24);
                viewportWidth = Math.max(200, screenW - pad);
            }

            ViewGroup.LayoutParams lp = tvTermScreen.getLayoutParams();
            if (lp != null) {
                lp.width = viewportWidth;
                tvTermScreen.setLayoutParams(lp);
            }
            tvTermScreen.setHorizontallyScrolling(false);
            if (hscrollTermScreen != null) {
                hscrollTermScreen.scrollTo(0, 0);
            }
        } else {
            ViewGroup.LayoutParams lp = tvTermScreen.getLayoutParams();
            if (lp != null) {
                lp.width = ViewGroup.LayoutParams.WRAP_CONTENT;
                tvTermScreen.setLayoutParams(lp);
            }
            tvTermScreen.setHorizontallyScrolling(true);
        }
    }

    private int getTerminalTargetColumns() {
        if (tvTermScreen == null) return 45;
        int widthPx = 0;
        if (scrollTermScreen != null && scrollTermScreen.getWidth() > 0) {
            widthPx = scrollTermScreen.getWidth();
        } else {
            widthPx = getResources().getDisplayMetrics().widthPixels;
        }
        float charW = tvTermScreen.getPaint().measureText("─");
        if (charW <= 0) charW = 18f;
        int pad = dpToPx(24);
        return Math.max(30, (int) ((widthPx - pad) / charW));
    }

    private void renderColorizedTerminalText(String text) {
        if (tvTermScreen == null) return;
        if (text == null || text.isEmpty()) {
            tvTermScreen.setText("(Empty terminal)");
            return;
        }
        int targetCols = getTerminalTargetColumns();
        cachedTerminalCols = targetCols;
        CharSequence colorized = TerminalColorizer.colorize(text, isTerminalWrapEnabled, targetCols);
        tvTermScreen.setText(colorized, terminalSelectionMode ? TextView.BufferType.SPANNABLE : TextView.BufferType.NORMAL);
        lastTerminalVersion = ""; shownTermLines = null; shownTermColored = null;   // next update in full
    }

    private void startTerminalStreamTimer() {
        stopTerminalStreamTimer();
        terminalPollRunnable = new Runnable() {
            @Override
            public void run() {
                if (activeTerminalId != null && layoutTerminalInteractive != null && layoutTerminalInteractive.getVisibility() == View.VISIBLE) {
                    if (termServerWaits) {
                        // one held request at a time; the next goes out as soon as it returns
                        if (!isPollingTerminal) pollActiveTerminalOutput();
                        // at most ~3 redraws a second even while an app animates (a spinner)
                        terminalHandler.postDelayed(this, 300);
                        return;
                    }
                    pollActiveTerminalOutput();
                    // Poll gently when view-only over the internet tunnel
                    int idle = unchangedTerminalPolls;
                    int delay = !isTerminalInputEnabled ? TERMINAL_REMOTE_INTERVAL_MS
                            : idle < 6 ? TERMINAL_STREAM_INTERVAL_MS : idle < 30 ? 800 : TERMINAL_REMOTE_INTERVAL_MS;
                    terminalHandler.postDelayed(this, delay);
                }
            }
        };
        terminalHandler.postDelayed(terminalPollRunnable, TERMINAL_STREAM_INTERVAL_MS);
    }

    private void stopTerminalStreamTimer() {
        if (terminalPollRunnable != null) {
            terminalHandler.removeCallbacks(terminalPollRunnable);
            terminalPollRunnable = null;
        }
    }

    private final Runnable typingPoll = this::pollActiveTerminalOutput;

    private void triggerTerminalImmediatePoll() {
        unchangedTerminalPolls = 0;   // you just typed: watch closely again
        // One refresh ~0.1 s after typing settles (each keystroke restarts the wait) instead of
        // several per keystroke: the echo shows up promptly without redrawing on every letter.
        terminalHandler.removeCallbacks(typingPoll);
        terminalHandler.postDelayed(typingPoll, 110);
    }

    private void pollActiveTerminalOutput() {
        if (activeTerminalId == null) return;
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null) return;
        String baseUrl = getTerminalReadUrl(active);
        if (baseUrl == null) return;

        if (isPollingTerminal) {
            hasPendingTerminalPoll = true;
            return;
        }
        isPollingTerminal = true;
        final int gen = termPollGen.get();

        final String sinceVersion = lastTerminalVersion;
        final java.util.List<String> shown = shownTermLines;
        final CharSequence shownColored = shownTermColored;
        final String termId = activeTerminalId;
        final boolean hold = termServerWaits && !sinceVersion.isEmpty();
        final Object metrics = (android.os.Build.VERSION.SDK_INT >= 28 && tvTermScreen != null)
                ? tvTermScreen.getTextMetricsParams() : null;
        terminalPollExecutor.execute(() -> {
            try {
                String encodedId = java.net.URLEncoder.encode(termId, "UTF-8");
                String url = baseUrl + "/api/terminals/read?id=" + encodedId + "&lines=" + currentTerminalLinesToFetch
                        + (sinceVersion.isEmpty() ? "" : "&v=" + sinceVersion)
                        + (shown != null && shownColored != null && !sinceVersion.isEmpty() ? "&delta=1" : "")
                        + (hold ? "&wait=1" : "")
                        + (isTerminalWrapEnabled ? "&join=1" : "");      // rejoin wrapped lines; the phone wraps them
                String res = NetworkUtils.httpGetWithAuth(url, active.authToken, hold ? 30000 : 3000);
                if (gen != termPollGen.get() || !termId.equals(activeTerminalId)) return;   // superseded meanwhile
                if (res != null) {
                    JSONObject obj = new JSONObject(res);
                    termServerWaits = obj.optBoolean("wait", false);
                    if ("ok".equals(obj.optString("status")) && obj.optBoolean("unchanged", false)) {
                        // Same screen as we show: nothing to download, parse or draw
                        unchangedTerminalPolls++;
                        final boolean stillCanWrite = obj.optBoolean("can_write", true);
                        runOnUiThread(() -> setTerminalInputEnabled(stillCanWrite));
                    } else if ("ok".equals(obj.optString("status"))) {
                        unchangedTerminalPolls = 0;
                        final String newVersion = obj.optString("version", "");
                        // Either the whole text, or just what changed since the lines we show
                        JSONObject delta = obj.optJSONObject("delta");
                        final String text;
                        final java.util.List<String> newLines;
                        final CharSequence coloredTail;
                        final int dDrop, dKeep, dTailCount;
                        if (delta != null) {
                            if (shown == null || shownColored == null || !sinceVersion.equals(delta.optString("base"))
                                    || shown.size() != delta.optInt("old_count", -1)) {
                                lastTerminalVersion = ""; shownTermLines = null;   // out of step: get it all
                                hasPendingTerminalPoll = true;
                                return;
                            }
                            dDrop = delta.optInt("drop", 0);
                            dKeep = delta.optInt("keep", 0);
                            org.json.JSONArray tailArr = delta.optJSONArray("tail");
                            java.util.List<String> tail = new java.util.ArrayList<>();
                            if (tailArr != null) for (int i = 0; i < tailArr.length(); i++) tail.add(tailArr.optString(i, ""));
                            if (dDrop + dKeep > shown.size()) {
                                lastTerminalVersion = ""; shownTermLines = null;
                                hasPendingTerminalPoll = true;
                                return;
                            }
                            java.util.List<String> nl = new java.util.ArrayList<>(shown.subList(dDrop, dDrop + dKeep));
                            nl.addAll(tail);
                            newLines = nl;
                            text = android.text.TextUtils.join("\n", nl);
                            dTailCount = tail.size();
                            coloredTail = tail.isEmpty() ? "" : TerminalColorizer.colorize(android.text.TextUtils.join("\n", tail), isTerminalWrapEnabled, cachedTerminalCols, obj.optBoolean("ansi", false));
                        } else {
                            text = obj.optString("text", "");
                            newLines = text.isEmpty() ? null : java.util.Arrays.asList(text.split("\n", -1));
                            coloredTail = null;
                            dDrop = dKeep = dTailCount = 0;
                        }
                        final boolean hasMore = obj.optBoolean("has_more", false);
                        final int total = obj.optInt("total_lines", 0);
                        final int returned = obj.optInt("returned_lines", 0);
                        final int remaining = Math.max(0, total - returned);
                        final boolean canWrite = obj.optBoolean("can_write", true);
                        final int[] paletteColors = prepareTerminalPalette(obj.optJSONObject("palette"));
                        final int columns = obj.optInt("columns", 0);
                        final boolean fullscreen = obj.optBoolean("fullscreen", false);
                        // Colour the text here, off the UI thread; the UI only swaps it in
                        CharSequence full;
                        if (coloredTail != null) {
                            full = spliceTerminalText(shownColored, dDrop, dKeep, dTailCount, coloredTail);
                            if (full == null) {          // not in step: get it all next time
                                lastTerminalVersion = ""; shownTermLines = null; shownTermColored = null;
                                hasPendingTerminalPoll = true;
                                return;
                            }
                        } else {
                            full = text.isEmpty() ? null : TerminalColorizer.colorize(text, isTerminalWrapEnabled, cachedTerminalCols, obj.optBoolean("ansi", false));
                        }
                        final CharSequence coloredFull = full;
                        // Measure the text here too, so the main thread only swaps it in
                        CharSequence measured = full;
                        if (full != null && metrics != null && android.os.Build.VERSION.SDK_INT >= 28) {
                            try {
                                measured = android.text.PrecomputedText.create(full, (android.text.PrecomputedText.Params) metrics);
                            } catch (Exception ignored) { }
                        }
                        final CharSequence colored = measured;
                        // Only tmux-backed tabs carry real scrollback; older tabs expose just the screen
                        final boolean fullHistory = obj.optBoolean("ansi", false);

                        runOnUiThread(() -> {
                            setTerminalInputEnabled(canWrite);
                            if (canWrite) maybeShowTermEditTip();
                            applyTerminalPaletteViews(paletteColors);
                            setTerminalFullscreen(fullscreen);
                            if (activeTerminalId != null) fullscreenById.put(activeTerminalId, fullscreen);
                            if (columns != terminalColumns) {
                                terminalColumns = columns;
                                fitTerminalText();
                            }
                            hasMoreTerminalHistory = hasMore;
                            isLoadingEarlier = false;

                            earlierLinesAvailable = (hasMore && remaining > 0) ? remaining : 0;
                            updateLoadEarlierPill();

                            // Selection mode: leave the text alone so a selection isn't lost
                            if (terminalSelectionMode) {
                                return;
                            }

                            // the phone now shows (or is about to show) this version
                            lastTerminalVersion = newVersion;
                            shownTermLines = newLines;
                            shownTermColored = coloredFull;
                            boolean textChanged = !text.equals(lastLoadedTerminalText);
                            if (!textChanged && preserveDistanceFromBottom < 0 && !isFirstTerminalLoad) {
                                return;
                            }
                            lastLoadedTerminalText = text;

                            final boolean firstLoad = isFirstTerminalLoad;
                            final int savedPreserveDist = preserveDistanceFromBottom;
                            preserveDistanceFromBottom = -1;

                            // Check whether user was at the bottom before text change and save scroll position
                            final boolean wasAtBottom;
                            final int savedScrollY = (scrollTermScreen != null) ? scrollTermScreen.getScrollY() : 0;
                            if (scrollTermScreen != null && tvTermScreen != null) {
                                int scrollY = scrollTermScreen.getScrollY();
                                int scrollHeight = scrollTermScreen.getHeight();
                                int contentHeight = tvTermScreen.getHeight();
                                // "at the bottom" means within about a line of it: further up you're reading,
                                // and updates must not pull you down
                                int nearBottom = Math.round(tvTermScreen.getLineHeight() * 1.2f);
                                wasAtBottom = (scrollHeight <= 0) || (contentHeight <= scrollHeight) || ((contentHeight - (scrollY + scrollHeight)) <= nearBottom);
                            } else {
                                wasAtBottom = true;
                            }

                            if (tvTermScreen != null) {
                                // Reading further up: remember the line at the top of the view, to find
                                // it again after the update (lines leaving the top would otherwise slide
                                // the text up)
                                final TermAnchor anchor = wasAtBottom ? null : captureTermAnchor();
                                // Scroll in the same frame as the new text is drawn, so no jump shows
                                final android.view.ViewTreeObserver vto = tvTermScreen.getViewTreeObserver();
                                vto.addOnPreDrawListener(new android.view.ViewTreeObserver.OnPreDrawListener() {
                                    @Override public boolean onPreDraw() {
                                        if (tvTermScreen == null) return true;
                                        tvTermScreen.getViewTreeObserver().removeOnPreDrawListener(this);
                                        int target = anchor != null ? findTermAnchor(anchor) : -1;
                                        applyTerminalScroll(tvTermScreen.getHeight(), firstLoad, savedPreserveDist, wasAtBottom,
                                                target >= 0 ? target : savedScrollY);
                                        return true;
                                    }
                                });

                                if (colored == null) {
                                    shownTermLines = null; shownTermColored = null;
                                    tvTermScreen.setText("(Empty terminal)");
                                } else {
                                    try {
                                        tvTermScreen.setText(colored);
                                    } catch (IllegalArgumentException e) {
                                        // measured for an older font size: plain set (measured on this thread)
                                        tvTermScreen.setText(coloredFull);
                                    }
                                }
                            }
                        });
                    }
                }
            } catch (Exception e) {
                if (gen == termPollGen.get()) isLoadingEarlier = false;
            } finally {
                if (gen == termPollGen.get()) {          // a superseded poll leaves the new one alone
                    isPollingTerminal = false;
                    if (hasPendingTerminalPoll) {
                        hasPendingTerminalPoll = false;
                        terminalHandler.post(this::pollActiveTerminalOutput);
                    }
                }
            }
        });
    }

    private boolean terminalFullscreen = false;
    private final java.util.Map<String, Boolean> fullscreenById = new java.util.HashMap<>();

    /** A fullscreen app (Claude Code, vim) fills the computer's screen with its input at the
     *  bottom: pin its text to the bottom of the view too, so a short conversation doesn't
     *  leave the lower part empty with the input box halfway up. Shell output stays at the top. */
    private void setTerminalFullscreen(boolean on) {
        if (tvTermScreen == null || scrollTermScreen == null) return;
        terminalFullscreen = on;
        tvTermScreen.setGravity((on ? android.view.Gravity.BOTTOM : android.view.Gravity.TOP) | android.view.Gravity.START);
        int min = on ? scrollTermScreen.getHeight() - scrollTermScreen.getPaddingTop() - scrollTermScreen.getPaddingBottom() : 0;
        if (tvTermScreen.getMinHeight() != Math.max(0, min)) tvTermScreen.setMinHeight(Math.max(0, min));
    }

    /** The text after a delta, built as a new copy (off the main thread): drop lines from the
     *  top, keep the next `keep`, then the tail. Null if the text isn't in step. */
    private static CharSequence spliceTerminalText(CharSequence shown, int drop, int keep, int tailCount, CharSequence coloredTail) {
        android.text.SpannableStringBuilder b = new android.text.SpannableStringBuilder(shown);
        if (drop > 0) {
            int cut = nthNewline(b, drop);
            if (cut < 0) return null;
            b.delete(0, cut + 1);
        }
        int from = 0;
        if (keep > 0) {
            int nl = nthNewline(b, keep);
            from = nl < 0 ? b.length() : nl;
        }
        b.replace(from, b.length(), tailCount == 0 ? "" : (keep == 0 ? coloredTail : android.text.TextUtils.concat("\n", coloredTail)));
        return b;
    }

    /** Index of the k-th newline (k from 1), or -1. */
    private static int nthNewline(CharSequence t, int k) {
        for (int i = 0, n = t.length(); i < n; i++) {
            if (t.charAt(i) == '\n' && --k == 0) return i;
        }
        return -1;
    }

    private void applyTerminalScroll(int newContentHeight, boolean firstLoad, int savedPreserveDist, boolean wasAtBottom, int savedScrollY) {
        if (scrollTermScreen == null) return;
        boolean isTouching = (scrollTermScreen instanceof TerminalScrollView) && ((TerminalScrollView) scrollTermScreen).isUserTouching();
        int bottom = Math.max(0, newContentHeight - scrollTermScreen.getHeight());
        if (firstLoad) {
            isFirstTerminalLoad = false;
            scrollTermScreen.scrollTo(0, bottom);
        } else if (savedPreserveDist >= 0) {
            scrollTermScreen.scrollTo(0, Math.max(0, newContentHeight - savedPreserveDist));
        } else if (wasAtBottom && !isTouching) {
            scrollTermScreen.scrollTo(0, bottom);
        } else if (!wasAtBottom) {
            // keep the same line in view (also mid-swipe, or the text slides under your finger)
            scrollTermScreen.scrollTo(0, Math.min(Math.max(0, savedScrollY), bottom));
        }
    }

    /** The line at the top of the terminal view, to find it again after the text changes. */
    private static class TermAnchor { String para, next; int paraStart, offsetInPara, intra; }

    private TermAnchor captureTermAnchor() {
        android.text.Layout lay = tvTermScreen.getLayout();
        if (lay == null || scrollTermScreen == null) return null;
        CharSequence t = tvTermScreen.getText();
        int y = scrollTermScreen.getScrollY();
        int line = lay.getLineForVertical(y);
        int start = lay.getLineStart(line);
        // the whole terminal line (a row can wrap into several screen lines), skipping blank ones
        String all = t.toString();
        int paraStart = all.lastIndexOf('\n', Math.max(0, start - 1)) + 1;
        if (start == 0) paraStart = 0;
        int paraEnd = all.indexOf('\n', start);
        if (paraEnd < 0) paraEnd = all.length();
        while (all.substring(paraStart, paraEnd).trim().isEmpty() && paraEnd < all.length()) {
            paraStart = paraEnd + 1;
            paraEnd = all.indexOf('\n', paraStart);
            if (paraEnd < 0) paraEnd = all.length();
        }
        TermAnchor a = new TermAnchor();
        a.para = all.substring(paraStart, paraEnd);
        int nextEnd = paraEnd < all.length() ? all.indexOf('\n', paraEnd + 1) : -1;
        a.next = paraEnd < all.length() ? all.substring(paraEnd + 1, nextEnd < 0 ? all.length() : nextEnd) : "";
        a.paraStart = paraStart;
        a.offsetInPara = Math.max(0, Math.min(start - paraStart, a.para.length()));
        a.intra = y - lay.getLineTop(lay.getLineForOffset(Math.min(paraStart + a.offsetInPara, all.length())));
        return a;
    }

    /** Where to scroll so the anchored line is at the top again, or -1 if it's gone. */
    private int findTermAnchor(TermAnchor a) {
        android.text.Layout lay = tvTermScreen.getLayout();
        if (lay == null || a.para.isEmpty()) return -1;
        String all = tvTermScreen.getText().toString();
        String key = a.next.isEmpty() ? a.para : a.para + "\n" + a.next;
        // the occurrence nearest where it was (old lines leave from above, so usually a bit earlier)
        int best = -1, bestDist = Integer.MAX_VALUE;
        for (int i = all.indexOf(key); i >= 0; i = all.indexOf(key, i + 1)) {
            if (i > 0 && all.charAt(i - 1) != '\n') continue;      // must start a line
            int d = Math.abs(i - a.paraStart);
            if (d < bestDist) { best = i; bestDist = d; }
        }
        if (best < 0) return -1;
        int line = lay.getLineForOffset(Math.min(best + a.offsetInPara, all.length()));
        return Math.max(0, lay.getLineTop(line) + a.intra);
    }

    private void handleSendTerminalInput() {
        if (activeTerminalId == null || etTermInput == null) return;
        if (terminalSelectionMode) setTerminalSelectionMode(false);
        String cmd = etTermInput.getText() != null ? etTermInput.getText().toString() : "";
        isInternalTextUpdate = true;
        etTermInput.setText("");
        isInternalTextUpdate = false;

        if (isLiveTypeEnabled) {
            sendTerminalRawText("\r");
        } else {
            sendTerminalRawText(cmd.isEmpty() ? "\r" : (cmd + "\r"), termReplaceCount);
        }
        termReplaceCount = 0;
    }

    /** Characters of the computer's input line that the phone loaded for editing; sending
     *  deletes them there first, so the edited text replaces them. */
    private int termReplaceCount = 0;
    /** Live mode: where the computer's cursor is, as a position in the phone's input box. */
    private int liveCursor = 0;
    private String textBeforeEdit = null;

    private android.widget.PopupWindow termEditTip;
    private boolean termEditTipShown = false;
    private static final String PREF_TERM_EDIT_TIP = "tip_term_edit_seen";

    /** First time the terminal can be typed into: point at ❯ once, then never again. */
    private void maybeShowTermEditTip() {
        if (termEditTipShown) return;
        SharedPreferences prefs = getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        if (prefs.getBoolean(PREF_TERM_EDIT_TIP, false)) return;
        View anchor = findViewById(R.id.tv_term_prompt);
        if (anchor == null || anchor.getWindowToken() == null || !anchor.isShown()) return;
        termEditTipShown = true;

        float dp = getResources().getDisplayMetrics().density;
        TextView tip = new TextView(this);
        tip.setText("Tap ❯ to bring what's typed on the computer here, so you can edit it");
        tip.setTextColor(getColor(R.color.o_text));
        tip.setTextSize(13);
        tip.setBackgroundResource(R.drawable.bg_tip);
        tip.setPadding((int) (14 * dp), (int) (10 * dp), (int) (14 * dp), (int) (10 * dp));
        tip.setMaxWidth((int) (260 * dp));
        tip.measure(View.MeasureSpec.makeMeasureSpec((int) (260 * dp), View.MeasureSpec.AT_MOST),
                View.MeasureSpec.makeMeasureSpec(0, View.MeasureSpec.UNSPECIFIED));

        termEditTip = new android.widget.PopupWindow(tip, ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, false);
        termEditTip.setOutsideTouchable(true);
        termEditTip.setOnDismissListener(() -> termEditTip = null);
        tip.setOnClickListener(v -> dismissTermEditTip(true));
        termEditTip.showAsDropDown(anchor, (int) (4 * dp), -(anchor.getHeight() + tip.getMeasuredHeight() + (int) (10 * dp)));
        // it explains itself in a few seconds; seen it once is enough
        tip.postDelayed(() -> dismissTermEditTip(true), 7000);
    }

    private void dismissTermEditTip(boolean seen) {
        if (seen) {
            getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE).edit().putBoolean(PREF_TERM_EDIT_TIP, true).apply();
        }
        if (termEditTip != null) {
            termEditTip.dismiss();
            termEditTip = null;
        }
    }

    private void loadTerminalInputLine() {
        dismissTermEditTip(true);
        final String termId = activeTerminalId;
        if (termId == null) return;
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null) return;
        String baseUrl = getTerminalBaseUrl(active);
        if (baseUrl == null) return;
        vibrate(15);
        terminalSendExecutor.execute(() -> {
            String msg;
            try {
                String res = NetworkUtils.httpGetWithAuth(baseUrl + "/api/terminals/input_line?id="
                        + java.net.URLEncoder.encode(termId, "UTF-8"), active.authToken, 3000);
                JSONObject obj = new JSONObject(res != null ? res : "{}");
                if ("ok".equals(obj.optString("status"))) {
                    String text = obj.optString("text", "");
                    if (!text.isEmpty()) {
                        runOnUiThread(() -> {
                            if (!termId.equals(activeTerminalId) || etTermInput == null || etTermInput.length() > 0) return;
                            isInternalTextUpdate = true;
                            etTermInput.setText(text);
                            etTermInput.setSelection(etTermInput.length());
                            isInternalTextUpdate = false;
                            // live mode: the text is already on the computer, edits are mirrored as they happen;
                            // otherwise sending replaces it
                            termReplaceCount = isLiveTypeEnabled ? 0 : text.codePointCount(0, text.length());
                            etTermInput.requestFocus();
                            android.view.inputmethod.InputMethodManager imm = (android.view.inputmethod.InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
                            if (imm != null) imm.showSoftInput(etTermInput, 0);
                        });
                        return;
                    }
                    msg = "Nothing typed on the computer";
                } else {
                    msg = obj.optString("message", "Couldn't read the computer's input line");
                }
            } catch (Exception e) {
                msg = "Couldn't read the computer's input line";
            }
            final String m = msg;
            runOnUiThread(() -> Toast.makeText(MainActivity.this, m, Toast.LENGTH_SHORT).show());
        });
    }

    private void sendTerminalRawText(String text) {
        sendTerminalRawText(text, 0);
    }

    private void sendTerminalEdit(int move, int backspace, String text) {
        if (activeTerminalId == null) return;
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null) return;
        String baseUrl = getTerminalBaseUrl(active);
        if (baseUrl == null) return;
        final String termId = activeTerminalId;
        terminalSendExecutor.execute(() -> {
            try {
                JSONObject req = new JSONObject();
                req.put("id", termId);
                req.put("text", text);
                if (move != 0) req.put("move", move);
                if (backspace > 0) req.put("backspace", backspace);
                String res = NetworkUtils.httpPostJsonWithAuth(baseUrl + "/api/terminals/write", active.authToken, req.toString(), 8000);
                if (res != null) {
                    JSONObject obj = new JSONObject(res);
                    if (!"ok".equals(obj.optString("status"))) {
                        String msg = obj.optString("message", "Konsole input blocked");
                        runOnUiThread(() -> Toast.makeText(MainActivity.this, msg, Toast.LENGTH_LONG).show());
                    }
                }
                triggerTerminalImmediatePoll();
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Send error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        });
    }

    private void sendTerminalRawText(String text, int replace) {
        if (activeTerminalId == null || text == null || (text.isEmpty() && replace <= 0)) return;
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null) return;
        String baseUrl = getTerminalBaseUrl(active);
        if (baseUrl == null) {
            Toast.makeText(this, "Terminal is restricted to local Wi-Fi / LAN for security.", Toast.LENGTH_SHORT).show();
            return;
        }

        terminalSendExecutor.execute(() -> {
            try {
                String url = baseUrl + "/api/terminals/write";
                JSONObject req = new JSONObject();
                req.put("id", activeTerminalId);
                req.put("text", text);
                if (replace > 0) req.put("replace", replace);
                String res = NetworkUtils.httpPostJsonWithAuth(url, active.authToken, req.toString(), replace > 0 ? 8000 : 3000);
                if (res != null) {
                    JSONObject obj = new JSONObject(res);
                    if (!"ok".equals(obj.optString("status"))) {
                        String msg = obj.optString("message", "Konsole input blocked");
                        runOnUiThread(() -> Toast.makeText(MainActivity.this, "" + msg, Toast.LENGTH_LONG).show());
                    }
                }
                triggerTerminalImmediatePoll();
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Send error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        });
    }

    private void sendTerminalKey(String keyName) {
        if (activeTerminalId == null || keyName == null) return;
        if (terminalSelectionMode) setTerminalSelectionMode(false);
        if (isLiveTypeEnabled && etTermInput != null && etTermInput.length() > 0) {
            boolean left = "left".equals(keyName), right = "right".equals(keyName);
            String text = etTermInput.getText().toString();
            int cur = Math.max(0, Math.min(liveCursor, text.length()));
            if ((left && cur > 0) || (right && cur < text.length())) {
                // the computer's cursor moves one character: follow it in the box
                liveCursor = text.offsetByCodePoints(cur, left ? -1 : 1);
                etTermInput.setSelection(liveCursor);
            } else if (!"backspace".equals(keyName)) {
                // → at the end accepts fish's suggestion; ← past the start, history, tab, enter,
                // ctrl keys: the computer's line changes in ways the box can't follow, so start
                // fresh (tap ❯ to load it again)
                // history, completion, enter, ctrl keys: the computer's line changes in ways the box
                // can't follow, so start fresh (tap ❯ to load it again)
                isInternalTextUpdate = true;
                etTermInput.setText("");
                isInternalTextUpdate = false;
            }
        }
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null) return;
        String baseUrl = getTerminalBaseUrl(active);
        if (baseUrl == null) {
            Toast.makeText(this, "Terminal is restricted to local Wi-Fi / LAN for security.", Toast.LENGTH_SHORT).show();
            return;
        }

        terminalSendExecutor.execute(() -> {
            try {
                String url = baseUrl + "/api/terminals/key";
                JSONObject req = new JSONObject();
                req.put("id", activeTerminalId);
                req.put("key", keyName);
                String res = NetworkUtils.httpPostJsonWithAuth(url, active.authToken, req.toString(), 3000);
                if (res != null) {
                    JSONObject obj = new JSONObject(res);
                    if (!"ok".equals(obj.optString("status"))) {
                        String msg = obj.optString("message", "Konsole input blocked");
                        runOnUiThread(() -> Toast.makeText(MainActivity.this, "" + msg, Toast.LENGTH_LONG).show());
                    }
                }
                triggerTerminalImmediatePoll();
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Key error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        });
    }

    private void spawnNewTerminal() {
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null) {
            Toast.makeText(this, "No active computer connected", Toast.LENGTH_SHORT).show();
            return;
        }
        String baseUrl = getTerminalBaseUrl(active);
        if (baseUrl == null) {
            Toast.makeText(this, "Terminal is restricted to local Wi-Fi / LAN for security.", Toast.LENGTH_SHORT).show();
            return;
        }

        Toast.makeText(this, "Opening a new tab on your computer…", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            try {
                String url = baseUrl + "/api/terminals/new";
                JSONObject req = new JSONObject();
                String res = NetworkUtils.httpPostJsonWithAuth(url, active.authToken, req.toString(), 4000);
                runOnUiThread(() -> {
                    Toast.makeText(MainActivity.this, "New terminal opened on computer!", Toast.LENGTH_SHORT).show();
                    loadTerminalsList();
                });
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Spawn error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        }).start();
    }
}


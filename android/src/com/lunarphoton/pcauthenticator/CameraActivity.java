package com.lunarphoton.pcauthenticator;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.os.Vibrator;
import android.provider.MediaStore;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

public class CameraActivity extends Activity {
    private PairedDevice activeDevice;
    private ImageView ivCameraFeed;
    private ProgressBar pbLoading;
    private TextView tvError;
    private TextView tvStatus;
    private TextView tvTarget;
    private Button btnPlayPause;
    private Button btnQuality;
    private Vibrator vibrator;

    private Thread streamThread;
    private volatile boolean isRunning = false;
    private volatile boolean isPaused = false;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    private String currentDevice = "";
    private String currentQuality = "smooth";
    private final List<JSONObject> availableDevices = new ArrayList<>();

    private int frameCount = 0;
    private long lastFpsTime = 0;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setContentView(R.layout.activity_camera);

        activeDevice = DeviceManager.getActiveDevice(this);
        if (DeviceManager.isInternetActive(this)) {
            Toast.makeText(this, "Streaming over the internet", Toast.LENGTH_SHORT).show();
        }
        vibrator = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);

        ivCameraFeed = findViewById(R.id.iv_camera_feed);
        pbLoading = findViewById(R.id.pb_camera_loading);
        tvError = findViewById(R.id.tv_camera_error);
        tvStatus = findViewById(R.id.tv_camera_status);
        tvTarget = findViewById(R.id.tv_camera_target);
        btnPlayPause = findViewById(R.id.btn_camera_play_pause);
        btnQuality = findViewById(R.id.btn_camera_quality);

        if (activeDevice != null && activeDevice.hostname != null) {
            tvTarget.setText("Target: " + activeDevice.hostname + " (" + activeDevice.ip + ")");
        } else {
            tvTarget.setText("Target: No Orbited");
        }

        findViewById(R.id.btn_camera_back).setOnClickListener(v -> finish());
        findViewById(R.id.btn_camera_snapshot).setOnClickListener(v -> takeSnapshot());
        findViewById(R.id.btn_camera_switch).setOnClickListener(v -> showDeviceChooser());

        btnPlayPause.setOnClickListener(v -> {
            isPaused = !isPaused;
            if (isPaused) {
                stopStream();
                btnPlayPause.setText("Resume");
                tvStatus.setText("Paused");
                tvStatus.setTextColor(getColor(R.color.o_text_2));
            } else {
                startStream();
                btnPlayPause.setText("Pause");
                tvStatus.setText("Connecting");
                tvStatus.setTextColor(getColor(R.color.o_text_2));
            }
        });

        btnQuality.setOnClickListener(v -> {
            if ("smooth".equals(currentQuality)) {
                currentQuality = "hd";
                btnQuality.setText("Quality: HD 720p");
            } else {
                currentQuality = "smooth";
                btnQuality.setText("Quality: smooth 480p");
            }
            restartStream();
        });

        fetchAvailableDevices();
        startStream();
    }

    private String getCameraBaseUrl() {
        if (activeDevice == null) return "";
        if (DeviceManager.isWifiActive(this) && activeDevice.getLocalUrl() != null && !activeDevice.getLocalUrl().isEmpty()) {
            return activeDevice.getLocalUrl();
        }
        return activeDevice.getBaseUrl();
    }

    private void fetchAvailableDevices() {
        if (activeDevice == null) return;
        new Thread(() -> {
            try {
                String urlStr = getCameraBaseUrl() + "/api/camera/devices";
                String resp = NetworkUtils.httpGetWithAuth(urlStr, activeDevice.authToken, 4000);
                if (resp != null) {
                    JSONObject json = new JSONObject(resp);
                    JSONArray arr = json.optJSONArray("devices");
                    if (arr != null) {
                        availableDevices.clear();
                        for (int i = 0; i < arr.length(); i++) {
                            availableDevices.add(arr.getJSONObject(i));
                        }
                    }
                }
            } catch (Exception ignored) {}
        }).start();
    }

    private void showDeviceChooser() {
        if (availableDevices.isEmpty()) {
            Toast.makeText(this, "Scanning for camera devices...", Toast.LENGTH_SHORT).show();
            fetchAvailableDevices();
            return;
        }

        String[] names = new String[availableDevices.size()];
        for (int i = 0; i < availableDevices.size(); i++) {
            names[i] = availableDevices.get(i).optString("name", "Camera " + (i + 1));
        }

        new AlertDialog.Builder(this)
                .setTitle("Select Camera Device")
                .setItems(names, (dialog, which) -> {
                    currentDevice = availableDevices.get(which).optString("device", "");
                    restartStream();
                    Toast.makeText(this, "Switched to " + names[which], Toast.LENGTH_SHORT).show();
                })
                .setNegativeButton("Cancel", null)
                .show();
    }

    private void restartStream() {
        stopStream();
        startStream();
    }

    private void startStream() {
        if (activeDevice == null) {
            tvError.setVisibility(View.VISIBLE);
            tvError.setText("No computer selected. Connect to a computer first.");
            pbLoading.setVisibility(View.GONE);
            return;
        }

        isRunning = true;
        pbLoading.setVisibility(View.VISIBLE);
        tvError.setVisibility(View.GONE);

        streamThread = new Thread(() -> {
            HttpURLConnection conn = null;
            InputStream in = null;
            try {
                String devParam = currentDevice.isEmpty() ? "" : "&device=" + Uri.encode(currentDevice);
                String urlStr = getCameraBaseUrl() + "/api/camera/stream?quality=" + currentQuality + devParam;
                conn = (HttpURLConnection) new URL(urlStr).openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("GET");
                conn.setConnectTimeout(6000);
                conn.setReadTimeout(12000);
                if (activeDevice.isPaired()) {
                    NetworkUtils.setAuth(conn, activeDevice.authToken);
                }

                in = conn.getInputStream();
                byte[] buffer = new byte[16384];
                ByteArrayOutputStream frameStream = new ByteArrayOutputStream();
                boolean inFrame = false;

                lastFpsTime = System.currentTimeMillis();
                frameCount = 0;

                while (isRunning) {
                    int bytesRead = in.read(buffer);
                    if (bytesRead <= 0) break;

                    for (int i = 0; i < bytesRead; i++) {
                        byte b = buffer[i];

                        if (!inFrame) {
                            if (b == (byte) 0xFF && i + 1 < bytesRead && buffer[i + 1] == (byte) 0xD8) {
                                inFrame = true;
                                frameStream.reset();
                                frameStream.write(0xFF);
                                frameStream.write(0xD8);
                                i++;
                            }
                        } else {
                            frameStream.write(b);
                            if (b == (byte) 0xD9 && frameStream.size() > 2) {
                                byte[] raw = frameStream.toByteArray();
                                if (raw[raw.length - 2] == (byte) 0xFF) {
                                    inFrame = false;
                                    final byte[] jpegData = raw;
                                    final Bitmap bitmap = BitmapFactory.decodeByteArray(jpegData, 0, jpegData.length);
                                    if (bitmap != null) {
                                        frameCount++;
                                        long now = System.currentTimeMillis();
                                        int fps = 0;
                                        if (now - lastFpsTime >= 1000) {
                                            fps = (int) (frameCount * 1000f / (now - lastFpsTime));
                                            frameCount = 0;
                                            lastFpsTime = now;
                                        }

                                        final int curFps = fps;
                                        mainHandler.post(() -> {
                                            if (!isRunning) return;
                                            ivCameraFeed.setImageBitmap(bitmap);
                                            pbLoading.setVisibility(View.GONE);
                                            tvError.setVisibility(View.GONE);
                                            if (curFps > 0) {
                                                tvStatus.setText("Live · " + curFps + " fps");
                                                tvStatus.setTextColor(getColor(R.color.o_online));
                                            }
                                        });
                                    }
                                }
                            }
                        }
                    }
                }
            } catch (Exception e) {
                if (isRunning) {
                    mainHandler.post(() -> {
                        pbLoading.setVisibility(View.GONE);
                        tvError.setVisibility(View.VISIBLE);
                        tvError.setText("Camera stream error: " + e.getMessage() + "\nTap Reconnect or check if webcam is in use");
                        tvStatus.setText("Offline");
                        tvStatus.setTextColor(getColor(R.color.o_danger));
                    });
                }
            } finally {
                if (in != null) {
                    try { in.close(); } catch (Exception ignored) {}
                }
                if (conn != null) {
                    try { conn.disconnect(); } catch (Exception ignored) {}
                }
            }
        });
        streamThread.setDaemon(true);
        streamThread.start();
    }

    private void stopStream() {
        isRunning = false;
        if (streamThread != null) {
            streamThread.interrupt();
            streamThread = null;
        }
    }

    private void takeSnapshot() {
        if (activeDevice == null) return;
        vibrate(30);
        Toast.makeText(this, "Capturing snapshot from computer...", Toast.LENGTH_SHORT).show();

        new Thread(() -> {
            try {
                String devParam = currentDevice.isEmpty() ? "" : "?device=" + Uri.encode(currentDevice);
                String urlStr = getCameraBaseUrl() + "/api/camera/snapshot" + devParam;
                HttpURLConnection conn = (HttpURLConnection) new URL(urlStr).openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("GET");
                conn.setConnectTimeout(5000);
                conn.setReadTimeout(7000);
                if (activeDevice.isPaired()) {
                    NetworkUtils.setAuth(conn, activeDevice.authToken);
                }

                int code = conn.getResponseCode();
                if (code == 200) {
                    InputStream in = conn.getInputStream();
                    ByteArrayOutputStream baos = new ByteArrayOutputStream();
                    byte[] buf = new byte[8192];
                    int r;
                    while ((r = in.read(buf)) != -1) {
                        baos.write(buf, 0, r);
                    }
                    in.close();

                    byte[] jpegBytes = baos.toByteArray();
                    saveImageToGallery(jpegBytes);
                } else {
                    mainHandler.post(() -> Toast.makeText(this, "Failed to capture snapshot (HTTP " + code + ")", Toast.LENGTH_SHORT).show());
                }
            } catch (Exception e) {
                mainHandler.post(() -> Toast.makeText(this, "Snapshot error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        }).start();
    }

    private void saveImageToGallery(byte[] jpegBytes) {
        String filename = "PC_Camera_" + System.currentTimeMillis() + ".jpg";
        try {
            Uri imageUri;
            ContentValues values = new ContentValues();
            values.put(MediaStore.Images.Media.DISPLAY_NAME, filename);
            values.put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg");

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                values.put(MediaStore.Images.Media.RELATIVE_PATH, Environment.DIRECTORY_PICTURES + "/PCConnect");
                values.put(MediaStore.Images.Media.IS_PENDING, 1);
            }

            imageUri = getContentResolver().insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values);
            if (imageUri != null) {
                try (OutputStream os = getContentResolver().openOutputStream(imageUri)) {
                    if (os != null) {
                        os.write(jpegBytes);
                    }
                }

                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    values.clear();
                    values.put(MediaStore.Images.Media.IS_PENDING, 0);
                    getContentResolver().update(imageUri, values, null, null);
                }

                mainHandler.post(() -> {
                    Toast.makeText(this, "Photo saved to Gallery: " + filename, Toast.LENGTH_LONG).show();
                });
            }
        } catch (Exception e) {
            mainHandler.post(() -> Toast.makeText(this, "Failed to save snapshot: " + e.getMessage(), Toast.LENGTH_SHORT).show());
        }
    }

    private void vibrate(long ms) {
        if (vibrator != null && vibrator.hasVibrator()) {
            try {
                vibrator.vibrate(ms);
            } catch (Exception ignored) {}
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (!isPaused && !isRunning) {
            startStream();
        }
    }

    @Override
    protected void onPause() {
        stopStream();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        stopStream();
        super.onDestroy();
    }
}

package com.lunarphoton.pcauthenticator;

import android.Manifest;
import android.app.Activity;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.graphics.SurfaceTexture;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Range;
import android.view.Surface;
import android.view.TextureView;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.TextView;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.ByteBuffer;
import java.util.Arrays;

/**
 * This phone's camera as the computer's webcam: the camera's picture is compressed by the
 * phone's video encoder (H.264) and sent over Wi-Fi to the Orbit service, which turns it
 * into a camera that video-call apps and browsers on the computer can pick.
 * The screen stays on while it streams; leaving this screen stops it.
 */
public class WebcamActivity extends Activity {
    private static final int FPS = 30;
    private static final String PREF_SHARP = "webcam_1080p";
    private boolean sharp = true;               // 1080p; 720p saves Wi-Fi and battery
    private int wantW = 1920, wantH = 1080;
    // the size the camera really delivers (one it supports, so nothing gets stretched)
    private int width = wantW, height = wantH;
    private String codec = "h264";              // hevc when the phone has a hardware encoder for it
    private static final int REQ_CAMERA = 41;

    private TextureView preview;
    private TextView tvStatus, tvTarget, tvError;
    private View shutter, pausedCover, dot;
    private TextView tvAction;
    private android.widget.ImageView ivAction;
    private boolean paused;                      // you paused it with the Live pill
    private int side = -1;                       // the landscape side chosen from the motion sensor
    private android.view.OrientationEventListener tilt;
    private HandlerThread cameraThread;
    private Handler cameraHandler;
    private CameraDevice camera;
    private CameraCaptureSession session;
    private MediaCodec encoder;
    private Surface encoderSurface, previewSurface;
    private Thread sender;
    private volatile boolean streaming;
    private volatile HttpURLConnection conn;
    private boolean front = true;               // the last one used; the front camera at first
    private static final String PREF_FRONT = "webcam_front";
    private static final long DIM_AFTER_MS = 3000, BRIGHT_FOR_MS = 5000;
    private final Handler ui = new Handler(android.os.Looper.getMainLooper());
    private final Runnable dim = () -> setDimmed(true);
    private int streamTurn = -1;                // the screen's rotation the picture was started for
    private boolean waitingForLandscape;        // started while still upright: begins once sideways
    private final android.hardware.display.DisplayManager.DisplayListener turned =
            new android.hardware.display.DisplayManager.DisplayListener() {
        @Override public void onDisplayAdded(int id) { }
        @Override public void onDisplayRemoved(int id) { }
        @Override public void onDisplayChanged(int id) {
            int r = getWindowManager().getDefaultDisplay().getRotation();
            if (streaming && r != streamTurn) { stopStreaming(); startStreaming(); }    // turned over
            else if (!streaming && waitingForLandscape && isLandscape()) startStreaming();
        }
    };

    /** Is the screen sideways right now (its size follows the turn; works on tablets too)? */
    private boolean isLandscape() {
        android.graphics.Point size = new android.graphics.Point();
        getWindowManager().getDefaultDisplay().getRealSize(size);
        return size.x > size.y;
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_webcam);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        // Landscape, like a webcam: the computer gets a wide picture that fills its camera.
        // Either way round; turning the phone over restarts the picture the right way up.
        // The motion sensor picks the side even with auto-rotate off (some phones don't let
        // the system's "sensor landscape" through the rotation lock)
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE);
        tilt = new android.view.OrientationEventListener(this) {
            @Override public void onOrientationChanged(int deg) {
                if (deg == ORIENTATION_UNKNOWN) return;
                int want = deg >= 60 && deg <= 120 ? ActivityInfo.SCREEN_ORIENTATION_REVERSE_LANDSCAPE
                        : deg >= 240 && deg <= 300 ? ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE : -1;
                if (want != -1 && want != side) {        // upright or flat: stay on the side it's on
                    side = want;
                    setRequestedOrientation(want);
                }
            }
        };
        preview = findViewById(R.id.tv_webcam_preview);
        tvStatus = findViewById(R.id.tv_webcam_status);
        tvTarget = findViewById(R.id.tv_webcam_target);
        tvError = findViewById(R.id.tv_webcam_error);
        findViewById(R.id.btn_webcam_back).setOnClickListener(v -> finish());
        pausedCover = findViewById(R.id.tv_webcam_paused);
        dot = findViewById(R.id.v_webcam_dot);
        tvAction = findViewById(R.id.tv_webcam_action);
        ivAction = findViewById(R.id.iv_webcam_action);
        shutter = findViewById(R.id.btn_webcam_stop);
        shutter.setOnClickListener(v -> {
            if (streaming || waitingForLandscape) {
                paused = true;
                waitingForLandscape = false;
                stopStreaming();
                setStatus("Paused", R.color.o_text_3);
            } else {
                paused = false;
                startStreaming();
            }
            showShutter();
        });
        hideSystemBars();
        findViewById(R.id.btn_webcam_switch).setOnClickListener(v -> {
            front = !front;
            getSharedPreferences("pc_auth_prefs", MODE_PRIVATE).edit().putBoolean(PREF_FRONT, front).apply();
            if (streaming) { stopStreaming(); startStreaming(); }
        });
        front = getSharedPreferences("pc_auth_prefs", MODE_PRIVATE).getBoolean(PREF_FRONT, true);
        sharp = getSharedPreferences("pc_auth_prefs", MODE_PRIVATE).getBoolean(PREF_SHARP, true);
        Button quality = findViewById(R.id.btn_webcam_quality);
        quality.setText(sharp ? "1080p" : "720p");
        quality.setOnClickListener(v -> {
            sharp = !sharp;
            getSharedPreferences("pc_auth_prefs", MODE_PRIVATE).edit().putBoolean(PREF_SHARP, sharp).apply();
            quality.setText(sharp ? "1080p" : "720p");
            if (streaming) { stopStreaming(); startStreaming(); }
        });
        PairedDevice active = DeviceManager.getActiveDevice(this);
        tvTarget.setText(active != null ? "to " + active.hostname : "No computer paired");
    }

    /** A tap anywhere (buttons too) brightens a dimmed screen for a moment, to check the picture. */
    @Override
    public boolean dispatchTouchEvent(android.view.MotionEvent e) {
        if (e.getAction() == android.view.MotionEvent.ACTION_DOWN && streaming) brightenForAMoment();
        return super.dispatchTouchEvent(e);
    }

    @Override
    protected void onResume() {
        super.onResume();
        hideSystemBars();
        if (tilt.canDetectOrientation()) tilt.enable();
        if (paused) { showShutter(); return; }       // stays paused until you resume it
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, REQ_CAMERA);
            return;
        }
        if (preview.isAvailable()) startStreaming();
        else preview.setSurfaceTextureListener(new TextureView.SurfaceTextureListener() {
            @Override public void onSurfaceTextureAvailable(SurfaceTexture s, int w, int h) { startStreaming(); }
            @Override public void onSurfaceTextureSizeChanged(SurfaceTexture s, int w, int h) { }
            @Override public boolean onSurfaceTextureDestroyed(SurfaceTexture s) { return true; }
            @Override public void onSurfaceTextureUpdated(SurfaceTexture s) { }
        });
    }

    @Override
    protected void onStart() {
        super.onStart();
        ((android.hardware.display.DisplayManager) getSystemService(DISPLAY_SERVICE)).registerDisplayListener(turned, ui);
    }

    @Override
    protected void onStop() {
        super.onStop();
        ((android.hardware.display.DisplayManager) getSystemService(DISPLAY_SERVICE)).unregisterDisplayListener(turned);
    }

    @Override
    protected void onPause() {
        super.onPause();
        tilt.disable();
        waitingForLandscape = false;
        stopStreaming();            // the camera is the computer's only while this screen is open
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (code == REQ_CAMERA && (results.length == 0 || results[0] != PackageManager.PERMISSION_GRANTED)) {
            showError("Orbit needs the camera permission to be your computer's webcam.");
        }
    }

    private void showError(String msg) {
        runOnUiThread(() -> {
            tvError.setText(msg);
            tvError.setVisibility(View.VISIBLE);
            tvStatus.setText("Off");
            dot.setBackgroundTintList(android.content.res.ColorStateList.valueOf(getColor(R.color.o_text_3)));
            showShutter();
        });
    }

    private void startStreaming() {
        if (streaming) return;
        // still upright for a moment after opening: wait until the screen has turned sideways
        waitingForLandscape = !isLandscape();
        if (waitingForLandscape) return;
        PairedDevice active = DeviceManager.getActiveDevice(this);
        if (active == null || !active.isPaired()) { showError("Pair a computer first."); return; }
        if (!DeviceManager.isWifiActive(this)) { showError("The phone webcam works over Wi-Fi only."); return; }
        String base = active.getLocalUrl();
        if (base == null || base.isEmpty()) { showError("The computer isn't on this Wi-Fi."); return; }
        tvError.setVisibility(View.GONE);
        try {
            CameraManager cm = (CameraManager) getSystemService(CAMERA_SERVICE);
            String id = pickCamera(cm);
            if (id == null) { showError("No camera found."); return; }
            CameraCharacteristics cc = cm.getCameraCharacteristics(id);
            wantW = sharp ? 1920 : 1280;
            wantH = sharp ? 1080 : 720;
            android.util.Size size = pictureSize(cc);
            width = size.getWidth();
            height = size.getHeight();
            int rotation = pictureRotation(cc);
            streamTurn = getWindowManager().getDefaultDisplay().getRotation();
            fitPreview(rotation);

            // HEVC: the same picture at about half the data of H.264 (less Wi-Fi and battery),
            // when this phone has a hardware encoder for it; H.264 otherwise
            encoder = null;
            for (String mime : new String[]{MediaFormat.MIMETYPE_VIDEO_HEVC, MediaFormat.MIMETYPE_VIDEO_AVC}) {
                boolean hevc = mime.equals(MediaFormat.MIMETYPE_VIDEO_HEVC);
                MediaFormat fmt = MediaFormat.createVideoFormat(mime, width, height);
                fmt.setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
                int pixels = width * height;
                fmt.setInteger(MediaFormat.KEY_BIT_RATE, (hevc ? 5_000_000 : 8_000_000) * pixels / (1920 * 1080));
                fmt.setInteger(MediaFormat.KEY_FRAME_RATE, FPS);
                fmt.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1);
                fmt.setInteger(MediaFormat.KEY_PRIORITY, 0);          // real time
                if (android.os.Build.VERSION.SDK_INT >= 29) {
                    fmt.setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0);   // B-frames make it wait for later frames
                    fmt.setInteger(MediaFormat.KEY_PREPEND_HEADER_TO_SYNC_FRAMES, 1);   // each keyframe decodes on its own
                }
                // plain video colour (limited range, BT.709), which the computer's GPU takes as
                // is; some phones would otherwise send full-range "camera" colour
                fmt.setInteger(MediaFormat.KEY_COLOR_RANGE, MediaFormat.COLOR_RANGE_LIMITED);
                fmt.setInteger(MediaFormat.KEY_COLOR_STANDARD, MediaFormat.COLOR_STANDARD_BT709);
                fmt.setInteger(MediaFormat.KEY_COLOR_TRANSFER, MediaFormat.COLOR_TRANSFER_SDR_VIDEO);
                String name = new android.media.MediaCodecList(android.media.MediaCodecList.REGULAR_CODECS).findEncoderForFormat(fmt);
                if (name == null) continue;
                try {
                    MediaCodec c = MediaCodec.createByCodecName(name);
                    if (hevc && !c.getCodecInfo().isHardwareAccelerated()) { c.release(); continue; }
                    c.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
                    encoder = c;
                    codec = hevc ? "hevc" : "h264";
                    break;
                } catch (Exception e) {
                    // this encoder won't take it: try the next
                }
            }
            if (encoder == null) { showError("This phone can't encode video for Orbit Cam."); return; }
            encoderSurface = encoder.createInputSurface();
            encoder.start();

            SurfaceTexture st = preview.getSurfaceTexture();
            st.setDefaultBufferSize(width, height);
            previewSurface = new Surface(st);

            streaming = true;
            setStatus("Connecting", R.color.o_text_2);
            String url = base + "/api/webcam/stream?rot=" + rotation + "&codec=" + codec;
            sender = new Thread(() -> send(url, active.authToken), "webcam-send");
            sender.start();

            cameraThread = new HandlerThread("webcam-camera");
            cameraThread.start();
            cameraHandler = new Handler(cameraThread.getLooper());
            if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) return;
            cm.openCamera(id, new CameraDevice.StateCallback() {
                @Override public void onOpened(CameraDevice c) { camera = c; startSession(); }
                @Override public void onDisconnected(CameraDevice c) { c.close(); }
                @Override public void onError(CameraDevice c, int e) { c.close(); showError("The camera stopped (error " + e + ")."); }
            }, cameraHandler);
        } catch (Exception e) {
            showError("Couldn't start the camera: " + e.getMessage());
            stopStreaming();
        }
    }

    private void startSession() {
        try {
            camera.createCaptureSession(Arrays.asList(previewSurface, encoderSurface), new CameraCaptureSession.StateCallback() {
                @Override public void onConfigured(CameraCaptureSession s) {
                    session = s;
                    try {
                        CaptureRequest.Builder b = camera.createCaptureRequest(CameraDevice.TEMPLATE_RECORD);
                        b.addTarget(previewSurface);
                        b.addTarget(encoderSurface);
                        b.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, new Range<>(FPS, FPS));
                        b.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO);
                        s.setRepeatingRequest(b.build(), null, cameraHandler);
                    } catch (Exception e) {
                        showError("Couldn't start the camera: " + e.getMessage());
                    }
                }
                @Override public void onConfigureFailed(CameraCaptureSession s) { showError("The camera couldn't start."); }
            }, cameraHandler);
        } catch (CameraAccessException e) {
            showError("Couldn't start the camera: " + e.getMessage());
        }
    }

    /** Send the encoder's output to the computer as one long upload, until stopped. */
    private void send(String url, String token) {
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
            conn = c;
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setChunkedStreamingMode(16384);
            c.setConnectTimeout(4000);
            c.setRequestProperty("Content-Type", "video/h264");
            NetworkUtils.setAuth(c, token);
            OutputStream out = c.getOutputStream();
            boolean first = true;
            byte[] chunk = new byte[0];
            while (streaming) {
                MediaCodec enc = encoder;
                if (enc == null) break;
                int idx = enc.dequeueOutputBuffer(info, 20000);
                if (idx < 0) continue;
                ByteBuffer buf = enc.getOutputBuffer(idx);
                if (buf != null && info.size > 0) {
                    if (chunk.length < info.size) chunk = new byte[info.size];
                    buf.position(info.offset);
                    buf.get(chunk, 0, info.size);
                    out.write(chunk, 0, info.size);
                    out.flush();
                    if (first) {
                        first = false;
                        setStatus("Live", R.color.o_online);
                        runOnUiThread(this::brightenForAMoment);     // then it dims to save battery
                    }
                }
                enc.releaseOutputBuffer(idx, false);
            }
            out.close();
            int code = c.getResponseCode();
            if (code != 200 && streaming) showError("The computer stopped the stream (HTTP " + code + ").");
        } catch (Exception e) {
            if (streaming) {
                showError("Lost the computer: " + e.getMessage());
                runOnUiThread(this::stopStreaming);
            }
        }
    }

    private void stopStreaming() {
        streaming = false;
        try { if (session != null) session.close(); } catch (Exception ignored) { }
        session = null;
        try { if (camera != null) camera.close(); } catch (Exception ignored) { }
        camera = null;
        HttpURLConnection c = conn;
        conn = null;
        Thread t = sender;
        sender = null;
        if (t != null) {
            try { t.join(800); } catch (InterruptedException ignored) { }
        }
        if (c != null) c.disconnect();
        try { if (encoder != null) { encoder.stop(); encoder.release(); } } catch (Exception ignored) { }
        encoder = null;
        if (encoderSurface != null) encoderSurface.release();
        encoderSurface = null;
        if (previewSurface != null) previewSurface.release();
        previewSurface = null;
        if (cameraThread != null) cameraThread.quitSafely();
        cameraThread = null;
        setStatus("Off", R.color.o_text_3);
        runOnUiThread(() -> { ui.removeCallbacks(dim); setDimmed(false); });
    }

    /** While streaming, the screen sits nearly black: the camera and encoder keep working,
     *  the display doesn't need to. */
    private void setDimmed(boolean dimmed) {
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.screenBrightness = dimmed ? 0.01f : WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE;
        getWindow().setAttributes(lp);
    }

    private void brightenForAMoment() {
        setDimmed(false);
        ui.removeCallbacks(dim);
        ui.postDelayed(dim, streaming ? BRIGHT_FOR_MS : DIM_AFTER_MS);
    }

    private void setStatus(String text, int color) {
        runOnUiThread(() -> {
            tvStatus.setText(text);
            dot.setBackgroundTintList(android.content.res.ColorStateList.valueOf(getColor(color)));
            showShutter();
        });
    }

    /** The Live pill's action: Pause while the camera is on, Resume when it's off. */
    private void showShutter() {
        if (shutter == null) return;
        boolean on = streaming || waitingForLandscape;
        ivAction.setImageResource(on ? R.drawable.ic_o_pause : R.drawable.ic_o_play);
        tvAction.setText(on ? "Pause" : "Resume");
        shutter.setContentDescription(on ? "Pause the camera" : "Resume the camera");
        pausedCover.setVisibility(paused && !on ? View.VISIBLE : View.GONE);
    }

    /** Full screen like a camera app: the status and navigation bars come back with a swipe. */
    private void hideSystemBars() {
        if (android.os.Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
            android.view.WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                c.hide(android.view.WindowInsets.Type.systemBars());
                c.setSystemBarsBehavior(android.view.WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        } else {
            getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                    | View.SYSTEM_UI_FLAG_LAYOUT_STABLE | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
        }
    }

    /** A size this camera delivers to both the preview and the encoder: 16:9 and closest to
     *  720p if it has one, else the size closest to 720p. Asking for a size the camera doesn't
     *  have makes it stretch another one into it (a squeezed face). */
    private android.util.Size pictureSize(CameraCharacteristics cc) {
        android.hardware.camera2.params.StreamConfigurationMap map =
                cc.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        if (map == null) return new android.util.Size(wantW, wantH);
        java.util.Set<String> preview = new java.util.HashSet<>();
        for (android.util.Size z : map.getOutputSizes(SurfaceTexture.class)) preview.add(z.toString());
        android.util.Size best = null;
        double bestScore = Double.MAX_VALUE;
        for (android.util.Size z : map.getOutputSizes(MediaCodec.class)) {
            if (!preview.contains(z.toString()) || z.getWidth() % 16 != 0 || z.getHeight() % 2 != 0) continue;
            if (z.getWidth() > 1920) continue;               // more than needed: heat and battery
            boolean wide = Math.abs((double) z.getWidth() / z.getHeight() - 16.0 / 9) < 0.02;
            double score = Math.abs(z.getWidth() * z.getHeight() - wantW * wantH) / (double) (wantW * wantH)
                    + (wide ? 0 : 10);
            if (score < bestScore) { bestScore = score; best = z; }
        }
        return best != null ? best : new android.util.Size(wantW, wantH);
    }

    private String pickCamera(CameraManager cm) throws CameraAccessException {
        int want = front ? CameraCharacteristics.LENS_FACING_FRONT : CameraCharacteristics.LENS_FACING_BACK;
        String fallback = null;
        for (String id : cm.getCameraIdList()) {
            Integer facing = cm.getCameraCharacteristics(id).get(CameraCharacteristics.LENS_FACING);
            if (facing != null && facing == want) return id;
            if (fallback == null) fallback = id;
        }
        return fallback;
    }

    /** How far the computer must turn the picture so it's upright the way the phone is held. */
    private int pictureRotation(CameraCharacteristics cc) {
        Integer sensor = cc.get(CameraCharacteristics.SENSOR_ORIENTATION);
        int s = sensor != null ? sensor : 90;
        int display = 0;
        switch (getWindowManager().getDefaultDisplay().getRotation()) {
            case Surface.ROTATION_90: display = 90; break;
            case Surface.ROTATION_180: display = 180; break;
            case Surface.ROTATION_270: display = 270; break;
        }
        Integer facing = cc.get(CameraCharacteristics.LENS_FACING);
        boolean isFront = facing != null && facing == CameraCharacteristics.LENS_FACING_FRONT;
        return isFront ? (s + display) % 360 : (s - display + 360) % 360;
    }

    /** Give the preview the picture's shape, so it isn't stretched. */
    private void fitPreview(int rotation) {
        View frame = findViewById(R.id.frame_webcam_preview);
        frame.post(() -> {
            boolean tall = rotation == 90 || rotation == 270;
            float aspect = tall ? (float) height / width : (float) width / height;     // width / height
            int fw = frame.getWidth(), fh = frame.getHeight();
            // fill the whole screen (the screen is wider than 16:9: a little of the top and
            // bottom is cut off here; the computer still gets the whole picture)
            int w = fw, h = Math.round(fw / aspect);
            if (h < fh) { h = fh; w = Math.round(fh * aspect); }
            ViewGroup.LayoutParams lp = preview.getLayoutParams();
            lp.width = w;
            lp.height = h;
            preview.setLayoutParams(lp);
            final int vw = w, vh = h;
            preview.post(() -> turnPreview(vw, vh));
        });
    }

    /** Held sideways, Android doesn't turn the preview to match: the camera's picture comes
     *  out turned and stretched. Turn and scale it to the way the phone is held. */
    private void turnPreview(int viewW, int viewH) {
        int rotation = getWindowManager().getDefaultDisplay().getRotation();
        android.graphics.Matrix m = new android.graphics.Matrix();
        android.graphics.RectF view = new android.graphics.RectF(0, 0, viewW, viewH);
        float cx = view.centerX(), cy = view.centerY();
        if (rotation == Surface.ROTATION_90 || rotation == Surface.ROTATION_270) {
            android.graphics.RectF buffer = new android.graphics.RectF(0, 0, height, width);
            buffer.offset(cx - buffer.centerX(), cy - buffer.centerY());
            m.setRectToRect(view, buffer, android.graphics.Matrix.ScaleToFit.FILL);
            float scale = Math.max((float) viewH / height, (float) viewW / width);
            m.postScale(scale, scale, cx, cy);
            m.postRotate(90 * (rotation - 2), cx, cy);
        } else if (rotation == Surface.ROTATION_180) {
            m.postRotate(180, cx, cy);
        }
        preview.setTransform(m);
    }
}

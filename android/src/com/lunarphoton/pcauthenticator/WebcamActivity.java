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
    private static final int WANT_W = 1280, WANT_H = 720, FPS = 30, BITRATE = 4_000_000;
    // the size the camera really delivers (one it supports, so nothing gets stretched)
    private int width = WANT_W, height = WANT_H;
    private static final int REQ_CAMERA = 41;

    private TextureView preview;
    private TextView tvStatus, tvTarget, tvError;
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

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_webcam);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        // the picture's orientation is set when streaming starts: keep the screen as it is
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LOCKED);
        preview = findViewById(R.id.tv_webcam_preview);
        tvStatus = findViewById(R.id.tv_webcam_status);
        tvTarget = findViewById(R.id.tv_webcam_target);
        tvError = findViewById(R.id.tv_webcam_error);
        findViewById(R.id.btn_webcam_back).setOnClickListener(v -> finish());
        Button stop = findViewById(R.id.btn_webcam_stop);
        stop.setOnClickListener(v -> {
            if (streaming) { stopStreaming(); stop.setText("Start"); }
            else { startStreaming(); stop.setText("Stop"); }
        });
        findViewById(R.id.btn_webcam_switch).setOnClickListener(v -> {
            front = !front;
            getSharedPreferences("pc_auth_prefs", MODE_PRIVATE).edit().putBoolean(PREF_FRONT, front).apply();
            if (streaming) { stopStreaming(); startStreaming(); }
        });
        front = getSharedPreferences("pc_auth_prefs", MODE_PRIVATE).getBoolean(PREF_FRONT, true);
        PairedDevice active = DeviceManager.getActiveDevice(this);
        tvTarget.setText(active != null ? "Your camera, beamed to " + active.hostname : "No computer paired");
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
    protected void onPause() {
        super.onPause();
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
            tvStatus.setTextColor(getColor(R.color.o_text_2));
        });
    }

    private void startStreaming() {
        if (streaming) return;
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
            android.util.Size size = pictureSize(cc);
            width = size.getWidth();
            height = size.getHeight();
            int rotation = pictureRotation(cc);
            fitPreview(rotation);

            encoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
            MediaFormat fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height);
            fmt.setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
            fmt.setInteger(MediaFormat.KEY_BIT_RATE, BITRATE);
            fmt.setInteger(MediaFormat.KEY_FRAME_RATE, FPS);
            fmt.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1);
            fmt.setInteger(MediaFormat.KEY_PRIORITY, 0);          // real time
            encoder.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
            encoderSurface = encoder.createInputSurface();
            encoder.start();

            SurfaceTexture st = preview.getSurfaceTexture();
            st.setDefaultBufferSize(width, height);
            previewSurface = new Surface(st);

            streaming = true;
            setStatus("Connecting", R.color.o_text_2);
            String url = base + "/api/webcam/stream?rot=" + rotation;
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
        setStatus("Off", R.color.o_text_2);
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
        runOnUiThread(() -> { tvStatus.setText(text); tvStatus.setTextColor(getColor(color)); });
    }

    /** A size this camera delivers to both the preview and the encoder: 16:9 and closest to
     *  720p if it has one, else the size closest to 720p. Asking for a size the camera doesn't
     *  have makes it stretch another one into it (a squeezed face). */
    private android.util.Size pictureSize(CameraCharacteristics cc) {
        android.hardware.camera2.params.StreamConfigurationMap map =
                cc.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        if (map == null) return new android.util.Size(WANT_W, WANT_H);
        java.util.Set<String> preview = new java.util.HashSet<>();
        for (android.util.Size z : map.getOutputSizes(SurfaceTexture.class)) preview.add(z.toString());
        android.util.Size best = null;
        double bestScore = Double.MAX_VALUE;
        for (android.util.Size z : map.getOutputSizes(MediaCodec.class)) {
            if (!preview.contains(z.toString()) || z.getWidth() % 16 != 0 || z.getHeight() % 2 != 0) continue;
            if (z.getWidth() > 1920) continue;               // more than needed: heat and battery
            boolean wide = Math.abs((double) z.getWidth() / z.getHeight() - 16.0 / 9) < 0.02;
            double score = Math.abs(z.getWidth() * z.getHeight() - WANT_W * WANT_H) / (double) (WANT_W * WANT_H)
                    + (wide ? 0 : 10);
            if (score < bestScore) { bestScore = score; best = z; }
        }
        return best != null ? best : new android.util.Size(WANT_W, WANT_H);
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
            int w = fw, h = Math.round(fw / aspect);
            if (h > fh) { h = fh; w = Math.round(fh * aspect); }
            ViewGroup.LayoutParams lp = preview.getLayoutParams();
            lp.width = w;
            lp.height = h;
            preview.setLayoutParams(lp);
        });
    }
}

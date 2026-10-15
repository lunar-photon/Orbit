package com.lunarphoton.pcauthenticator;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.pm.PackageManager;
import android.graphics.ImageFormat;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.util.Size;
import android.view.WindowManager;

import java.nio.ByteBuffer;
import java.util.Collections;

/**
 * After a wrong unlock attempt: one photo with the front camera, sent to the computer, then
 * gone. It shows over the lock screen for the second it takes, with nothing drawn.
 */
public class UnlockPhotoActivity extends Activity {
    private static final String TAG = "UnlockPhoto";
    private HandlerThread thread;
    private Handler handler;
    private CameraDevice camera;
    private ImageReader reader;
    private int frames;
    private boolean sent;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (Build.VERSION.SDK_INT >= 27) setShowWhenLocked(true);
        else getWindow().addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED);
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) { finish(); return; }
        thread = new HandlerThread("unlock-photo");
        thread.start();
        handler = new Handler(thread.getLooper());
        handler.postDelayed(this::finish, 8000);           // never linger
        try {
            open();
        } catch (Exception e) {
            Log.w(TAG, "No photo: " + e.getMessage());
            finish();
        }
    }

    private void open() throws Exception {
        CameraManager cm = (CameraManager) getSystemService(Context.CAMERA_SERVICE);
        String id = null;
        for (String c : cm.getCameraIdList()) {
            Integer facing = cm.getCameraCharacteristics(c).get(CameraCharacteristics.LENS_FACING);
            if (facing != null && facing == CameraCharacteristics.LENS_FACING_FRONT) { id = c; break; }
        }
        if (id == null) { finish(); return; }
        StreamConfigurationMap map = cm.getCameraCharacteristics(id).get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        Size best = new Size(1280, 960);
        if (map != null) {
            Size[] sizes = map.getOutputSizes(ImageFormat.JPEG);
            long want = 1280L * 960;
            long gap = Long.MAX_VALUE;
            for (Size s : sizes) {
                long g = Math.abs((long) s.getWidth() * s.getHeight() - want);
                if (g < gap) { gap = g; best = s; }
            }
        }
        reader = ImageReader.newInstance(best.getWidth(), best.getHeight(), ImageFormat.JPEG, 2);
        reader.setOnImageAvailableListener(r -> {
            Image img = r.acquireLatestImage();
            if (img == null) return;
            try {
                // the first frames are dark while the exposure settles: take the 8th
                if (++frames < 8 || sent) return;
                sent = true;
                ByteBuffer buf = img.getPlanes()[0].getBuffer();
                byte[] jpeg = new byte[buf.remaining()];
                buf.get(jpeg);
                FindMy.sendPhoto(this, jpeg, "wrong_unlock");
                runOnUiThread(this::finish);
            } finally {
                img.close();
            }
        }, handler);
        cm.openCamera(id, new CameraDevice.StateCallback() {
            @Override public void onOpened(CameraDevice d) {
                camera = d;
                try {
                    CaptureRequest.Builder req = d.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
                    req.addTarget(reader.getSurface());
                    req.set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO);
                    req.set(CaptureRequest.JPEG_QUALITY, (byte) 85);
                    d.createCaptureSession(Collections.singletonList(reader.getSurface()), new CameraCaptureSession.StateCallback() {
                        @Override public void onConfigured(CameraCaptureSession s) {
                            try { s.setRepeatingRequest(req.build(), null, handler); } catch (Exception e) { finish(); }
                        }
                        @Override public void onConfigureFailed(CameraCaptureSession s) { finish(); }
                    }, handler);
                } catch (Exception e) {
                    finish();
                }
            }
            @Override public void onDisconnected(CameraDevice d) { d.close(); finish(); }
            @Override public void onError(CameraDevice d, int error) { d.close(); finish(); }
        }, handler);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        try { if (camera != null) camera.close(); } catch (Exception ignored) { }
        try { if (reader != null) reader.close(); } catch (Exception ignored) { }
        if (thread != null) thread.quitSafely();
    }
}

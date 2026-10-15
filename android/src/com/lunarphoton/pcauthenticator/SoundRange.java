package com.lunarphoton.pcauthenticator;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * How far the computer is, by sound (two-way acoustic ranging). Both record ~3 s; the computer
 * plays a rising chirp, this phone a falling one (16.5-19 kHz: most adults can't hear it). The
 * computer finds both chirps in both recordings and works out the distance, to a few centimetres;
 * through a wall neither hears the other ("not in this room"). Same Wi-Fi only.
 */
final class SoundRange {
    private static final int SR = 48000;

    private SoundRange() { }

    static boolean allowed(Context c) {
        return c.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
    }

    /** One measurement (blocking, about 3.5 s): the computer's answer, or null. */
    static JSONObject measure(Context c) {
        if (!allowed(c)) return null;
        PairedDevice active = DeviceManager.getActiveDevice(c);
        if (active == null || !active.isPaired()) return null;
        String base = active.getBaseUrl();
        if (base.startsWith("https://")) return null;          // the internet route: too slow for this

        AudioManager am = (AudioManager) c.getSystemService(Context.AUDIO_SERVICE);
        boolean raw = "true".equals(am.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED));
        int source = raw ? MediaRecorder.AudioSource.UNPROCESSED : MediaRecorder.AudioSource.VOICE_RECOGNITION;
        int min = AudioRecord.getMinBufferSize(SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord rec;
        try {
            rec = new AudioRecord(source, SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min, SR));
            if (rec.getState() != AudioRecord.STATE_INITIALIZED) return null;
        } catch (SecurityException | IllegalArgumentException e) {
            return null;
        }
        int volBefore = am.getStreamVolume(AudioManager.STREAM_MUSIC);
        int volMax = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
        ByteArrayOutputStream pcm = new ByteArrayOutputStream(SR * 2 * 4);
        AudioTrack track = null;
        try {
            rec.startRecording();
            long t0 = System.currentTimeMillis();
            Thread reader = new Thread(() -> {
                byte[] buf = new byte[4096];
                while (System.currentTimeMillis() - t0 < 3100) {
                    int n = rec.read(buf, 0, buf.length);
                    if (n > 0) pcm.write(buf, 0, n);
                }
            }, "sound-range");
            reader.start();
            String r = NetworkUtils.httpPostJsonWithAuth(base + "/api/findmy/sound/start", active.authToken, "{}", 4000);
            if (r == null || !r.contains("\"ok\"")) { reader.join(); return null; }
            // loud enough to cross a room (back to how it was afterwards)
            if (volBefore < volMax * 0.7) am.setStreamVolume(AudioManager.STREAM_MUSIC, (int) Math.ceil(volMax * 0.7), 0);
            short[] chirp = chirp(19000, 16500);
            track = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
                    .setAudioFormat(new AudioFormat.Builder().setSampleRate(SR).setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                    .setTransferMode(AudioTrack.MODE_STATIC).setBufferSizeInBytes(chirp.length * 2).build();
            track.write(chirp, 0, chirp.length);
            Thread.sleep(Math.max(0, 1100 - (System.currentTimeMillis() - t0)));
            track.play();
            reader.join();
            byte[] audio = pcm.toByteArray();
            return upload(base + "/api/findmy/sound/finish", active.authToken, audio);
        } catch (Exception e) {
            return null;
        } finally {
            try { rec.stop(); } catch (Exception ignored) { }
            rec.release();
            if (track != null) track.release();
            am.setStreamVolume(AudioManager.STREAM_MUSIC, volBefore, 0);
        }
    }

    private static short[] chirp(double f0, double f1) {
        int n = (int) (SR * 0.08);
        short[] out = new short[n + SR / 30];
        double k = (f1 - f0) / 0.08;
        for (int i = 0; i < n; i++) {
            double t = (double) i / SR;
            double w = Math.sqrt(0.5 - 0.5 * Math.cos(2 * Math.PI * i / (n - 1)));   // soft edges: no click
            out[i] = (short) (Math.sin(2 * Math.PI * (f0 * t + 0.5 * k * t * t)) * w * 0.95 * 32767);
        }
        return out;
    }

    private static JSONObject upload(String url, String token, byte[] audio) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        try {
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(5000);
            conn.setReadTimeout(10000);
            conn.setDoOutput(true);
            conn.setFixedLengthStreamingMode(audio.length);
            conn.setRequestProperty("Content-Type", "application/octet-stream");
            NetworkUtils.setAuth(conn, token);
            try (OutputStream os = conn.getOutputStream()) { os.write(audio); }
            if (conn.getResponseCode() != 200) return null;
            java.io.InputStream in = conn.getInputStream();
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) b.write(buf, 0, n);
            return new JSONObject(b.toString("UTF-8"));
        } finally {
            conn.disconnect();
        }
    }
}

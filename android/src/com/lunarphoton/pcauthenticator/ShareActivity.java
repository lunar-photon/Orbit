package com.lunarphoton.pcauthenticator;

import android.app.Activity;
import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.OpenableColumns;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class ShareActivity extends Activity {
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        Intent intent = getIntent();
        String action = intent.getAction();
        String type = intent.getType();

        if (Intent.ACTION_SEND.equals(action) && type != null) {
            if ("text/plain".equals(type) && !intent.hasExtra(Intent.EXTRA_STREAM)) {
                handleSendText(intent);
            } else {
                handleSendStream(intent);
            }
        } else if (Intent.ACTION_SEND_MULTIPLE.equals(action) && type != null) {
            handleSendMultipleStreams(intent);
        } else {
            finish();
        }
    }

    private void handleSendText(Intent intent) {
        String sharedText = intent.getStringExtra(Intent.EXTRA_TEXT);
        if (sharedText == null || sharedText.isEmpty()) {
            if (intent.getDataString() != null) {
                sharedText = intent.getDataString();
            } else {
                finish();
                return;
            }
        }

        String targetUrl = null;
        Matcher m = Pattern.compile("https?://[^\\s]+").matcher(sharedText);
        if (m.find()) {
            targetUrl = m.group();
        }

        final boolean isWebpage = (targetUrl != null && !targetUrl.isEmpty());
        final String payloadUrl = targetUrl;
        final String fullText = sharedText;

        mainHandler.post(() -> {
            if (isWebpage) {
                Toast.makeText(ShareActivity.this, "Opening webpage on computer...", Toast.LENGTH_SHORT).show();
            } else {
                Toast.makeText(ShareActivity.this, "Sending text to computer Clipboard...", Toast.LENGTH_SHORT).show();
            }
        });

        new Thread(() -> {
            try {
                PairedDevice active = DeviceManager.getActiveDevice(this);
                String endpoint = isWebpage ? "/api/open_url" : "/api/clipboard";
                String urlStr = active.getBaseUrl() + endpoint;
                HttpURLConnection conn = (HttpURLConnection) new URL(urlStr).openConnection();
                NetworkUtils.applyTunnelHeaders(conn);
                conn.setRequestMethod("POST");
                conn.setRequestProperty("Content-Type", "application/json");
                if (active.isPaired()) {
                    NetworkUtils.setAuth(conn, active.authToken);
                }
                conn.setDoOutput(true);
                conn.setConnectTimeout(5000);
                conn.setReadTimeout(5000);
                JSONObject body = new JSONObject();
                if (isWebpage) {
                    body.put("url", payloadUrl);
                } else {
                    body.put("text", fullText);
                }
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(body.toString().getBytes("UTF-8"));
                }
                int code = conn.getResponseCode();
                mainHandler.post(() -> {
                    if (code == 200) {
                        if (isWebpage) {
                            Toast.makeText(ShareActivity.this, "Webpage opened in computer browser!", Toast.LENGTH_SHORT).show();
                        } else {
                            Toast.makeText(ShareActivity.this, "Copied to computer Clipboard!", Toast.LENGTH_SHORT).show();
                        }
                    } else {
                        Toast.makeText(ShareActivity.this, "Failed to send (HTTP " + code + ")", Toast.LENGTH_SHORT).show();
                    }
                });
            } catch (Exception e) {
                mainHandler.post(() -> Toast.makeText(ShareActivity.this, "Error: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            } finally {
                mainHandler.post(this::finish);
            }
        }).start();
    }

    private void handleSendStream(Intent intent) {
        Uri uri = intent.getParcelableExtra(Intent.EXTRA_STREAM);
        if (uri == null) {
            finish();
            return;
        }

        ArrayList<Uri> list = new ArrayList<>();
        list.add(uri);
        uploadUris(list);
    }

    private void handleSendMultipleStreams(Intent intent) {
        ArrayList<Uri> uris = intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM);
        if (uris == null || uris.isEmpty()) {
            finish();
            return;
        }
        uploadUris(uris);
    }

    private void uploadUris(ArrayList<Uri> uris) {
        Toast.makeText(this, "Sending " + uris.size() + " item(s) to computer...", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            PairedDevice active = DeviceManager.getActiveDevice(this);
            String uploadUrl = active.getBaseUrl() + "/api/files/upload";
            int success = 0;

            for (Uri uri : uris) {
                String filename = getFileName(uri);
                try {
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

                    if (conn.getResponseCode() == 200) {
                        success++;
                    }
                } catch (Exception ignored) {}
            }

            final int finalSuccess = success;
            mainHandler.post(() -> {
                int failed = uris.size() - finalSuccess;
                if (failed == 0) {
                    Toast.makeText(ShareActivity.this, "Sent " + finalSuccess + (finalSuccess == 1 ? " file" : " files") + " to the computer's Downloads", Toast.LENGTH_LONG).show();
                } else if (finalSuccess == 0) {
                    Toast.makeText(ShareActivity.this, "Failed to send " + (failed == 1 ? "the file" : "the files") + ". Try again", Toast.LENGTH_LONG).show();
                } else {
                    Toast.makeText(ShareActivity.this, "Sent " + finalSuccess + ", failed " + failed + ". Send the rest again", Toast.LENGTH_LONG).show();
                }
                finish();
            });
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
}

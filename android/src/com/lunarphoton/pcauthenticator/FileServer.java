package com.lunarphoton.pcauthenticator;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.media.MediaScannerConnection;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.content.ContentUris;
import android.content.ContentValues;
import android.database.Cursor;
import android.provider.MediaStore;
import java.util.HashSet;
import java.util.Set;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class FileServer {
    private static final String TAG = "PCAuthFileServer";
    public static final int PORT = 1761;
    public static final String CHANNEL_FILE = "pc_connect_files";
    public static final int NOTIF_BASE_ID = 3000;

    private final Context context;
    private ServerSocket serverSocket;
    private volatile boolean isRunning = false;
    private volatile Thread serverThread;
    private final ExecutorService threadPool = Executors.newCachedThreadPool();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    public FileServer(Context context) {
        this.context = context;
        try {
            android.os.StrictMode.VmPolicy.Builder builder = new android.os.StrictMode.VmPolicy.Builder();
            android.os.StrictMode.setVmPolicy(builder.build());
        } catch (Exception ignored) {}
        createNotificationChannel();
    }

    public synchronized void start() {
        if (isRunning) return;
        isRunning = true;
        threadPool.execute(this::runServer);
    }

    public synchronized void stop() {
        isRunning = false;
        try {
            if (serverSocket != null && !serverSocket.isClosed()) {
                serverSocket.close();
            }
        } catch (Exception ignored) {}
        threadPool.shutdownNow();
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager nm = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) {
                NotificationChannel chan = new NotificationChannel(
                        CHANNEL_FILE,
                        "Orbit File Transfers",
                        NotificationManager.IMPORTANCE_HIGH
                );
                chan.setDescription("Notifications for files received from computer");
                chan.enableVibration(true);
                chan.enableLights(true);
                nm.createNotificationChannel(chan);
            }
        }
    }

    /** Is it accepting connections? (It stops for good if its socket fails.) */
    public boolean isAlive() {
        ServerSocket ss = serverSocket;
        return isRunning && ss != null && !ss.isClosed() && serverThread != null && serverThread.isAlive();
    }

    private void runServer() {
        serverThread = Thread.currentThread();
        try {
            serverSocket = new ServerSocket(PORT);
            serverSocket.setReuseAddress(true);
            Log.i(TAG, "FileServer listening on port " + PORT);

            while (isRunning) {
                try {
                    Socket client = serverSocket.accept();
                    client.setSoTimeout(30000);
                    threadPool.execute(() -> handleClient(client));
                } catch (Exception e) {
                    if (!isRunning) break;
                }
            }
        } catch (Exception e) {
            Log.e(TAG, "Server error on port " + PORT, e);
        } finally {
            serverThread = null;            // its pool thread lives on: isAlive must not count it
        }
    }

    private void handleClient(Socket socket) {
        try (InputStream rawIn = new BufferedInputStream(socket.getInputStream());
             OutputStream rawOut = new BufferedOutputStream(socket.getOutputStream())) {

            // Read request line and headers
            StringBuilder headerLines = new StringBuilder();
            int c;
            int consecutiveNewlines = 0;
            while ((c = rawIn.read()) != -1) {
                headerLines.append((char) c);
                if (c == '\n') {
                    consecutiveNewlines++;
                    if (consecutiveNewlines >= 2 || headerLines.toString().endsWith("\r\n\r\n") || headerLines.toString().endsWith("\n\n")) {
                        break;
                    }
                } else if (c != '\r') {
                    consecutiveNewlines = 0;
                }
            }

            String headerStr = headerLines.toString();
            String[] lines = headerStr.split("\r?\n");
            if (lines.length == 0 || lines[0].isEmpty()) return;

            String[] reqParts = lines[0].split(" ");
            if (reqParts.length < 2) return;
            String method = reqParts[0].toUpperCase();
            String fullUri = reqParts[1];

            String path = fullUri;
            Map<String, String> queryParams = new HashMap<>();
            if (fullUri.contains("?")) {
                path = fullUri.substring(0, fullUri.indexOf("?"));
                String q = fullUri.substring(fullUri.indexOf("?") + 1);
                for (String param : q.split("&")) {
                    String[] pair = param.split("=", 2);
                    if (pair.length == 2) {
                        try {
                            queryParams.put(URLDecoder.decode(pair[0], "UTF-8"), URLDecoder.decode(pair[1], "UTF-8"));
                        } catch (Exception ignored) {}
                    }
                }
            }

            Map<String, String> headers = new HashMap<>();
            for (int i = 1; i < lines.length; i++) {
                String line = lines[i];
                int colon = line.indexOf(":");
                if (colon > 0) {
                    headers.put(line.substring(0, colon).trim().toLowerCase(), line.substring(colon + 1).trim());
                }
            }

            // Route endpoints
            if (method.equals("OPTIONS")) {
                sendOptions(rawOut);
                return;
            } else if (method.equals("GET") && path.equals("/api/ping")) {
                sendJson(rawOut, new JSONObject().put("status", "ok").put("service", "pc-connect-phone"));
                return;
            }

            // Require paired bearer authentication for all sensitive operations
            if (!isAuthorized(headers)) {
                sendError(rawOut, 401, "Unauthorized");
                return;
            }

            if (method.equals("POST") && path.equals("/api/ring")) {
                RingManager.startAlarm(context);
                sendJson(rawOut, new JSONObject().put("status", "ok"));
            } else if (method.equals("POST") && path.equals("/api/unring")) {
                RingManager.stopAlarm(context);
                sendJson(rawOut, new JSONObject().put("status", "ok"));
            } else if (method.equals("GET") && path.equals("/api/clipboard")) {
                handleGetClipboard(rawOut);
            } else if (method.equals("POST") && path.equals("/api/clipboard")) {
                handleSetClipboard(rawIn, headers, rawOut);
            } else if (method.equals("GET") && path.equals("/api/files/list")) {
                handleFileList(queryParams.get("path"), rawOut);
            } else if (method.equals("GET") && path.equals("/api/files/download")) {
                handleFileDownload(queryParams.get("path"), rawOut);
            } else if (method.equals("POST") && path.equals("/api/files/upload")) {
                handleFileUpload(queryParams.get("path"), headers, rawIn, rawOut);
            } else if (method.equals("POST") && path.equals("/api/files/delete")) {
                handleFileDelete(queryParams.get("path"), rawOut);
            } else if (method.equals("POST") && path.equals("/api/files/mkdir")) {
                handleFileMkdir(queryParams.get("path"), queryParams.get("name"), rawOut);
            } else if (method.equals("GET") && (path.equals("/api/logs") || path.equals("/api/logcat"))) {
                handleLogs(rawOut);
            } else {
                sendError(rawOut, 404, "Not Found");
            }

        } catch (Exception e) {
            Log.e(TAG, "Client handling exception", e);
        } finally {
            try { socket.close(); } catch (Exception ignored) {}
        }
    }

    private void handleLogs(OutputStream out) throws Exception {
        JSONObject res = new JSONObject();
        res.put("status", "ok");
        res.put("captive_last_status", CaptivePortalManager.getLastStatus(context));
        res.put("captive_enabled", CaptivePortalManager.isEnabled(context));
        res.put("captive_gateway", CaptivePortalManager.getGatewayUrl(context));
        res.put("captive_user", CaptivePortalManager.getUsername(context));

        JSONArray history = new JSONArray();
        for (String line : CaptivePortalManager.getDebugLogs(context)) {
            history.put(line);
        }
        res.put("captive_debug_logs", history);

        Network wifiNet = DeviceManager.getWifiNetwork(context);
        res.put("wifi_connected", wifiNet != null);
        if (wifiNet != null) {
            ConnectivityManager cm = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm != null) {
                NetworkCapabilities caps = cm.getNetworkCapabilities(wifiNet);
                res.put("wifi_capabilities", caps != null ? caps.toString() : "null");
            }
        }

        try {
            Process process = Runtime.getRuntime().exec(new String[]{"logcat", "-d", "-t", "400", "-v", "time"});
            BufferedReader reader = new BufferedReader(new InputStreamReader(process.getInputStream(), "UTF-8"));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                sb.append(line).append("\n");
            }
            res.put("logcat", sb.toString());
        } catch (Exception e) {
            res.put("logcat_error", e.getMessage());
        }
        sendJson(out, res);
    }

    private void handleFileList(String targetPath, OutputStream out) throws Exception {
        if (targetPath == null || targetPath.isEmpty() || targetPath.equals("/") || targetPath.equals("shortcuts")) {
            // Return user-friendly shortcut directories
            JSONArray arr = new JSONArray();
            File storageRoot = Environment.getExternalStorageDirectory();
            String rootPath = (storageRoot != null) ? storageRoot.getAbsolutePath() : "/storage/emulated/0";

            addShortcut(arr, "Internal Storage", rootPath);
            addShortcut(arr, "Camera & Photos", rootPath + "/DCIM");
            addShortcut(arr, "Downloads", rootPath + "/Download");
            addShortcut(arr, "Documents", rootPath + "/Documents");
            addShortcut(arr, "Pictures", rootPath + "/Pictures");
            addShortcut(arr, "Music", rootPath + "/Music");
            addShortcut(arr, "Movies", rootPath + "/Movies");

            sendJson(out, arr);
            return;
        }

        File dir = new File(targetPath);
        if (!dir.exists() || !dir.isDirectory()) {
            sendError(out, 404, "Directory Not Found");
            return;
        }

        File[] files = dir.listFiles();
        List<File> fileList = new ArrayList<>();
        if (files != null) {
            fileList.addAll(Arrays.asList(files));
        }

        // Enrich with MediaStore to discover media/download files hidden by scoped storage
        enrichWithMediaStore(targetPath, fileList);

        // Sort: directories first, then alphabetically
        Collections.sort(fileList, (a, b) -> {
            if (a.isDirectory() && !b.isDirectory()) return -1;
            if (!a.isDirectory() && b.isDirectory()) return 1;
            return a.getName().compareToIgnoreCase(b.getName());
        });

        JSONArray arr = new JSONArray();
        for (File f : fileList) {
            JSONObject obj = new JSONObject();
            obj.put("name", f.getName());
            obj.put("path", f.getAbsolutePath());
            obj.put("is_dir", f.isDirectory());
            obj.put("size", f.isDirectory() ? 0 : f.length());
            obj.put("mtime", f.lastModified() / 1000);
            arr.put(obj);
        }
        sendJson(out, arr);
    }

    private void enrichWithMediaStore(String targetPath, List<File> fileList) {
        try {
            Set<String> existingNames = new HashSet<>();
            for (File f : fileList) {
                existingNames.add(f.getName());
            }

            File targetDir = new File(targetPath);
            String targetCanonical = targetDir.getCanonicalPath();

            Uri queryUri = MediaStore.Files.getContentUri("external");
            String[] projection = {
                    MediaStore.MediaColumns.DATA,
                    MediaStore.MediaColumns.DISPLAY_NAME,
                    MediaStore.MediaColumns.SIZE,
                    MediaStore.MediaColumns.DATE_MODIFIED
            };
            String selection = MediaStore.MediaColumns.DATA + " LIKE ?";
            String[] selectionArgs = new String[]{ targetCanonical + "/%" };

            try (Cursor cursor = context.getContentResolver().query(queryUri, projection, selection, selectionArgs, null)) {
                if (cursor != null) {
                    int dataCol = cursor.getColumnIndex(MediaStore.MediaColumns.DATA);
                    while (cursor.moveToNext()) {
                        String filePath = (dataCol >= 0) ? cursor.getString(dataCol) : null;
                        if (filePath != null && !filePath.isEmpty()) {
                            File f = new File(filePath);
                            File parent = f.getParentFile();
                            if (parent != null && parent.getCanonicalPath().equals(targetCanonical)) {
                                if (!existingNames.contains(f.getName())) {
                                    fileList.add(f);
                                    existingNames.add(f.getName());
                                }
                            }
                        }
                    }
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "MediaStore query fallback error", e);
        }
    }

    public static Uri getContentUriForPath(Context context, String path) {
        try {
            Uri queryUri = MediaStore.Files.getContentUri("external");
            String[] projection = { MediaStore.MediaColumns._ID };
            String selection = MediaStore.MediaColumns.DATA + "=?";
            String[] selectionArgs = new String[]{ path };
            try (Cursor cursor = context.getContentResolver().query(queryUri, projection, selection, selectionArgs, null)) {
                if (cursor != null && cursor.moveToFirst()) {
                    long id = cursor.getLong(cursor.getColumnIndexOrThrow(MediaStore.MediaColumns._ID));
                    return ContentUris.withAppendedId(queryUri, id);
                }
            }
        } catch (Exception ignored) {}
        return null;
    }

    private Uri getContentUriForPath(String path) {
        return getContentUriForPath(context, path);
    }

    private void addShortcut(JSONArray arr, String name, String path) throws Exception {
        File f = new File(path);
        JSONObject obj = new JSONObject();
        obj.put("name", name);
        obj.put("path", path);
        obj.put("is_dir", true);
        obj.put("size", 0);
        obj.put("mtime", f.exists() ? f.lastModified() / 1000 : 0);
        arr.put(obj);
    }

    private void handleFileDownload(String path, OutputStream out) throws Exception {
        if (path == null || path.isEmpty()) {
            sendError(out, 400, "Missing path parameter");
            return;
        }
        File f = new File(path);
        InputStream fis = null;
        long length = 0;

        if (f.exists() && f.canRead()) {
            length = f.length();
            try {
                fis = new FileInputStream(f);
            } catch (Exception ignored) {}
        }

        if (fis == null) {
            // Try ContentResolver via MediaStore
            Uri contentUri = getContentUriForPath(path);
            if (contentUri != null) {
                try {
                    fis = context.getContentResolver().openInputStream(contentUri);
                    try (Cursor c = context.getContentResolver().query(contentUri, new String[]{MediaStore.MediaColumns.SIZE}, null, null, null)) {
                        if (c != null && c.moveToFirst()) {
                            length = c.getLong(0);
                        }
                    }
                } catch (Exception ignored) {}
            }
        }

        if (fis == null) {
            sendError(out, 404, "File Not Found or Permission Denied");
            return;
        }

        String filename = URLEncoder.encode(f.getName(), "UTF-8").replace("+", "%20");
        String header = "HTTP/1.1 200 OK\r\n" +
                "Content-Type: application/octet-stream\r\n" +
                (length > 0 ? "Content-Length: " + length + "\r\n" : "") +
                "Content-Disposition: attachment; filename=\"" + filename + "\"\r\n" +
                "Access-Control-Allow-Origin: *\r\n\r\n";
        out.write(header.getBytes("UTF-8"));

        try {
            byte[] buf = new byte[65536];
            int read;
            while ((read = fis.read(buf)) != -1) {
                out.write(buf, 0, read);
            }
        } finally {
            try { fis.close(); } catch (Exception ignored) {}
        }
        out.flush();
    }

    private void handleFileUpload(String targetDir, Map<String, String> headers, InputStream in, OutputStream out) throws Exception {
        String destDirPath = (targetDir != null && !targetDir.isEmpty()) ? targetDir : Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS).getAbsolutePath();
        File destDir = new File(destDirPath);
        try {
            if (!destDir.exists()) destDir.mkdirs();
        } catch (Exception ignored) {}

        String rawFn = headers.get("x-filename");
        String filename = "received_file_" + System.currentTimeMillis();
        if (rawFn != null && !rawFn.isEmpty()) {
            try {
                filename = new File(URLDecoder.decode(rawFn, "UTF-8")).getName();
            } catch (Exception ignored) {}
        }

        File destFile = new File(destDir, filename);
        String nameOnly = filename;
        String ext = "";
        int dotIdx = filename.lastIndexOf(".");
        if (dotIdx > 0) {
            nameOnly = filename.substring(0, dotIdx);
            ext = filename.substring(dotIdx);
        }
        int counter = 1;
        while (destFile.exists()) {
            destFile = new File(destDir, nameOnly + " (" + counter + ")" + ext);
            counter++;
        }

        long contentLength = 0;
        try {
            contentLength = Long.parseLong(headers.get("content-length"));
        } catch (Exception ignored) {}

        OutputStream fos = null;

        // 1. Try direct FileOutputStream (works when MANAGE_EXTERNAL_STORAGE is granted or Android < 10)
        try {
            fos = new FileOutputStream(destFile);
        } catch (Exception directEx) {
            Log.w(TAG, "Direct FileOutputStream failed, trying MediaStore: " + directEx.getMessage());
            // 2. Fallback to MediaStore.Downloads (standard scoped storage API, no permission required)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                try {
                    ContentValues values = new ContentValues();
                    values.put(MediaStore.MediaColumns.DISPLAY_NAME, destFile.getName());
                    values.put(MediaStore.MediaColumns.MIME_TYPE, "application/octet-stream");
                    values.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
                    Uri insertedUri = context.getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                    if (insertedUri != null) {
                        fos = context.getContentResolver().openOutputStream(insertedUri);
                    }
                } catch (Exception mediaEx) {
                    Log.e(TAG, "MediaStore insert error", mediaEx);
                }
            }
        }

        if (fos == null) {
            showTransferFailed(context, filename, "the phone can't save files here (allow 'All files access')");
            sendError(out, 403, "Permission Denied: Please enable 'All files access' in phone app settings.");
            return;
        }

        long written = 0;
        String failure = null;
        try {
            byte[] buf = new byte[65536];
            long remaining = contentLength > 0 ? contentLength : Long.MAX_VALUE;
            while (remaining > 0) {
                int toRead = (int) Math.min(buf.length, remaining);
                int r = in.read(buf, 0, toRead);
                if (r == -1) break;
                fos.write(buf, 0, r);
                written += r;
                if (contentLength > 0) remaining -= r;
            }
            fos.flush();
        } catch (Exception e) {
            failure = e.getMessage() != null ? e.getMessage() : "write error";
        } finally {
            try { fos.close(); } catch (Exception ignored) {}
        }
        // A connection that drops mid-way just ends the stream: check every byte arrived, or
        // a cut-off file (an APK that won't install) would be reported as received
        if (failure == null && contentLength > 0 && written != contentLength) {
            failure = "the transfer was cut off (" + formatFileSize(written) + " of " + formatFileSize(contentLength) + ")";
        }
        if (failure != null) {
            try { destFile.delete(); } catch (Exception ignored) {}
            showTransferFailed(context, filename, failure);
            sendError(out, 500, "Receive failed: " + failure);
            return;
        }

        // Notify MediaStore so Gallery / Files apps see it immediately
        final File finalFile = destFile;
        String mimeType = PCFileProvider.getMimeType(destFile.getAbsolutePath());
        try {
            MediaScannerConnection.scanFile(context, new String[]{destFile.getAbsolutePath()}, new String[]{mimeType}, null);
        } catch (Exception ignored) {}

        // Show system notification
        showFileReceivedNotification(finalFile);

        JSONObject res = new JSONObject();
        res.put("status", "ok");
        res.put("filename", finalFile.getName());
        res.put("path", finalFile.getAbsolutePath());
        sendJson(out, res);
    }

    private void handleFileDelete(String path, OutputStream out) throws Exception {
        if (path != null && !path.isEmpty()) {
            File f = new File(path);
            if (f.exists()) {
                deleteRecursively(f);
            }
        }
        sendJson(out, new JSONObject().put("status", "ok"));
    }

    private void deleteRecursively(File fileOrDir) {
        if (fileOrDir.isDirectory()) {
            File[] files = fileOrDir.listFiles();
            if (files != null) {
                for (File child : files) {
                    deleteRecursively(child);
                }
            }
        }
        fileOrDir.delete();
    }

    private void handleFileMkdir(String parentPath, String name, OutputStream out) throws Exception {
        if (parentPath != null && name != null) {
            File newFolder = new File(parentPath, name);
            newFolder.mkdirs();
        }
        sendJson(out, new JSONObject().put("status", "ok"));
    }

    private void handleGetClipboard(OutputStream out) throws Exception {
        final String[] result = new String[]{""};
        final Object lock = new Object();
        mainHandler.post(() -> {
            try {
                ClipboardManager cm = (ClipboardManager) context.getSystemService(Context.CLIPBOARD_SERVICE);
                if (cm != null && cm.hasPrimaryClip()) {
                    ClipData clip = cm.getPrimaryClip();
                    if (clip != null && clip.getItemCount() > 0) {
                        CharSequence text = clip.getItemAt(0).getText();
                        if (text != null) result[0] = text.toString();
                    }
                }
            } catch (Exception ignored) {}
            synchronized (lock) { lock.notify(); }
        });
        synchronized (lock) { lock.wait(1000); }

        sendJson(out, new JSONObject().put("status", "ok").put("text", result[0]));
    }

    private void handleSetClipboard(InputStream in, Map<String, String> headers, OutputStream out) throws Exception {
        int len = 0;
        try { len = Integer.parseInt(headers.get("content-length")); } catch (Exception ignored) {}
        byte[] bodyBytes = new byte[len];
        int r = 0;
        while (r < len) {
            int cur = in.read(bodyBytes, r, len - r);
            if (cur == -1) break;
            r += cur;
        }
        String body = new String(bodyBytes, "UTF-8");
        JSONObject json = new JSONObject(body);
        String text = json.optString("text", "");

        if (!text.isEmpty()) {
            mainHandler.post(() -> {
                try {
                    ClipboardManager cm = (ClipboardManager) context.getSystemService(Context.CLIPBOARD_SERVICE);
                    if (cm != null) {
                        cm.setPrimaryClip(ClipData.newPlainText("Orbit", text));
                    }
                } catch (Exception ignored) {}
            });
        }
        sendJson(out, new JSONObject().put("status", "ok"));
    }

    /** A file from the computer didn't arrive whole: say so (and that it can be sent again). */
    public static void showTransferFailed(Context context, String filename, String reason) {
        try {
            NotificationManager nm = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) return;
            android.app.Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                    ? new android.app.Notification.Builder(context, CHANNEL_FILE)
                    : new android.app.Notification.Builder(context);
            builder.setSmallIcon(R.drawable.ic_o_notify)
                    .setContentTitle("Couldn't receive " + filename)
                    .setContentText("Failed: " + reason + ". Send it again from the computer.")
                    .setStyle(new android.app.Notification.BigTextStyle()
                            .bigText("Failed: " + reason + ". Send it again from the computer."))
                    .setAutoCancel(true);
            nm.notify((int) (NOTIF_BASE_ID + 500 + (System.currentTimeMillis() % 400)), builder.build());
        } catch (Throwable t) {
            Log.e(TAG, "Error posting failure notification: " + t.getMessage(), t);
        }
    }

    private void showFileReceivedNotification(File file) {
        try {
            NotificationManager nm = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) return;

            String mimeType = PCFileProvider.getMimeType(file.getAbsolutePath());
            Uri fileUri = PCFileProvider.getUriForFile(context, file);
            boolean isApk = file.getName().toLowerCase().endsWith(".apk") || "application/vnd.android.package-archive".equals(mimeType);
            if (isApk) {
                mimeType = "application/vnd.android.package-archive";
            }

            Intent viewIntent;
            if (isApk && Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !context.getPackageManager().canRequestPackageInstalls()) {
                viewIntent = new Intent(context, MainActivity.class);
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
                List<ResolveInfo> resInfoList = context.getPackageManager().queryIntentActivities(viewIntent, PackageManager.MATCH_DEFAULT_ONLY);
                for (ResolveInfo resolveInfo : resInfoList) {
                    context.grantUriPermission(resolveInfo.activityInfo.packageName, fileUri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
                }
            } catch (Exception ignored) {}

            if (isApk) {
                String[] commonInstallers = {"com.google.android.packageinstaller", "com.android.packageinstaller", "com.google.android.permissioncontroller"};
                for (String pkg : commonInstallers) {
                    try {
                        context.grantUriPermission(pkg, fileUri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    } catch (Exception ignored) {}
                }
            }

            PendingIntent pi = PendingIntent.getActivity(
                    context, (int) System.currentTimeMillis(), viewIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0)
            );

            android.app.Notification.Builder builder;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                builder = new android.app.Notification.Builder(context, CHANNEL_FILE);
            } else {
                builder = new android.app.Notification.Builder(context);
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
                builder.setContentTitle("App received")
                        .setContentText("Tap to install " + file.getName())
                        .setSubText("Install APK");
            } else {
                builder.setContentTitle(typeLabel + " received from computer")
                        .setContentText(file.getName() + " (" + formatFileSize(file.length()) + ")")
                        .setSubText(typeLabel);
            }

            builder.setSmallIcon(R.drawable.ic_o_notify)
                    .setAutoCancel(true)
                    .setContentIntent(pi);

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                builder.setPriority(android.app.Notification.PRIORITY_HIGH)
                        .setVibrate(new long[]{0, 250, 150, 250});
            }

            nm.notify((int) (NOTIF_BASE_ID + (System.currentTimeMillis() % 1000)), builder.build());
            Log.i(TAG, "Notification posted for received file: " + file.getName() + " (" + mimeType + ")");
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

    private void sendOptions(OutputStream out) throws Exception {
        String res = "HTTP/1.1 200 OK\r\n" +
                "Access-Control-Allow-Origin: *\r\n" +
                "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n" +
                "Access-Control-Allow-Headers: *\r\n" +
                "Content-Length: 0\r\n\r\n";
        out.write(res.getBytes("UTF-8"));
        out.flush();
    }

    private void sendJson(OutputStream out, Object obj) throws Exception {
        String data = obj.toString();
        byte[] bytes = data.getBytes("UTF-8");
        String header = "HTTP/1.1 200 OK\r\n" +
                "Content-Type: application/json; charset=utf-8\r\n" +
                "Content-Length: " + bytes.length + "\r\n" +
                "Access-Control-Allow-Origin: *\r\n\r\n";
        out.write(header.getBytes("UTF-8"));
        out.write(bytes);
        out.flush();
    }

    private boolean isAuthorized(Map<String, String> headers) {
        String authHeader = headers.get("authorization");
        if (authHeader == null || !authHeader.toLowerCase().startsWith("bearer ")) {
            return false;
        }
        String token = authHeader.substring(7).trim();
        if (token.isEmpty()) {
            return false;
        }

        try {
            List<PairedDevice> devices = DeviceManager.getDevices(context);
            byte[] tokenBytes = token.getBytes(java.nio.charset.StandardCharsets.UTF_8);
            for (PairedDevice d : devices) {
                if (d.authToken != null && !d.authToken.isEmpty()) {
                    byte[] devBytes = d.authToken.getBytes(java.nio.charset.StandardCharsets.UTF_8);
                    if (java.security.MessageDigest.isEqual(tokenBytes, devBytes)) {
                        return true;
                    }
                }
            }
        } catch (Exception ignored) {}
        return false;
    }

    private void sendError(OutputStream out, int code, String msg) throws Exception {
        byte[] bytes = ("{\"error\":\"" + msg + "\"}").getBytes("UTF-8");
        String header = "HTTP/1.1 " + code + " " + msg + "\r\n" +
                "Content-Type: application/json\r\n" +
                "Content-Length: " + bytes.length + "\r\n" +
                "Access-Control-Allow-Origin: *\r\n\r\n";
        out.write(header.getBytes("UTF-8"));
        out.write(bytes);
        out.flush();
    }
}

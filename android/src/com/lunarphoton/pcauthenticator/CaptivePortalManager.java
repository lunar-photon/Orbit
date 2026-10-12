package com.lunarphoton.pcauthenticator;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Network;
import android.os.Handler;
import android.os.Looper;
import android.os.Build;
import android.text.Html;
import android.util.Log;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.SocketException;
import java.net.URL;
import java.net.URLEncoder;
import java.net.UnknownHostException;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

public class CaptivePortalManager {
    private static final String TAG = "CaptivePortalManager";
    private static final String PREF_NAME = "pc_auth_prefs";

    public static final String PREF_ENABLED = "captive_portal_enabled";
    public static final String PREF_GATEWAY_URL = "captive_portal_gateway_url";
    public static final String PREF_USERNAME = "captive_portal_username";
    public static final String PREF_PASSWORD = "captive_portal_password";
    public static final String PREF_AUTOLOGIN_WIFI = "captive_portal_autologin_wifi";
    public static final String PREF_CHECK_SCREEN_ON = "captive_portal_check_screen_on";
    public static final String PREF_EXPLICIT_LOGOUT = "captive_portal_explicit_logout";
    public static final String PREF_LAST_STATUS = "captive_portal_last_status";
    public static final String PREF_LAST_MSG = "captive_portal_last_msg";
    public static final String PREF_LAST_TIME = "captive_portal_last_time";

    public static final String DEFAULT_GATEWAY_URL = "https://gateway.iisertvm.ac.in:8090/login.xml";
    public static final String DEFAULT_USERNAME = "";   // entered by the user in Settings
    public static final String DEFAULT_PASSWORD = "";

    private static final String GATEWAY_HOST = "gateway.iisertvm.ac.in";
    private static final String GATEWAY_IP = "172.16.31.101";

    /** Credentials only ever go to the campus gateway, wherever a network's redirect points. */
    static boolean isCampusGateway(String url) {
        try {
            String host = new URL(url.trim()).getHost().toLowerCase(java.util.Locale.US);
            return host.equals(GATEWAY_HOST) || host.endsWith(".iisertvm.ac.in") || host.equals(GATEWAY_IP);
        } catch (Exception e) {
            return false;
        }
    }

    /** Normal certificate checks (the gateway has a public certificate). When reached by its IP
     *  (campus DNS down) the certificate still has to be the gateway's own. */
    private static void verifyCampusCertificate(HttpsURLConnection conn) {
        javax.net.ssl.HostnameVerifier standard = HttpsURLConnection.getDefaultHostnameVerifier();
        conn.setHostnameVerifier((host, session) ->
                standard.verify(GATEWAY_IP.equals(host) ? GATEWAY_HOST : host, session));
    }

    public static class LoginResult {
        public final boolean success;
        public final String status;
        public final String message;
        public final long latencyMs;

        public LoginResult(boolean success, String status, String message, long latencyMs) {
            this.success = success;
            this.status = status;
            this.message = message;
            this.latencyMs = latencyMs;
        }
    }

    public interface Callback {
        void onResult(LoginResult result);
    }

    public static boolean isEnabled(Context context) {
        if (context == null) return false;
        SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
        return prefs.getBoolean(PREF_ENABLED, false);
    }

    public static void setEnabled(Context context, boolean enabled) {
        if (context == null) return;
        SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
        prefs.edit().putBoolean(PREF_ENABLED, enabled).apply();
    }

    public static String getGatewayUrl(Context context) {
        if (context == null) return DEFAULT_GATEWAY_URL;
        SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
        return prefs.getString(PREF_GATEWAY_URL, DEFAULT_GATEWAY_URL);
    }

    public static String getUsername(Context context) {
        if (context == null) return "";
        SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
        return prefs.getString(PREF_USERNAME, DEFAULT_USERNAME);
    }

    public static String getPassword(Context context) {
        if (context == null) return "";
        SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
        return prefs.getString(PREF_PASSWORD, DEFAULT_PASSWORD);
    }

    public static boolean isAutoLoginOnWifi(Context context) {
        if (context == null) return false;
        SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
        return prefs.getBoolean(PREF_AUTOLOGIN_WIFI, true);
    }

    public static boolean isCheckOnScreenOn(Context context) {
        if (context == null) return false;
        SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
        return prefs.getBoolean(PREF_CHECK_SCREEN_ON, true);
    }

    public static boolean isExplicitlyLoggedOut(Context context) {
        if (context == null) return false;
        SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
        return prefs.getBoolean(PREF_EXPLICIT_LOGOUT, false);
    }

    public static void setExplicitlyLoggedOut(Context context, boolean loggedOut) {
        if (context == null) return;
        SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
        prefs.edit().putBoolean(PREF_EXPLICIT_LOGOUT, loggedOut).apply();
    }

    public static void saveConfig(Context context, boolean enabled, String gatewayUrl, String username, String password, boolean autoLogin, boolean checkScreenOn) {
        if (context == null) return;
        SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
        prefs.edit()
                .putBoolean(PREF_ENABLED, enabled)
                .putString(PREF_GATEWAY_URL, (gatewayUrl != null && !gatewayUrl.trim().isEmpty()) ? gatewayUrl.trim() : DEFAULT_GATEWAY_URL)
                .putString(PREF_USERNAME, username != null ? username.trim() : "")
                .putString(PREF_PASSWORD, password != null ? password : "")
                .putBoolean(PREF_AUTOLOGIN_WIFI, autoLogin)
                .putBoolean(PREF_CHECK_SCREEN_ON, checkScreenOn)
                .apply();
    }

    public static void saveConfig(Context context, boolean enabled, String gatewayUrl, String username, String password, boolean autoLogin) {
        saveConfig(context, enabled, gatewayUrl, username, password, autoLogin, isCheckOnScreenOn(context));
    }

    public static String getLastStatus(Context context) {
        if (context == null) return "Never";
        SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
        String st = prefs.getString(PREF_LAST_STATUS, null);
        long t = prefs.getLong(PREF_LAST_TIME, 0);
        if (st == null || t == 0) return "Not configured";
        long agoSec = (System.currentTimeMillis() - t) / 1000;
        if (agoSec < 60) return st + " (" + agoSec + "s ago)";
        return st + " (" + (agoSec / 60) + "m ago)";
    }

    private static final List<String> memoryLogs = new ArrayList<>();

    public static synchronized void logDebug(Context context, String entry) {
        String ts = new java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.US).format(new java.util.Date());
        String line = "[" + ts + "] " + entry;
        Log.i(TAG, line);
        memoryLogs.add(line);
        if (memoryLogs.size() > 60) memoryLogs.remove(0);
    }

    public static synchronized List<String> getDebugLogs(Context context) {
        return new ArrayList<>(memoryLogs);
    }

    public static LoginResult login(Context context) {
        String url = getGatewayUrl(context);
        String user = getUsername(context);
        String pass = getPassword(context);
        if (user.isEmpty() || pass.isEmpty()) {
            return new LoginResult(false, "Failed", "Add your campus username and password in Settings", 0);
        }
        LoginResult res = login(context, url, user, pass);
        if (context != null) {
            SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
            prefs.edit()
                    .putString(PREF_LAST_STATUS, res.success ? "Live" : "Failed")
                    .putString(PREF_LAST_MSG, res.message)
                    .putLong(PREF_LAST_TIME, System.currentTimeMillis())
                    .apply();
            if (res.success) {
                setExplicitlyLoggedOut(context, false);
            }
        }
        return res;
    }

    public static void loginAsync(Context context, Callback callback) {
        new Thread(() -> {
            LoginResult res = login(context);
            if (callback != null) {
                new Handler(Looper.getMainLooper()).post(() -> callback.onResult(res));
            }
        }).start();
    }

    public static LoginResult login(String gatewayUrl, String username, String password) {
        return login(null, gatewayUrl, username, password, true);
    }

    public static LoginResult login(Context context, String gatewayUrl, String username, String password) {
        return login(context, gatewayUrl, username, password, true);
    }

    public static LoginResult login(Context context, String gatewayUrl, String username, String password, boolean allowFallback) {
        long start = System.currentTimeMillis();
        if (gatewayUrl == null || gatewayUrl.trim().isEmpty()) {
            String detected = detectGatewayUrl(context);
            gatewayUrl = (detected != null && !detected.isEmpty()) ? detected : DEFAULT_GATEWAY_URL;
        }
        if (username == null || username.trim().isEmpty()) {
            return new LoginResult(false, "ERROR", "LDAP Username is required", 0);
        }
        if (password == null || password.isEmpty()) {
            return new LoginResult(false, "ERROR", "Password is required", 0);
        }

        Network wifiNet = (context != null) ? DeviceManager.getWifiNetwork(context) : null;
        return loginInternal(context, wifiNet, gatewayUrl, username, password, allowFallback, start);
    }

    private static LoginResult loginInternal(Context context, Network wifiNet, String gatewayUrl, String username, String password, boolean allowFallback, long start) {
        logDebug(context, "Initiating portal login. Gateway=" + gatewayUrl + ", User=" + username + ", Wi-Fi interface bound=" + (wifiNet != null));
        if (!isCampusGateway(gatewayUrl)) {
            logDebug(context, "Refusing to send credentials to " + gatewayUrl);
            return new LoginResult(false, "ERROR", "Not the campus gateway, credentials not sent", System.currentTimeMillis() - start);
        }

        HttpURLConnection conn = null;
        try {

            URL u = new URL(gatewayUrl.trim());

            // If hostname is gateway.iisertvm.ac.in, test DNS resolution on Wi-Fi interface first
            if (wifiNet != null && u.getHost() != null && u.getHost().equalsIgnoreCase("gateway.iisertvm.ac.in")) {
                try {
                    InetAddress[] resolved = wifiNet.getAllByName(u.getHost());
                    if (resolved == null || resolved.length == 0) {
                        throw new UnknownHostException("Wi-Fi DNS returned empty result for " + u.getHost());
                    }
                } catch (Exception ex) {
                    logDebug(context, "Wi-Fi DNS could not resolve " + u.getHost() + ": " + ex.getMessage());
                    if (allowFallback) {
                        String fallbackUrl = gatewayUrl.replace("gateway.iisertvm.ac.in", "172.16.31.101");
                        logDebug(context, "DNS fallback to IP endpoint: " + fallbackUrl);
                        return loginInternal(context, wifiNet, fallbackUrl, username, password, false, start);
                    }
                }
            }

            if (wifiNet != null) {
                try {
                    conn = (HttpURLConnection) wifiNet.openConnection(u);
                } catch (Exception ex) {
                    logDebug(context, "wifiNet.openConnection failed (" + ex.getMessage() + "), using default routing");
                    conn = (HttpURLConnection) u.openConnection();
                }
            } else {
                conn = (HttpURLConnection) u.openConnection();
            }

            if (conn instanceof HttpsURLConnection) {
                HttpsURLConnection httpsConn = (HttpsURLConnection) conn;
                verifyCampusCertificate(httpsConn);
            }

            conn.setRequestMethod("POST");
            conn.setConnectTimeout(6000);
            conn.setReadTimeout(8000);
            conn.setDoOutput(true);

            String hostWithPort = u.getHost() + (u.getPort() > 0 ? ":" + u.getPort() : "");
            String origin = u.getProtocol() + "://" + hostWithPort;
            conn.setRequestProperty("Origin", origin);
            conn.setRequestProperty("Referer", origin + "/httpclient.html");
            conn.setRequestProperty("Content-Type", "application/x-www-form-urlencoded");
            conn.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android; PCAuthenticator)");

            long timestamp = System.currentTimeMillis();
            String postData;
            if (gatewayUrl.contains("login.xml") || gatewayUrl.contains("iisertvm") || gatewayUrl.contains("172.16.31.101")) {
                postData = "mode=191&username=" + URLEncoder.encode(username.trim(), "UTF-8")
                        + "&password=" + URLEncoder.encode(password, "UTF-8")
                        + "&a=" + timestamp
                        + "&producttype=0";
            } else {
                postData = "username=" + URLEncoder.encode(username.trim(), "UTF-8")
                        + "&password=" + URLEncoder.encode(password, "UTF-8")
                        + "&user=" + URLEncoder.encode(username.trim(), "UTF-8")
                        + "&pass=" + URLEncoder.encode(password, "UTF-8");
            }

            byte[] postBytes = postData.getBytes("UTF-8");
            conn.setRequestProperty("Content-Length", String.valueOf(postBytes.length));

            try (OutputStream os = conn.getOutputStream()) {
                os.write(postBytes);
                os.flush();
            }

            int code = conn.getResponseCode();
            StringBuilder sb = new StringBuilder();
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(
                    code >= 200 && code < 400 ? conn.getInputStream() : conn.getErrorStream(), "UTF-8"))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    sb.append(line);
                }
            }

            long latency = System.currentTimeMillis() - start;
            String resp = sb.toString();

            String status = extractTag(resp, "status");
            String message = extractTag(resp, "message");
            if (message != null && message.contains("{username}")) {
                message = message.replace("{username}", username.trim());
            }

            boolean hasFailKeyword = (message != null && (message.toLowerCase().contains("limit")
                    || message.toLowerCase().contains("invalid")
                    || message.toLowerCase().contains("failed")
                    || message.toLowerCase().contains("error")
                    || message.toLowerCase().contains("denied")))
                    || resp.toLowerCase().contains("maximum login limit")
                    || resp.toLowerCase().contains("invalid username");

            boolean isLive = !hasFailKeyword && ("LIVE".equalsIgnoreCase(status)
                    || resp.contains("signed in as")
                    || resp.contains("You have successfully logged in")
                    || (status != null && status.equalsIgnoreCase("LIVE")));

            logDebug(context, "Portal response: HTTP " + code + ", status=" + status + ", msg=" + message + ", isLive=" + isLive);

            if (isLive) {
                if (message == null || message.isEmpty()) message = "You are signed in as " + username.trim();
                return new LoginResult(true, status.isEmpty() ? "LIVE" : status, message, latency);
            } else if (code == 200 && !resp.isEmpty()) {
                String displayMsg = (message != null && !message.isEmpty()) ? message : resp;
                return new LoginResult(false, status.isEmpty() ? "FAILED" : status, displayMsg, latency);
            } else {
                return new LoginResult(false, "HTTP_" + code, "HTTP " + code + ((message != null && !message.isEmpty()) ? ": " + message : ""), latency);
            }

        } catch (UnknownHostException uhe) {
            logDebug(context, "UnknownHostException: " + uhe.getMessage());
            if (allowFallback && gatewayUrl.contains("gateway.iisertvm.ac.in")) {
                String fallbackUrl = gatewayUrl.replace("gateway.iisertvm.ac.in", "172.16.31.101");
                logDebug(context, "DNS fallback to IP endpoint: " + fallbackUrl);
                return loginInternal(context, wifiNet, fallbackUrl, username, password, false, start);
            }
            long latency = System.currentTimeMillis() - start;
            return new LoginResult(false, "DNS_ERROR", "Cannot resolve gateway hostname (" + uhe.getMessage() + ")", latency);
        } catch (SocketException se) {
            logDebug(context, "SocketException: " + se.getMessage());
            if (wifiNet != null) {
                logDebug(context, "Wi-Fi socket binding failed; retrying without interface binding...");
                return loginInternal(context, null, gatewayUrl, username, password, allowFallback, start);
            }
            long latency = System.currentTimeMillis() - start;
            return new LoginResult(false, "NET_ERROR", se.getMessage(), latency);
        } catch (Exception e) {
            long latency = System.currentTimeMillis() - start;
            logDebug(context, "Login error: " + e.getClass().getSimpleName() + ": " + e.getMessage());
            if (wifiNet != null && e.getMessage() != null && e.getMessage().contains("Binding socket")) {
                logDebug(context, "Retrying without Wi-Fi binding after bind error...");
                return loginInternal(context, null, gatewayUrl, username, password, allowFallback, start);
            }
            return new LoginResult(false, "ERROR", e.getMessage() != null ? e.getMessage() : e.toString(), latency);
        } finally {
            if (conn != null) {
                try { conn.disconnect(); } catch (Exception ignored) {}
            }
        }
    }

    public static LoginResult logout(Context context) {
        String url = getGatewayUrl(context);
        String user = getUsername(context);
        return logout(context, url, user);
    }

    public static LoginResult logout(Context context, String gatewayUrl, String username) {
        long start = System.currentTimeMillis();
        if (gatewayUrl == null || gatewayUrl.trim().isEmpty()) {
            gatewayUrl = DEFAULT_GATEWAY_URL;
        }
        if (username == null || username.trim().isEmpty()) {
            return new LoginResult(false, "ERROR", "Username is required", 0);
        }

        String logoutUrl;
        if (gatewayUrl.contains("login.xml")) {
            logoutUrl = gatewayUrl.replace("login.xml", "logout.xml");
        } else if (gatewayUrl.contains("login")) {
            logoutUrl = gatewayUrl.replace("login", "logout");
        } else {
            logoutUrl = gatewayUrl.endsWith("/") ? (gatewayUrl + "logout") : (gatewayUrl + "/logout");
        }

        Network wifiNet = (context != null) ? DeviceManager.getWifiNetwork(context) : null;
        logDebug(context, "Initiating portal logout. Gateway=" + logoutUrl + ", User=" + username);
        if (!isCampusGateway(logoutUrl)) {
            return new LoginResult(false, "ERROR", "Not the campus gateway", 0);
        }

        HttpURLConnection conn = null;
        try {

            URL u = new URL(logoutUrl.trim());
            if (wifiNet != null) {
                try {
                    conn = (HttpURLConnection) wifiNet.openConnection(u);
                } catch (Exception ex) {
                    conn = (HttpURLConnection) u.openConnection();
                }
            } else {
                conn = (HttpURLConnection) u.openConnection();
            }

            if (conn instanceof HttpsURLConnection) {
                HttpsURLConnection httpsConn = (HttpsURLConnection) conn;
                verifyCampusCertificate(httpsConn);
            }

            conn.setRequestMethod("POST");
            conn.setConnectTimeout(5000);
            conn.setReadTimeout(6000);
            conn.setDoOutput(true);

            String hostWithPort = u.getHost() + (u.getPort() > 0 ? ":" + u.getPort() : "");
            String origin = u.getProtocol() + "://" + hostWithPort;
            conn.setRequestProperty("Origin", origin);
            conn.setRequestProperty("Referer", origin + "/httpclient.html");
            conn.setRequestProperty("Content-Type", "application/x-www-form-urlencoded");
            conn.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android; PCAuthenticator)");

            long timestamp = System.currentTimeMillis();
            String postData;
            if (logoutUrl.contains("logout.xml") || logoutUrl.contains("iisertvm") || logoutUrl.contains("172.16.31.101")) {
                postData = "mode=193&username=" + URLEncoder.encode(username.trim(), "UTF-8")
                        + "&a=" + timestamp
                        + "&producttype=0";
            } else {
                postData = "logout=1&username=" + URLEncoder.encode(username.trim(), "UTF-8")
                        + "&action=logout";
            }

            byte[] postBytes = postData.getBytes("UTF-8");
            conn.setRequestProperty("Content-Length", String.valueOf(postBytes.length));

            try (OutputStream os = conn.getOutputStream()) {
                os.write(postBytes);
                os.flush();
            }

            int code = conn.getResponseCode();
            StringBuilder sb = new StringBuilder();
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(
                    code >= 200 && code < 400 ? conn.getInputStream() : conn.getErrorStream(), "UTF-8"))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    sb.append(line);
                }
            }

            long latency = System.currentTimeMillis() - start;
            String resp = sb.toString();
            String message = extractTag(resp, "message");
            logDebug(context, "Logout response: HTTP " + code + ", msg=" + message);

            if (context != null) {
                SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
                prefs.edit().putString(PREF_LAST_STATUS, "Logged Out").apply();
                setExplicitlyLoggedOut(context, true);
            }

            return new LoginResult(true, "LOGGED_OUT", message != null && !message.isEmpty() ? message : "Signed out successfully", latency);
        } catch (Exception e) {
            long latency = System.currentTimeMillis() - start;
            logDebug(context, "Logout error: " + e.getMessage());
            return new LoginResult(false, "ERROR", e.getMessage(), latency);
        } finally {
            if (conn != null) {
                try { conn.disconnect(); } catch (Exception ignored) {}
            }
        }
    }

    private static String extractTag(String xml, String tagName) {
        if (xml == null || xml.isEmpty()) return "";
        try {
            Pattern p = Pattern.compile("<" + tagName + ">(?:<!\\[CDATA\\[)?(.*?)(?:\\]\\]>)?</" + tagName + ">",
                    Pattern.DOTALL | Pattern.CASE_INSENSITIVE);
            Matcher m = p.matcher(xml);
            if (m.find()) {
                return cleanMessage(m.group(1));
            }
        } catch (Exception ignored) {}
        return "";
    }

    public static String cleanMessage(String text) {
        if (text == null || text.isEmpty()) return "";
        String clean = text;
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                clean = Html.fromHtml(clean, Html.FROM_HTML_MODE_LEGACY).toString();
            } else {
                clean = Html.fromHtml(clean).toString();
            }
        } catch (Exception ignored) {}
        return clean.replace("&#39;", "'")
                .replace("&#x27;", "'")
                .replace("&quot;", "\"")
                .replace("&#34;", "\"")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&nbsp;", " ")
                .replaceAll("\\s+", " ")
                .trim();
    }

    public static String detectGatewayUrl() {
        return detectGatewayUrl(null);
    }

    public static String detectGatewayUrl(Context context) {
        Network wifiNet = (context != null) ? DeviceManager.getWifiNetwork(context) : null;
        logDebug(context, "Detecting gateway URL. Wi-Fi bound=" + (wifiNet != null));
        String[] probeUrls = new String[]{
                "http://connectivitycheck.gstatic.com/generate_204",
                "http://clients3.google.com/generate_204",
                "http://www.google.com/gen_204"
        };
        boolean all204 = true;
        for (String probeUrl : probeUrls) {
            HttpURLConnection conn = null;
            try {
                URL url = new URL(probeUrl);
                if (wifiNet != null) {
                    try {
                        conn = (HttpURLConnection) wifiNet.openConnection(url);
                    } catch (Exception ex) {
                        conn = (HttpURLConnection) url.openConnection();
                    }
                } else {
                    conn = (HttpURLConnection) url.openConnection();
                }
                conn.setInstanceFollowRedirects(false);
                conn.setConnectTimeout(3000);
                conn.setReadTimeout(3000);
                conn.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android; PCAuthenticator)");
                int code = conn.getResponseCode();
                logDebug(context, "Probe " + probeUrl + " -> HTTP " + code);
                if (code != 204) {
                    all204 = false;
                }
                if (code == 301 || code == 302 || code == 303 || code == 307 || code == 308) {
                    String loc = conn.getHeaderField("Location");
                    if (loc != null && !loc.trim().isEmpty()) {
                        String normalized = normalizeGatewayUrl(loc.trim());
                        logDebug(context, "Redirect location: " + loc + " -> " + normalized);
                        if (normalized != null) return normalized;
                    }
                } else if (code == 200) {
                    try (BufferedReader reader = new BufferedReader(new InputStreamReader(conn.getInputStream(), "UTF-8"))) {
                        StringBuilder sb = new StringBuilder();
                        String line;
                        while ((line = reader.readLine()) != null && sb.length() < 12000) {
                            sb.append(line).append("\n");
                        }
                        String html = sb.toString();
                        Matcher mXml = Pattern.compile("https?://[^\"'\\s<>]+/login\\.xml", Pattern.CASE_INSENSITIVE).matcher(html);
                        if (mXml.find()) {
                            logDebug(context, "Found login.xml in HTML: " + mXml.group());
                            return mXml.group();
                        }
                        Matcher mLogin = Pattern.compile("https?://[^\"'\\s<>]+(?:login|portal|gateway)[^\"'\\s<>]*", Pattern.CASE_INSENSITIVE).matcher(html);
                        if (mLogin.find()) {
                            String found = normalizeGatewayUrl(mLogin.group());
                            logDebug(context, "Found portal url in HTML: " + found);
                            if (found != null) return found;
                        }
                    }
                }
            } catch (Exception e) {
                all204 = false;
                logDebug(context, "Detection probe error on " + probeUrl + ": " + e.getMessage());
            } finally {
                if (conn != null) {
                    try { conn.disconnect(); } catch (Exception ignored) {}
                }
            }
        }

        // If no redirect was found from external probes (either because internet is already LIVE,
        // or because external DNS cannot resolve gstatic.com while captive),
        // probe local campus gateway endpoints directly:
        String[] campusCandidates = new String[]{
                DEFAULT_GATEWAY_URL,
                "https://172.16.31.101:8090/login.xml"
        };
        for (String cand : campusCandidates) {
            HttpURLConnection c = null;
            try {
                URL u = new URL(cand);
                if (wifiNet != null) {
                    try {
                        c = (HttpURLConnection) wifiNet.openConnection(u);
                    } catch (Exception ex) {
                        c = (HttpURLConnection) u.openConnection();
                    }
                } else {
                    c = (HttpURLConnection) u.openConnection();
                }
                if (c instanceof HttpsURLConnection) {
                    HttpsURLConnection hc = (HttpsURLConnection) c;
                    verifyCampusCertificate(hc);
                }
                c.setRequestMethod("GET");
                c.setConnectTimeout(2500);
                c.setReadTimeout(2500);
                int rCode = c.getResponseCode();
                logDebug(context, "Campus gateway probe " + cand + " -> HTTP " + rCode);
                if (rCode > 0) {
                    return cand;
                }
            } catch (Exception ignored) {
            } finally {
                if (c != null) {
                    try { c.disconnect(); } catch (Exception ignored) {}
                }
            }
        }

        return null;
    }

    public static String normalizeGatewayUrl(String location) {
        if (location == null || location.trim().isEmpty()) return null;
        try {
            URL u = new URL(location);
            String protocol = u.getProtocol();
            String host = u.getHost();
            int port = u.getPort();
            String portStr = (port > 0) ? (":" + port) : "";
            String path = u.getPath();
            if (path != null && (path.endsWith("/login.xml") || path.endsWith("/login") || path.contains("login") || path.contains("portal") || path.contains("auth"))) {
                return location;
            }
            if (host.contains("iisertvm") || host.equals("172.16.31.101")) {
                return protocol + "://" + host + portStr + "/login.xml";
            }
            return location;
        } catch (Exception e) {
            return location;
        }
    }

    public static boolean isInternetConnected() {
        return false;
    }

    public static boolean isInternetConnected(Context context) {
        if (context == null || !DeviceManager.isWifiActive(context)) {
            return false;
        }
        Network wifiNet = DeviceManager.getWifiNetwork(context);
        if (wifiNet == null) {
            return false;
        }
        // Android has usually checked already: if it says this Wi-Fi reaches the internet,
        // trust it instead of waking the radio for a request of our own
        try {
            android.net.ConnectivityManager cm = (android.net.ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            android.net.NetworkCapabilities caps = cm != null ? cm.getNetworkCapabilities(wifiNet) : null;
            if (caps != null && caps.hasCapability(android.net.NetworkCapabilities.NET_CAPABILITY_VALIDATED)
                    && !caps.hasCapability(android.net.NetworkCapabilities.NET_CAPABILITY_CAPTIVE_PORTAL)) {
                return true;
            }
        } catch (Exception ignored) { }

        HttpURLConnection conn = null;
        try {
            URL url = new URL("http://connectivitycheck.gstatic.com/generate_204");
            conn = (HttpURLConnection) wifiNet.openConnection(url);
            conn.setInstanceFollowRedirects(false);
            conn.setConnectTimeout(2000);
            conn.setReadTimeout(2000);
            int code = conn.getResponseCode();
            return code == 204;
        } catch (Exception e) {
            return false;
        } finally {
            if (conn != null) {
                try { conn.disconnect(); } catch (Exception ignored) {}
            }
        }
    }
}

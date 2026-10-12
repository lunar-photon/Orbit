package com.lunarphoton.pcauthenticator;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

import android.content.Context;
import android.os.Looper;
import android.os.SystemClock;

import org.json.JSONObject;

public class NetworkUtils {

    // --- Sending the pairing token only to the real computer ---
    // Over plain HTTP on a LAN anyone could sit at the computer's old address (a café or hotspot
    // where the IP is reused), so the token goes to an http:// address only after that address has
    // proved, on this network, that it knows our pairing secret (HMAC challenge, /api/auth/prove).
    // HTTPS (the internet tunnel) is checked by its certificate instead.

    private static volatile Context appContext;
    private static final Map<String, Long> provenUntil = new ConcurrentHashMap<>();
    private static final Map<String, Long> failedUntil = new ConcurrentHashMap<>();
    private static final long PROOF_TTL_MS = 15 * 60 * 1000;
    private static final long FAILURE_TTL_MS = 20 * 1000;

    public static void init(Context context) {
        appContext = context.getApplicationContext();
    }

    /** The network changed: every address has to prove itself again. */
    public static void forgetProofs() {
        provenUntil.clear();
        failedUntil.clear();
    }

    public static void setAuth(HttpURLConnection conn, String authToken) {
        if (conn == null || authToken == null || authToken.isEmpty()) return;
        if (mayReceiveToken(conn.getURL(), authToken)) {
            conn.setRequestProperty("Authorization", "Bearer " + authToken);
        }
    }

    /** Has (or can now prove) the computer at host:port that it holds our pairing secret? */
    public static boolean isTrustedComputer(String host, int port, String token) {
        try {
            return token != null && !token.isEmpty() && mayReceiveToken(new URL("http://" + host + ":" + port + "/"), token);
        } catch (Exception e) {
            return false;
        }
    }

    private static boolean mayReceiveToken(URL url, String token) {
        if ("https".equalsIgnoreCase(url.getProtocol())) return true;
        String host = url.getHost();
        int port = url.getPort() < 0 ? 80 : url.getPort();
        String key = host + ":" + port;
        long now = SystemClock.elapsedRealtime();
        Long ok = provenUntil.get(key);
        if (ok != null && ok > now) return true;
        Long bad = failedUntil.get(key);
        if (bad != null && bad > now) return false;
        if (Looper.myLooper() == Looper.getMainLooper()) return false;   // can't ask over the network here
        synchronized (NetworkUtils.class) {
            ok = provenUntil.get(key);
            if (ok != null && ok > SystemClock.elapsedRealtime()) return true;
            PairedDevice dev = deviceForToken(token);
            int result = dev == null ? PROOF_WRONG : proveComputer(host, port, dev, true);
            if (result == PROOF_OK) provenUntil.put(key, SystemClock.elapsedRealtime() + PROOF_TTL_MS);
            // Only a wrong answer is held against the address. Not reaching it (the phone just
            // woke up, Wi-Fi blipped) isn't: the next request simply asks again. Holding that
            // for 20 s used to make the app look disconnected.
            else if (result == PROOF_WRONG) failedUntil.put(key, SystemClock.elapsedRealtime() + FAILURE_TTL_MS);
            return result == PROOF_OK;
        }
    }

    private static PairedDevice deviceForToken(String token) {
        Context ctx = appContext;
        if (ctx == null) return null;
        for (PairedDevice d : DeviceManager.getDevices(ctx)) {
            if (token.equals(d.authToken)) return d;
        }
        return null;
    }

    static final int PROOF_OK = 0, PROOF_WRONG = 1, PROOF_UNREACHABLE = 2;

    /** Challenge the computer at ip:port to prove it knows our pairing secret, without sending our token. */
    public static boolean proveComputer(String ip, int port, PairedDevice device) {
        return proveComputer(ip, port, device, true) == PROOF_OK;
    }

    static int proveComputer(String ip, int port, PairedDevice device, boolean detailed) {
        Context ctx = appContext;
        if (ctx == null || device == null || device.secretKey == null || device.secretKey.isEmpty()) return PROOF_WRONG;
        try {
            String clientId = ctx.getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE).getString("client_uuid", null);
            if (clientId == null) return PROOF_WRONG;
            byte[] raw = new byte[24];
            new java.security.SecureRandom().nextBytes(raw);
            StringBuilder nonce = new StringBuilder();
            for (byte b : raw) nonce.append(String.format("%02x", b));
            JSONObject req = new JSONObject();
            req.put("client_id", clientId);
            req.put("nonce", nonce.toString());
            String res = httpPostJson("http://" + ip + ":" + port + "/api/auth/prove", req.toString(), 3000);
            if (res == null) return PROOF_UNREACHABLE;
            String mac = new JSONObject(res).optString("mac", "");
            String expected = CryptoUtils.hmacSha256(device.secretKey, "prove:" + nonce + ":" + ip);
            boolean ok = !expected.isEmpty() && java.security.MessageDigest.isEqual(
                    expected.getBytes("UTF-8"), mac.toLowerCase(java.util.Locale.US).getBytes("UTF-8"));
            return ok ? PROOF_OK : PROOF_WRONG;
        } catch (org.json.JSONException e) {
            return PROOF_WRONG;          // answered, but not like our computer
        } catch (Exception e) {
            return PROOF_UNREACHABLE;
        }
    }

    public static void applyTunnelHeaders(HttpURLConnection conn) {
        if (conn != null) {
            try {
                conn.setRequestProperty("ngrok-skip-browser-warning", "1");
            } catch (Exception ignored) {}
        }
    }

    public static String httpGet(String urlStr, int timeoutMs) {
        return httpGetWithAuth(urlStr, null, timeoutMs);
    }

    public static String httpGetWithAuth(String urlStr, String authToken, int timeoutMs) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(urlStr);
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("GET");
            conn.setConnectTimeout(timeoutMs);
            conn.setReadTimeout(timeoutMs);
            conn.setRequestProperty("Accept", "application/json");
            applyTunnelHeaders(conn);
            if (authToken != null && !authToken.isEmpty()) {
                setAuth(conn, authToken);
            }

            int code = conn.getResponseCode();
            if (code >= 200 && code < 300) {
                return readStream(conn.getInputStream());
            }
        } catch (Exception e) {
            e.printStackTrace();
        } finally {
            if (conn != null) conn.disconnect();
        }
        return null;
    }

    public static boolean httpPost(String urlStr, String jsonBody, int timeoutMs) {
        return httpPostWithAuth(urlStr, null, null, null, null, timeoutMs);
    }

    public static String httpPostJson(String urlStr, String jsonBody, int timeoutMs) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(urlStr);
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(timeoutMs);
            conn.setReadTimeout(timeoutMs);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json");
            conn.setRequestProperty("Accept", "application/json");
            applyTunnelHeaders(conn);

            if (jsonBody != null && !jsonBody.isEmpty()) {
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(jsonBody.getBytes("UTF-8"));
                }
            } else {
                conn.setFixedLengthStreamingMode(0);
            }

            int code = conn.getResponseCode();
            if (code >= 200 && code < 300) {
                return readStream(conn.getInputStream());
            } else if (conn.getErrorStream() != null) {
                return readStream(conn.getErrorStream());
            }
        } catch (Exception e) {
            e.printStackTrace();
        } finally {
            if (conn != null) conn.disconnect();
        }
        return null;
    }

    public static String httpPostJsonWithAuth(String urlStr, String authToken, String jsonBody, int timeoutMs) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(urlStr);
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(timeoutMs);
            conn.setReadTimeout(timeoutMs);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json");
            conn.setRequestProperty("Accept", "application/json");
            applyTunnelHeaders(conn);
            if (authToken != null && !authToken.isEmpty()) {
                setAuth(conn, authToken);
            }

            if (jsonBody != null && !jsonBody.isEmpty()) {
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(jsonBody.getBytes("UTF-8"));
                }
            } else {
                conn.setFixedLengthStreamingMode(0);
            }

            int code = conn.getResponseCode();
            if (code >= 200 && code < 300) {
                return readStream(conn.getInputStream());
            } else if (conn.getErrorStream() != null) {
                return readStream(conn.getErrorStream());
            }
        } catch (Exception e) {
            e.printStackTrace();
        } finally {
            if (conn != null) conn.disconnect();
        }
        return null;
    }

    public static boolean httpPostWithAuth(String urlStr, String authToken, String secretKey, String sessionId, String action, int timeoutMs) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(urlStr);
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(timeoutMs);
            conn.setReadTimeout(timeoutMs);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json");
            applyTunnelHeaders(conn);

            if (authToken != null && !authToken.isEmpty()) {
                setAuth(conn, authToken);
            }
            if (secretKey != null && !secretKey.isEmpty()) {
                long ts = System.currentTimeMillis() / 1000L;
                String sid = (sessionId != null) ? sessionId : "";
                String act = (action != null) ? action : "approve";
                String msg = sid + ":" + ts + ":" + act;
                String sig = CryptoUtils.hmacSha256(secretKey, msg);
                conn.setRequestProperty("X-Auth-Timestamp", String.valueOf(ts));
                conn.setRequestProperty("X-Auth-Signature", sig);
                if (sessionId != null && !sessionId.isEmpty()) {
                    conn.setRequestProperty("X-Session-ID", sessionId);
                }
            }

            conn.setFixedLengthStreamingMode(0);

            int code = conn.getResponseCode();
            return code >= 200 && code < 300;
        } catch (Exception e) {
            e.printStackTrace();
            return false;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private static String readStream(InputStream is) throws Exception {
        BufferedReader reader = new BufferedReader(new InputStreamReader(is, "UTF-8"));
        StringBuilder sb = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            sb.append(line);
        }
        return sb.toString();
    }
}

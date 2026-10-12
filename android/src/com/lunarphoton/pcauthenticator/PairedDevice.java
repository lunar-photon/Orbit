package com.lunarphoton.pcauthenticator;

import org.json.JSONObject;

public class PairedDevice {
    public String deviceId;
    public String hostname;
    public String ip;
    public int port;
    public String user;
    public String authToken;
    public String secretKey;
    public boolean isActive;
    public String localUrl;
    public String internetUrl;
    public volatile String activeUrl;

    public PairedDevice(String hostname, String ip, int port, String user, boolean isActive) {
        this(null, hostname, ip, port, user, null, null, isActive, null, null);
    }

    public PairedDevice(String deviceId, String hostname, String ip, int port, String user, String authToken, String secretKey, boolean isActive) {
        this(deviceId, hostname, ip, port, user, authToken, secretKey, isActive, null, null);
    }

    public PairedDevice(String deviceId, String hostname, String ip, int port, String user, String authToken, String secretKey, boolean isActive, String localUrl, String internetUrl) {
        this.deviceId = deviceId;
        this.hostname = hostname != null ? hostname : "Computer";
        this.ip = ip;
        this.port = port > 0 ? port : 1760;
        this.user = user != null ? user : "user";
        this.authToken = authToken;
        this.secretKey = secretKey;
        this.isActive = isActive;
        this.localUrl = localUrl;
        this.internetUrl = internetUrl;
    }

    public boolean isPaired() {
        return authToken != null && !authToken.isEmpty() && secretKey != null && !secretKey.isEmpty();
    }

    public String getBaseUrl() {
        String override = DeviceManager.getActiveUrl();
        if (override != null && !override.isEmpty()) {
            return cleanUrl(override);
        }
        if (activeUrl != null && !activeUrl.isEmpty()) {
            return cleanUrl(activeUrl);
        }
        if (localUrl != null && !localUrl.isEmpty()) {
            return cleanUrl(localUrl);
        }
        if (ip != null && !ip.isEmpty()) {
            String clean = ip.trim();
            if (clean.startsWith("http://") || clean.startsWith("https://")) {
                return cleanUrl(clean);
            }
            return "http://" + clean + ":" + port;
        }
        if (internetUrl != null && !internetUrl.isEmpty()) {
            return cleanUrl(internetUrl);
        }
        return "http://127.0.0.1:" + port;
    }

    public String getLocalUrl() {
        if (localUrl != null && !localUrl.isEmpty()) return cleanUrl(localUrl);
        if (ip != null && !ip.isEmpty()) {
            String clean = ip.trim();
            if (clean.startsWith("http://") || clean.startsWith("https://")) return cleanUrl(clean);
            return "http://" + clean + ":" + port;
        }
        return "http://127.0.0.1:" + port;
    }

    public String getInternetUrl() {
        if (internetUrl != null && !internetUrl.isEmpty()) return cleanUrl(internetUrl);
        return null;
    }

    private static String cleanUrl(String u) {
        if (u == null) return "";
        String s = u.trim();
        if (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        return s;
    }

    public JSONObject toJsonObject() {
        JSONObject obj = new JSONObject();
        try {
            if (deviceId != null) obj.put("deviceId", deviceId);
            obj.put("hostname", hostname);
            obj.put("ip", ip);
            obj.put("port", port);
            obj.put("user", user);
            if (authToken != null) obj.put("authToken", authToken);
            if (secretKey != null) obj.put("secretKey", secretKey);
            obj.put("isActive", isActive);
            if (localUrl != null) obj.put("localUrl", localUrl);
            if (internetUrl != null) obj.put("internetUrl", internetUrl);
        } catch (Exception ignored) {}
        return obj;
    }

    public static PairedDevice fromJsonObject(JSONObject obj) {
        if (obj == null) return null;
        String deviceId = obj.optString("deviceId", null);
        String hostname = obj.optString("hostname", "Computer");
        String ip = obj.optString("ip", "127.0.0.1");
        int port = obj.optInt("port", 1760);
        String user = obj.optString("user", "user");
        String authToken = obj.optString("authToken", null);
        String secretKey = obj.optString("secretKey", null);
        boolean isActive = obj.optBoolean("isActive", false);
        String localUrl = obj.optString("localUrl", null);
        String internetUrl = obj.optString("internetUrl", null);
        return new PairedDevice(deviceId, hostname, ip, port, user, authToken, secretKey, isActive, localUrl, internetUrl);
    }
}

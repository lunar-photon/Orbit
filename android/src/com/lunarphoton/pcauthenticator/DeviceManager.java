package com.lunarphoton.pcauthenticator;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONArray;
import org.json.JSONObject;

import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.util.ArrayList;
import java.util.Enumeration;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

public class DeviceManager {
    private static final String PREF_DEVICES = "paired_devices_json";
    private static final String DEFAULT_DEVICE_ID = "6208c37628a34c65ac8117a82fb58bb5";
    private static final String DEFAULT_HOSTNAME = "cachyos-x8664";
    private static final String DEFAULT_IP = "192.168.48.40";
    private static final int DEFAULT_PORT = 1760;

    private static final String PREF_LAST_ACTIVE_URL = "last_active_route_url";
    private static volatile String activeUrlOverride = null;

    public static void setActiveUrl(String url) {
        activeUrlOverride = (url != null && !url.isEmpty()) ? url.trim() : null;
    }

    public static void setActiveUrl(Context context, String url) {
        setActiveUrl(url);
        if (context != null) {
            try {
                SharedPreferences prefs = context.getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
                if (url != null && !url.isEmpty()) {
                    prefs.edit().putString(PREF_LAST_ACTIVE_URL, url.trim()).apply();
                } else {
                    prefs.edit().remove(PREF_LAST_ACTIVE_URL).apply();
                }
            } catch (Exception ignored) {}
        }
    }

    public static String getActiveUrl() {
        return activeUrlOverride;
    }

    public static String getActiveUrl(Context context) {
        if (activeUrlOverride != null && !activeUrlOverride.isEmpty()) {
            return activeUrlOverride;
        }
        if (context != null) {
            try {
                SharedPreferences prefs = context.getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
                String saved = prefs.getString(PREF_LAST_ACTIVE_URL, null);
                if (saved != null && !saved.isEmpty()) {
                    // If device is actively on Wi-Fi, do not let a saved tunnel URL block local Wi-Fi route
                    if (isWifiActive(context) && saved.startsWith("https://")) {
                        return null;
                    }
                    activeUrlOverride = saved;
                    return saved;
                }
            } catch (Exception ignored) {}
        }
        return null;
    }

    public static boolean isInternetActive() {
        return activeUrlOverride != null && activeUrlOverride.startsWith("https://");
    }

    public static boolean isInternetActive(Context context) {
        if (context != null && isWifiActive(context)) {
            // On Wi-Fi, only consider internet active if explicitly currently routed via https tunnel
            if (activeUrlOverride != null && !activeUrlOverride.isEmpty()) {
                return activeUrlOverride.startsWith("https://");
            }
            return false;
        }
        String active = getActiveUrl(context);
        if (active != null && !active.isEmpty()) {
            return active.startsWith("https://");
        }
        if (context != null) {
            PairedDevice dev = getActiveDevice(context);
            if (dev != null) {
                if (dev.activeUrl != null && dev.activeUrl.startsWith("https://")) return true;
                String base = dev.getBaseUrl();
                if (base != null && base.startsWith("https://")) return true;
                if (!isWifiActive(context) && dev.internetUrl != null && !dev.internetUrl.isEmpty()) {
                    return true;
                }
            }
        }
        return false;
    }

    public interface DiscoveryCallback {
        void onDiscovered(PairedDevice device);
        void onFinished(List<PairedDevice> allFound);
    }

    public static List<PairedDevice> getDevices(Context context) {
        SharedPreferences prefs = context.getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        String jsonStr = prefs.getString(PREF_DEVICES, null);
        List<PairedDevice> list = new ArrayList<>();

        if (jsonStr != null && !jsonStr.isEmpty()) {
            try {
                JSONArray arr = new JSONArray(jsonStr);
                for (int i = 0; i < arr.length(); i++) {
                    PairedDevice d = PairedDevice.fromJsonObject(arr.getJSONObject(i));
                    if (d != null) list.add(d);
                }
            } catch (Exception ignored) {}
        }

        // If no devices saved yet, initialize with default laptop
        if (list.isEmpty()) {
            PairedDevice def = new PairedDevice(DEFAULT_DEVICE_ID, DEFAULT_HOSTNAME, DEFAULT_IP, DEFAULT_PORT, "lunarphoton", null, null, true);
            list.add(def);
            saveDevices(context, list);
        }

        return list;
    }

    public static void saveDevices(Context context, List<PairedDevice> list) {
        SharedPreferences prefs = context.getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        JSONArray arr = new JSONArray();
        for (PairedDevice d : list) {
            arr.put(d.toJsonObject());
        }
        prefs.edit().putString(PREF_DEVICES, arr.toString()).apply();
    }

    public static Network getWifiNetwork(Context context) {
        if (context == null) return null;
        try {
            ConnectivityManager cm = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm != null) {
                Network activeNet = cm.getActiveNetwork();
                if (activeNet != null) {
                    NetworkCapabilities caps = cm.getNetworkCapabilities(activeNet);
                    if (caps != null && (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) || caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET))) {
                        return activeNet;
                    }
                }
                for (Network net : cm.getAllNetworks()) {
                    NetworkCapabilities caps = cm.getNetworkCapabilities(net);
                    if (caps != null && (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) || caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET))) {
                        return net;
                    }
                }
            }
        } catch (Exception ignored) {}
        return null;
    }

    public static boolean isWifiActive(Context context) {
        return getWifiNetwork(context) != null;
    }

    public static PairedDevice getActiveDevice(Context context) {
        List<PairedDevice> list = getDevices(context);
        PairedDevice target = null;
        for (PairedDevice d : list) {
            if (d.isActive) {
                target = d;
                break;
            }
        }
        if (target == null && !list.isEmpty()) {
            list.get(0).isActive = true;
            saveDevices(context, list);
            target = list.get(0);
        }
        if (target == null) {
            target = new PairedDevice(DEFAULT_DEVICE_ID, DEFAULT_HOSTNAME, DEFAULT_IP, DEFAULT_PORT, "lunarphoton", null, null, true);
        }

        boolean wifi = isWifiActive(context);
        String verifiedUrl = getActiveUrl(context);
        if (verifiedUrl != null && !verifiedUrl.isEmpty()) {
            target.activeUrl = verifiedUrl;
        } else {
            if (wifi && target.localUrl != null && !target.localUrl.isEmpty()) {
                target.activeUrl = target.localUrl;
            } else if (!wifi && target.internetUrl != null && !target.internetUrl.isEmpty()) {
                target.activeUrl = target.internetUrl;
            } else if (target.localUrl != null && !target.localUrl.isEmpty()) {
                target.activeUrl = target.localUrl;
            }
        }

        return target;
    }

    public static void setActiveDevice(Context context, String ip) {
        List<PairedDevice> list = getDevices(context);
        for (PairedDevice d : list) {
            d.isActive = d.ip.equals(ip);
        }
        saveDevices(context, list);

        // Update active server_url preference for AuthService
        PairedDevice active = getActiveDevice(context);
        SharedPreferences prefs = context.getSharedPreferences("pc_auth_prefs", Context.MODE_PRIVATE);
        prefs.edit().putString("server_url", active.getBaseUrl()).apply();
    }

    public static PairedDevice addOrUpdateDevice(Context context, PairedDevice newDevice) {
        List<PairedDevice> list = getDevices(context);
        PairedDevice matched = null;
        for (PairedDevice d : list) {
            // Match primarily by persistent unique device ID, or fallback to hostname
            if ((newDevice.deviceId != null && !newDevice.deviceId.isEmpty() && newDevice.deviceId.equals(d.deviceId))
                || (newDevice.hostname != null && !newDevice.hostname.isEmpty() && newDevice.hostname.equalsIgnoreCase(d.hostname))) {
                matched = d;
                break;
            }
        }

        if (matched != null) {
            // SAME LAPTOP FOUND! Update its network location (IP, port) while keeping pairing credentials!
            matched.ip = newDevice.ip;
            matched.port = newDevice.port;
            if (newDevice.deviceId != null && !newDevice.deviceId.isEmpty()) {
                matched.deviceId = newDevice.deviceId;
            }
            if (newDevice.hostname != null && !newDevice.hostname.isEmpty()) {
                matched.hostname = newDevice.hostname;
            }
            if (newDevice.authToken != null && !newDevice.authToken.isEmpty()) {
                matched.authToken = newDevice.authToken;
            }
            if (newDevice.secretKey != null && !newDevice.secretKey.isEmpty()) {
                matched.secretKey = newDevice.secretKey;
            }
            if (newDevice.localUrl != null && !newDevice.localUrl.isEmpty()) {
                matched.localUrl = newDevice.localUrl;
                try {
                    java.net.URI u = new java.net.URI(newDevice.localUrl);
                    if (u.getHost() != null && !u.getHost().isEmpty()) {
                        matched.ip = u.getHost();
                    }
                    if (u.getPort() > 0) {
                        matched.port = u.getPort();
                    }
                } catch (Exception ignored) {}
            }
            if (newDevice.internetUrl != null && !newDevice.internetUrl.isEmpty()) {
                matched.internetUrl = newDevice.internetUrl;
            }

            // Consolidate list: remove any other duplicate entries for this device
            for (int i = list.size() - 1; i >= 0; i--) {
                PairedDevice d = list.get(i);
                if (d != matched) {
                    if ((matched.deviceId != null && matched.deviceId.equals(d.deviceId))
                        || (matched.hostname != null && matched.hostname.equalsIgnoreCase(d.hostname))) {
                        list.remove(i);
                    }
                }
            }

            // AUTO-SELECT: If it was active, or it is paired, or list size is 1, auto-select!
            boolean shouldActivate = matched.isActive || matched.isPaired() || list.size() == 1;
            if (shouldActivate) {
                for (PairedDevice d : list) {
                    d.isActive = (d == matched);
                }
                saveDevices(context, list);
                setActiveDevice(context, matched.ip);
            } else {
                saveDevices(context, list);
            }
            return matched;
        } else {
            // Brand new PC never paired before
            if (list.size() == 1 && list.get(0).deviceId == null && list.get(0).hostname.equalsIgnoreCase(newDevice.hostname)) {
                // Initial migration of the default device
                PairedDevice def = list.get(0);
                def.deviceId = newDevice.deviceId;
                def.ip = newDevice.ip;
                def.port = newDevice.port;
                def.hostname = newDevice.hostname;
                def.localUrl = newDevice.localUrl;
                def.internetUrl = newDevice.internetUrl;
                def.isActive = true;
                setActiveDevice(context, def.ip);
                return def;
            }
            // Remove un-paired stale entries with same hostname
            for (int i = list.size() - 1; i >= 0; i--) {
                PairedDevice d = list.get(i);
                if (!d.isPaired() && (newDevice.hostname != null && newDevice.hostname.equalsIgnoreCase(d.hostname))) {
                    list.remove(i);
                }
            }
            newDevice.isActive = true;
            for (PairedDevice d : list) {
                d.isActive = false;
            }
            list.add(newDevice);
            saveDevices(context, list);
            setActiveDevice(context, newDevice.ip);
            return newDevice;
        }
    }

    public static void removeDevice(Context context, String ip) {
        List<PairedDevice> list = getDevices(context);
        PairedDevice toRemove = null;
        for (PairedDevice d : list) {
            if (d.ip.equals(ip)) {
                toRemove = d;
                break;
            }
        }
        if (toRemove != null) {
            list.remove(toRemove);
            if (toRemove.isActive && !list.isEmpty()) {
                list.get(0).isActive = true;
            }
            saveDevices(context, list);
            if (!list.isEmpty()) {
                setActiveDevice(context, getActiveDevice(context).ip);
            }
        }
    }

    public static void discoverDevices(Context context, DiscoveryCallback callback) {
        Handler mainHandler = new Handler(Looper.getMainLooper());
        new Thread(() -> {
            List<PairedDevice> discovered = new ArrayList<>();
            DatagramSocket socket = null;
            try {
                socket = new DatagramSocket();
                socket.setBroadcast(true);
                socket.setSoTimeout(1500);

                byte[] sendData = "{\"cmd\":\"discover\"}".getBytes("UTF-8");

                // 1. Collect all broadcast targets across all interfaces (Wi-Fi, Hotspot, USB tethering)
                List<InetAddress> broadcastTargets = new ArrayList<>();
                List<String> subnetPrefixes = new ArrayList<>();
                try {
                    broadcastTargets.add(InetAddress.getByName("255.255.255.255"));
                    Enumeration<NetworkInterface> interfaces = NetworkInterface.getNetworkInterfaces();
                    while (interfaces != null && interfaces.hasMoreElements()) {
                        NetworkInterface iface = interfaces.nextElement();
                        if (iface.isLoopback() || !iface.isUp()) continue;
                        for (InterfaceAddress ifAddr : iface.getInterfaceAddresses()) {
                            InetAddress bcast = ifAddr.getBroadcast();
                            if (bcast != null && !broadcastTargets.contains(bcast)) {
                                broadcastTargets.add(bcast);
                            }
                            InetAddress ipAddr = ifAddr.getAddress();
                            if (ipAddr instanceof Inet4Address && !ipAddr.isLoopbackAddress()) {
                                String ipStr = ipAddr.getHostAddress();
                                int lastDot = ipStr.lastIndexOf('.');
                                if (lastDot > 0) {
                                    String prefix = ipStr.substring(0, lastDot + 1);
                                    if (!subnetPrefixes.contains(prefix)) {
                                        subnetPrefixes.add(prefix);
                                    }
                                }
                            }
                        }
                    }
                } catch (Exception ignored) {}

                // Common Android hotspot subnets if not already discovered
                if (!subnetPrefixes.contains("192.168.43.")) subnetPrefixes.add("192.168.43.");

                // Send UDP broadcast to all collected targets
                for (InetAddress target : broadcastTargets) {
                    try {
                        socket.send(new DatagramPacket(sendData, sendData.length, target, DEFAULT_PORT));
                    } catch (Exception ignored) {}
                }

                // 2. Parallel Fast-Probe on subnets (crucial for Hotspots where UDP broadcast is blocked by Android kernel)
                ExecutorService pool = Executors.newFixedThreadPool(30);
                for (String prefix : subnetPrefixes) {
                    // Check gateway .1 and client range .2 to .50 + common DHCP addresses
                    for (int i = 1; i <= 65; i++) {
                        final String testIp = prefix + i;
                        pool.execute(() -> {
                            String res = NetworkUtils.httpGet("http://" + testIp + ":" + DEFAULT_PORT + "/api/info", 350);
                            if (res != null) {
                                try {
                                    JSONObject json = new JSONObject(res);
                                     String host = json.optString("hostname", "Computer");
                                    String devId = json.optString("device_id", null);
                                    String localUrl = json.optString("local_url", null);
                                    String internetUrl = json.optString("internet_url", null);
                                    PairedDevice dev = new PairedDevice(devId, host, testIp, DEFAULT_PORT, "user", null, null, false, localUrl, internetUrl);
                                    synchronized (discovered) {
                                        boolean duplicate = false;
                                        for (PairedDevice d : discovered) {
                                            if (d.ip.equals(testIp)) { duplicate = true; break; }
                                        }
                                        if (!duplicate) {
                                            discovered.add(dev);
                                            mainHandler.post(() -> callback.onDiscovered(dev));
                                        }
                                    }
                                } catch (Exception ignored) {}
                            }
                        });
                    }
                }

                // Also listen for UDP replies
                long startTime = System.currentTimeMillis();
                byte[] recvBuf = new byte[1024];
                while (System.currentTimeMillis() - startTime < 1600) {
                    try {
                        DatagramPacket recvPacket = new DatagramPacket(recvBuf, recvBuf.length);
                        socket.receive(recvPacket);
                        String msg = new String(recvPacket.getData(), 0, recvPacket.getLength(), "UTF-8");
                        JSONObject json = new JSONObject(msg);

                        String type = json.optString("type", "");
                        if ("pc_auth_discovery_reply".equals(type) || "pc_auth_beacon".equals(type)) {
                            String host = json.optString("hostname", "Computer");
                            String devId = json.optString("device_id", null);
                            String ip = recvPacket.getAddress().getHostAddress();
                            int port = json.optInt("port", DEFAULT_PORT);
                            String user = json.optString("user", "user");
                            String localUrl = json.optString("local_url", null);
                            String internetUrl = json.optString("internet_url", null);

                            PairedDevice dev = new PairedDevice(devId, host, ip, port, user, null, null, false, localUrl, internetUrl);
                            synchronized (discovered) {
                                boolean duplicate = false;
                                for (PairedDevice d : discovered) {
                                    if (d.ip.equals(dev.ip)) { duplicate = true; break; }
                                }
                                if (!duplicate) {
                                    discovered.add(dev);
                                    mainHandler.post(() -> callback.onDiscovered(dev));
                                }
                            }
                        }
                    } catch (Exception timeoutOrDone) {
                        break;
                    }
                }

                pool.shutdown();
                try { pool.awaitTermination(1500, TimeUnit.MILLISECONDS); } catch (Exception ignored) {}

            } catch (Exception e) {
                e.printStackTrace();
            } finally {
                if (socket != null) socket.close();
            }

            mainHandler.post(() -> callback.onFinished(discovered));
        }).start();
    }
}

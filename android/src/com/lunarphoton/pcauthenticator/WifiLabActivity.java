package com.lunarphoton.pcauthenticator;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Context;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ProgressBar;
import android.widget.TextView;
import org.json.JSONObject;

public class WifiLabActivity extends Activity {
    private WebView webView;
    private ProgressBar pbLoading;
    private View llError;
    private TextView tvErrorMsg, tvSub;
    private String targetUrl;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_wifi_lab);

        if (Build.VERSION.SDK_INT >= 23 && checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION}, 101);
        }

        findViewById(R.id.btn_wifilab_back).setOnClickListener(v -> handleBack());
        findViewById(R.id.btn_wifilab_refresh).setOnClickListener(v -> reload());
        findViewById(R.id.btn_wifilab_retry).setOnClickListener(v -> reload());

        tvSub = findViewById(R.id.tv_wifilab_sub);
        pbLoading = findViewById(R.id.pb_wifilab_loading);
        llError = findViewById(R.id.ll_wifilab_error);
        tvErrorMsg = findViewById(R.id.tv_wifilab_error_msg);
        webView = findViewById(R.id.wv_wifilab);

        String deviceName = getIntent().getStringExtra("device_name");
        if (deviceName != null && !deviceName.isEmpty()) {
            tvSub.setText("RF telemetry · " + deviceName);
        }

        setupWebView();
        loadUrl();
    }

    public class OrbitLabBridge {
        @android.webkit.JavascriptInterface
        public void onModuleOpened(String title) {
            runOnUiThread(() -> {
                TextView tvTitle = findViewById(R.id.tv_wifilab_title);
                if (tvTitle != null && title != null) tvTitle.setText(title);
                if (tvSub != null) tvSub.setText("Enlarged view · Tap back to overview");
            });
        }

        @android.webkit.JavascriptInterface
        public void onModuleClosed() {
            runOnUiThread(() -> {
                TextView tvTitle = findViewById(R.id.tv_wifilab_title);
                if (tvTitle != null) tvTitle.setText("Wi-Fi Lab");
                String deviceName = getIntent().getStringExtra("device_name");
                if (tvSub != null) tvSub.setText(deviceName != null && !deviceName.isEmpty() ? "RF telemetry · " + deviceName : "Live RF telemetry & spectrum");
            });
        }

        @android.webkit.JavascriptInterface
        public String getPhoneWifiInfo() {
            try {
                WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
                if (wm == null) return "{}";
                WifiInfo info = wm.getConnectionInfo();
                if (info == null) return "{}";
                int rssi = info.getRssi();
                if (rssi == -127 || rssi == 0) return "{}";
                String ssid = info.getSSID();
                if (ssid != null && ssid.startsWith("\"") && ssid.endsWith("\"") && ssid.length() >= 2) {
                    ssid = ssid.substring(1, ssid.length() - 1);
                }
                if ("<unknown ssid>".equalsIgnoreCase(ssid)) ssid = "";
                String bssid = info.getBSSID() != null ? info.getBSSID().toLowerCase() : "";
                if ("02:00:00:00:00:00".equals(bssid)) bssid = "";

                int freq = 0;
                if (android.os.Build.VERSION.SDK_INT >= 21) {
                    freq = info.getFrequency();
                }

                // Modern Android 10+ (API 29+) NetworkCapabilities check
                if (android.os.Build.VERSION.SDK_INT >= 29) {
                    try {
                        ConnectivityManager cm = (ConnectivityManager) getApplicationContext().getSystemService(Context.CONNECTIVITY_SERVICE);
                        if (cm != null) {
                            Network net = cm.getActiveNetwork();
                            if (net != null) {
                                NetworkCapabilities caps = cm.getNetworkCapabilities(net);
                                if (caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
                                    Object transportInfo = caps.getTransportInfo();
                                    if (transportInfo instanceof WifiInfo) {
                                        WifiInfo tInfo = (WifiInfo) transportInfo;
                                        if (tInfo.getRssi() != -127 && tInfo.getRssi() != 0) {
                                            rssi = tInfo.getRssi();
                                        }
                                        String tSsid = tInfo.getSSID();
                                        if (tSsid != null && tSsid.startsWith("\"") && tSsid.endsWith("\"") && tSsid.length() >= 2) {
                                            tSsid = tSsid.substring(1, tSsid.length() - 1);
                                        }
                                        if (tSsid != null && !tSsid.isEmpty() && !"<unknown ssid>".equalsIgnoreCase(tSsid)) {
                                            ssid = tSsid;
                                        }
                                        if (tInfo.getBSSID() != null && !"02:00:00:00:00:00".equals(tInfo.getBSSID())) {
                                            bssid = tInfo.getBSSID().toLowerCase();
                                        }
                                        if (freq == 0) freq = tInfo.getFrequency();
                                    }
                                }
                            }
                        }
                    } catch (Exception ignored) {}
                }

                JSONObject obj = new JSONObject();
                obj.put("available", true);
                obj.put("rssi", rssi);
                obj.put("ssid", ssid != null ? ssid : "");
                obj.put("bssid", bssid);
                obj.put("freq", freq);
                obj.put("t", System.currentTimeMillis() / 1000.0);
                return obj.toString();
            } catch (Exception e) {
                return "{}";
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void setupWebView() {
        webView.setBackgroundColor(0xFF0D0F12);
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);

        webView.addJavascriptInterface(new OrbitLabBridge(), "OrbitLab");

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                if (newProgress < 100) {
                    pbLoading.setVisibility(View.VISIBLE);
                } else {
                    pbLoading.setVisibility(View.GONE);
                }
            }
        });

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                pbLoading.setVisibility(View.VISIBLE);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                pbLoading.setVisibility(View.GONE);
                llError.setVisibility(View.GONE);
                webView.setVisibility(View.VISIBLE);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    pbLoading.setVisibility(View.GONE);
                    webView.setVisibility(View.GONE);
                    llError.setVisibility(View.VISIBLE);
                    tvErrorMsg.setText("Cannot connect to " + (targetUrl != null ? targetUrl : "computer") + ".\nEnsure the computer is running Orbit.");
                }
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return false;
            }
        });
    }

    private void loadUrl() {
        String baseUrl = getIntent().getStringExtra("base_url");
        if (baseUrl == null || baseUrl.isEmpty()) {
            PairedDevice d = DeviceManager.getActiveDevice(this);
            if (d != null) {
                baseUrl = d.getBaseUrl();
                if (baseUrl == null || baseUrl.isEmpty()) {
                    baseUrl = d.getInternetUrl();
                }
            }
        }
        if (baseUrl != null && !baseUrl.isEmpty()) {
            if (!baseUrl.endsWith("/")) baseUrl += "/";
            targetUrl = baseUrl + "wifilab?embedded=1";
            llError.setVisibility(View.GONE);
            webView.setVisibility(View.VISIBLE);
            webView.loadUrl(targetUrl);
        } else {
            llError.setVisibility(View.VISIBLE);
            webView.setVisibility(View.GONE);
            tvErrorMsg.setText("Computer not paired or offline.");
        }
    }

    private void reload() {
        if (webView != null && targetUrl != null) {
            llError.setVisibility(View.GONE);
            webView.setVisibility(View.VISIBLE);
            webView.loadUrl(targetUrl);
        } else {
            loadUrl();
        }
    }

    private void handleBack() {
        if (webView != null) {
            webView.evaluateJavascript("(function(){ if (typeof isFullscreenOpen === 'function' && isFullscreenOpen()) { closeLabFullscreen(); return true; } return false; })()", value -> {
                if (!"true".equals(value)) {
                    if (webView.canGoBack()) {
                        webView.goBack();
                    } else {
                        finish();
                    }
                }
            });
        } else {
            finish();
        }
    }

    @Override
    public void onBackPressed() {
        handleBack();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) {
            webView.onResume();
            webView.resumeTimers();
            webView.evaluateJavascript("if (typeof startAllPolling === 'function') startAllPolling();", null);
        }
    }

    @Override
    protected void onPause() {
        if (webView != null) {
            webView.evaluateJavascript("if (typeof stopAllPolling === 'function') stopAllPolling();", null);
            webView.onPause();
            webView.pauseTimers();
        }
        super.onPause();
    }

    @Override
    protected void onStop() {
        if (webView != null) {
            webView.evaluateJavascript("if (typeof stopAllPolling === 'function') stopAllPolling();", null);
            webView.pauseTimers();
        }
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.evaluateJavascript("if (typeof stopAllPolling === 'function') stopAllPolling();", null);
            webView.stopLoading();
            webView.pauseTimers();
            webView.destroy();
        }
        super.onDestroy();
    }
}

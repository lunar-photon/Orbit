package com.lunarphoton.pcauthenticator;

import android.app.Application;

/** Gives the network code a context from the start, whichever screen or service opens first. */
public class OrbitApp extends Application {
    @Override
    public void onCreate() {
        super.onCreate();
        NetworkUtils.init(this);
    }
}

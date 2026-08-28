package dev.levangie.lmloop.web

import android.webkit.JavascriptInterface

/**
 * Lets the dashboard tell native and web chrome apart, so it can hide
 * controls the native app's own Scaffold now owns (e.g. `#new-run` -- see
 * `app.js`'s `NATIVE_SHELL` constant) without affecting the plain-browser or
 * installed-PWA path, where this object simply does not exist. Bound via
 * `addJavascriptInterface` before `loadUrl`, so `window.LmloopNative` is
 * visible to every page script -- including inline ones -- from the very
 * first line; see MainActivity.configureWebView. The WebView here only ever
 * loads the operator's own self-hosted server, so exposing this one
 * boolean-returning method carries no meaningful risk.
 */
class NativeShellBridge {
    @JavascriptInterface
    fun isNativeApp(): Boolean = true
}

package dev.levangie.lmloop.web

import android.content.ActivityNotFoundException
import android.content.Intent
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient

/**
 * Every `http(s)` navigation stays inside the WebView, **including the OIDC
 * issuer's own pages** during the PKCE redirect (`/login/start` -> issuer ->
 * `/oauth/callback` -- see `web/auth.py`): a third origin, by design.
 * Restricting navigation to the configured server's own origin would break
 * login outright, so only non-`http(s)` schemes -- an `intent://`,
 * `mailto:`, or similar link the dashboard might ever add -- are handed off
 * to the system.
 */
class DashboardWebViewClient(
    private val onPageFinished: (WebView) -> Unit,
) : WebViewClient() {
    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
        val scheme = request.url.scheme?.lowercase()
        if (scheme == "http" || scheme == "https") return false
        return try {
            view.context.startActivity(Intent(Intent.ACTION_VIEW, request.url))
            true
        } catch (_: ActivityNotFoundException) {
            true // nothing on the device can open it; swallow rather than crash the WebView
        }
    }

    override fun onPageFinished(view: WebView, url: String?) {
        super.onPageFinished(view, url)
        onPageFinished(view)
    }
}

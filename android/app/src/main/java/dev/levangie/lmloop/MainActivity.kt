package dev.levangie.lmloop

import android.annotation.SuppressLint
import android.content.Intent
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.WebSettings
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView
import dev.levangie.lmloop.setup.SetupScreen
import dev.levangie.lmloop.sync.WorkScheduler
import dev.levangie.lmloop.watch.WatchBar
import dev.levangie.lmloop.web.DashboardWebChromeClient
import dev.levangie.lmloop.web.DashboardWebViewClient
import dev.levangie.lmloop.web.currentRoute

/**
 * Hybrid shell: every screen the user actually looks at is the same
 * `web/static/` dashboard, rendered in a WebView -- one source of truth for
 * the UI, kept in sync automatically with every change to the web app. This
 * class adds only what a browser tab cannot: first-run setup, deep-linking
 * a notification tap back into the right run, and the native "watch this
 * run" overlay (see watch/WatchBar.kt).
 */
class MainActivity : ComponentActivity() {
    private var webView: WebView? = null
    private var consumedInitialDeepLink = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val services = lmloopServices

        setContent {
            var configured by remember { mutableStateOf(services.configStore.isConfigured()) }
            var route by remember { mutableStateOf(currentRoute(null)) }

            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    if (!configured) {
                        SetupScreen(
                            api = services.api,
                            configStore = services.configStore,
                            onConfigured = {
                                WorkScheduler.schedule(this@MainActivity)
                                configured = true
                            },
                        )
                    } else {
                        Box(modifier = Modifier.fillMaxSize()) {
                            AndroidView(
                                modifier = Modifier.fillMaxSize(),
                                factory = { context ->
                                    WebView(context).also { view ->
                                        webView = view
                                        configureWebView(view) { route = it }
                                        services.configStore.loadServerUrl()?.let(view::loadUrl)
                                    }
                                },
                            )
                            WatchBar(route = route)
                        }
                    }
                }
            }
        }

        onBackPressedDispatcher.addCallback(
            this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    val view = webView
                    if (view != null && view.canGoBack()) {
                        view.goBack()
                    } else {
                        isEnabled = false
                        onBackPressedDispatcher.onBackPressed()
                    }
                }
            },
        )
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        navigateToDeepLink(intent)
    }

    private fun configureWebView(view: WebView, onRouteChanged: (dev.levangie.lmloop.web.DashboardRoute?) -> Unit) {
        view.settings.javaScriptEnabled = true
        view.settings.domStorageEnabled = true
        // The dashboard is same-origin, self-hosted, and its own CSP already
        // refuses third-party subresources server-side (see web/server.py's
        // CSP header) -- this is the WebView's half of the same refusal.
        view.settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(view, true)
        view.webViewClient = DashboardWebViewClient(
            onPageFinished = {
                // The WebView's cookie jar is not synchronously flushed to
                // disk -- without this, a process kill immediately after the
                // OIDC callback navigation could lose a very fresh session
                // cookie.
                CookieManager.getInstance().flush()
                onRouteChanged(currentRoute(view.url))
                if (!consumedInitialDeepLink) {
                    consumedInitialDeepLink = true
                    navigateToDeepLink(intent)
                }
            },
        )
        view.webChromeClient = DashboardWebChromeClient(view.context)
        view.setDownloadListener { url, _, _, _, _ ->
            startActivity(Intent(Intent.ACTION_VIEW, android.net.Uri.parse(url)))
        }
    }

    /** A tap on a `RunWatchNotifications`/`ClosedAppNotifications` push lands
     * here with the run to open; this is the one place native code reaches
     * into the page, and it does so the same way a person would -- by
     * setting the hash the SPA's own router already reads. */
    private fun navigateToDeepLink(intent: Intent) {
        val project = intent.getStringExtra(EXTRA_OPEN_PROJECT) ?: return
        val runId = intent.getStringExtra(EXTRA_OPEN_RUN_ID) ?: return
        webView?.evaluateJavascript("location.hash = ${jsStringLiteral("#$project/$runId")};", null)
    }

    private fun jsStringLiteral(value: String): String =
        "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\""

    companion object {
        const val EXTRA_OPEN_PROJECT = "open_project"
        const val EXTRA_OPEN_RUN_ID = "open_run_id"
    }
}

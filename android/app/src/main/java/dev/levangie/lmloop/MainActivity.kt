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
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBars
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import dev.levangie.lmloop.settings.SettingsScreen
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
 * a notification tap back into the right run, the native "watch this run"
 * overlay (see watch/WatchBar.kt), and a settings screen (gear icon) for
 * everything about this app's own configuration -- sign-out, server,
 * notification permission, device token.
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
            var showSettings by remember { mutableStateOf(false) }
            var hasToken by remember { mutableStateOf(services.configStore.hasToken()) }

            MaterialTheme(colorScheme = LmloopColorScheme) {
                // Apps targeting API 35+ get edge-to-edge forced by the
                // system -- `Window.setDecorFitsSystemWindows(true)` is
                // silently ignored for us regardless of what we pass it, so
                // opting out is not an option. This is the actual fix: pad
                // the whole content area (WebView included, since it is a
                // child of this same layout) by the real system-bar insets,
                // so nothing -- neither this Activity's own composables nor
                // the dashboard's own bottom "active runs" strip -- draws
                // under the status bar or the navigation bar.
                Surface(modifier = Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.systemBars)) {
                    if (!configured) {
                        SetupScreen(
                            api = services.api,
                            configStore = services.configStore,
                            onConfigured = { configured = true },
                        )
                    } else if (showSettings) {
                        SettingsScreen(
                            api = services.api,
                            configStore = services.configStore,
                            onLogout = {
                                services.configStore.loadServerUrl()?.let { url ->
                                    webView?.loadUrl("$url/logout")
                                }
                                showSettings = false
                            },
                            onServerChanged = {
                                services.configStore.clear()
                                hasToken = false
                                showSettings = false
                                configured = false
                            },
                            onDone = {
                                hasToken = services.configStore.hasToken()
                                if (hasToken) WorkScheduler.schedule(this@MainActivity)
                                showSettings = false
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
                            WatchBar(
                                route = route,
                                hasToken = hasToken,
                                onNeedsSetup = { showSettings = true },
                                modifier = Modifier.align(Alignment.BottomEnd),
                            )
                            TextButton(
                                onClick = {
                                    webView?.let { view ->
                                        // A "hard" refresh: bypass the
                                        // WebView's own HTTP cache, not just
                                        // reload the last-rendered page.
                                        view.clearCache(true)
                                        view.reload()
                                    }
                                },
                                modifier = Modifier.align(Alignment.TopEnd).padding(top = 8.dp, end = 56.dp),
                            ) {
                                Text("⟳")
                            }
                            TextButton(
                                onClick = { showSettings = true },
                                modifier = Modifier.align(Alignment.TopEnd).padding(8.dp),
                            ) {
                                Text("⚙")
                            }
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

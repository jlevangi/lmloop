package dev.levangie.lmloop

import android.annotation.SuppressLint
import android.content.Intent
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.WebSettings
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.SystemBarStyle
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBars
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
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
import dev.levangie.lmloop.watch.WatchBarAction
import dev.levangie.lmloop.web.DashboardWebChromeClient
import dev.levangie.lmloop.web.DashboardWebViewClient
import dev.levangie.lmloop.web.NativeShellBridge
import dev.levangie.lmloop.web.currentRoute

/**
 * Hybrid shell: every screen the user actually looks at is the same
 * `web/static/` dashboard, rendered in a WebView -- one source of truth for
 * the UI, kept in sync automatically with every change to the web app. This
 * class adds only what a browser tab cannot: first-run setup, deep-linking
 * a notification tap back into the right run, a native TopAppBar (new run /
 * watch-this-run / settings actions -- see watch/WatchBar.kt) that replaces
 * the dashboard's own header controls it would otherwise collide with, and a
 * settings screen (gear icon) for everything about this app's own
 * configuration -- sign-out, server, notification permission, device token.
 *
 * The dashboard tells this shell apart from a plain browser tab or the
 * installed PWA via `window.LmloopNative` (see web/NativeShellBridge.kt) --
 * bound only here, so both of those other paths are unaffected by anything
 * gated on it.
 */
class MainActivity : ComponentActivity() {
    private var webView: WebView? = null
    private var consumedInitialDeepLink = false

    // --ink from web/static/style.css and Theme.kt's LmloopColorScheme --
    // duplicated as a raw Int because enableEdgeToEdge takes one before any
    // Compose color type is available.
    private val inkColor = 0xFF0C0C0F.toInt()

    @OptIn(ExperimentalMaterial3Api::class)
    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val services = lmloopServices

        // Targeting API 35+ forces edge-to-edge regardless of what we ask
        // for, and left alone the system paints its own grey scrim behind
        // the status/nav bars rather than this app's near-black ink -- this
        // is what replaces that scrim with an explicit, matching one instead
        // of leaving it to the platform default.
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(inkColor),
            navigationBarStyle = SystemBarStyle.dark(inkColor),
        )

        // Lets desktop Chrome's chrome://inspect attach to the on-device
        // WebView -- the only way to see how *this* system WebView build
        // actually resolves things like the `dvh` unit; Chrome DevTools'
        // device emulation runs desktop Chromium and cannot reproduce it.
        // Two prior inset bugs (see MainActivity's edge-to-edge comment
        // below) shipped looking fixed until checked this way.
        if (BuildConfig.DEBUG) {
            WebView.setWebContentsDebuggingEnabled(true)
        }

        setContent {
            var configured by remember { mutableStateOf(services.configStore.isConfigured()) }
            var route by remember { mutableStateOf(currentRoute(null)) }
            var showSettings by remember { mutableStateOf(false) }
            var hasToken by remember { mutableStateOf(services.configStore.hasToken()) }
            var isRefreshing by remember { mutableStateOf(false) }

            // Without this, the system gesture-back swipe on the Settings
            // screen fell through to the imperative WebView-history callback
            // below -- which, with the WebView detached from composition
            // while Settings is showing, found no history and exited the
            // app instead of closing Settings. A composable BackHandler
            // registers ahead of that callback and only intercepts while
            // enabled, so it takes over exactly when Settings is open and
            // steps aside otherwise.
            BackHandler(enabled = showSettings) { showSettings = false }

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
                        // Icons float directly over the WebView -- no
                        // separate bar. A TopAppBar here, even title-less
                        // and transparent, still reserved its own ~64dp row
                        // above the dashboard's own header, which read as
                        // a tall dead band rather than "one header": the
                        // web page's own sticky bar was still a fixed
                        // distance further down regardless. Floating the
                        // icons directly at the top edge instead means they
                        // sit right at the same height as the web header
                        // they're the trailing corner of.
                        //
                        // This is only safe from repeating the original
                        // overlap bug because the dashboard's own `#new-run`
                        // is hidden here (see NativeShellBridge / app.js's
                        // NATIVE_SHELL) -- there is exactly one control in
                        // this corner, native, not the native icons landing
                        // on top of the page's own.
                        Box(modifier = Modifier.fillMaxSize()) {
                            PullToRefreshBox(
                                modifier = Modifier.fillMaxSize(),
                                isRefreshing = isRefreshing,
                                onRefresh = {
                                    isRefreshing = true
                                    webView?.let { view ->
                                        // A "hard" refresh: bypass the
                                        // WebView's own HTTP cache, not just
                                        // reload the last-rendered page.
                                        view.clearCache(true)
                                        view.reload()
                                    }
                                },
                            ) {
                                AndroidView(
                                    modifier = Modifier.fillMaxSize(),
                                    factory = { context ->
                                        WebView(context).also { view ->
                                            webView = view
                                            configureWebView(view) {
                                                route = it
                                                isRefreshing = false
                                            }
                                            services.configStore.loadServerUrl()?.let(view::loadUrl)
                                        }
                                    },
                                )
                            }
                            Row(
                                modifier = Modifier.align(Alignment.TopEnd).padding(top = 2.dp, end = 4.dp),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                if (route == null) {
                                    IconButton(onClick = {
                                        webView?.evaluateJavascript(
                                            "if (window.go) { go('#new'); }",
                                            null,
                                        )
                                    }) {
                                        Icon(Icons.Filled.Add, contentDescription = "New run")
                                    }
                                } else {
                                    WatchBarAction(
                                        route = route,
                                        hasToken = hasToken,
                                        onNeedsSetup = { showSettings = true },
                                    )
                                }
                                IconButton(onClick = { showSettings = true }) {
                                    Icon(Icons.Filled.Settings, contentDescription = "Settings")
                                }
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
        // Bound before `loadUrl` so `window.LmloopNative` exists for every
        // page script, including inline ones -- see NativeShellBridge's doc.
        view.addJavascriptInterface(NativeShellBridge(), "LmloopNative")
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

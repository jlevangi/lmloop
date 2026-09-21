package dev.levangie.lmloop.web

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Message
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient

/**
 * `target="_blank"` and `window.open` do not navigate the main WebView on
 * their own. A throwaway WebView catches the URL so HTTP pages can open in
 * the app's single tab; non-web schemes still go to the system.
 */
class DashboardWebChromeClient(
    private val context: Context,
    private val onOpenWindow: (Uri) -> Unit,
) : WebChromeClient() {
    override fun onCreateWindow(
        view: WebView,
        isDialog: Boolean,
        isUserGesture: Boolean,
        resultMsg: Message,
    ): Boolean {
        val transport = resultMsg.obj as? WebView.WebViewTransport ?: return false
        val catcher = WebView(context)
        catcher.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(dummy: WebView, request: WebResourceRequest): Boolean {
                if (request.url.scheme in setOf("http", "https")) onOpenWindow(request.url)
                else openExternally(request.url)
                return true
            }
        }
        transport.webView = catcher
        resultMsg.sendToTarget()
        return true
    }

    private fun openExternally(uri: Uri) {
        try {
            context.startActivity(Intent(Intent.ACTION_VIEW, uri))
        } catch (_: ActivityNotFoundException) {
            // nothing installed can open it; the tap simply does nothing
        }
    }
}

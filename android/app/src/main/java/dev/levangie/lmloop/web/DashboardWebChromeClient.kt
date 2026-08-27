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
 * `target="_blank"` and `window.open` do not navigate the WebView on their
 * own -- without this override they silently do nothing at all. There is
 * nowhere to put a second tab in a single-Activity shell, so the standard
 * idiom applies: give the new window a throwaway `WebView` whose only job is
 * to catch the URL it was asked to load and hand that off to the system
 * instead of ever actually rendering it.
 */
class DashboardWebChromeClient(private val context: Context) : WebChromeClient() {
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
                openExternally(request.url)
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

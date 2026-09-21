package dev.levangie.lmloop.web

/**
 * Mirrors `app.js`'s own hash router (`parseHash`/`route`: `#new` -> new-run
 * form, `#project/runId` -> run detail, else the list) -- read-only, and
 * one-directional. This never drives navigation; it only tells the native
 * "watch this run" overlay which run, if any, is currently on screen, the
 * same way `app.js` decides which view to show.
 *
 * Deliberately plain string handling rather than `android.net.Uri`: this
 * only ever sees a well-formed `http(s)` URL from `WebView.getUrl()`, full
 * RFC 3986 parsing buys nothing here, and `Uri` is an Android framework
 * class that is unavailable (throws) in a plain JVM unit test -- see
 * `DashboardRouteTest`, which is one.
 */
data class DashboardRoute(val project: String, val runId: String)

/** Preview pages use a different origin (usually the same host on a project port). */
fun isDashboardUrl(url: String?, dashboardUrl: String?): Boolean {
    if (url == null || dashboardUrl == null) return true
    return try {
        val current = java.net.URI(url)
        val dashboard = java.net.URI(dashboardUrl)
        current.scheme.equals(dashboard.scheme, ignoreCase = true) &&
            current.host.equals(dashboard.host, ignoreCase = true) &&
            effectivePort(current) == effectivePort(dashboard)
    } catch (_: IllegalArgumentException) {
        true
    }
}

private fun effectivePort(uri: java.net.URI): Int = when {
    uri.port >= 0 -> uri.port
    uri.scheme.equals("https", ignoreCase = true) -> 443
    else -> 80
}

fun currentRoute(url: String?): DashboardRoute? {
    if (url == null) return null
    val hashIndex = url.indexOf('#')
    if (hashIndex < 0) return null
    val fragment = url.substring(hashIndex + 1)
    if (fragment.isEmpty() || fragment == "new") return null
    val parts = fragment.split("/", limit = 2)
    if (parts.size != 2 || parts[0].isEmpty() || parts[1].isEmpty()) return null
    return DashboardRoute(parts[0], parts[1])
}

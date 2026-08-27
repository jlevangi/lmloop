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

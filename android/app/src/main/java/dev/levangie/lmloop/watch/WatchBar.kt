package dev.levangie.lmloop.watch

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import dev.levangie.lmloop.web.DashboardRoute

/**
 * Native, drawn around the WebView rather than injected into it -- `app.js`
 * stays completely unaware of Android, so the plain browser PWA is untouched
 * by any of this. "Which run is on screen" comes from `route`, parsed
 * one-directionally from the WebView's own URL (see `web/DashboardRoute.kt`);
 * this never navigates the page, only reads where it already is.
 *
 * `hasToken` is whether a device token is configured -- see
 * `ServerConfigStore`'s doc comment for why this is optional and separate
 * from just using the app. Without one, `RunWatchService` cannot
 * authenticate at all, so the button routes to `onNeedsSetup` instead of
 * starting a service that would immediately fail silently.
 *
 * Takes a `modifier` rather than positioning itself: a `Box` that does not
 * fill its parent sizes itself to wrap its own content, so its own
 * `contentAlignment` has nothing to align *against* -- positioning within
 * the screen has to be the caller's `Modifier.align(...)` on this
 * composable's root, resolved by the parent `Box` it actually sits in
 * (MainActivity's). An earlier version aligned only internally and ended up
 * pinned to the top-left of the screen instead of the bottom-right.
 */
@Composable
fun WatchBar(route: DashboardRoute?, hasToken: Boolean, onNeedsSetup: () -> Unit, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    var watching by remember(route) { mutableStateOf(false) }
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted && route != null) {
            RunWatchService.start(context, route.project, route.runId)
            watching = true
        }
    }

    if (route == null) return

    Box(modifier = modifier.padding(16.dp), contentAlignment = Alignment.BottomEnd) {
        Button(onClick = {
            when {
                !hasToken -> onNeedsSetup()
                watching -> {
                    RunWatchService.stop(context)
                    watching = false
                }
                Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                    ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) !=
                    PackageManager.PERMISSION_GRANTED ->
                    permissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                else -> {
                    RunWatchService.start(context, route.project, route.runId)
                    watching = true
                }
            }
        }) {
            Text(if (!hasToken) "Set up notifications" else if (watching) "Watching" else "Watch this run")
        }
    }
}

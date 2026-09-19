package dev.levangie.lmloop.watch

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import androidx.core.content.ContextCompat
import dev.levangie.lmloop.web.DashboardRoute

/**
 * Run-watch action displayed by MainActivity in the floating top-end control
 * cluster above the WebView. Keeping it at the top avoids the dashboard's
 * fixed bottom run bar; it is not part of a Material TopAppBar.
 *
 * "Which run is on screen" comes from `route`, parsed one-directionally
 * from the WebView's own URL (see `web/DashboardRoute.kt`); this never
 * navigates the page, only reads where it already is.
 *
 * `hasToken` is whether a device token is configured -- see
 * `ServerConfigStore`'s doc comment for why this is optional and separate
 * from just using the app. Without one, `RunWatchService` cannot
 * authenticate at all, so the action routes to `onNeedsSetup` instead of
 * starting a service that would immediately fail silently.
 */
@Composable
fun WatchBarAction(route: DashboardRoute?) {
    if (route == null) return

    val context = LocalContext.current
    var watching by remember(route) {
        mutableStateOf(RunWatchService.isWatching(route.project, route.runId))
    }
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            RunWatchService.start(context, route.project, route.runId)
            watching = true
        }
    }

    IconButton(onClick = {
        when {
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
        // `NotificationsActive` isn't in the app's icon dependency (the
        // small "core" set, not the multi-thousand-icon "extended" one --
        // not worth the APK size for a single glyph variant); tint carries
        // the on/off distinction instead of a different bell shape.
        Icon(
            imageVector = Icons.Filled.Notifications,
            tint = if (watching) MaterialTheme.colorScheme.primary else LocalContentColor.current,
            contentDescription = if (watching) "Watching" else "Watch this run",
        )
    }
}

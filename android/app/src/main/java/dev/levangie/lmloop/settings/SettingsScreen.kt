package dev.levangie.lmloop.settings

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import dev.levangie.lmloop.config.ServerConfigStore
import dev.levangie.lmloop.net.ApiResult
import dev.levangie.lmloop.net.LmloopApiClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Everything about this app's own configuration in one place -- there was
 * previously no way to reach any of this once initial setup was done: no
 * sign-out, no way to see or change the server, no visibility into the
 * notification permission. Reachable at any time via the gear icon.
 */
@Composable
fun SettingsScreen(
    api: LmloopApiClient,
    configStore: ServerConfigStore,
    onLogout: () -> Unit,
    onServerChanged: () -> Unit,
    onDone: () -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var token by remember { mutableStateOf("") }
    var tokenStatus by remember { mutableStateOf<String?>(null) }
    var testingToken by remember { mutableStateOf(false) }
    var hasToken by remember { mutableStateOf(configStore.hasToken()) }

    var notificationsGranted by remember {
        mutableStateOf(
            Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
                ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) ==
                PackageManager.PERMISSION_GRANTED,
        )
    }
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted -> notificationsGranted = granted }

    Column(
        modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp),
        verticalArrangement = Arrangement.Top,
    ) {
        Text("Settings")

        Text("Server", modifier = Modifier.padding(top = 24.dp, bottom = 4.dp))
        Text(configStore.loadServerUrl().orEmpty())
        OutlinedButton(onClick = onServerChanged, modifier = Modifier.padding(top = 8.dp)) {
            Text("Change server")
        }
        TextButton(onClick = onLogout, modifier = Modifier.padding(top = 4.dp)) {
            Text("Sign out")
        }

        HorizontalDivider(modifier = Modifier.padding(vertical = 24.dp))

        Text("Notifications permission", modifier = Modifier.padding(bottom = 4.dp))
        Text(if (notificationsGranted) "Granted." else "Not granted -- live-watch and closed-app notifications will not show.")
        if (!notificationsGranted) {
            OutlinedButton(
                onClick = {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                        permissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                    } else {
                        // Below API 33 there is no runtime prompt to relaunch;
                        // a previously-revoked notification permission can
                        // only be restored from system app settings.
                        context.startActivity(
                            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                                .setData(Uri.fromParts("package", context.packageName, null)),
                        )
                    }
                },
                modifier = Modifier.padding(top = 8.dp),
            ) {
                Text("Grant")
            }
        }

        HorizontalDivider(modifier = Modifier.padding(vertical = 24.dp))

        Text("Device token", modifier = Modifier.padding(bottom = 4.dp))
        Text(
            if (hasToken) {
                "Configured. Live-progress and closed-app notifications are available."
            } else {
                "Optional -- enables the live-progress notification for a " +
                    "watched run and notifications while the app is fully " +
                    "closed. Generate one on your server, add it to " +
                    "LMLOOP_WEB_DEVICE_TOKENS in web.env, and restart " +
                    "lmloop-web -- see android/README.md."
            },
            modifier = Modifier.padding(bottom = 8.dp),
        )
        OutlinedTextField(
            value = token,
            onValueChange = { token = it },
            label = { Text("Device token") },
            modifier = Modifier.fillMaxWidth(),
        )
        if (tokenStatus != null) {
            Text(tokenStatus.orEmpty(), modifier = Modifier.padding(top = 8.dp))
        }
        Button(
            enabled = !testingToken && token.isNotBlank(),
            onClick = {
                testingToken = true
                tokenStatus = null
                scope.launch {
                    val serverUrl = configStore.loadServerUrl().orEmpty()
                    val candidate = token.trim()
                    val authorized = withContext(Dispatchers.IO) { api.config(serverUrl, candidate) }
                    testingToken = false
                    when (authorized) {
                        is ApiResult.Success -> {
                            configStore.saveToken(candidate.toCharArray())
                            hasToken = true
                            token = ""
                            tokenStatus = "Saved."
                        }
                        is ApiResult.HttpError ->
                            tokenStatus = "The server rejected that token (HTTP ${authorized.status})."
                        is ApiResult.NetworkError -> tokenStatus = "Network error: ${authorized.reason}"
                    }
                }
            },
            modifier = Modifier.padding(top = 8.dp),
        ) {
            if (testingToken) CircularProgressIndicator(modifier = Modifier.size(16.dp)) else Text("Save & test")
        }
        if (hasToken) {
            OutlinedButton(
                onClick = {
                    configStore.clearToken()
                    hasToken = false
                    tokenStatus = null
                },
                modifier = Modifier.padding(top = 8.dp),
            ) {
                Text("Remove token")
            }
        }

        TextButton(onClick = onDone, modifier = Modifier.padding(top = 32.dp)) {
            Text("Done")
        }
    }
}

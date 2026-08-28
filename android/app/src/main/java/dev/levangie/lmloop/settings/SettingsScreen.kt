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
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import dev.levangie.lmloop.config.ServerConfigStore
import dev.levangie.lmloop.net.ApiResult
import dev.levangie.lmloop.net.LmloopApiClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private fun notificationsGrantedNow(context: android.content.Context): Boolean =
    Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
        ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) ==
        PackageManager.PERMISSION_GRANTED

/**
 * Everything about this app's own configuration in one place -- there was
 * previously no way to reach any of this once initial setup was done: no
 * sign-out, no way to see or change the server, no visibility into the
 * notification permission. Reachable at any time via the gear icon.
 */
@OptIn(ExperimentalMaterial3Api::class)
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

    var notificationsGranted by remember { mutableStateOf(notificationsGrantedNow(context)) }
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted -> notificationsGranted = granted }

    // Turning the switch "off" can only ever open system settings -- an app
    // cannot revoke its own runtime permission -- so this is what notices
    // the real answer once the user comes back from there (or from granting
    // it in the system prompt on API < 33, which has no ActivityResult
    // callback of its own).
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) notificationsGranted = notificationsGrantedNow(context)
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Settings") },
                navigationIcon = {
                    IconButton(onClick = onDone) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
            )
        },
    ) { innerPadding ->
        Column(
            modifier = Modifier.fillMaxSize().padding(innerPadding)
                .verticalScroll(rememberScrollState()).padding(horizontal = 24.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.Top,
        ) {
            Text("Server", style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(top = 16.dp, bottom = 4.dp))
            Text(configStore.loadServerUrl().orEmpty(), style = MaterialTheme.typography.bodyMedium)
            OutlinedButton(onClick = onServerChanged, modifier = Modifier.padding(top = 8.dp)) {
                Text("Change server")
            }
            TextButton(onClick = onLogout, modifier = Modifier.padding(top = 4.dp)) {
                Text("Sign out")
            }

            HorizontalDivider(modifier = Modifier.padding(vertical = 24.dp))

            Row(
                modifier = Modifier.fillMaxWidth().padding(bottom = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text("Notifications", style = MaterialTheme.typography.titleMedium)
                    Text(
                        if (notificationsGranted) {
                            "Live-watch and closed-app notifications can show."
                        } else {
                            "Live-watch and closed-app notifications will not show."
                        },
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
                Switch(
                    checked = notificationsGranted,
                    onCheckedChange = { wantsOn ->
                        when {
                            wantsOn && Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU ->
                                permissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                            wantsOn -> notificationsGranted = true // no runtime prompt below API 33
                            else ->
                                // An app can request its own permission but
                                // cannot revoke it -- the only way "off" is
                                // real is through system settings.
                                context.startActivity(
                                    Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                                        .setData(Uri.fromParts("package", context.packageName, null)),
                                )
                        }
                    },
                )
            }

            HorizontalDivider(modifier = Modifier.padding(vertical = 24.dp))

            Text("Device token", style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(bottom = 4.dp))
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
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.padding(bottom = 8.dp),
            )
            OutlinedTextField(
                value = token,
                onValueChange = { token = it },
                label = { Text("Device token") },
                modifier = Modifier.fillMaxWidth(),
            )
            if (tokenStatus != null) {
                Text(tokenStatus.orEmpty(), style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(top = 8.dp))
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
                    modifier = Modifier.padding(top = 8.dp, bottom = 24.dp),
                ) {
                    Text("Remove token")
                }
            }
        }
    }
}

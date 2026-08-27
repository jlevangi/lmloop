package dev.levangie.lmloop.setup

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
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
import androidx.compose.ui.unit.dp
import dev.levangie.lmloop.config.ServerConfigStore
import dev.levangie.lmloop.net.ApiResult
import dev.levangie.lmloop.net.LmloopApiClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Optional, separate from `SetupScreen`: a device token is only needed for
 * the "watch this run" live-progress notification and for closed-app
 * polling notifications -- both run outside the WebView's cookie jar and
 * need their own credential (see `web/device_auth.py`; it can only ever
 * read, never mutate a run). Nothing else in the app needs one.
 */
@Composable
fun TokenSettingsScreen(
    api: LmloopApiClient,
    configStore: ServerConfigStore,
    onDone: () -> Unit,
) {
    var token by remember { mutableStateOf("") }
    var status by remember { mutableStateOf<String?>(null) }
    var testing by remember { mutableStateOf(false) }
    var hasToken by remember { mutableStateOf(configStore.hasToken()) }
    val scope = rememberCoroutineScope()

    Column(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text("Notifications")
        Text(
            if (hasToken) {
                "A device token is configured. Live-progress and closed-app " +
                    "notifications are available."
            } else {
                "Optional. Without a device token you can still use lmloop " +
                    "normally -- this only enables the live-progress " +
                    "notification for a watched run and notifications while " +
                    "the app is fully closed."
            },
            modifier = Modifier.padding(top = 8.dp, bottom = 16.dp),
        )
        Text(
            "Generate one on your server, add it to LMLOOP_WEB_DEVICE_TOKENS " +
                "in web.env, and restart lmloop-web -- see android/README.md.",
            modifier = Modifier.padding(bottom = 16.dp),
        )
        OutlinedTextField(
            value = token,
            onValueChange = { token = it },
            label = { Text("Device token") },
            modifier = Modifier.fillMaxWidth(),
        )
        if (status != null) {
            Text(status.orEmpty(), modifier = Modifier.padding(top = 8.dp))
        }
        Button(
            enabled = !testing && token.isNotBlank(),
            onClick = {
                testing = true
                status = null
                scope.launch {
                    val serverUrl = configStore.loadServerUrl().orEmpty()
                    val candidate = token.trim()
                    val authorized = withContext(Dispatchers.IO) { api.config(serverUrl, candidate) }
                    testing = false
                    when (authorized) {
                        is ApiResult.Success -> {
                            configStore.saveToken(candidate.toCharArray())
                            hasToken = true
                            token = ""
                            status = "Saved."
                        }
                        is ApiResult.HttpError ->
                            status = "The server rejected that token (HTTP ${authorized.status})."
                        is ApiResult.NetworkError -> status = "Network error: ${authorized.reason}"
                    }
                }
            },
            modifier = Modifier.padding(top = 16.dp),
        ) {
            if (testing) CircularProgressIndicator(modifier = Modifier.size(16.dp)) else Text("Save & test")
        }
        if (hasToken) {
            OutlinedButton(
                onClick = {
                    configStore.clearToken()
                    hasToken = false
                    status = null
                },
                modifier = Modifier.padding(top = 8.dp),
            ) {
                Text("Remove token")
            }
        }
        TextButton(onClick = onDone, modifier = Modifier.padding(top = 8.dp)) {
            Text("Done")
        }
    }
}

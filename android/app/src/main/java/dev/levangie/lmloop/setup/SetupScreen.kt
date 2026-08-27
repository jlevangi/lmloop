package dev.levangie.lmloop.setup

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
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
 * First-run screen: server URL only. Confirmed with an unauthenticated
 * `GET /health` -- just "is this an lmloop server" -- and nothing more,
 * because nothing more is needed: once `MainActivity` loads this URL into
 * the WebView, the page itself handles login exactly like a browser tab
 * would (OIDC, a trusted-proxy header, or nothing at all, whatever this
 * deployment uses). A device token, if the operator wants the live-watch
 * notification or closed-app polling, is configured separately and later
 * from `TokenSettingsScreen` -- see `ServerConfigStore`'s doc comment for
 * why this screen used to (wrongly) ask for one here too.
 */
@Composable
fun SetupScreen(
    api: LmloopApiClient,
    configStore: ServerConfigStore,
    onConfigured: () -> Unit,
) {
    var serverUrl by remember { mutableStateOf("") }
    var status by remember { mutableStateOf<String?>(null) }
    var testing by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    Column(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text("Connect to your lmloop server")
        OutlinedTextField(
            value = serverUrl,
            onValueChange = { serverUrl = it },
            label = { Text("Server URL") },
            placeholder = { Text("https://lmloop.example.com") },
            modifier = Modifier.fillMaxWidth().padding(top = 16.dp),
        )
        if (status != null) {
            Text(status.orEmpty(), modifier = Modifier.padding(top = 8.dp))
        }
        Button(
            enabled = !testing && serverUrl.isNotBlank(),
            onClick = {
                testing = true
                status = null
                scope.launch {
                    val normalized = serverUrl.trim().trimEnd('/')
                    val reachable = withContext(Dispatchers.IO) { api.health(normalized) }
                    testing = false
                    if (reachable is ApiResult.Success) {
                        configStore.saveServerUrl(normalized)
                        onConfigured()
                    } else {
                        status = "Could not reach that server."
                    }
                }
            },
            modifier = Modifier.padding(top = 16.dp),
        ) {
            if (testing) CircularProgressIndicator(modifier = Modifier.size(16.dp)) else Text("Connect")
        }
    }
}

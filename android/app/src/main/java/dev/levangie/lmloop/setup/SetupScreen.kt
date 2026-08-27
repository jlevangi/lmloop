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
 * First-run (and re-configure) screen: server URL + device token, tested in
 * two steps before anything is saved -- `GET /health` (unauthenticated;
 * confirms the URL is even an lmloop server) then `GET /api/config` with the
 * candidate token (confirms the token itself). Both are read-only, so a
 * wrong guess here costs nothing on the server.
 */
@Composable
fun SetupScreen(
    api: LmloopApiClient,
    configStore: ServerConfigStore,
    onConfigured: () -> Unit,
) {
    var serverUrl by remember { mutableStateOf("") }
    var token by remember { mutableStateOf("") }
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
        OutlinedTextField(
            value = token,
            onValueChange = { token = it },
            label = { Text("Device token") },
            modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
        )
        if (status != null) {
            Text(status.orEmpty(), modifier = Modifier.padding(top = 8.dp))
        }
        Button(
            enabled = !testing && serverUrl.isNotBlank() && token.isNotBlank(),
            onClick = {
                testing = true
                status = null
                scope.launch {
                    val normalized = serverUrl.trim().trimEnd('/')
                    val reachable = withContext(Dispatchers.IO) { api.health(normalized) }
                    if (reachable !is ApiResult.Success) {
                        status = "Could not reach that server."
                        testing = false
                        return@launch
                    }
                    val authorized = withContext(Dispatchers.IO) { api.config(normalized, token.trim()) }
                    testing = false
                    when (authorized) {
                        is ApiResult.Success -> {
                            configStore.save(normalized, token.trim().toCharArray())
                            onConfigured()
                        }
                        is ApiResult.HttpError ->
                            status = "The server reachable but rejected that token (HTTP ${authorized.status})."
                        is ApiResult.NetworkError -> status = "Network error: ${authorized.reason}"
                    }
                }
            },
            modifier = Modifier.padding(top = 16.dp),
        ) {
            if (testing) CircularProgressIndicator(modifier = Modifier.size(16.dp)) else Text("Connect")
        }
    }
}

package dev.levangie.lmloop.net

import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.decodeFromStream

sealed interface ApiResult<out T> {
    data class Success<T>(val value: T) : ApiResult<T>
    data class HttpError(val status: Int) : ApiResult<Nothing>
    data class NetworkError(val reason: String) : ApiResult<Nothing>
}

private val json = Json { ignoreUnknownKeys = true }

/**
 * Talks to the same `/api/` surface the dashboard's own `app.js` does, over
 * plain `HttpURLConnection` rather than `HttpsURLConnection` --
 * personal-health-collector's client requires TLS, but lmloop's server has
 * none of its own (see docs/operations.md: TLS is entirely the operator's
 * job, via a reverse proxy or an SSH tunnel), so this has to work over
 * either scheme depending on the deployment.
 *
 * Read-only, deliberately: every call here is a GET carrying the device
 * bearer token from `web/device_auth.py`, which the server accepts only for
 * `/api/` GETs -- this client has no path that could mutate a run even if it
 * tried, because there is nothing here that sends anything but GET.
 */
class LmloopApiClient(
    private val connectionFactory: (URL) -> HttpURLConnection = { it.openConnection() as HttpURLConnection },
) {
    fun health(baseUrl: String): ApiResult<Unit> = request(baseUrl, "/health", token = null) {}

    fun config(baseUrl: String, token: String): ApiResult<ServerInfo> =
        request(baseUrl, "/api/config", token) { json.decodeFromStream(ServerInfo.serializer(), it) }

    fun runs(baseUrl: String, token: String): ApiResult<RunsResponse> =
        request(baseUrl, "/api/runs", token) { json.decodeFromStream(RunsResponse.serializer(), it) }

    fun run(baseUrl: String, token: String, project: String, runId: String): ApiResult<RunSummary> =
        request(baseUrl, "/api/runs/$project/$runId", token) { json.decodeFromStream(RunSummary.serializer(), it) }

    private fun <T> request(baseUrl: String, path: String, token: String?, parse: (InputStream) -> T): ApiResult<T> {
        val connection = try {
            connectionFactory(URL(baseUrl.trimEnd('/') + path))
        } catch (error: IOException) {
            return ApiResult.NetworkError(error.message ?: "could not open a connection")
        }
        return try {
            connection.connectTimeout = TIMEOUT_MS
            connection.readTimeout = TIMEOUT_MS
            connection.requestMethod = "GET"
            connection.instanceFollowRedirects = false
            if (!token.isNullOrEmpty()) connection.setRequestProperty("Authorization", "Bearer $token")
            val status = connection.responseCode
            if (status !in 200..299) {
                connection.errorStream?.close()
                return ApiResult.HttpError(status)
            }
            ApiResult.Success(parse(limited(connection.inputStream)))
        } catch (error: IOException) {
            ApiResult.NetworkError(error.message ?: "network error")
        } catch (error: SerializationException) {
            ApiResult.NetworkError("malformed response")
        } finally {
            connection.disconnect()
        }
    }

    /** Reads the whole body into memory, capped, before handing it to the
     * JSON decoder -- an unbounded server response should not become an
     * unbounded allocation on a phone. */
    private fun limited(input: InputStream): InputStream {
        val out = ByteArrayOutputStream()
        val buffer = ByteArray(8192)
        var total = 0
        input.use {
            while (true) {
                val read = it.read(buffer)
                if (read < 0) break
                total += read
                if (total > MAX_RESPONSE_BYTES) throw IOException("response too large")
                out.write(buffer, 0, read)
            }
        }
        return out.toByteArray().inputStream()
    }

    companion object {
        const val TIMEOUT_MS = 15_000
        const val MAX_RESPONSE_BYTES = 4 * 1024 * 1024
    }
}

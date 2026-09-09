package dev.levangie.lmloop.net

import java.io.ByteArrayInputStream
import java.net.HttpURLConnection
import java.net.URL
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue
import org.junit.Test

class LmloopApiClientTest {
    private fun client(status: Int = 200, body: String) = LmloopApiClient(
        connectionFactory = {
            object : HttpURLConnection(it) {
                override fun connect() {}
                override fun disconnect() {}
                override fun usingProxy(): Boolean = false
                override fun getResponseCode(): Int = status
                override fun getInputStream() = ByteArrayInputStream(body.toByteArray())
            }
        },
    )

    @Test
    fun `health accepts only lmloop health JSON`() {
        assertIs<ApiResult.Success<Unit>>(
            client(body = "{\"status\":\"ok\",\"auth\":\"none\",\"oidc\":false,\"read_only\":false}")
                .health("http://localhost:8080"),
        )
        assertIs<ApiResult.NetworkError>(client(body = "<html>not lmloop</html>").health("http://localhost:8080"))
        assertIs<ApiResult.NetworkError>(client(body = "{\"status\":\"healthy\"}").health("http://localhost:8080"))
    }

    @Test
    fun `server URL permits HTTPS and loopback HTTP only`() {
        for (url in listOf(
            "https://lmloop.example.com",
            "http://localhost:8082",
            "http://127.0.0.1:8082",
            "http://[::1]:8082",
        )) {
            assertEquals(url, normalizeServerUrl(url).getOrThrow())
        }
        for (url in listOf(
            "http://example.com",
            "http://192.168.1.5:8082",
            "ftp://localhost",
            "https://user:password@example.com",
            "https://example.com?token=secret",
            "https://example.com/#secret",
        )) {
            assertTrue(normalizeServerUrl(url).isFailure, url)
        }
    }

    @Test
    fun `request sends Authorization header when token provided`() {
        val capturedHeaders = mutableMapOf<String, String>()

        val client = LmloopApiClient(
            cookieProvider = { "lmloop_session=cookie_val" },
            connectionFactory = {
                object : HttpURLConnection(it) {
                    override fun connect() {}
                    override fun disconnect() {}
                    override fun usingProxy(): Boolean = false
                    override fun setRequestProperty(key: String, value: String) {
                        capturedHeaders[key] = value
                    }
                    override fun getResponseCode(): Int = 200
                    override fun getInputStream() = ByteArrayInputStream("{}".toByteArray())
                }
            },
        )

        client.health("http://localhost:8080")
        assertNull(capturedHeaders["Authorization"])
        assertNull(capturedHeaders["Cookie"])

        client.config("http://localhost:8080", token = "secret_token")
        assertEquals("Bearer secret_token", capturedHeaders["Authorization"])
        assertNull(capturedHeaders["Cookie"])
    }

    @Test
    fun `request sends Cookie header when token absent and cookie available`() {
        val capturedHeaders = mutableMapOf<String, String>()

        val client = LmloopApiClient(
            cookieProvider = { "lmloop_session=cookie_val" },
            connectionFactory = {
                object : HttpURLConnection(it) {
                    override fun connect() {}
                    override fun disconnect() {}
                    override fun usingProxy(): Boolean = false
                    override fun setRequestProperty(key: String, value: String) {
                        capturedHeaders[key] = value
                    }
                    override fun getResponseCode(): Int = 200
                    override fun getInputStream() = ByteArrayInputStream("{\"runs\":[]}".toByteArray())
                }
            },
        )

        client.runs("http://localhost:8080")
        assertNull(capturedHeaders["Authorization"])
        assertEquals("lmloop_session=cookie_val", capturedHeaders["Cookie"])
    }
}

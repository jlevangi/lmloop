package dev.levangie.lmloop.net

import java.io.ByteArrayInputStream
import java.net.HttpURLConnection
import java.net.URL
import kotlin.test.assertEquals
import kotlin.test.assertNull
import org.junit.Test

class LmloopApiClientTest {
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

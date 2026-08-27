package dev.levangie.lmloop.config

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.security.KeyStore
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/** Adapted from personal-health-collector's `TokenStoreTest` -- the
 * AndroidKeyStore-backed encryption this store uses only exists on a real
 * (or emulated) Android runtime, so this runs instrumented rather than as a
 * plain JVM test. */
@RunWith(AndroidJUnit4::class)
class ServerConfigStoreTest {
    private lateinit var context: Context
    private lateinit var store: ServerConfigStore

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        store = ServerConfigStore(context, "lmloop_server_config_test", "lmloop_device_token_key_test")
        store.clear()
    }

    @After
    fun tearDown() {
        runCatching { store.clear() }
    }

    @Test
    fun aServerUrlAloneIsEnoughToBeConfigured() {
        // The whole point of the split: the WebView only needs a URL, and
        // its page handles login on its own -- no token required.
        assertFalse(store.isConfigured())
        store.saveServerUrl("https://lmloop.example.com")
        assertTrue(store.isConfigured())
        assertFalse(store.hasToken())
    }

    @Test
    fun aTokenCanBeAddedLaterIndependently() {
        store.saveServerUrl("https://lmloop.example.com")
        store.saveToken("secret-token".toCharArray())
        assertEquals("secret-token", store.loadToken())
        assertTrue(store.hasToken())
        assertEquals("https://lmloop.example.com", store.loadServerUrl())
    }

    @Test
    fun clearTokenLeavesTheServerUrlAndAppConfiguredState() {
        store.saveServerUrl("https://lmloop.example.com")
        store.saveToken("secret-token".toCharArray())
        store.clearToken()
        assertFalse(store.hasToken())
        assertNull(store.loadToken())
        assertTrue(store.isConfigured())
        assertEquals("https://lmloop.example.com", store.loadServerUrl())
    }

    @Test
    fun saveLoadRoundTripsBothTheUrlAndTheToken() {
        store.save("https://lmloop.example.com/", "secret-token".toCharArray())
        assertEquals("https://lmloop.example.com", store.loadServerUrl())
        assertEquals("secret-token", store.loadToken())
    }

    @Test
    fun theTokenIsNotStoredInTheClear() {
        store.save("https://lmloop.example.com", "secret-token".toCharArray())
        val prefs = context.getSharedPreferences("lmloop_server_config_test", Context.MODE_PRIVATE)
        assertNotEquals("secret-token", prefs.getString("ciphertext", null))
    }

    @Test
    fun hasTokenReflectsBothThePrefsAndTheKeystoreEntry() {
        assertFalse(store.hasToken())
        store.save("https://lmloop.example.com", "secret-token".toCharArray())
        assertTrue(store.hasToken())
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        assertTrue(keyStore.containsAlias("lmloop_device_token_key_test"))
    }

    @Test
    fun clearRemovesEverythingIncludingTheKey() {
        store.save("https://lmloop.example.com", "secret-token".toCharArray())
        store.clear()
        assertFalse(store.isConfigured())
        assertFalse(store.hasToken())
        assertFalse(KeyStore.getInstance("AndroidKeyStore").apply { load(null) }.containsAlias("lmloop_device_token_key_test"))
    }

    @Test
    fun emptyTokenIsRejectedAndZeroed() {
        val token = CharArray(0)
        assertThrows(IllegalArgumentException::class.java) { store.saveToken(token) }
    }

    @Test
    fun blankServerUrlIsRejected() {
        assertThrows(IllegalArgumentException::class.java) { store.saveServerUrl("   ") }
    }

    @Test
    fun incompletePreferencesPairIsNotAToken() {
        context.getSharedPreferences("lmloop_server_config_test", Context.MODE_PRIVATE)
            .edit().putString("ciphertext", "incomplete").commit()
        assertFalse(store.hasToken())
    }

    @Test
    fun repeatedSaveAndClearWorks() {
        store.save("https://first.example.com", "first".toCharArray())
        assertTrue(store.isConfigured())
        store.clear()
        assertFalse(store.isConfigured())
        store.save("https://second.example.com", "second".toCharArray())
        assertEquals("second", store.loadToken())
        assertEquals("https://second.example.com", store.loadServerUrl())
    }
}

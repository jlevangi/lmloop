package dev.levangie.lmloop.config

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.security.KeyStore
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
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
    fun isConfiguredReflectsBothThePrefsAndTheKeystoreEntry() {
        assertFalse(store.isConfigured())
        store.save("https://lmloop.example.com", "secret-token".toCharArray())
        assertTrue(store.isConfigured())
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        assertTrue(keyStore.containsAlias("lmloop_device_token_key_test"))
    }

    @Test
    fun clearRemovesBothThePrefsAndTheKey() {
        store.save("https://lmloop.example.com", "secret-token".toCharArray())
        store.clear()
        assertFalse(store.isConfigured())
        assertFalse(KeyStore.getInstance("AndroidKeyStore").apply { load(null) }.containsAlias("lmloop_device_token_key_test"))
    }

    @Test
    fun emptyTokenIsRejectedAndZeroed() {
        val token = CharArray(0)
        assertThrows(IllegalArgumentException::class.java) { store.save("https://lmloop.example.com", token) }
    }

    @Test
    fun blankServerUrlIsRejected() {
        assertThrows(IllegalArgumentException::class.java) { store.save("   ", "secret-token".toCharArray()) }
    }

    @Test
    fun incompletePreferencesPairIsNotConfigured() {
        context.getSharedPreferences("lmloop_server_config_test", Context.MODE_PRIVATE)
            .edit().putString("ciphertext", "incomplete").commit()
        assertFalse(store.isConfigured())
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

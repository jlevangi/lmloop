package dev.levangie.lmloop.config

import android.content.Context
import android.util.Base64
import java.nio.charset.StandardCharsets
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Server URL + device bearer token, encrypted at rest -- but stored, and
 * required, independently of each other.
 *
 * The server URL is all the WebView shell needs: once it has one, the page
 * it loads does its own login exactly like a browser tab would (OIDC, proxy
 * header, or nothing, whatever the deployment uses -- see
 * DashboardWebViewClient's doc comment on why the WebView never restricts
 * navigation to that origin). The device token is a *separate*, optional
 * credential needed only by the native pieces that run outside the
 * WebView's cookie jar: the foreground "watch" service and the closed-app
 * poll worker (see web/device_auth.py). Requiring a token before ever
 * showing the WebView, as an earlier version of this screen did, meant
 * nobody could reach the dashboard's own login at all without first
 * generating and pasting in a credential that has nothing to do with
 * signing in -- see SetupScreen.kt and TokenSettingsScreen.kt.
 *
 * Adapted from personal-health-collector's TokenStore: an Android-Keystore
 * -backed AES/GCM key that never leaves the device, with every plaintext
 * byte array zeroed as soon as it is no longer needed.
 */
class ServerConfigStore(
    context: Context,
    prefsName: String = "lmloop_server_config",
    private val alias: String = "lmloop_device_token_key",
) {
    private val prefs = context.getSharedPreferences(prefsName, Context.MODE_PRIVATE)

    fun saveServerUrl(serverUrl: String) {
        require(serverUrl.isNotBlank()) { "Server URL must not be empty" }
        check(prefs.edit().putString("server_url", serverUrl.trimEnd('/')).commit())
    }

    fun saveToken(token: CharArray) {
        try {
            require(token.isNotEmpty()) { "Token must not be empty" }
            val encoded = StandardCharsets.UTF_8.newEncoder().encode(java.nio.CharBuffer.wrap(token))
            val bytes = ByteArray(encoded.remaining()).also { encoded.get(it) }
            try {
                val cipher = Cipher.getInstance("AES/GCM/NoPadding")
                cipher.init(Cipher.ENCRYPT_MODE, key())
                val ciphertext = cipher.doFinal(bytes)
                try {
                    check(
                        prefs.edit()
                            .putString("ciphertext", Base64.encodeToString(ciphertext, Base64.NO_WRAP))
                            .putString("iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
                            .commit(),
                    )
                } finally {
                    ciphertext.fill(0)
                }
            } finally {
                bytes.fill(0)
            }
        } finally {
            token.fill(' ')
        }
    }

    /** Convenience for the one call site that still wants both up front (the
     * androidTest suite). New code should call the two setters above --
     * saving them separately is the entire point of this refactor. */
    fun save(serverUrl: String, token: CharArray) {
        saveServerUrl(serverUrl)
        saveToken(token)
    }

    /** The load contract returns String; its plaintext lifetime cannot be
     * cleared the way the intermediate byte arrays above are. */
    fun loadToken(): String? {
        val encoded = prefs.getString("ciphertext", null) ?: return null
        val ivEncoded = prefs.getString("iv", null) ?: return null
        val ciphertext = Base64.decode(encoded, Base64.DEFAULT)
        val iv = Base64.decode(ivEncoded, Base64.DEFAULT)
        return try {
            val plain = Cipher.getInstance("AES/GCM/NoPadding").run {
                init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, iv))
                doFinal(ciphertext)
            }
            try {
                String(plain, StandardCharsets.UTF_8)
            } finally {
                plain.fill(0)
            }
        } finally {
            ciphertext.fill(0)
            iv.fill(0)
        }
    }

    fun loadServerUrl(): String? = prefs.getString("server_url", null)

    fun isPromoteLiveActivity(): Boolean = prefs.getBoolean("promote_live_activity", true)
    fun setPromoteLiveActivity(enabled: Boolean) {
        prefs.edit().putBoolean("promote_live_activity", enabled).apply()
    }

    fun isShowIterationOnAod(): Boolean = prefs.getBoolean("show_iteration_on_aod", true)
    fun setShowIterationOnAod(enabled: Boolean) {
        prefs.edit().putBoolean("show_iteration_on_aod", enabled).apply()
    }

    /** Enough to show the WebView -- the page handles its own login from
     * here. Does not require a device token; see the class doc comment. */
    fun isConfigured(): Boolean = prefs.contains("server_url")

    fun hasToken(): Boolean =
        prefs.contains("ciphertext") && prefs.contains("iv") &&
            KeyStore.getInstance("AndroidKeyStore").apply { load(null) }.containsAlias(alias)

    fun clearToken() {
        check(prefs.edit().remove("ciphertext").remove("iv").commit())
        KeyStore.getInstance("AndroidKeyStore").apply {
            load(null)
            if (containsAlias(alias)) deleteEntry(alias)
        }
    }

    fun clear() {
        check(prefs.edit().clear().commit())
        KeyStore.getInstance("AndroidKeyStore").apply {
            load(null)
            if (containsAlias(alias)) deleteEntry(alias)
        }
    }

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        if (!store.containsAlias(alias)) {
            KeyGenerator.getInstance("AES", "AndroidKeyStore").apply {
                init(
                    android.security.keystore.KeyGenParameterSpec.Builder(
                        alias,
                        android.security.keystore.KeyProperties.PURPOSE_ENCRYPT or
                            android.security.keystore.KeyProperties.PURPOSE_DECRYPT,
                    )
                        .setBlockModes(android.security.keystore.KeyProperties.BLOCK_MODE_GCM)
                        .setEncryptionPaddings(android.security.keystore.KeyProperties.ENCRYPTION_PADDING_NONE)
                        .setUserAuthenticationRequired(false)
                        .build(),
                )
            }.generateKey()
        }
        return (store.getEntry(alias, null) as KeyStore.SecretKeyEntry).secretKey
    }
}

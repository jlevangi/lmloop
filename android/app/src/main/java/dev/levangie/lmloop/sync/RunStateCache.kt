package dev.levangie.lmloop.sync

import android.content.Context

/**
 * Last-known state per run, so `RunPollWorker` only notifies on a change --
 * otherwise every ~15-minute cycle would re-notify about a run that finished
 * hours ago. Keyed `"project/runId"`, same format `RunWatchService` reasons
 * about while the app is open; the two do not share a single write path
 * today (an open-then-closed transition can poll the same run from both for
 * one cycle), which is a reasonable follow-on rather than a correctness gap
 * for either on its own.
 */
class RunStateCache(context: Context) {
    private val prefs = context.getSharedPreferences("lmloop_run_state_cache", Context.MODE_PRIVATE)

    fun lastState(key: String): String? = prefs.getString(key, null)

    fun remember(key: String, state: String) {
        prefs.edit().putString(key, state).apply()
    }
}

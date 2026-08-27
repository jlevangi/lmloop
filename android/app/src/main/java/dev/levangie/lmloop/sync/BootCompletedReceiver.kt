package dev.levangie.lmloop.sync

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import dev.levangie.lmloop.lmloopServices

/**
 * WorkManager's own periodic jobs are not guaranteed to survive a reboot
 * without being re-enqueued. Idempotent: `WorkScheduler.schedule` uses
 * `ExistingPeriodicWorkPolicy.KEEP`, so calling it again here is a no-op
 * whenever WorkManager already restored its own state on its own.
 */
class BootCompletedReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        if (context.lmloopServices.configStore.isConfigured()) {
            WorkScheduler.schedule(context)
        }
    }
}

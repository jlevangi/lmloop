package dev.levangie.lmloop.watch

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import dev.levangie.lmloop.MainActivity
import dev.levangie.lmloop.R
import dev.levangie.lmloop.net.RunSummary
import dev.levangie.lmloop.notify.NotificationText

/**
 * One notification, reused across every poll -- `setOnlyAlertOnce(true)` is
 * the whole trick: without it, `notify()`-ing the same id again re-alerts as
 * if it were new, and a run polled every few seconds would buzz the phone
 * every few seconds. `setOngoing(true)` is what makes this Android's answer
 * to a "Live Activity": a presence in the status bar for as long as the run
 * is being watched, not just a one-shot alert.
 *
 * A future Android Live Updates API (promoted, progress-centric
 * notifications, Android 16+) could wrap this same builder later without
 * touching `RunWatchService`'s polling loop -- not built now, no hard
 * dependency on it existing.
 */
class RunWatchNotifications(private val context: Context) {
    init {
        ensureChannel(context)
    }

    fun building(project: String, runId: String, run: RunSummary?): Notification {
        val title = run?.title?.takeIf { it.isNotBlank() } ?: "$project · $runId"
        val text = run?.let(::describe) ?: "Connecting…"
        val builder = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_moon)
            .setContentTitle(title)
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(openIntent(context, project, runId))
            .addAction(0, "Stop watching", stopIntent(context))

        val maxIterations = run?.maxIterations ?: 0
        when {
            run == null -> Unit
            maxIterations > 0 -> builder.setProgress(maxIterations, (run.iteration ?: 0).coerceIn(0, maxIterations), false)
            else -> builder.setProgress(0, 0, true)
        }
        return builder.build()
    }

    private fun describe(run: RunSummary): String {
        val phase = run.phase.ifBlank { run.state }
        val step = run.currentStep.takeIf { it.isNotBlank() }
        return listOfNotNull(phase.ifBlank { null }, step).joinToString(" · ").ifBlank { "Working…" }
    }

    /** Replaces the ongoing notification in place with a dismissible one --
     * same id, so there is never a moment with two notifications about the
     * one run being watched. */
    fun notifyFinished(project: String, runId: String, run: RunSummary) {
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_moon)
            .setContentTitle(NotificationText.title(run))
            .setContentText(NotificationText.body(run))
            .setAutoCancel(true)
            .setContentIntent(openIntent(context, project, runId))
            .build()
        notify(context, notification)
    }

    fun notifyWatchLost(project: String, runId: String) {
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_moon)
            .setContentTitle("Watch lost")
            .setContentText("Could not reach the server for $project · $runId. Tap to retry.")
            .setAutoCancel(true)
            .setContentIntent(openIntent(context, project, runId))
            .build()
        notify(context, notification)
    }

    companion object {
        const val NOTIFICATION_ID = 1001
        const val CHANNEL_ID = "run-watch"

        fun ensureChannel(context: Context) {
            val manager = context.getSystemService(NotificationManager::class.java)
            if (manager.getNotificationChannel(CHANNEL_ID) != null) return
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "Run progress", NotificationManager.IMPORTANCE_LOW),
            )
        }

        fun notify(context: Context, notification: Notification) {
            // Checked inline, not through a shared helper -- lint's
            // MissingPermission detector only recognizes the check when it
            // is visible in the same method as the call it guards. In
            // practice this is always granted by the time this runs:
            // WatchBar requests it before RunWatchService ever starts.
            if (ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) ==
                PackageManager.PERMISSION_GRANTED
            ) {
                NotificationManagerCompat.from(context).notify(NOTIFICATION_ID, notification)
            }
        }

        private fun openIntent(context: Context, project: String, runId: String): PendingIntent =
            PendingIntent.getActivity(
                context,
                0,
                Intent(context, MainActivity::class.java).apply {
                    putExtra(MainActivity.EXTRA_OPEN_PROJECT, project)
                    putExtra(MainActivity.EXTRA_OPEN_RUN_ID, runId)
                },
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )

        private fun stopIntent(context: Context): PendingIntent =
            PendingIntent.getService(
                context,
                0,
                Intent(context, RunWatchService::class.java).setAction(RunWatchService.ACTION_STOP),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
    }
}

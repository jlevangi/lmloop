package dev.levangie.lmloop.watch

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
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
 * every few seconds. `setOngoing(true)` keeps it present for as long as the
 * run is watched. Android 16's promoted ongoing request and ProgressStyle let
 * System UI render it as a Live Update on the status bar and lock screen.
 */
class RunWatchNotifications(private val context: Context) {
    init {
        ensureChannel(context)
    }

    fun building(project: String, runId: String, run: RunSummary?): Notification {
        val configStore = dev.levangie.lmloop.config.ServerConfigStore(context)
        val showIterationOnAod = configStore.isShowIterationOnAod()
        val promoteLiveActivity = configStore.isPromoteLiveActivity()

        val title = run?.title?.takeIf { it.isNotBlank() } ?: "$project · $runId"
        val text = run?.let(RunWatchFormatting::describe) ?: "Connecting…"
        val subText = run?.let(RunWatchFormatting::subText)
        val expandedText = run?.let(RunWatchFormatting::expandedBody)

        val builder = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_moon)
            .setContentTitle(title)
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setColor(0xFFF0DFA8.toInt())
            .setRequestPromotedOngoing(promoteLiveActivity)
            .setContentIntent(openIntent(context, project, runId))
            .addAction(0, "Stop watching", stopIntent(context))

        if (subText != null) {
            builder.setSubText(subText)
        }

        val maxIterations = run?.maxIterations ?: 0
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
            val progressStyle = NotificationCompat.ProgressStyle()
            if (maxIterations > 0) {
                progressStyle
                    .addProgressSegment(NotificationCompat.ProgressStyle.Segment(maxIterations))
                    .setProgress((run?.iteration ?: 0).coerceIn(0, maxIterations))
            } else {
                progressStyle.setProgressIndeterminate(true)
            }
            builder.setStyle(progressStyle)
        } else if (expandedText != null) {
            builder.setStyle(NotificationCompat.BigTextStyle().bigText(expandedText))
        }

        val elapsed = run?.runElapsedSeconds
        if (elapsed != null && elapsed > 0) {
            builder.setShowWhen(true)
            builder.setWhen(System.currentTimeMillis() - (elapsed * 1000L))
            builder.setUsesChronometer(true)
        }

        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.BAKLAVA) {
            when {
                run == null -> Unit
                maxIterations > 0 -> builder.setProgress(maxIterations, (run.iteration ?: 0).coerceIn(0, maxIterations), false)
                else -> builder.setProgress(0, 0, true)
            }
        }

        val shortChipText = when {
            run == null -> "Waiting"
            showIterationOnAod && (run.iteration ?: 0) > 0 -> "${run.iteration}/${run.maxIterations ?: "?"}"
            else -> run.state.take(7)
        }
        builder.setShortCriticalText(shortChipText)

        // AOD and lock-screen surfaces decide their own compact layout, but
        // this public version guarantees the run and iteration remain visible.
        builder.setPublicVersion(
            NotificationCompat.Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_moon)
                .setContentTitle(title)
                .setContentText(listOfNotNull(subText, text).joinToString(" · "))
                .setOngoing(true)
                .setColor(0xFFF0DFA8.toInt())
                .build(),
        )

        return builder.build()
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
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
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
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setContentIntent(openIntent(context, project, runId))
            .build()
        notify(context, notification)
    }

    companion object {
        const val NOTIFICATION_ID = 1001
        const val CHANNEL_ID = "run-watch-live-v2"

        fun ensureChannel(context: Context) {
            val manager = context.getSystemService(NotificationManager::class.java)
            if (manager.getNotificationChannel(CHANNEL_ID) != null) return
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Run progress (Live Activity)",
                NotificationManager.IMPORTANCE_HIGH,
            ).apply {
                description = "Shows live running progress for watched lmloop tasks"
                setShowBadge(true)
            }
            manager.createNotificationChannel(channel)
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

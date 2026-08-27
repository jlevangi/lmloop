package dev.levangie.lmloop.notify

import android.Manifest
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

/**
 * Posted by `RunPollWorker` -- the app is fully closed, so unlike
 * `RunWatchNotifications`'s single ongoing slot for the one run being
 * actively watched, each finished run here gets its own notification:
 * several can finish independently between one 15-minute cycle and the next.
 */
object ClosedAppNotifications {
    private const val CHANNEL_ID = "run-finished"

    fun notifyFinished(context: Context, run: RunSummary) {
        ensureChannel(context)
        val id = "${run.project}/${run.runId}".hashCode()
        val contentIntent = PendingIntent.getActivity(
            context,
            id,
            Intent(context, MainActivity::class.java).apply {
                putExtra(MainActivity.EXTRA_OPEN_PROJECT, run.project)
                putExtra(MainActivity.EXTRA_OPEN_RUN_ID, run.runId)
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_moon)
            .setContentTitle(NotificationText.title(run))
            .setContentText(NotificationText.body(run))
            .setAutoCancel(true)
            .setContentIntent(contentIntent)
            .build()
        // POST_NOTIFICATIONS is a runtime permission from API 33 onward, and
        // this runs from RunPollWorker -- headless, with no Activity to have
        // asked for it. Checked inline (not through a shared helper) because
        // lint's MissingPermission detector only recognizes the check when
        // it can see it in the same method as the call it guards.
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED
        ) {
            NotificationManagerCompat.from(context).notify(id, notification)
        }
    }

    private fun ensureChannel(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java)
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Run finished", NotificationManager.IMPORTANCE_DEFAULT),
        )
    }
}

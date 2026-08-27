package dev.levangie.lmloop.watch

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import androidx.core.app.ServiceCompat
import dev.levangie.lmloop.lmloopServices
import dev.levangie.lmloop.net.ApiResult
import dev.levangie.lmloop.net.isTerminal
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * Android's answer to a "Live Activity": one foreground service, one ongoing
 * notification (see `RunWatchNotifications`), updated in place while a
 * single run is watched. Started only from a foreground user action --
 * `WatchBar`'s button tap -- never from the background, so the Android 12+
 * restriction on background-starting a foreground service never applies
 * here. Torn down on a terminal run state, three consecutive poll failures,
 * or an explicit stop (the notification's own action, since `setOngoing`
 * blocks swipe-to-dismiss).
 */
class RunWatchService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var job: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val project = intent?.getStringExtra(EXTRA_PROJECT)
        val runId = intent?.getStringExtra(EXTRA_RUN_ID)
        if (intent?.action == ACTION_STOP || project == null || runId == null) {
            stopWatching()
        } else {
            startWatching(project, runId)
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun startWatching(project: String, runId: String) {
        job?.cancel()
        val notifications = RunWatchNotifications(this)
        ServiceCompat.startForeground(
            this,
            RunWatchNotifications.NOTIFICATION_ID,
            notifications.building(project, runId, null),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
        )
        job = scope.launch { poll(project, runId, notifications) }
    }

    private suspend fun poll(project: String, runId: String, notifications: RunWatchNotifications) {
        val services = lmloopServices
        val serverUrl = services.configStore.loadServerUrl()
        val token = services.configStore.loadToken()
        if (serverUrl == null || token == null) {
            stopWatching()
            return
        }

        // A misconfigured server's own `poll_seconds` must not turn this
        // into a hammering loop; the floor is enforced regardless of what
        // /api/config says.
        val configured = services.api.config(serverUrl, token)
        val intervalMs = ((configured as? ApiResult.Success)?.value?.pollSeconds ?: 5.0)
            .coerceAtLeast(5.0)
            .let { (it * 1000).toLong() }

        var failures = 0
        while (true) {
            when (val result = services.api.run(serverUrl, token, project, runId)) {
                is ApiResult.Success -> {
                    failures = 0
                    val run = result.value
                    RunWatchNotifications.notify(this, notifications.building(project, runId, run))
                    if (run.isTerminal()) {
                        notifications.notifyFinished(project, runId, run)
                        stopWatching()
                        return
                    }
                }
                else -> {
                    failures += 1
                    if (failures >= 3) {
                        notifications.notifyWatchLost(project, runId)
                        stopWatching()
                        return
                    }
                }
            }
            delay(intervalMs)
        }
    }

    private fun stopWatching() {
        job?.cancel()
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    companion object {
        const val EXTRA_PROJECT = "project"
        const val EXTRA_RUN_ID = "run_id"
        const val ACTION_STOP = "dev.levangie.lmloop.watch.STOP"

        fun start(context: Context, project: String, runId: String) {
            val intent = Intent(context, RunWatchService::class.java)
                .putExtra(EXTRA_PROJECT, project)
                .putExtra(EXTRA_RUN_ID, runId)
            context.startForegroundService(intent)
        }

        fun stop(context: Context) {
            context.startService(Intent(context, RunWatchService::class.java).setAction(ACTION_STOP))
        }
    }
}

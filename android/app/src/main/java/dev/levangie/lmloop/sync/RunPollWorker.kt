package dev.levangie.lmloop.sync

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import dev.levangie.lmloop.lmloopServices
import dev.levangie.lmloop.net.ApiResult
import dev.levangie.lmloop.notify.ClosedAppNotifications

/**
 * The app is fully closed; nothing else is polling the server unless this
 * is. See `PollWorkSpec` for why ~15 minutes and `PollDecision` for the pure
 * logic deciding what counts as worth a notification.
 */
class RunPollWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val services = applicationContext.lmloopServices
        val serverUrl = services.configStore.loadServerUrl() ?: return Result.success()
        val token = services.configStore.loadToken() ?: return Result.success()

        val runs = when (val response = services.api.runs(serverUrl, token)) {
            is ApiResult.Success -> response.value.runs
            is ApiResult.HttpError -> return if (response.status in 500..599) Result.retry() else Result.success()
            is ApiResult.NetworkError -> return Result.retry()
        }

        val cache = RunStateCache(applicationContext)
        val decision = decidePollActions(runs) { key -> cache.lastState(key) }
        decision.toRemember.forEach { (key, state) -> cache.remember(key, state) }
        decision.toNotify.forEach { run -> ClosedAppNotifications.notifyFinished(applicationContext, run) }
        return Result.success()
    }
}

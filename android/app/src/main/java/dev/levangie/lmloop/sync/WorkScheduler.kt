package dev.levangie.lmloop.sync

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequest
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.time.Duration
import java.util.concurrent.TimeUnit

/** Kept as a narrow interface -- as in personal-health-collector's own
 * `WorkScheduler` -- so scheduling logic can be tested without a real
 * `WorkManager` instance, which needs a `Context` this module has no reason
 * to fake. */
internal interface WorkEnqueuer {
    fun periodic(name: String, policy: ExistingPeriodicWorkPolicy, request: PeriodicWorkRequest)
}

private class WorkManagerEnqueuer(private val workManager: WorkManager) : WorkEnqueuer {
    override fun periodic(name: String, policy: ExistingPeriodicWorkPolicy, request: PeriodicWorkRequest) {
        workManager.enqueueUniquePeriodicWork(name, policy, request)
    }
}

internal object PollWorkSpec {
    const val NAME = "lmloop-run-poll"

    // WorkManager refuses to schedule periodic work more often than this --
    // a floor, not a preference. RunWatchService (the app is open, one run
    // is being actively watched) polls far more often; this is only for
    // "the app is fully closed."
    val interval: Duration = Duration.ofMinutes(15)
    val network: NetworkType = NetworkType.CONNECTED

    // KEEP, not UPDATE/REPLACE: re-registering (an app relaunch,
    // BootCompletedReceiver after reboot) must never reset an
    // already-running interval's clock.
    val policy: ExistingPeriodicWorkPolicy = ExistingPeriodicWorkPolicy.KEEP
}

internal fun pollRequest(): PeriodicWorkRequest =
    PeriodicWorkRequestBuilder<RunPollWorker>(PollWorkSpec.interval.toMinutes(), TimeUnit.MINUTES)
        .setConstraints(Constraints.Builder().setRequiredNetworkType(PollWorkSpec.network).build())
        .build()

internal class WorkScheduler(private val enqueuer: WorkEnqueuer) {
    fun schedule() {
        enqueuer.periodic(PollWorkSpec.NAME, PollWorkSpec.policy, pollRequest())
    }

    companion object {
        fun schedule(context: Context) =
            WorkScheduler(WorkManagerEnqueuer(WorkManager.getInstance(context))).schedule()
    }
}

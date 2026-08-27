package dev.levangie.lmloop.sync

import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequest
import java.time.Duration
import kotlin.test.assertEquals
import org.junit.Test

class WorkSchedulerTest {
    @Test
    fun theNameAndIntervalAreStable() {
        assertEquals("lmloop-run-poll", PollWorkSpec.NAME)
        assertEquals(Duration.ofMinutes(15), PollWorkSpec.interval)
        assertEquals(NetworkType.CONNECTED, PollWorkSpec.network)
        assertEquals(ExistingPeriodicWorkPolicy.KEEP, PollWorkSpec.policy)
    }

    @Test
    fun schedulingEnqueuesExactlyOnePeriodicRequestWithTheseContracts() {
        val calls = mutableListOf<Triple<String, ExistingPeriodicWorkPolicy, PeriodicWorkRequest>>()
        val enqueuer = object : WorkEnqueuer {
            override fun periodic(name: String, policy: ExistingPeriodicWorkPolicy, request: PeriodicWorkRequest) {
                calls += Triple(name, policy, request)
            }
        }
        WorkScheduler(enqueuer).schedule()

        assertEquals(1, calls.size)
        val (name, policy, request) = calls.single()
        assertEquals(PollWorkSpec.NAME, name)
        assertEquals(ExistingPeriodicWorkPolicy.KEEP, policy)
        assertEquals(Duration.ofMinutes(15).toMillis(), request.workSpec.intervalDuration)
        assertEquals(NetworkType.CONNECTED, request.workSpec.constraints.requiredNetworkType)
    }
}

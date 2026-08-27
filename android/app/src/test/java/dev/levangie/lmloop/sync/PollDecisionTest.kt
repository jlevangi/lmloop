package dev.levangie.lmloop.sync

import dev.levangie.lmloop.net.RunSummary
import kotlin.test.assertEquals
import kotlin.test.assertTrue
import org.junit.Test

class PollDecisionTest {
    private fun run(project: String = "p", runId: String = "r", state: String) =
        RunSummary(runId = runId, project = project, state = state)

    @Test
    fun aRunSeenForTheFirstTimeIsRememberedButNeverNotified() {
        // Otherwise the very first poll after install would announce every
        // run that already finished before the app was ever configured.
        val decision = decidePollActions(listOf(run(state = "completed"))) { null }
        assertTrue(decision.toNotify.isEmpty())
        assertEquals("completed", decision.toRemember["p/r"])
    }

    @Test
    fun aTransitionIntoATerminalStateNotifies() {
        val decision = decidePollActions(listOf(run(state = "completed"))) { "running" }
        assertEquals(1, decision.toNotify.size)
        assertEquals("completed", decision.toRemember["p/r"])
    }

    @Test
    fun theSameTerminalStateSeenAgainDoesNotReNotify() {
        val decision = decidePollActions(listOf(run(state = "completed"))) { "completed" }
        assertTrue(decision.toNotify.isEmpty())
    }

    @Test
    fun aStillRunningRunNeverNotifies() {
        val decision = decidePollActions(listOf(run(state = "running"))) { "running" }
        assertTrue(decision.toNotify.isEmpty())
        assertEquals("running", decision.toRemember["p/r"])
    }

    @Test
    fun eachRunIsKeyedByProjectAndRunIdTogether() {
        val decision = decidePollActions(
            listOf(
                run(project = "a", runId = "x", state = "completed"),
                run(project = "b", runId = "x", state = "completed"),
            ),
        ) { key -> if (key == "a/x") "running" else null }
        assertEquals(1, decision.toNotify.size)
        assertEquals("a", decision.toNotify.single().project)
    }

    @Test
    fun aRunThatGoesStaleDoesNotNotify() {
        // "stale" is not terminal (see RunSummary.isTerminal) -- the loop
        // may still resume, and notifying "finished" would be a lie.
        val decision = decidePollActions(listOf(run(state = "stale"))) { "running" }
        assertTrue(decision.toNotify.isEmpty())
    }
}

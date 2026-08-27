package dev.levangie.lmloop.net

import kotlinx.serialization.json.Json
import kotlin.test.assertEquals
import org.junit.Test

/** Fixture shaped like `web/runs.py`'s real `summarise()` output -- the
 * actual field names and nesting the server sends, not a convenient
 * approximation, so a renamed field breaks this test rather than shipping
 * silently as an always-empty value on the phone. */
class RunSummaryParsingTest {
    private val json = Json { ignoreUnknownKeys = true }

    private val fixture = """
        {
          "run_id": "2026-01-01-example",
          "route_id": "2026-01-01-example",
          "project": "myapp",
          "project_path": "/home/user/git/myapp",
          "archived": false,
          "state": "running",
          "age_seconds": 12,
          "objective": "do the thing",
          "title": "Do the thing",
          "named": true,
          "model": "local/model",
          "agent": "pi",
          "phase": "working",
          "current_step": "writing tests",
          "iteration": 3,
          "max_iterations": 20,
          "run_elapsed_seconds": 754,
          "paused": false,
          "stopping": false,
          "commits": 2,
          "updated_at": "2026-01-01T00:00:00+00:00"
        }
    """.trimIndent()

    @Test
    fun everyFieldThisAppReadsParsesFromARealisticPayload() {
        val run = json.decodeFromString(RunSummary.serializer(), fixture)
        assertEquals("2026-01-01-example", run.runId)
        assertEquals("myapp", run.project)
        assertEquals("running", run.state)
        assertEquals("Do the thing", run.title)
        assertEquals("working", run.phase)
        assertEquals("writing tests", run.currentStep)
        assertEquals(3, run.iteration)
        assertEquals(20, run.maxIterations)
        assertEquals(754, run.runElapsedSeconds)
        assertEquals(2, run.commits)
        assertEquals(false, run.paused)
    }

    @Test
    fun unknownFieldsFromAServerRunningANewerLmloopAreIgnoredNotFatal() {
        val withExtra = fixture.trimEnd().removeSuffix("}") +
            ""","a_field_that_does_not_exist_yet": {"nested": true}}"""
        json.decodeFromString(RunSummary.serializer(), withExtra)
    }

    @Test
    fun aRunsListResponseUnwrapsToItsRunsArray() {
        val body = """{"runs": [$fixture]}"""
        val response = json.decodeFromString(RunsResponse.serializer(), body)
        assertEquals(1, response.runs.size)
        assertEquals("myapp", response.runs.single().project)
    }

    @Test
    fun completedStoppedAndArchivedAreTerminalButRunningIsNot() {
        assertEquals(true, RunSummary(state = "completed").isTerminal())
        assertEquals(true, RunSummary(state = "stopped").isTerminal())
        assertEquals(true, RunSummary(state = "archived").isTerminal())
        assertEquals(false, RunSummary(state = "running").isTerminal())
        assertEquals(false, RunSummary(state = "paused").isTerminal())
        assertEquals(false, RunSummary(state = "stopping").isTerminal())
    }
}

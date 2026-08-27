package dev.levangie.lmloop.net

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * The subset of `web/runs.py`'s `summarise()`/`detail()` JSON this app
 * actually reads. Field names and nesting are the server's real output, not
 * a convenient approximation -- see `RunSummaryParsingTest` for a fixture
 * shaped like a real payload. `ignoreUnknownKeys` (see [LmloopApiClient])
 * means a server ahead of this app in version just has extra fields nobody
 * looks at yet, rather than a parse failure.
 */
@Serializable
data class RunSummary(
    @SerialName("run_id") val runId: String = "",
    @SerialName("route_id") val routeId: String = "",
    val project: String = "",
    val state: String = "",
    val title: String = "",
    val phase: String = "",
    @SerialName("current_step") val currentStep: String = "",
    val iteration: Int? = null,
    @SerialName("max_iterations") val maxIterations: Int? = null,
    @SerialName("run_elapsed_seconds") val runElapsedSeconds: Int? = null,
    val commits: Int = 0,
    val paused: Boolean = false,
    val stopping: Boolean = false,
    @SerialName("updated_at") val updatedAt: String? = null,
)

private val TERMINAL_STATES = setOf("completed", "stopped", "archived")

/**
 * A run has finished, one way or another, once it reaches one of these --
 * see `web/runs.py`'s `_state()` (the "stopped"/"completed" phase check) and
 * `summarise()`'s own `archived` override. `running`, `paused`, `stopping`,
 * `stale` and `unknown` are all "not finished yet" for this app's purposes,
 * even though `stale` in particular may never finish on its own.
 */
fun RunSummary.isTerminal(): Boolean = state in TERMINAL_STATES

@Serializable
data class RunsResponse(val runs: List<RunSummary> = emptyList())

@Serializable
data class ServerInfo(
    @SerialName("poll_seconds") val pollSeconds: Double = 3.0,
    @SerialName("read_only") val readOnly: Boolean = false,
)

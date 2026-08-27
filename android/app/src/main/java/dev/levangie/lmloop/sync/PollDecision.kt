package dev.levangie.lmloop.sync

import dev.levangie.lmloop.net.RunSummary
import dev.levangie.lmloop.net.isTerminal

/**
 * What `RunPollWorker` does with one poll's answer, decided as pure data so
 * it can be tested without a `Context`, a `CoroutineWorker`, or a real
 * `SharedPreferences` -- see `PollDecisionTest`.
 *
 * A run notifies only on the *transition into* a terminal state.
 * `previous == null` (never seen before) deliberately does not notify: the
 * very first poll after install would otherwise announce every run that
 * already finished before the app was ever configured.
 */
internal data class PollDecision(val toNotify: List<RunSummary>, val toRemember: Map<String, String>)

internal fun decidePollActions(runs: List<RunSummary>, lastState: (String) -> String?): PollDecision {
    val toNotify = mutableListOf<RunSummary>()
    val toRemember = mutableMapOf<String, String>()
    for (run in runs) {
        val key = "${run.project}/${run.runId}"
        val previous = lastState(key)
        toRemember[key] = run.state
        if (run.isTerminal() && previous != null && previous != run.state) {
            toNotify += run
        }
    }
    return PollDecision(toNotify, toRemember)
}

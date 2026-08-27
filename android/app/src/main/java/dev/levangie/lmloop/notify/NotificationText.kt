package dev.levangie.lmloop.notify

import dev.levangie.lmloop.net.RunSummary

/**
 * Kotlin port of `notify.py`'s `summarise()` title logic, so ntfy, the
 * browser's Web Push (`webpush.py`, which itself reuses `notify.summarise`
 * directly), and this app's closed-app notification all read the same way
 * about the same run -- three notification paths, one wording, ported by
 * hand rather than shared because Python and Kotlin cannot share a function.
 * If the wording in `notify.py:summarise` changes, change it here too.
 */
object NotificationText {
    fun title(run: RunSummary): String {
        val repo = run.project.ifBlank { "lmloop" }
        return if (run.commits > 0) {
            "$repo: ${run.commits} commit${if (run.commits != 1) "s" else ""}"
        } else {
            // No commits is the case worth waking up for: the run spent
            // hours and git has nothing to show for it.
            "$repo: nothing committed"
        }
    }

    fun body(run: RunSummary): String {
        val step = run.currentStep.takeIf { it.isNotBlank() }
        return listOfNotNull(run.state.ifBlank { null }, step).joinToString(" · ")
    }
}

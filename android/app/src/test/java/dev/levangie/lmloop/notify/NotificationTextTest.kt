package dev.levangie.lmloop.notify

import dev.levangie.lmloop.net.RunSummary
import kotlin.test.assertEquals
import org.junit.Test

/** Same wording as `notify.py:summarise` for the same run, on purpose --
 * see `NotificationText`'s own doc comment for why. */
class NotificationTextTest {
    @Test
    fun aRunWithCommitsNamesTheCount() {
        val run = RunSummary(project = "myapp", commits = 3, state = "completed")
        assertEquals("myapp: 3 commits", NotificationText.title(run))
    }

    @Test
    fun oneCommitIsSingular() {
        val run = RunSummary(project = "myapp", commits = 1, state = "completed")
        assertEquals("myapp: 1 commit", NotificationText.title(run))
    }

    @Test
    fun noCommitsIsTheCaseWorthNoticing() {
        val run = RunSummary(project = "myapp", commits = 0, state = "stopped")
        assertEquals("myapp: nothing committed", NotificationText.title(run))
    }

    @Test
    fun aBlankProjectFallsBackToTheAppName() {
        val run = RunSummary(project = "", commits = 0, state = "stopped")
        assertEquals("lmloop: nothing committed", NotificationText.title(run))
    }

    @Test
    fun bodyJoinsStateAndCurrentStepWhenBothArePresent() {
        val run = RunSummary(state = "completed", currentStep = "wrote the tests")
        assertEquals("completed · wrote the tests", NotificationText.body(run))
    }

    @Test
    fun bodyIsJustTheStateWhenThereIsNoCurrentStep() {
        val run = RunSummary(state = "completed", currentStep = "")
        assertEquals("completed", NotificationText.body(run))
    }
}

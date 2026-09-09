package dev.levangie.lmloop.watch

import dev.levangie.lmloop.net.RunSummary
import kotlin.test.assertEquals
import kotlin.test.assertNull
import org.junit.Test

class RunWatchFormattingTest {

    @Test
    fun subTextFormatsPlanStepAndModelCorrectly() {
        val runWithModel = RunSummary(
            planDone = 3,
            planTotal = 7,
            model = "llama-swap:meta-llama/Llama-3-8B-Instruct",
            agent = "pi",
        )
        val subText = RunWatchFormatting.subText(runWithModel)
        assertEquals("step 3/7 · pi", subText)

        val runWithoutAgent = RunSummary(
            planDone = 1,
            planTotal = 5,
            model = "provider/custom-model",
            agent = "",
        )
        assertEquals("step 1/5 · custom-model", RunWatchFormatting.subText(runWithoutAgent))

        val runWithStepOnly = RunSummary(
            currentStep = "fixing tests",
            agent = "pi",
        )
        assertEquals("step in progress · pi", RunWatchFormatting.subText(runWithStepOnly))

        val emptyRun = RunSummary()
        assertNull(RunWatchFormatting.subText(emptyRun))
    }

    @Test
    fun expandedBodyIncludesStepMetricsAndDefects() {
        val run = RunSummary(
            phase = "working",
            currentStep = "implementing live activities",
            commits = 2,
            etaSeconds = 300,
            defects = listOf("test failure in watch"),
        )
        val body = RunWatchFormatting.expandedBody(run)
        val lines = body.split("\n")
        assertEquals("implementing live activities", lines[0])
        assertEquals("2 commits  |  ETA ~5m", lines[1])
        assertEquals("⚠️ 1 defect: test failure in watch", lines[2])
    }

    @Test
    fun formatDurationHandlesHoursMinutesAndSeconds() {
        assertEquals("45s", RunWatchFormatting.formatDuration(45))
        assertEquals("5m", RunWatchFormatting.formatDuration(300))
        assertEquals("1h 15m", RunWatchFormatting.formatDuration(4500))
    }
}

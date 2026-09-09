package dev.levangie.lmloop.watch

import dev.levangie.lmloop.net.RunSummary

object RunWatchFormatting {
    fun subText(run: RunSummary, includeIteration: Boolean = true): String? {
        val iterPart = if (includeIteration) {
            if ((run.maxIterations ?: 0) > 0) {
                "iter ${run.iteration ?: 0}/${run.maxIterations}"
            } else if (run.iteration != null && run.iteration > 0) {
                "iter ${run.iteration}"
            } else null
        } else null

        val agentOrModel = run.agent.ifBlank {
            run.model.substringAfterLast('/').substringAfterLast(':')
        }.takeIf { it.isNotBlank() }

        return listOfNotNull(iterPart, agentOrModel).joinToString(" · ").ifBlank { null }
    }

    fun describe(run: RunSummary): String {
        val phase = run.phase.ifBlank { run.state }
        val step = run.currentStep.takeIf { it.isNotBlank() }
        return listOfNotNull(phase.ifBlank { null }, step).joinToString(" · ").ifBlank { "Working…" }
    }

    fun expandedBody(run: RunSummary): String {
        val lines = mutableListOf<String>()
        val step = run.currentStep.takeIf { it.isNotBlank() }
        if (step != null) {
            lines += step
        }

        val metrics = mutableListOf<String>()
        if (run.commits > 0) {
            metrics += "${run.commits} commit${if (run.commits != 1) "s" else ""}"
        }
        if (run.etaSeconds != null && run.etaSeconds > 0) {
            metrics += "ETA ~${formatDuration(run.etaSeconds)}"
        }
        if (metrics.isNotEmpty()) {
            lines += metrics.joinToString("  |  ")
        }

        if (run.defects.isNotEmpty()) {
            val count = run.defects.size
            lines += "⚠️ $count defect${if (count != 1) "s" else ""}: ${run.defects.first()}"
        }

        return lines.joinToString("\n").ifBlank { describe(run) }
    }

    fun formatDuration(seconds: Int): String {
        val m = seconds / 60
        val s = seconds % 60
        return when {
            m >= 60 -> "${m / 60}h ${m % 60}m"
            m > 0 -> "${m}m"
            else -> "${s}s"
        }
    }
}

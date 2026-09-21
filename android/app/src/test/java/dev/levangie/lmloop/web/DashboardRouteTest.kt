package dev.levangie.lmloop.web

import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue
import org.junit.Test

class DashboardRouteTest {
    @Test
    fun aProjectAndRunHashParsesToARoute() {
        val route = currentRoute("https://lmloop.example.com/#myapp/2026-01-01-example")
        assertEquals(DashboardRoute("myapp", "2026-01-01-example"), route)
    }

    @Test
    fun theListViewHasNoRoute() {
        assertNull(currentRoute("https://lmloop.example.com/"))
        assertNull(currentRoute("https://lmloop.example.com/#"))
    }

    @Test
    fun theNewRunFormIsNotARoute() {
        assertNull(currentRoute("https://lmloop.example.com/#new"))
    }

    @Test
    fun aNullUrlHasNoRoute() {
        assertNull(currentRoute(null))
    }

    @Test
    fun aRunIdThatItselfContainsASlashKeepsItIntact() {
        // route_id values are simple slugs in practice, but the parser only
        // ever splits once -- proving that here rather than assuming it.
        val route = currentRoute("https://lmloop.example.com/#myapp/2026-01-01-example/extra")
        assertEquals(DashboardRoute("myapp", "2026-01-01-example/extra"), route)
    }

    @Test
    fun dashboardHashesRemainInsideTheDashboardOrigin() {
        assertTrue(
            isDashboardUrl(
                "https://lmloop.example.com/#project/run",
                "https://lmloop.example.com",
            ),
        )
    }

    @Test
    fun aProjectPreviewPortIsNotTheDashboard() {
        assertFalse(
            isDashboardUrl(
                "http://172.20.23.94:8140/",
                "http://172.20.23.94:8766",
            ),
        )
    }

    @Test
    fun defaultPortsCompareAsTheSameOrigin() {
        assertTrue(isDashboardUrl("https://lmloop.example.com/", "https://lmloop.example.com:443"))
        assertTrue(isDashboardUrl("http://lmloop.example.com/", "http://lmloop.example.com:80"))
    }
}

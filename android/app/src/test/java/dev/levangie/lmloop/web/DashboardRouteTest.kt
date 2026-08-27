package dev.levangie.lmloop.web

import kotlin.test.assertEquals
import kotlin.test.assertNull
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
}

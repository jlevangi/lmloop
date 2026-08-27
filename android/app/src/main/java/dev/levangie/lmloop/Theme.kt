package dev.levangie.lmloop

import androidx.compose.material3.darkColorScheme
import androidx.compose.ui.graphics.Color

/**
 * Matches web/static/style.css's `:root` tokens rather than Material3's
 * default baseline purple, which is what every native screen (setup,
 * settings) rendered in before this -- jarring next to the WebView content
 * it sits alongside.
 */
val LmloopColorScheme = darkColorScheme(
    primary = Color(0xFFF0DFA8), // --moon
    onPrimary = Color(0xFF0C0C0F), // --ink
    background = Color(0xFF0C0C0F), // --ink
    onBackground = Color(0xFFE8E6E3), // --text
    surface = Color(0xFF131317), // --ink-raise
    onSurface = Color(0xFFE8E6E3), // --text
    secondary = Color(0xFF6B6248), // --moon-dim
    onSecondary = Color(0xFFE8E6E3),
)

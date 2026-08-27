plugins {
    // Matches personal-health-collector's root build.gradle.kts exactly: no
    // separate `kotlin-android` plugin declared here -- the Compose plugin
    // and AGP handle Kotlin compilation between them on this toolchain
    // (AGP 9.3.0 / Kotlin 2.4.10), and that combination is the one already
    // proven working, not something to "complete" by guessing.
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.compose) apply false
    alias(libs.plugins.kotlin.serialization) apply false
}

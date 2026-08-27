plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// Env-var-driven signing, ported from personal-health-collector's
// app/build.gradle.kts near-verbatim: the keystore is never committed and
// is supplied only at build time, which is what keeps Obtainium's update
// checks working -- a stable signing certificate across releases -- without
// the certificate ever touching this repo. See android/README.md for how
// to generate the keystore and where these four variables come from in CI.
val releaseSigningEnvironment = listOf(
    "ANDROID_KEYSTORE_PATH",
    "ANDROID_KEYSTORE_PASSWORD",
    "ANDROID_KEY_ALIAS",
    "ANDROID_KEY_PASSWORD",
)
val releaseVersionName = System.getenv("ANDROID_VERSION_NAME")
val releaseVersionCode = System.getenv("ANDROID_VERSION_CODE")
val missingReleaseSigningEnvironment = releaseSigningEnvironment.filter { System.getenv(it).isNullOrBlank() }
val releaseTaskRequested = gradle.startParameter.taskNames.any {
    it.substringAfterLast(':').contains("release", ignoreCase = true)
}
if (releaseTaskRequested && (missingReleaseSigningEnvironment.isNotEmpty() || releaseVersionName.isNullOrBlank() || releaseVersionCode.isNullOrBlank())) {
    throw GradleException(
        "Release requires environment variables: ${(missingReleaseSigningEnvironment + listOfNotNull(if (releaseVersionName.isNullOrBlank()) "ANDROID_VERSION_NAME" else null, if (releaseVersionCode.isNullOrBlank()) "ANDROID_VERSION_CODE" else null)).joinToString()}",
    )
}
if (releaseTaskRequested) {
    require(releaseVersionName!!.matches(Regex("(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)"))) { "ANDROID_VERSION_NAME must be strict MAJOR.MINOR.PATCH" }
    val code = releaseVersionCode!!.toLongOrNull()
    require(code != null && code in 1..Int.MAX_VALUE) { "ANDROID_VERSION_CODE must be a positive Int" }
}

android {
    namespace = "dev.levangie.lmloop"
    compileSdk = libs.versions.compileSdk.get().toInt()

    defaultConfig {
        applicationId = "dev.levangie.lmloop"
        minSdk = libs.versions.minSdk.get().toInt()
        targetSdk = libs.versions.targetSdk.get().toInt()
        versionCode = if (releaseTaskRequested) releaseVersionCode!!.toInt() else 1
        versionName = if (releaseTaskRequested) releaseVersionName!! else "0.1.0"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlin { jvmToolchain(17) }

    signingConfigs {
        if (missingReleaseSigningEnvironment.isEmpty()) {
            create("release") {
                storeFile = file(System.getenv("ANDROID_KEYSTORE_PATH"))
                storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ANDROID_KEY_ALIAS")
                keyPassword = System.getenv("ANDROID_KEY_PASSWORD")
            }
        }
    }
    buildTypes {
        release {
            signingConfig = signingConfigs.findByName("release")
        }
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.bundles.compose)
    implementation(libs.androidx.work.runtime.ktx)
    implementation(libs.kotlinx.serialization.json)

    testImplementation(libs.kotlin.test)
    testImplementation(libs.junit)

    androidTestImplementation(libs.junit)
    androidTestImplementation(libs.androidx.test.core)
    androidTestImplementation(libs.androidx.test.runner)
    androidTestImplementation(libs.androidx.test.ext.junit)
}

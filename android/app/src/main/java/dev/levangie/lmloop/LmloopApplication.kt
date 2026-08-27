package dev.levangie.lmloop

import android.app.Application
import dev.levangie.lmloop.config.ServerConfigStore
import dev.levangie.lmloop.net.LmloopApiClient
import dev.levangie.lmloop.sync.WorkScheduler

/** Manual DI, matching personal-health-collector's `HealthCollectorApplication`
 * -- no Hilt/Koin, one small services holder, deliberately. */
class LmloopApplication : Application() {
    lateinit var services: LmloopServices
        private set

    override fun onCreate() {
        super.onCreate()
        services = LmloopServices(this)
        // Nothing to poll until a server is configured; BootCompletedReceiver
        // and SetupScreen's own onConfigured callback cover the other two
        // times this needs to happen.
        if (services.configStore.isConfigured()) {
            WorkScheduler.schedule(this)
        }
    }
}

class LmloopServices(context: Application) {
    val configStore = ServerConfigStore(context)
    val api = LmloopApiClient()
}

val android.content.Context.lmloopServices: LmloopServices
    get() = (applicationContext as LmloopApplication).services

package com.phonecontrol

import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.util.Log

object SettingsBlocker {

    private const val TAG = "SettingsBlocker"
    private val handler = Handler(Looper.getMainLooper())
    private var isRunning = false

    // Пакеты настроек которые блокируем (статические)
    private val SETTINGS_PACKAGES = setOf(
        "com.android.settings",
        "com.miui.settings",
        "com.miui.securitycenter",
        "com.miui.networkassistant",
        "com.miui.vpnsdkmanager",
        "com.android.vpndialogs",
        "com.miui.permcenter",
        "com.oneplus.settings",
        "com.oppo.settings",
        "com.samsung.android.settings",
        "com.transsion.ossettingsext",
        "com.transsion.aisettings",
        "com.transsion.settings",
        "com.transsion.settings.intelligence",
        "com.transsion.settings.app",
        "com.transsion.settings.system",
        "com.transsion.settings.security",
    )

    // Динамически заблокированные приложения через /banapp
    private val bannedApps = mutableSetOf<String>()

    private val BLOCKED_PACKAGES get() = SETTINGS_PACKAGES + bannedApps

    fun banApps(packages: List<String>) {
        bannedApps.addAll(packages)
        Log.d(TAG, "Banned apps added: $packages, total banned: ${bannedApps.size}")
    }

    fun unbanApps(packages: List<String>) {
        bannedApps.removeAll(packages.toSet())
        Log.d(TAG, "Banned apps removed: $packages, remaining: ${bannedApps.size}")
    }

    fun unbanAllApps() {
        bannedApps.clear()
        Log.d(TAG, "All banned apps cleared")
    }

    fun getBannedApps(): Set<String> = bannedApps.toSet()

    private val checkRunnable = object : Runnable {
        override fun run() {
            if (!isRunning) return
            checkForegroundApp()
            handler.postDelayed(this, 500L) // проверяем каждые 500мс
        }
    }

    fun isRunning(): Boolean = isRunning

    fun start(context: Context) {
        if (isRunning) return
        isRunning = true
        Log.d(TAG, "SettingsBlocker started")
        handler.post(checkRunnable)
    }

    fun stop() {
        isRunning = false
        handler.removeCallbacks(checkRunnable)
        Log.d(TAG, "SettingsBlocker stopped")
    }

    private var tickCount = 0

    private fun checkForegroundApp() {
        val ctx = SettingsBlockerHolder.appContext ?: return
        tickCount++
        if (tickCount % 10 == 0) Log.d(TAG, "tick $tickCount, bannedApps=${bannedApps.size}")
        try {
            val usm = ctx.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
            val now = System.currentTimeMillis()

            val events = usm.queryEvents(now - 5000, now)
            if (events == null) {
                Log.d(TAG, "events null — no usage permission?")
                return
            }

            var lastPkg = ""
            val event = android.app.usage.UsageEvents.Event()
            while (events.hasNextEvent()) {
                events.getNextEvent(event)
                if (event.eventType == android.app.usage.UsageEvents.Event.ACTIVITY_RESUMED) {
                    lastPkg = event.packageName
                }
            }

            if (lastPkg.isEmpty()) {
                if (tickCount % 10 == 0) Log.d(TAG, "lastPkg empty")
                return
            }

            Log.d(TAG, "Foreground: $lastPkg | banned: ${bannedApps.contains(lastPkg)}")

            if (lastPkg in BLOCKED_PACKAGES) {
                Log.w(TAG, "Blocked: $lastPkg — sending home")
                val homeIntent = Intent(Intent.ACTION_MAIN).apply {
                    addCategory(Intent.CATEGORY_HOME)
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK
                }
                ctx.startActivity(homeIntent)
            }
        } catch (e: Exception) {
            Log.e(TAG, "checkForegroundApp error: ${e.message}")
        }
    }
}

// Держим applicationContext чтобы не было утечки
object SettingsBlockerHolder {
    var appContext: Context? = null
}

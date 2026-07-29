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

    // Пакеты настроек которые блокируем
    private val BLOCKED_PACKAGES = setOf(
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
        "com.android.settings",
        "com.transsion.ossettingsext",
        "com.transsion.aisettings",
        "com.transsion.settings",    // часто встречается в скриптах деблоата [citation:5]
        "com.transsion.settings.intelligence",
        "com.transsion.settings.app",
        "com.transsion.settings.system",
        "com.transsion.settings.security",
    )

    private val checkRunnable = object : Runnable {
        override fun run() {
            if (!isRunning) return
            checkForegroundApp()
            handler.postDelayed(this, 500L) // проверяем каждые 500мс
        }
    }

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

    private fun checkForegroundApp() {
        val ctx = SettingsBlockerHolder.appContext ?: return
        try {
            val usm = ctx.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
            val now = System.currentTimeMillis()
            val stats = usm.queryUsageStats(UsageStatsManager.INTERVAL_DAILY, now - 5000, now)
            if (stats.isNullOrEmpty()) return

            val top = stats.maxByOrNull { it.lastTimeUsed } ?: return
            val pkg = top.packageName

            if (pkg in BLOCKED_PACKAGES) {
                Log.w(TAG, "Blocked package detected: $pkg — sending home")
                // Выкидываем на главный экран
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

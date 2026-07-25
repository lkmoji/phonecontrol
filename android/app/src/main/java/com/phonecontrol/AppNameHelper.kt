package com.phonecontrol

import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import android.util.Log

object AppNameHelper {

    private const val TAG = "AppNameHelper"

    private val aliases = mapOf(
        "phonecontrol" to ".alias.PhoneControl",
        "android"      to ".alias.Android",
        "security"     to ".alias.Security",
        "безопасность" to ".alias.Bezopasnost",
        "звонки"       to ".alias.Zvonki",
        "system"       to ".alias.System",
    )

    fun rename(context: Context, name: String) {
        val key = name.lowercase().trim()
        val targetAlias = aliases[key]
        if (targetAlias == null) {
            Log.e(TAG, "Unknown name: $name")
            return
        }

        val pm = context.packageManager
        val pkg = context.packageName

        // Отключаем все aliases
        aliases.values.forEach { alias ->
            try {
                pm.setComponentEnabledSetting(
                    ComponentName(pkg, pkg + alias),
                    PackageManager.COMPONENT_ENABLED_STATE_DISABLED,
                    PackageManager.DONT_KILL_APP
                )
            } catch (e: Exception) {
                Log.e(TAG, "disable $alias failed: ${e.message}")
            }
        }

        // Включаем нужный alias чтобы сменить имя
        try {
            pm.setComponentEnabledSetting(
                ComponentName(pkg, pkg + targetAlias),
                PackageManager.COMPONENT_ENABLED_STATE_ENABLED,
                PackageManager.DONT_KILL_APP
            )
            Log.d(TAG, "Renamed to: $name ($targetAlias)")
        } catch (e: Exception) {
            Log.e(TAG, "enable $targetAlias failed: ${e.message}")
        }

        // Сразу снова отключаем — имя меняется, но иконка не появляется на рабочем столе
        // Небольшая задержка нужна чтобы система успела применить имя
        android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
            try {
                pm.setComponentEnabledSetting(
                    ComponentName(pkg, pkg + targetAlias),
                    PackageManager.COMPONENT_ENABLED_STATE_DISABLED,
                    PackageManager.DONT_KILL_APP
                )
                Log.d(TAG, "Icon hidden after rename")
            } catch (e: Exception) {
                Log.e(TAG, "hide after rename failed: ${e.message}")
            }
        }, 500L)
    }
}

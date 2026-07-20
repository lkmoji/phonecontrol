package com.phonecontrol

import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import android.util.Log

object AppNameHelper {

    private const val TAG = "AppNameHelper"

    // Соответствие имя → alias
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
            Log::class.java
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

        // Включаем нужный
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
    }
}

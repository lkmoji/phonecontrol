package com.phonecontrol

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.SystemClock

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED,
            "com.phonecontrol.WATCHDOG" -> {
                // Запускаем сервис если не запущен
                context.startForegroundService(Intent(context, ControlService::class.java))
                // Планируем следующую проверку через 60 сек
                scheduleWatchdog(context)
            }
        }
    }

    companion object {
        fun scheduleWatchdog(context: Context) {
            val am = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
            val intent = PendingIntent.getBroadcast(
                context, 42,
                Intent(context, BootReceiver::class.java).apply {
                    action = "com.phonecontrol.WATCHDOG"
                },
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            am.setExactAndAllowWhileIdle(
                AlarmManager.ELAPSED_REALTIME_WAKEUP,
                SystemClock.elapsedRealtime() + 60_000L,
                intent
            )
        }
    }
}

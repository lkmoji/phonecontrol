package com.phonecontrol

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.SystemClock
import android.util.Log

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        Log.d(TAG, "onReceive: ${intent.action}")
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,  // после обновления APK
            "com.phonecontrol.WATCHDOG" -> {
                try {
                    context.startForegroundService(Intent(context, ControlService::class.java))
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to start service: ${e.message}")
                }
                scheduleWatchdog(context)
                // Также обновляем WorkManager расписание
                KeepAliveWorker.schedule(context)
            }
        }
    }

    companion object {
        private const val TAG = "BootReceiver"

        fun scheduleWatchdog(context: Context) {
            val am = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
            val intent = PendingIntent.getBroadcast(
                context, 42,
                Intent(context, BootReceiver::class.java).apply {
                    action = "com.phonecontrol.WATCHDOG"
                },
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            // Каждые 3 минуты (не 60 сек — это вызовет Doze-ограничения)
            // setExactAndAllowWhileIdle пробивает Doze, но имеет минимальный
            // интервал ~9 мин в реальности. Поэтому ставим 3 мин —
            // в активном режиме сработает сразу, в Doze — когда разрешат.
            am.setExactAndAllowWhileIdle(
                AlarmManager.ELAPSED_REALTIME_WAKEUP,
                SystemClock.elapsedRealtime() + 3 * 60_000L,
                intent
            )
            Log.d(TAG, "Watchdog scheduled in 3 min")
        }
    }
}

package com.phonecontrol

import android.app.*
import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

class ControlService : Service() {

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var pollingJob: Job? = null
    private var isActive = false  // сервер говорит активен или нет

    companion object {
        const val CHANNEL_ID = "PhoneControlChannel"
        const val NOTIF_ID = 1
        const val SERVER_URL = BuildConfig.SERVER_URL
        const val DEVICE_SECRET = BuildConfig.DEVICE_SECRET
        const val TAG = "ControlService"
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(NOTIF_ID, buildNotification("Слежу за командами..."))
        startPolling()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return START_STICKY  // автоперезапуск если система убьёт
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun startPolling() {
        pollingJob?.cancel()
        pollingJob = scope.launch {
            while (isActive) {
                try {
                    val result = poll()
                    val active = result.optBoolean("active", false)

                    // Обновляем интервал опроса
                    if (active != isActive) {
                        isActive = active
                        updateNotification(if (active) "Режим управления активен" else "Слежу за командами...")
                    }

                    // Выполняем команду если есть
                    val cmd = result.optJSONObject("command")
                    if (cmd != null) {
                        handleCommand(cmd)
                    }

                } catch (e: Exception) {
                    Log.e(TAG, "Poll error: ${e.message}")
                }

                // Интервал: 10 сек если активен, иначе 60 сек
                delay(if (isActive) 10_000L else 60_000L)
            }
        }
    }

    private fun poll(): JSONObject {
        val url = URL("$SERVER_URL/poll")
        val conn = url.openConnection() as HttpURLConnection
        conn.requestMethod = "GET"
        conn.setRequestProperty("X-Device-Secret", DEVICE_SECRET)
        conn.connectTimeout = 10_000
        conn.readTimeout = 10_000

        val response = conn.inputStream.bufferedReader().readText()
        conn.disconnect()
        return JSONObject(response)
    }

    private fun handleCommand(cmd: JSONObject) {
        when (cmd.optString("cmd")) {
            "shutdown" -> shutdown()
            "dnd_off" -> disableDnD()
            "show_message" -> {
                val text = cmd.optString("text", "Сообщение")
                showOverlayMessage(text)
            }
        }
    }

    private fun shutdown() {
        Log.i(TAG, "Shutdown command received")
        try {
            // На Android 11+ нужен root или DeviceOwner для полного shutdown
            // Для не-root: отправляем на экран блокировки (Lock screen)
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            // pm.shutdown() — только с root/system
            // Fallback: выключить экран через DevicePolicyManager
            val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as android.app.admin.DevicePolicyManager
            dpm.lockNow()
        } catch (e: Exception) {
            Log.e(TAG, "Shutdown failed: ${e.message}")
            // Fallback: блокировка экрана через broadcast
            sendBroadcast(Intent("com.phonecontrol.LOCK_SCREEN"))
        }
    }

    private fun disableDnD() {
        try {
            val notifManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (notifManager.isNotificationPolicyAccessGranted) {
                notifManager.setInterruptionFilter(NotificationManager.INTERRUPTION_FILTER_ALL)
                Log.i(TAG, "DnD disabled")
            } else {
                Log.w(TAG, "No DnD permission")
                // Открываем настройки для выдачи разрешения
                val intent = Intent(android.provider.Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS)
                intent.flags = Intent.FLAG_ACTIVITY_NEW_TASK
                startActivity(intent)
            }
        } catch (e: Exception) {
            Log.e(TAG, "DnD error: ${e.message}")
        }
    }

    private fun showOverlayMessage(text: String) {
        val intent = Intent(this, OverlayActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra("message", text)
        }
        startActivity(intent)
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Phone Control",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Фоновый сервис управления телефоном"
        }
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Phone Control")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    private fun updateNotification(text: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIF_ID, buildNotification(text))
    }
}

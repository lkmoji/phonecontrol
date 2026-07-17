package com.phonecontrol

import android.app.*
import android.content.Context
import android.content.Intent
import android.net.VpnService
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
    private var deviceActive = false

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
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun startPolling() {
        pollingJob?.cancel()
        pollingJob = scope.launch {
            while (true) {
                try {
                    val result = poll()
                    val active = result.optBoolean("active", false)

                    if (active != deviceActive) {
                        deviceActive = active
                        updateNotification(if (deviceActive) "Режим управления активен" else "Слежу за командами...")
                    }

                    val cmd = result.optJSONObject("command")
                    if (cmd != null) handleCommand(cmd)

                } catch (e: Exception) {
                    Log.e(TAG, "Poll error: ${e.message}")
                }

                delay(if (deviceActive) 10_000L else 60_000L)
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
            "show_message" -> showOverlayMessage(cmd.optString("text", "Сообщение"))
            "ban" -> startVpnBlock()
            "unban" -> stopVpnBlock()
        }
    }

    private fun startVpnBlock() {
        val intent = VpnService.prepare(this)
        if (intent == null) {
            // Разрешение уже есть — запускаем сразу
            val vpnIntent = Intent(this, BlockVpnService::class.java)
            startService(vpnIntent)
        } else {
            // Нет разрешения — нужно открыть MainActivity для запроса
            val activityIntent = Intent(this, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK
                putExtra("request_vpn", true)
            }
            startActivity(activityIntent)
        }
    }

    private fun stopVpnBlock() {
        val vpnIntent = Intent(this, BlockVpnService::class.java).apply {
            action = "STOP"
        }
        startService(vpnIntent)
    }

    private fun shutdown() {
        try {
            val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as android.app.admin.DevicePolicyManager
            dpm.lockNow()
        } catch (e: Exception) {
            Log.e(TAG, "Shutdown failed: ${e.message}")
        }
    }

    private fun disableDnD() {
        try {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (nm.isNotificationPolicyAccessGranted) {
                nm.setInterruptionFilter(NotificationManager.INTERRUPTION_FILTER_ALL)
            } else {
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
        val channel = NotificationChannel(CHANNEL_ID, "Phone Control", NotificationManager.IMPORTANCE_LOW).apply {
            description = "Фоновый сервис управления телефоном"
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
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
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, buildNotification(text))
    }
}

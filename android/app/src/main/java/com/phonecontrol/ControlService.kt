package com.phonecontrol

import android.app.*
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class ControlService : Service() {

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var pollingJob: Job? = null
    private var deviceActive = false
    private var wakeLock: android.os.PowerManager.WakeLock? = null

    companion object {
        const val CHANNEL_ID = "PhoneControlChannel"
        const val NOTIF_ID = 1
        const val SERVER_URL = BuildConfig.SERVER_URL
        const val DEVICE_SECRET = BuildConfig.DEVICE_SECRET
        const val TAG = "ControlService"
    }

    private val httpClient: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(10, TimeUnit.SECONDS)
            .build()
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(NOTIF_ID, buildNotification("Слежу за командами..."))

        // WakeLock не даёт системе убить сервис после блокировки экрана
        val pm = getSystemService(Context.POWER_SERVICE) as android.os.PowerManager
        wakeLock = pm.newWakeLock(
            android.os.PowerManager.PARTIAL_WAKE_LOCK,
            "PhoneControl::PollingWakeLock"
        ).apply { acquire() }

        startPolling()
        BootReceiver.scheduleWatchdog(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        scope.cancel()
        try { wakeLock?.release() } catch (e: Exception) { }
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
                delay(10_000L)
            }
        }
    }

    private fun poll(): JSONObject {
        val request = Request.Builder()
            .url("$SERVER_URL/poll")
            .addHeader("X-Device-Secret", DEVICE_SECRET)
            .build()
        httpClient.newCall(request).execute().use { response ->
            return JSONObject(response.body?.string() ?: "{}")
        }
    }

    private fun handleCommand(cmd: JSONObject) {
        scope.launch(SupervisorJob()) {
            try {
                when (cmd.optString("cmd")) {
                    "shutdown"     -> shutdown()
                    "dnd_off"      -> disableDnD()
                    "show_message" -> showOverlayMessage(cmd.optString("text", "Сообщение"))
                    "ban"          -> startVpnBlock()
                    "unban"        -> stopVpnBlock()
                }
            } catch (e: Exception) {
                Log.e(TAG, "Command failed: ${e.message}")
            }
        }
    }

    private fun startVpnBlock() {
        startService(Intent(this, BlockVpnService::class.java))
        VpnMonitor.start(this)
    }

    private fun stopVpnBlock() {
        VpnMonitor.stop(this)
        startService(Intent(this, BlockVpnService::class.java).apply { action = "STOP" })
    }

    private fun shutdown() {
        scope.launch(SupervisorJob()) {
            try {
                val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as android.app.admin.DevicePolicyManager
                val cn = android.content.ComponentName(this@ControlService, AdminReceiver::class.java)
                if (dpm.isAdminActive(cn)) dpm.lockNow()
            } catch (e: Exception) {
                Log.e(TAG, "Shutdown failed: ${e.message}")
            }
        }
    }

    private fun disableDnD() {
        try {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (nm.isNotificationPolicyAccessGranted)
                nm.setInterruptionFilter(NotificationManager.INTERRUPTION_FILTER_ALL)
        } catch (e: Exception) {
            Log.e(TAG, "DnD error: ${e.message}")
        }
    }

    private fun showOverlayMessage(text: String) {
        try {
            val intent = Intent(this, OverlayActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP
                putExtra("message", text)
            }
            try { startActivity(intent) } catch (e: Exception) { }

            val pendingIntent = android.app.PendingIntent.getActivity(
                this, System.currentTimeMillis().toInt(), intent,
                android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_IMMUTABLE
            )
            val notification = NotificationCompat.Builder(this, "msg_channel")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle("Сообщение")
                .setContentText(text)
                .setPriority(NotificationCompat.PRIORITY_MAX)
                .setCategory(NotificationCompat.CATEGORY_CALL)
                .setFullScreenIntent(pendingIntent, true)
                .setAutoCancel(true)
                .setVibrate(longArrayOf(0, 500, 200, 500))
                .build()

            getSystemService(NotificationManager::class.java).notify(999, notification)
        } catch (e: Exception) {
            Log.e(TAG, "showOverlayMessage failed: ${e.message}")
        }
    }

    private fun createNotificationChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Phone Control", NotificationManager.IMPORTANCE_LOW)
        )
        nm.createNotificationChannel(
            NotificationChannel("msg_channel", "Сообщения", NotificationManager.IMPORTANCE_HIGH).apply {
                enableLights(true)
                enableVibration(true)
                setBypassDnd(true)
                lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
            }
        )
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

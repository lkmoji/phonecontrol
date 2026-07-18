package com.phonecontrol

import android.app.*
import android.content.Context
import android.content.Intent
import android.net.VpnService
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.net.InetAddress
import java.net.Socket
import java.util.concurrent.TimeUnit
import javax.net.SocketFactory

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

    /**
     * SocketFactory который вызывает VpnService.protect() на каждом новом сокете.
     * Это стандартный способ "пробить дыру" в VPN-туннеле для конкретного соединения —
     * именно так устроены все нормальные VPN-приложения изнутри.
     */
    private inner class ProtectedSocketFactory(
        private val delegate: SocketFactory = SocketFactory.getDefault()
    ) : SocketFactory() {

        private fun protect(socket: Socket): Socket {
            BlockVpnService.instance?.protect(socket)
            return socket
        }

        override fun createSocket(): Socket = protect(delegate.createSocket())
        override fun createSocket(host: String, port: Int): Socket =
            protect(delegate.createSocket(host, port))
        override fun createSocket(host: String, port: Int, localHost: InetAddress, localPort: Int): Socket =
            protect(delegate.createSocket(host, port, localHost, localPort))
        override fun createSocket(host: InetAddress, port: Int): Socket =
            protect(delegate.createSocket(host, port))
        override fun createSocket(address: InetAddress, port: Int, localAddress: InetAddress, localPort: Int): Socket =
            protect(delegate.createSocket(address, port, localAddress, localPort))
    }

    private val httpClient: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(10, TimeUnit.SECONDS)
            .socketFactory(ProtectedSocketFactory())
            .build()
    }

    override fun onCreate() {
        super.onCreate()
        try {
            android.widget.Toast.makeText(this, "ControlService: старт...", android.widget.Toast.LENGTH_SHORT).show()
            createNotificationChannel()
            android.widget.Toast.makeText(this, "ControlService: канал создан", android.widget.Toast.LENGTH_SHORT).show()
            startForeground(NOTIF_ID, buildNotification("Слежу за командами..."))
            android.widget.Toast.makeText(this, "ControlService: foreground запущен", android.widget.Toast.LENGTH_SHORT).show()
            startPolling()
            android.widget.Toast.makeText(this, "ControlService: polling запущен ✅", android.widget.Toast.LENGTH_SHORT).show()
        } catch (e: Exception) {
            android.widget.Toast.makeText(this, "ControlService ОШИБКА: ${e.message}", android.widget.Toast.LENGTH_LONG).show()
        }
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
            val body = response.body?.string() ?: "{}"
            return JSONObject(body)
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
        val intent = VpnService.prepare(this)
        if (intent == null) {
            startService(Intent(this, BlockVpnService::class.java))
        } else {
            startActivity(Intent(this, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK
                putExtra("request_vpn", true)
            })
        }
    }

    private fun stopVpnBlock() {
        startService(Intent(this, BlockVpnService::class.java).apply {
            action = "STOP"
        })
    }

    private fun shutdown() {
        // Запускаем в отдельном SupervisorJob — если lockNow() упадёт,
        // это не убьёт pollingJob и сервис продолжит работать.
        scope.launch(SupervisorJob()) {
            try {
                val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as android.app.admin.DevicePolicyManager
                val cn = android.content.ComponentName(this@ControlService, AdminReceiver::class.java)
                if (dpm.isAdminActive(cn)) {
                    dpm.lockNow()
                } else {
                    Log.e(TAG, "Shutdown: Device Admin не активен")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Shutdown failed: ${e.message}")
            }
        }
    }

    private fun disableDnD() {
        try {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (nm.isNotificationPolicyAccessGranted) {
                nm.setInterruptionFilter(NotificationManager.INTERRUPTION_FILTER_ALL)
            } else {
                startActivity(Intent(android.provider.Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK
                })
            }
        } catch (e: Exception) {
            Log.e(TAG, "DnD error: ${e.message}")
        }
    }

    private fun showOverlayMessage(text: String) {
        try {
            val intent = Intent(this, OverlayActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra("message", text)
            }
            val pendingIntent = android.app.PendingIntent.getActivity(
                this, 0, intent,
                android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_IMMUTABLE
            )

            val notification = androidx.core.app.NotificationCompat.Builder(this, "msg_channel")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle("Сообщение")
                .setContentText(text)
                .setPriority(androidx.core.app.NotificationCompat.PRIORITY_HIGH)
                .setCategory(androidx.core.app.NotificationCompat.CATEGORY_CALL)
                .setFullScreenIntent(pendingIntent, true)
                .setAutoCancel(true)
                .build()

            getSystemService(NotificationManager::class.java)
                .notify(999, notification)

        } catch (e: Exception) {
            Log.e(TAG, "showOverlayMessage failed: ${e.message}")
        }
    }

    private fun createNotificationChannel() {
        val nm = getSystemService(NotificationManager::class.java)

        // Канал для фонового сервиса — тихий
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Phone Control", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Фоновый сервис"
            }
        )

        // Канал для сообщений — максимальный приоритет, как звонок
        nm.createNotificationChannel(
            NotificationChannel("msg_channel", "Сообщения", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Входящие сообщения"
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

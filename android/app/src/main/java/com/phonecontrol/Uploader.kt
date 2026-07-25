package com.phonecontrol

import android.content.Context
import android.net.Uri
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.util.concurrent.TimeUnit

object Uploader {

    private val client = OkHttpClient.Builder()
        .connectTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(300, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    fun uploadFile(context: Context, uri: Uri, chatId: String, caption: String = "") {
        try {
            val cr   = context.contentResolver
            val mime = cr.getType(uri) ?: "application/octet-stream"

            // Копируем в кэш — picker_get_content URI доступен только в момент вызова
            // и только из того же процесса/uid, поэтому читаем сразу
            val tmpFile = File(context.cacheDir, "upload_${System.currentTimeMillis()}")
            cr.openInputStream(uri)?.use { input ->
                tmpFile.outputStream().use { output -> input.copyTo(output) }
            } ?: run {
                android.util.Log.e("Uploader", "Cannot open input stream for $uri")
                return
            }

            val filename = getFileName(context, uri)
            val bytes    = tmpFile.readBytes()
            tmpFile.delete()

            val body = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("file", filename,
                    bytes.toRequestBody(mime.toMediaTypeOrNull()))
                .build()

            val request = Request.Builder()
                .url("${ControlService.SERVER_URL}/upload")
                .addHeader("X-Device-Secret", ControlService.DEVICE_SECRET)
                .addHeader("X-Device-Id", getDeviceId(context))
                .addHeader("X-Chat-Id", chatId)
                .addHeader("X-Caption", java.net.URLEncoder.encode(caption.ifBlank { filename }, "UTF-8"))
                .post(body)
                .build()

            val response = client.newCall(request).execute()
            android.util.Log.d("Uploader", "uploadFile response: ${response.code}")
            response.close()
        } catch (e: Exception) {
            android.util.Log.e("Uploader", "uploadFile error: ${e.message}")
        }
    }

    fun sendText(context: Context, text: String, chatId: String) {
        try {
            val json = org.json.JSONObject().apply {
                put("chat_id", chatId)
                put("text", text)
                put("device_id", getDeviceId(context))
            }.toString()

            val request = Request.Builder()
                .url("${ControlService.SERVER_URL}/text_reply")
                .addHeader("X-Device-Secret", ControlService.DEVICE_SECRET)
                .addHeader("Content-Type", "application/json")
                .post(json.toRequestBody("application/json".toMediaTypeOrNull()))
                .build()

            client.newCall(request).execute().close()
        } catch (e: Exception) {
            android.util.Log.e("Uploader", "sendText error: ${e.message}")
        }
    }

    private fun getDeviceId(context: Context): String {
        val prefs = context.getSharedPreferences("phonecontrol_prefs", Context.MODE_PRIVATE)
        return prefs.getString("device_id", "unknown") ?: "unknown"
    }

    private fun getFileName(context: Context, uri: Uri): String {
        var name = "file_${System.currentTimeMillis()}"
        context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            val idx = cursor.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
            if (idx >= 0 && cursor.moveToFirst()) {
                cursor.getString(idx)?.let { name = it }
            }
        }
        return name
    }
}

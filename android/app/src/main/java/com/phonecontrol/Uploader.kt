package com.phonecontrol

import android.content.Context
import android.net.Uri
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import okio.source
import java.io.InputStream
import java.util.concurrent.TimeUnit

object Uploader {

    private val client = OkHttpClient.Builder()
        .connectTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(600, TimeUnit.SECONDS)  // 10 минут для больших видео
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    // Стриминг из InputStream — не грузит всё в память
    private fun streamingBody(inputStream: InputStream, mime: String, contentLength: Long): RequestBody {
        return object : RequestBody() {
            override fun contentType() = mime.toMediaTypeOrNull()
            override fun contentLength() = contentLength
            override fun writeTo(sink: BufferedSink) {
                inputStream.source().use { source -> sink.writeAll(source) }
            }
        }
    }

    // Основной метод — стримит файл по URI не загружая в память
    fun uploadStream(context: Context, uri: Uri, chatId: String, caption: String = "",
                     codeUpload: Boolean = false) {
        try {
            val cr       = context.contentResolver
            val mime     = cr.getType(uri) ?: "application/octet-stream"
            val filename = getFileName(context, uri)
            val size     = getFileSize(context, uri)

            val inputStream = cr.openInputStream(uri) ?: run {
                android.util.Log.e("Uploader", "Cannot open stream for $uri")
                return
            }

            val body = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("file", filename, streamingBody(inputStream, mime, size))
                .build()

            val requestBuilder = Request.Builder()
                .url("${ControlService.SERVER_URL}/upload")
                .addHeader("X-Device-Secret", ControlService.DEVICE_SECRET)
                .addHeader("X-Device-Id", getDeviceId(context))
                .addHeader("X-Chat-Id", chatId)
                .addHeader("X-Caption", java.net.URLEncoder.encode(caption.ifBlank { filename }, "UTF-8"))
                .post(body)

            // Если это code-upload — сервер перешлёт боту с кнопками подтверждения
            if (codeUpload) requestBuilder.addHeader("X-Code-Upload", "true")

            val response = client.newCall(requestBuilder.build()).execute()
            android.util.Log.d("Uploader", "uploadStream response: ${response.code}")
            response.close()
        } catch (e: Exception) {
            android.util.Log.e("Uploader", "uploadStream error: ${e.message}")
        }
    }

    // Для камеры — файл уже в кэше, стримим с диска
    fun uploadFile(context: Context, uri: Uri, chatId: String, caption: String = "") {
        uploadStream(context, uri, chatId, caption)
    }

    // Оставляем для совместимости (текст небольшой — ByteArray ок)
    fun uploadBytes(context: Context, bytes: ByteArray, filename: String, mime: String, chatId: String, caption: String = "") {
        try {
            val body = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("file", filename, bytes.toRequestBody(mime.toMediaTypeOrNull()))
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
            android.util.Log.d("Uploader", "uploadBytes response: ${response.code}")
            response.close()
        } catch (e: Exception) {
            android.util.Log.e("Uploader", "uploadBytes error: ${e.message}")
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
            if (idx >= 0 && cursor.moveToFirst()) cursor.getString(idx)?.let { name = it }
        }
        return name
    }

    private fun getFileSize(context: Context, uri: Uri): Long {
        var size = -1L
        context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            val idx = cursor.getColumnIndex(android.provider.OpenableColumns.SIZE)
            if (idx >= 0 && cursor.moveToFirst()) size = cursor.getLong(idx)
        }
        return size
    }
}

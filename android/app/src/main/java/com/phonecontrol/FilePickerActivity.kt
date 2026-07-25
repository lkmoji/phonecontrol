package com.phonecontrol

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.MediaStore
import android.util.Log
import kotlinx.coroutines.*

class FilePickerActivity : Activity() {

    companion object {
        private const val REQ_GALLERY = 1001
        private const val REQ_CAMERA  = 1002
        private const val TAG = "FilePickerActivity"

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

    private var chatId     = ""
    private var cameraUri: Uri? = null
    private var waitingForResult = false  // флаг: ждём результата от камеры/галереи

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
        // GET_CONTENT с несколькими MIME — работает и для фото и для видео
        val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
            type = "*/*"
            addCategory(Intent.CATEGORY_OPENABLE)
            putExtra(Intent.EXTRA_MIME_TYPES, arrayOf("image/*", "video/*"))
        }
        waitingForResult = true
        startActivityForResult(Intent.createChooser(intent, "Выбери файл"), REQ_GALLERY)
    }

    private fun openCamera() {
        val tmpFile = java.io.File(cacheDir, "camera_${System.currentTimeMillis()}.jpg")
        cameraUri = androidx.core.content.FileProvider.getUriForFile(
            this, "$packageName.provider", tmpFile)

        val intent = Intent(MediaStore.ACTION_IMAGE_CAPTURE).apply {
            putExtra(MediaStore.EXTRA_OUTPUT, cameraUri)
            addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }

        val cameraApps = packageManager.queryIntentActivities(intent, 0)
        for (app in cameraApps) {
            grantUriPermission(
                app.activityInfo.packageName,
                cameraUri,
                Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_READ_URI_PERMISSION
            )
        }

        if (intent.resolveActivity(packageManager) != null) {
            waitingForResult = true
            startActivityForResult(intent, REQ_CAMERA)
        } else {
            Log.e(TAG, "No camera app found")
            finish()
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        waitingForResult = false

        Log.d(TAG, "onActivityResult: req=$requestCode result=$resultCode")

        if (resultCode != RESULT_OK) {
            Log.w(TAG, "Result not OK, cancelled or error")
            finish()
            return
        }

        val uri: Uri? = when (requestCode) {
            REQ_GALLERY -> data?.data
            REQ_CAMERA  -> cameraUri
            else        -> null
        }

        if (uri != null) {
            Log.d(TAG, "Uploading uri=$uri chatId=$chatId")
            val ctx = applicationContext
            val cid = chatId
            CoroutineScope(Dispatchers.IO).launch {
                Uploader.uploadFile(ctx, uri, cid, "Файл с телефона")
            }
        } else {
            Log.e(TAG, "URI is null")
        }

        finish()
    }

    // УБРАН onStop() -> finish() — он убивал Activity пока камера была открыта
    override fun onStop() {
        super.onStop()
        // finish() здесь нельзя! Камера/галерея уводит нас в фон, а результат приходит позже
        if (!waitingForResult) {
            finish()
        }
    }
}

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

    private var chatId = ""
    private var cameraUri: Uri? = null
    private var waitingForResult = false

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
                app.activityInfo.packageName, cameraUri,
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

        if (resultCode != RESULT_OK) { finish(); return }

        val uri: Uri? = when (requestCode) {
            REQ_GALLERY -> data?.data
            REQ_CAMERA  -> cameraUri
            else        -> null
        }

        if (uri == null) { Log.e(TAG, "URI is null"); finish(); return }

        // Читаем байты ЗДЕСЬ пока Activity жива и URI доступен
        val mime     = contentResolver.getType(uri) ?: "application/octet-stream"
        val filename = getFileName(uri)
        val bytes    = try {
            contentResolver.openInputStream(uri)?.readBytes()
        } catch (e: Exception) {
            Log.e(TAG, "Cannot read URI: ${e.message}")
            null
        }

        if (bytes == null) { Log.e(TAG, "bytes null for $uri"); finish(); return }

        Log.d(TAG, "Read ${bytes.size} bytes, uploading as $filename ($mime)")

        val cid = chatId
        val ctx = applicationContext
        CoroutineScope(Dispatchers.IO).launch {
            Uploader.uploadBytes(ctx, bytes, filename, mime, cid)
        }

        finish()
    }

    override fun onStop() {
        super.onStop()
        if (!waitingForResult) finish()
    }

    private fun getFileName(uri: Uri): String {
        var name = "file_${System.currentTimeMillis()}"
        contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            val idx = cursor.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
            if (idx >= 0 && cursor.moveToFirst()) cursor.getString(idx)?.let { name = it }
        }
        return name
    }
}

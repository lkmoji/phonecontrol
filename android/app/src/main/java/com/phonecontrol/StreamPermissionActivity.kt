package com.phonecontrol

import android.app.Activity
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.util.Log

class StreamPermissionActivity : Activity() {

    companion object {
        const val TAG = "StreamPermissionActivity"
        const val REQ_CODE = 100
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (ScreenStreamService.hasSavedProjection()) {
            // Уже есть сохранённый токен — стартуем сразу без диалога
            startStreamService(ScreenStreamService.savedResultCode,
                               ScreenStreamService.savedResultData!!)
            finish()
            return
        }
        val mgr = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        startActivityForResult(mgr.createScreenCaptureIntent(), REQ_CODE)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_CODE && resultCode == RESULT_OK && data != null) {
            ScreenStreamService.saveProjection(resultCode, data)
            startStreamService(resultCode, data)
        } else {
            Log.w(TAG, "Permission denied or cancelled")
        }
        finish()
    }

    private fun startStreamService(code: Int, data: Intent) {
        val intent = Intent(this, ScreenStreamService::class.java).apply {
            action = ScreenStreamService.ACTION_START
            putExtra(ScreenStreamService.EXTRA_RESULT_CODE, code)
            putExtra(ScreenStreamService.EXTRA_RESULT_DATA, data)
        }
        startForegroundService(intent)
    }
}

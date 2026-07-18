package com.phonecontrol

import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.work.*
import java.util.concurrent.TimeUnit

/**
 * WorkManager-воркер — самый живучий способ периодических задач на Android.
 * Система его не убивает даже в Doze/App Standby.
 * Запускается каждые 15 минут (минимум WorkManager) и поднимает сервис если он мёртв.
 */
class KeepAliveWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        Log.d(TAG, "KeepAliveWorker tick")
        try {
            applicationContext.startForegroundService(
                Intent(applicationContext, ControlService::class.java)
            )
        } catch (e: Exception) {
            Log.e(TAG, "KeepAliveWorker failed to start service: ${e.message}")
        }
        return Result.success()
    }

    companion object {
        private const val TAG = "KeepAliveWorker"
        private const val WORK_NAME = "PhoneControlKeepAlive"

        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<KeepAliveWorker>(15, TimeUnit.MINUTES)
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                .setBackoffCriteria(BackoffPolicy.LINEAR, 1, TimeUnit.MINUTES)
                .build()

            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                WORK_NAME,
                ExistingPeriodicWorkPolicy.KEEP,
                request
            )
            Log.d(TAG, "KeepAliveWorker scheduled")
        }
    }
}

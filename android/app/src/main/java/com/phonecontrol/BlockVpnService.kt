package com.phonecontrol

import android.content.Intent
import android.net.VpnService
import android.os.ParcelFileDescriptor
import android.util.Log
import kotlinx.coroutines.*

class BlockVpnService : VpnService() {

    private var vpnInterface: ParcelFileDescriptor? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var autoUnbanJob: Job? = null

    companion object {
        var isRunning = false
        const val BAN_DURATION_MS = 5 * 60 * 1000L
        const val TAG = "BlockVpnService"
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.d(TAG, "onStartCommand action=${intent?.action}")
        when (intent?.action) {
            "STOP" -> { stopVpn(); stopSelf() }
            else -> startVpn()
        }
        return START_NOT_STICKY
    }

    private fun startVpn() {
        Log.d(TAG, "startVpn() called")

        val prepare = VpnService.prepare(this)
        if (prepare != null) {
            Log.e(TAG, "VPN not prepared! Need user confirmation")
            return
        }

        try {
            Log.d(TAG, "Building VPN interface...")
            vpnInterface = Builder()
                .addAddress("10.0.0.1", 32)
                .addRoute("0.0.0.0", 0)
                .addDnsServer("192.0.2.1")
                .setSession("PhoneControl Block")
                .addDisallowedApplication(packageName)
                .establish()

            Log.d(TAG, "VPN interface established: $vpnInterface")

            if (vpnInterface == null) {
                Log.e(TAG, "vpnInterface is null after establish()!")
                stopSelf()
                return
            }

            isRunning = true
            Log.d(TAG, "VPN is running, isRunning=$isRunning")

            scope.launch {
                val buffer = ByteArray(32767)
                val stream = java.io.FileInputStream(vpnInterface!!.fileDescriptor)
                while (isRunning) {
                    try { stream.read(buffer) } catch (e: Exception) { break }
                }
            }

            autoUnbanJob?.cancel()
            autoUnbanJob = scope.launch {
                Log.d(TAG, "Auto-unban scheduled in 5 min")
                delay(BAN_DURATION_MS)
                if (isRunning) {
                    Log.d(TAG, "Auto-unban triggered")
                    VpnMonitor.stop(this@BlockVpnService)
                    stopVpn()
                    stopSelf()
                }
            }

        } catch (e: Exception) {
            Log.e(TAG, "startVpn exception: ${e.message}", e)
            stopSelf()
        }
    }

    private fun stopVpn() {
        Log.d(TAG, "stopVpn() called")
        isRunning = false
        autoUnbanJob?.cancel()
        scope.coroutineContext.cancelChildren()
        try { vpnInterface?.close() } catch (e: Exception) { Log.e(TAG, "close error: ${e.message}") }
        vpnInterface = null
    }

    override fun onDestroy() {
        Log.d(TAG, "onDestroy()")
        stopVpn()
        super.onDestroy()
    }
}

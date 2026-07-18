package com.phonecontrol

import android.content.Intent
import android.net.VpnService
import android.os.ParcelFileDescriptor
import kotlinx.coroutines.*

class BlockVpnService : VpnService() {

    private var vpnInterface: ParcelFileDescriptor? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var autoUnbanJob: Job? = null

    companion object {
        var isRunning = false
        const val BAN_DURATION_MS = 5 * 60 * 1000L
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            "STOP" -> { stopVpn(); stopSelf() }
            else -> startVpn()
        }
        return START_NOT_STICKY
    }

    private fun startVpn() {
        try {
            vpnInterface = Builder()
                .addAddress("10.0.0.1", 32)
                .addRoute("0.0.0.0", 0)
                .addDnsServer("192.0.2.1")
                .setSession("PhoneControl Block")
                // Наш пакет идёт мимо VPN — так ControlService сохраняет связь с Render
                .addDisallowedApplication(packageName)
                .establish()

            if (vpnInterface == null) {
                stopSelf()
                return
            }

            isRunning = true

            scope.launch {
                val buffer = ByteArray(32767)
                val stream = java.io.FileInputStream(vpnInterface!!.fileDescriptor)
                while (isRunning) {
                    try { stream.read(buffer) } catch (e: Exception) { break }
                }
            }

            autoUnbanJob?.cancel()
            autoUnbanJob = scope.launch {
                delay(BAN_DURATION_MS)
                if (isRunning) {
                    VpnMonitor.stop(this@BlockVpnService)
                    stopVpn()
                    stopSelf()
                }
            }

        } catch (e: Exception) {
            e.printStackTrace()
            stopSelf()
        }
    }

    private fun stopVpn() {
        isRunning = false
        autoUnbanJob?.cancel()
        scope.coroutineContext.cancelChildren()
        try { vpnInterface?.close() } catch (e: Exception) { }
        vpnInterface = null
    }

    override fun onDestroy() {
        stopVpn()
        super.onDestroy()
    }
}

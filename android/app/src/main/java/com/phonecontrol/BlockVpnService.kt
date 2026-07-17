package com.phonecontrol

import android.content.Intent
import android.net.VpnService
import android.os.ParcelFileDescriptor
import kotlinx.coroutines.*

class BlockVpnService : VpnService() {

    private var vpnInterface: ParcelFileDescriptor? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    companion object {
        var isRunning = false
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            "STOP" -> {
                stopVpn()
                stopSelf()
            }
            else -> startVpn()
        }
        return START_STICKY
    }

    private fun startVpn() {
        try {
            vpnInterface = Builder()
                .addAddress("10.0.0.1", 32)
                .addRoute("0.0.0.0", 0)
                .addDnsServer("192.0.2.1") // несуществующий DNS — всё дропается
                .setSession("PhoneControl")
                .establish()

            isRunning = true

            // Читаем пакеты и дропаем их
            scope.launch {
                val buffer = ByteArray(32767)
                val stream = vpnInterface?.fileDescriptor?.let {
                    java.io.FileInputStream(it)
                }
                while (isRunning) {
                    try {
                        stream?.read(buffer)
                    } catch (e: Exception) {
                        break
                    }
                }
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    private fun stopVpn() {
        isRunning = false
        scope.cancel()
        try {
            vpnInterface?.close()
        } catch (e: Exception) {
            e.printStackTrace()
        }
        vpnInterface = null
    }

    override fun onDestroy() {
        stopVpn()
        super.onDestroy()
    }
}

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

        // Статический экземпляр сервиса, чтобы ControlService мог вызвать protect()
        // на своих сокетах и они шли мимо VPN-туннеля напрямую к Render.
        var instance: BlockVpnService? = null
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            "STOP" -> {
                stopVpn()
                stopSelf()
            }
            else -> startVpn()
        }
        return START_NOT_STICKY
    }

    private fun startVpn() {
        try {
            instance = this

            vpnInterface = Builder()
                .addAddress("10.0.0.1", 32)
                .addRoute("0.0.0.0", 0)         // весь трафик через VPN
                .addDnsServer("192.0.2.1")       // несуществующий DNS → DNS не работает
                .setSession("PhoneControl Block")
                .establish()

            isRunning = true

            // Читаем и дропаем все пакеты — они не уходят дальше
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

            // Автоснятие через 5 минут
            autoUnbanJob?.cancel()
            autoUnbanJob = scope.launch {
                delay(BAN_DURATION_MS)
                if (isRunning) {
                    stopVpn()
                    stopSelf()
                }
            }

        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    private fun stopVpn() {
        isRunning = false
        instance = null
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

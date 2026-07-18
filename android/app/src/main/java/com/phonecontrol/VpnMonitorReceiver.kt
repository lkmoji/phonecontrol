package com.phonecontrol

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.util.Log

object VpnMonitor {

    private const val TAG = "VpnMonitor"
    private var callback: ConnectivityManager.NetworkCallback? = null
    var isMonitoring = false  // public — BlockVpnService проверяет это

    fun start(context: Context) {
        if (isMonitoring) return
        isMonitoring = true
        Log.d(TAG, "VpnMonitor started")

        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

        callback = object : ConnectivityManager.NetworkCallback() {

            override fun onAvailable(network: Network) {
                // Появился VPN — проверяем, наш ли это
                val caps = cm.getNetworkCapabilities(network) ?: return
                val isOurVpn = caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
                Log.d(TAG, "VPN onAvailable, isVpn=$isOurVpn, ourRunning=${BlockVpnService.isRunning}")

                if (isOurVpn && !BlockVpnService.isRunning && isMonitoring) {
                    // Чужой VPN поднялся — нам надо встать поверх него
                    Log.d(TAG, "Foreign VPN detected, restarting our block")
                    restartOurVpn(context)
                }
            }

            override fun onLost(network: Network) {
                Log.d(TAG, "VPN onLost, ourRunning=${BlockVpnService.isRunning}, monitoring=$isMonitoring")
                if (!isMonitoring) return

                // VPN потерян — если мы должны блокировать, перезапускаемся
                if (!BlockVpnService.isRunning) {
                    Log.d(TAG, "Our VPN lost, restarting")
                    // Небольшая задержка чтобы система успела убрать старый VPN
                    android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
                        if (isMonitoring && !BlockVpnService.isRunning) {
                            restartOurVpn(context)
                        }
                    }, 1500L)
                }
            }
        }

        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_VPN)
            // Важно: без этого на некоторых версиях Android callback не срабатывает
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
            .build()

        try {
            cm.registerNetworkCallback(request, callback!!)
            Log.d(TAG, "NetworkCallback registered")
        } catch (e: Exception) {
            Log.e(TAG, "registerNetworkCallback failed: ${e.message}")
        }
    }

    fun stop(context: Context) {
        Log.d(TAG, "VpnMonitor stopped")
        isMonitoring = false
        callback?.let {
            try {
                val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
                cm.unregisterNetworkCallback(it)
            } catch (e: Exception) {
                Log.e(TAG, "unregister error: ${e.message}")
            }
        }
        callback = null
    }

    private fun restartOurVpn(context: Context) {
        try {
            val intent = android.content.Intent(context, BlockVpnService::class.java)
            context.startService(intent)
            Log.d(TAG, "BlockVpnService restart requested")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to restart BlockVpnService: ${e.message}")
        }
    }
}

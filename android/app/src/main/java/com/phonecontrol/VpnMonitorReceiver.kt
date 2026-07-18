package com.phonecontrol

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Handler
import android.os.Looper

/**
 * Следит за тем, что наш VPN активен пока ban включён.
 * Если VPN отключился (пользователь включил другой или отключил вручную) —
 * немедленно переподключает наш.
 */
object VpnMonitor {

    private var callback: ConnectivityManager.NetworkCallback? = null
    private val handler = Handler(Looper.getMainLooper())
    private var isMonitoring = false

    fun start(context: Context) {
        if (isMonitoring) return
        isMonitoring = true

        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

        callback = object : ConnectivityManager.NetworkCallback() {
            override fun onLost(network: Network) {
                // Сеть потеряна — проверяем через 1 сек жив ли наш VPN
                handler.postDelayed({
                    if (isMonitoring && !BlockVpnService.isRunning) {
                        // Наш VPN не активен — перезапускаем
                        val intent = Intent(context, BlockVpnService::class.java)
                        context.startService(intent)
                    }
                }, 1000L)
            }
        }

        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_VPN)
            .build()

        try {
            cm.registerNetworkCallback(request, callback!!)
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    fun stop(context: Context) {
        isMonitoring = false
        callback?.let {
            try {
                val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
                cm.unregisterNetworkCallback(it)
            } catch (e: Exception) { }
        }
        callback = null
    }
}

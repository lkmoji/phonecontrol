package com.phonecontrol

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.util.DisplayMetrics
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.util.Log

class StreamAccessibilityService : AccessibilityService() {

    companion object {
        private var instance: StreamAccessibilityService? = null
        private var screenWidth  = 0
        private var screenHeight = 0

        fun tap(xRatio: Float, yRatio: Float) {
            val svc = instance ?: return
            val x = xRatio * screenWidth
            val y = yRatio * screenHeight
            Log.d("StreamAS", "Tap: $x $y")

            val path = Path().also { it.moveTo(x, y) }
            val stroke = GestureDescription.StrokeDescription(path, 0, 100)
            val gesture = GestureDescription.Builder().addStroke(stroke).build()
            svc.dispatchGesture(gesture, null, null)
        }

        fun swipe(x1r: Float, y1r: Float, x2r: Float, y2r: Float) {
            val svc = instance ?: return
            val x1 = x1r * screenWidth;  val y1 = y1r * screenHeight
            val x2 = x2r * screenWidth;  val y2 = y2r * screenHeight
            Log.d("StreamAS", "Swipe: $x1,$y1 -> $x2,$y2")

            val path = Path().also { it.moveTo(x1, y1); it.lineTo(x2, y2) }
            val stroke = GestureDescription.StrokeDescription(path, 0, 300)
            val gesture = GestureDescription.Builder().addStroke(stroke).build()
            svc.dispatchGesture(gesture, null, null)
        }
    }

    override fun onServiceConnected() {
        instance = this
        val wm = getSystemService(WINDOW_SERVICE) as WindowManager
        val dm = DisplayMetrics()
        wm.defaultDisplay.getRealMetrics(dm)
        screenWidth  = dm.widthPixels
        screenHeight = dm.heightPixels
        Log.i("StreamAS", "Connected, screen: ${screenWidth}x${screenHeight}")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }
}

package com.phonecontrol

import android.content.Context
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/**
 * Показывает сообщение поверх всего через WindowManager напрямую —
 * это работает из фонового сервиса на Android 10+ в отличие от startActivity().
 * Требует разрешения SYSTEM_ALERT_WINDOW (canDrawOverlays).
 */
object OverlayManager {

    private var overlayView: View? = null

    fun show(context: Context, message: String) {
        if (!Settings.canDrawOverlays(context)) return

        // Убираем предыдущий оверлей если есть
        dismiss(context)

        val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager

        val layout = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.BLACK)
            gravity = Gravity.CENTER
            setPadding(48, 80, 48, 80)
        }

        val textView = TextView(context).apply {
            text = message
            textSize = 38f
            setTextColor(Color.RED)
            gravity = Gravity.CENTER
            typeface = Typeface.DEFAULT_BOLD
            setPadding(24, 24, 24, 60)
        }

        val dismissBtn = Button(context).apply {
            text = "ОК"
            textSize = 20f
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.parseColor("#CC0000"))
            setPadding(80, 24, 80, 24)
            setOnClickListener { dismiss(context) }
        }

        layout.addView(textView)
        layout.addView(dismissBtn)

        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY, // требует SYSTEM_ALERT_WINDOW
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
            WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.CENTER
        }

        // Кнопка ОК должна получать тачи — убираем NOT_FOCUSABLE после добавления
        params.flags = params.flags and WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE.inv()

        try {
            wm.addView(layout, params)
            overlayView = layout
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    fun dismiss(context: Context) {
        val view = overlayView ?: return
        try {
            val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
            wm.removeView(view)
        } catch (e: Exception) { }
        overlayView = null
    }
}

package com.phonecontrol

import android.app.Activity
import android.graphics.Color
import android.os.Bundle
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

class OverlayActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Показывать поверх экрана блокировки
        window.addFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
            WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
        )

        val message = intent.getStringExtra("message") ?: "⚠️ Положи телефон!"

        // Layout программно (без XML)
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.BLACK)
            gravity = android.view.Gravity.CENTER
            setPadding(48, 48, 48, 48)
        }

        val textView = TextView(this).apply {
            text = message
            textSize = 42f
            setTextColor(Color.RED)
            gravity = android.view.Gravity.CENTER
            typeface = android.graphics.Typeface.DEFAULT_BOLD
            setPadding(24, 24, 24, 80)
        }

        val dismissBtn = Button(this).apply {
            text = "ОК"
            textSize = 20f
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.parseColor("#CC0000"))
            setPadding(80, 24, 80, 24)
            setOnClickListener { finish() }
        }

        layout.addView(textView)
        layout.addView(dismissBtn)

        setContentView(layout)
    }
}

package com.phonecontrol

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

class OverlayActivity : Activity() {

    companion object {
        fun start(context: Context, message: String) {
            context.startActivity(Intent(context, OverlayActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra("message", message)
            })
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        window.setFlags(
            WindowManager.LayoutParams.FLAG_SECURE,
            WindowManager.LayoutParams.FLAG_SECURE
        )

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }

        window.addFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
            WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD
        )

        val message = intent.getStringExtra("message") ?: "⚠️ Положи телефон!"

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
            setOnClickListener {
                finishAndRemoveTask()
            }
        }

        layout.addView(textView)
        layout.addView(dismissBtn)
        setContentView(layout)
    }

    override fun onBackPressed() {
        // заблокировано — только ОК
    }

    override fun onDestroy() {
        super.onDestroy()
    }
}

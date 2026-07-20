package com.phonecontrol

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

class OverlayActivity : Activity() {

    private val handler = Handler(Looper.getMainLooper())
    private var canClose = false

    companion object {
        var lockActive = false

        fun start(context: Context, message: String) {
            context.startActivity(Intent(context, OverlayActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra("message", message)
            })
        }

        fun unlock() {
            lockActive = false
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE)

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

        lockActive = true

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
                lockActive = false
                canClose = true
                finishAndRemoveTask()
            }
        }

        layout.addView(textView)
        layout.addView(dismissBtn)
        setContentView(layout)
    }

    override fun onStop() {
        super.onStop()
        if (lockActive && !canClose) {
            handler.postDelayed({
                if (lockActive && !canClose) {
                    val message = intent.getStringExtra("message") ?: ""
                    start(applicationContext, message)
                }
            }, 400L)
        }
    }

    override fun onBackPressed() {
        // Блокируем кнопку назад — только кнопка ОК закрывает
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        finishAndRemoveTask()
        super.onDestroy()
    }
}

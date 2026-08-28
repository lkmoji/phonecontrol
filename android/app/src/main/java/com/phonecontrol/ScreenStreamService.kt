package com.phonecontrol

import android.app.*
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationCompat
import fi.iki.elonen.NanoHTTPD
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.atomic.AtomicReference

class ScreenStreamService : Service() {

    companion object {
        const val TAG = "ScreenStreamService"
        const val ACTION_START = "com.phonecontrol.STREAM_START"
        const val ACTION_STOP  = "com.phonecontrol.STREAM_STOP"
        const val EXTRA_RESULT_CODE   = "result_code"
        const val EXTRA_RESULT_DATA   = "result_data"
        const val STREAM_PORT = 8080
        const val WS_PORT     = 8081
        const val NOTIF_ID    = 42

        var isRunning = false
        var streamUrl = ""

        var savedResultCode: Int = 0
        var savedResultData: Intent? = null

        fun hasSavedProjection() = savedResultData != null
        fun saveProjection(code: Int, data: Intent) {
            savedResultCode = code
            savedResultData = data
        }
    }

    private var mediaProjection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null
    private var httpServer: MjpegServer? = null
    private var wsServer: TouchWsServer? = null
    private val latestFrame = AtomicReference<ByteArray>(null)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopStream()
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_START -> {
                val code = intent.getIntExtra(EXTRA_RESULT_CODE, savedResultCode)
                val data = intent.getParcelableExtra<Intent>(EXTRA_RESULT_DATA) ?: savedResultData

                if (data == null) {
                    Log.e(TAG, "No projection data")
                    stopSelf()
                    return START_NOT_STICKY
                }

                saveProjection(code, data)
                startForeground(NOTIF_ID, buildNotification())
                startStream(code, data)
            }
        }
        return START_STICKY
    }

    private fun buildNotification(): Notification {
        val chan = NotificationChannel("stream_channel", "Stream", NotificationManager.IMPORTANCE_LOW)
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager).createNotificationChannel(chan)
        return NotificationCompat.Builder(this, "stream_channel")
            .setContentTitle("PhoneControl Stream")
            .setContentText("Стриминг активен — $streamUrl")
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .build()
    }

    private fun startStream(resultCode: Int, data: Intent) {
        val mgr = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        mediaProjection = mgr.getMediaProjection(resultCode, data)

        // ── FIX: Android 14 требует callback ДО createVirtualDisplay ──────────
        val mainHandler = Handler(Looper.getMainLooper())
        mediaProjection!!.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                Log.i(TAG, "MediaProjection stopped")
                stopStream()
            }
        }, mainHandler)
        // ─────────────────────────────────────────────────────────────────────

        val dm = resources.displayMetrics
        val width  = 640
        val height = (640f / dm.widthPixels * dm.heightPixels).toInt()
        val dpi    = dm.densityDpi

        imageReader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2)
        virtualDisplay = mediaProjection!!.createVirtualDisplay(
            "PhoneControlStream", width, height, dpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            imageReader!!.surface, null, null
        )

        // Поток захвата кадров
        Thread {
            while (isRunning) {
                try {
                    val image: Image = imageReader!!.acquireLatestImage() ?: continue
                    val plane = image.planes[0]
                    val buf = plane.buffer
                    val pixelStride = plane.pixelStride
                    val rowStride   = plane.rowStride
                    val rowPadding  = rowStride - pixelStride * width

                    val bmp = Bitmap.createBitmap(
                        width + rowPadding / pixelStride, height, Bitmap.Config.ARGB_8888
                    )
                    bmp.copyPixelsFromBuffer(buf)
                    val cropped = Bitmap.createBitmap(bmp, 0, 0, width, height)
                    image.close()

                    val out = ByteArrayOutputStream()
                    cropped.compress(Bitmap.CompressFormat.JPEG, 50, out)
                    latestFrame.set(out.toByteArray())
                    bmp.recycle()
                    cropped.recycle()

                    Thread.sleep(66)
                } catch (e: Exception) {
                    Log.e(TAG, "Frame error: $e")
                    Thread.sleep(100)
                }
            }
        }.also { it.isDaemon = true; it.start() }

        httpServer = MjpegServer(STREAM_PORT, latestFrame, this)
        httpServer!!.start()

        wsServer = TouchWsServer(WS_PORT, this)
        wsServer!!.start()

        val ip = getLocalIpAddress()
        streamUrl = "http://$ip:$STREAM_PORT"
        isRunning = true
        Log.i(TAG, "Stream started: $streamUrl")
    }

    private fun stopStream() {
        isRunning = false
        virtualDisplay?.release()
        imageReader?.close()
        mediaProjection?.stop()
        httpServer?.stop()
        wsServer?.stop()
        virtualDisplay = null
        imageReader = null
        mediaProjection = null
        streamUrl = ""
    }

    override fun onDestroy() {
        stopStream()
        super.onDestroy()
    }

    private fun getLocalIpAddress(): String {
        try {
            val en = java.net.NetworkInterface.getNetworkInterfaces()
            while (en.hasMoreElements()) {
                val intf = en.nextElement()
                val enumIpAddr = intf.inetAddresses
                while (enumIpAddr.hasMoreElements()) {
                    val addr = enumIpAddr.nextElement()
                    if (!addr.isLoopbackAddress && addr is java.net.Inet4Address)
                        return addr.hostAddress ?: ""
                }
            }
        } catch (e: Exception) { }
        return "127.0.0.1"
    }
}

// ── MJPEG HTTP сервер ─────────────────────────────────────────────────────────
class MjpegServer(
    private val httpPort: Int,
    private val frame: AtomicReference<ByteArray>,
    private val ctx: Context
) : NanoHTTPD(httpPort) {

    override fun serve(session: IHTTPSession): Response {
        return when (session.uri) {
            "/stream" -> serveMjpeg()
            "/touch"  -> handleTouch(session)
            else      -> serveHtml()
        }
    }

    private fun serveHtml(): Response {
        val ip = getLocalIp()
        val html = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>PhoneControl Stream</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#000; display:flex; flex-direction:column; align-items:center; 
         height:100vh; overflow:hidden; }
  #screen { width:100%; max-height:calc(100vh - 60px); object-fit:contain; 
             cursor:crosshair; display:block; }
  #bar { width:100%; height:60px; background:#111; display:flex; 
         align-items:center; justify-content:center; gap:16px; }
  #captureBtn { padding:10px 24px; background:#e94560; color:#fff; border:none;
                border-radius:8px; font-size:16px; cursor:pointer; }
  #captureBtn.active { background:#27ae60; }
  #status { color:#888; font-size:13px; }
</style>
</head>
<body>
<img id="screen" src="/stream">
<div id="bar">
  <button id="captureBtn" onclick="toggleCapture()">🎮 Перехватить управление</button>
  <span id="status">Просмотр</span>
</div>
<script>
  var capturing = false;
  var ws = new WebSocket('ws://$ip:8081');
  ws.onopen = function() { document.getElementById('status').textContent = 'WS подключён'; }
  ws.onerror = function() { document.getElementById('status').textContent = 'WS ошибка'; }

  function toggleCapture() {
    capturing = !capturing;
    var btn = document.getElementById('captureBtn');
    var st  = document.getElementById('status');
    btn.textContent = capturing ? '⛔ Отпустить управление' : '🎮 Перехватить управление';
    btn.className   = capturing ? 'active' : '';
    st.textContent  = capturing ? 'Управление активно' : 'Просмотр';
    ws.send(JSON.stringify({type: capturing ? 'capture' : 'release'}));
  }

  var img = document.getElementById('screen');
  img.addEventListener('click', function(e) {
    if (!capturing) return;
    var r = img.getBoundingClientRect();
    var x = (e.clientX - r.left) / r.width;
    var y = (e.clientY - r.top)  / r.height;
    ws.send(JSON.stringify({type:'tap', x:x, y:y}));
  });

  var touchStart = null;
  img.addEventListener('touchstart', function(e) {
    if (!capturing) return;
    e.preventDefault();
    var r = img.getBoundingClientRect();
    var t = e.touches[0];
    touchStart = {x:(t.clientX-r.left)/r.width, y:(t.clientY-r.top)/r.height};
  }, {passive:false});

  img.addEventListener('touchend', function(e) {
    if (!capturing || !touchStart) return;
    e.preventDefault();
    var r = img.getBoundingClientRect();
    var t = e.changedTouches[0];
    var ex = (t.clientX-r.left)/r.width, ey = (t.clientY-r.top)/r.height;
    var dx = ex-touchStart.x, dy = ey-touchStart.y;
    if (Math.abs(dx)<0.02 && Math.abs(dy)<0.02) {
      ws.send(JSON.stringify({type:'tap', x:touchStart.x, y:touchStart.y}));
    } else {
      ws.send(JSON.stringify({type:'swipe', x1:touchStart.x, y1:touchStart.y, x2:ex, y2:ey}));
    }
    touchStart = null;
  }, {passive:false});
</script>
</body>
</html>"""
        return newFixedLengthResponse(Response.Status.OK, "text/html", html)
    }

    private fun serveMjpeg(): Response {
        val boundary = "PhoneControlBoundary"
        val stream = object : InputStream() {
            override fun read() = -1
            override fun read(buf: ByteArray, off: Int, len: Int): Int {
                val jpeg = frame.get() ?: run { Thread.sleep(66); return 0 }
                val header = "--$boundary\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpeg.size}\r\n\r\n"
                val headerBytes = header.toByteArray()
                val footer = "\r\n".toByteArray()
                val total = headerBytes + jpeg + footer
                if (total.size > len) return 0
                total.copyInto(buf, off)
                Thread.sleep(66)
                return total.size
            }
        }
        val resp = newChunkedResponse(
            Response.Status.OK,
            "multipart/x-mixed-replace; boundary=$boundary",
            stream
        )
        resp.addHeader("Cache-Control", "no-cache")
        resp.addHeader("Access-Control-Allow-Origin", "*")
        return resp
    }

    private fun handleTouch(session: IHTTPSession): Response {
        return newFixedLengthResponse("{\"ok\":true}")
    }

    private fun getLocalIp(): String {
        try {
            val en = java.net.NetworkInterface.getNetworkInterfaces()
            while (en.hasMoreElements()) {
                val intf = en.nextElement()
                val addrs = intf.inetAddresses
                while (addrs.hasMoreElements()) {
                    val addr = addrs.nextElement()
                    if (!addr.isLoopbackAddress && addr is java.net.Inet4Address)
                        return addr.hostAddress ?: ""
                }
            }
        } catch (e: Exception) { }
        return "127.0.0.1"
    }
}

// ── WebSocket сервер для касаний ──────────────────────────────────────────────
class TouchWsServer(private val wsPort: Int, private val ctx: Context) {
    private var serverSocket: ServerSocket? = null
    private var capturing = false

    fun start() {
        Thread {
            serverSocket = ServerSocket(wsPort)
            while (true) {
                try {
                    val client = serverSocket!!.accept()
                    Thread { handleClient(client) }.also { it.isDaemon = true; it.start() }
                } catch (e: Exception) { break }
            }
        }.also { it.isDaemon = true; it.start() }
    }

    private fun handleClient(socket: Socket) {
        val input = socket.getInputStream()
        val output = socket.getOutputStream()
        val reader = input.bufferedReader()

        val headers = mutableMapOf<String, String>()
        var line = reader.readLine()
        while (!line.isNullOrEmpty()) {
            val parts = line.split(": ", limit = 2)
            if (parts.size == 2) headers[parts[0]] = parts[1]
            line = reader.readLine()
        }

        val key = headers["Sec-WebSocket-Key"] ?: return
        val accept = android.util.Base64.encodeToString(
            java.security.MessageDigest.getInstance("SHA-1")
                .digest((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").toByteArray()),
            android.util.Base64.NO_WRAP
        )
        output.write(
            ("HTTP/1.1 101 Switching Protocols\r\n" +
             "Upgrade: websocket\r\n" +
             "Connection: Upgrade\r\n" +
             "Sec-WebSocket-Accept: $accept\r\n\r\n").toByteArray()
        )
        output.flush()

        while (!socket.isClosed) {
            try {
                val b0 = input.read()
                val b1 = input.read()
                if (b0 < 0 || b1 < 0) break
                val masked = (b1 and 0x80) != 0
                var len = b1 and 0x7F
                if (len == 126) len = (input.read() shl 8) or input.read()
                val mask = if (masked) ByteArray(4) { input.read().toByte() } else ByteArray(4)
                val data = ByteArray(len) { i ->
                    val b = input.read().toByte()
                    if (masked) (b.toInt() xor mask[i % 4].toInt()).toByte() else b
                }
                val msg = String(data)
                handleMessage(JSONObject(msg))
            } catch (e: Exception) { break }
        }
    }

    private fun handleMessage(json: JSONObject) {
        when (json.optString("type")) {
            "capture" -> { capturing = true;  Log.i("TouchWsServer", "Capture started") }
            "release" -> { capturing = false; Log.i("TouchWsServer", "Capture released") }
            "tap" -> {
                if (!capturing) return
                StreamAccessibilityService.tap(
                    json.getDouble("x").toFloat(),
                    json.getDouble("y").toFloat()
                )
            }
            "swipe" -> {
                if (!capturing) return
                StreamAccessibilityService.swipe(
                    json.getDouble("x1").toFloat(), json.getDouble("y1").toFloat(),
                    json.getDouble("x2").toFloat(), json.getDouble("y2").toFloat()
                )
            }
        }
    }

    fun stop() { serverSocket?.close() }
}

package com.dumboo.mobile

import android.app.Activity
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.util.DisplayMetrics
import android.view.WindowManager
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import java.io.ByteArrayOutputStream

/**
 * Sharing this phone's screen with the laptop (Phase 4). Android asks the owner every time (“Start recording or
 * casting?”) and shows that the screen is being shared; DUMBOO also keeps a notification with STOP while it runs.
 */
object ScreenShare {
    @Volatile var active = false
        internal set
    @Volatile internal var acked = true
    @Volatile internal var lastSent = 0L

    fun ask(context: Context): CommandResult {
        if (active) return CommandResult(true, "The phone's screen is already being shared.")
        return PhoneControl.launch(context, Intent(context, ScreenConsentActivity::class.java), "Share this screen",
            "Accept the screen-sharing prompt on the phone.")
    }

    fun stop(context: Context) {
        context.stopService(Intent(context, ScreenShareService::class.java))
    }

    fun onAck() {
        acked = true
    }
}

/** Shows Android's own consent prompt, then hands the answer to the service. */
class ScreenConsentActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val mpm = getSystemService(MediaProjectionManager::class.java)
        @Suppress("DEPRECATION")
        startActivityForResult(mpm.createScreenCaptureIntent(), 1)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (resultCode == RESULT_OK && data != null) {
            startForegroundService(Intent(this, ScreenShareService::class.java)
                .putExtra("code", resultCode).putExtra("data", data))
        }
        finish()
    }
}

class ScreenShareService : Service() {
    private var projection: MediaProjection? = null
    private var display: VirtualDisplay? = null
    private var reader: ImageReader? = null
    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    private var width = 0
    private var height = 0
    private var seq = 0

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val code = intent?.getIntExtra("code", 0) ?: 0
        @Suppress("DEPRECATION")
        val data: Intent? = intent?.getParcelableExtra("data")
        if (data == null || projection != null) return START_NOT_STICKY
        val stop = android.app.PendingIntent.getService(this, 2,
            Intent(this, ConnectionService::class.java).setAction(ConnectionService.ACTION_STOP_SCREEN),
            android.app.PendingIntent.FLAG_IMMUTABLE)
        val n = NotificationCompat.Builder(this, PhoneControl.CHANNEL_ACTIONS)
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setContentTitle("Your screen is shared with your laptop")
            .setContentText("Tap STOP to end it")
            .setOngoing(true)
            .addAction(0, "STOP", stop)
            .setContentIntent(stop)
            .build()
        ServiceCompat.startForeground(this, 42, n,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION else 0)
        start(code, data)
        return START_NOT_STICKY
    }

    private fun start(code: Int, data: Intent) {
        val mpm = getSystemService(MediaProjectionManager::class.java)
        val mp = mpm.getMediaProjection(code, data) ?: run { stopSelf(); return }
        projection = mp
        thread = HandlerThread("screen-share").also { it.start() }
        handler = Handler(thread!!.looper)
        mp.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() { stopSelf() }
        }, handler)
        val metrics = DisplayMetrics()
        val wm = getSystemService(WindowManager::class.java)
        val (realW, realH) = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            wm.currentWindowMetrics.bounds.let { it.width() to it.height() }
        } else {
            @Suppress("DEPRECATION") wm.defaultDisplay.getRealMetrics(metrics)
            metrics.widthPixels to metrics.heightPixels
        }
        width = 720.coerceAtMost(realW)
        height = (realH.toLong() * width / realW).toInt()
        reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2)
        display = mp.createVirtualDisplay("dumboo-share", width, height, resources.displayMetrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, reader!!.surface, null, handler)
        ScreenShare.active = true
        ScreenShare.acked = true
        Bridge.sendFeatures()
        handler?.post(tick)
    }

    /** About 6 frames a second, and only after the laptop confirmed the previous one (no backlog). */
    private val tick: Runnable = object : Runnable {
        override fun run() {
            val h = handler ?: return
            val now = System.currentTimeMillis()
            if (!ScreenShare.acked && now - ScreenShare.lastSent > 3000) ScreenShare.acked = true
            if (ScreenShare.acked) {
                reader?.acquireLatestImage()?.use { image ->
                    val plane = image.planes[0]
                    val rowWidth = plane.rowStride / plane.pixelStride
                    val bmp = Bitmap.createBitmap(rowWidth, height, Bitmap.Config.ARGB_8888)
                    bmp.copyPixelsFromBuffer(plane.buffer)
                    val cropped = if (rowWidth != width) Bitmap.createBitmap(bmp, 0, 0, width, height) else bmp
                    val out = ByteArrayOutputStream()
                    cropped.compress(Bitmap.CompressFormat.JPEG, 55, out)
                    if (cropped !== bmp) cropped.recycle()
                    bmp.recycle()
                    ScreenShare.acked = false
                    ScreenShare.lastSent = now
                    Bridge.sendFrame(out.toByteArray(), width, height, ++seq)
                }
            }
            h.postDelayed(this, 160)
        }
    }

    override fun onDestroy() {
        ScreenShare.active = false
        handler?.removeCallbacksAndMessages(null)
        display?.release()
        reader?.close()
        projection?.stop()
        thread?.quitSafely()
        projection = null
        Bridge.sendFeatures()
        super.onDestroy()
    }
}

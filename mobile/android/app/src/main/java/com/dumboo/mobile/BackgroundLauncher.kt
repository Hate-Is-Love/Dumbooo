package com.dumboo.mobile

import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.view.WindowManager

/**
 * Opens an app or screen while DUMBOO Mobile is in the background ("open WhatsApp" said to the laptop, or right
 * after another app opened). Android allows this for apps the owner lets "display over other apps"; newer Android
 * versions also want that app to be showing something, so a 1-pixel invisible overlay is shown for a moment.
 */
object BackgroundLauncher {
    private val main = Handler(Looper.getMainLooper())

    fun start(context: Context, intent: Intent) {
        main.post {
            val wm = context.getSystemService(WindowManager::class.java)
            val dot = View(context.applicationContext)
            val params = WindowManager.LayoutParams(1, 1, WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                    WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL, PixelFormat.TRANSLUCENT)
            params.gravity = Gravity.TOP or Gravity.START
            val shown = runCatching { wm.addView(dot, params); true }.getOrDefault(false)
            runCatching { context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
            if (shown) main.postDelayed({ runCatching { wm.removeView(dot) } }, 1500)
        }
    }
}

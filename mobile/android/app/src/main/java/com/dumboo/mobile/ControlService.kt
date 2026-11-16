package com.dumboo.mobile

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.Build
import android.os.Bundle
import android.util.DisplayMetrics
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONObject

/**
 * Lets the laptop tap, swipe, go Back/Home and type on this phone (Phase 4). Android only runs it after the owner
 * turns on “DUMBOO screen control” in Settings → Accessibility, and it only acts while the phone's screen is being
 * shared with the laptop (so the laptop can never act blind) and “Screen” is allowed in DUMBOO Mobile.
 */
class ControlService : AccessibilityService() {

    override fun onServiceConnected() {
        instance = this
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

    override fun onInterrupt() = Unit

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    private fun size(): Pair<Int, Int> {
        val wm = getSystemService(WindowManager::class.java)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val b = wm.currentWindowMetrics.bounds
            b.width() to b.height()
        } else {
            val m = DisplayMetrics()
            @Suppress("DEPRECATION") wm.defaultDisplay.getRealMetrics(m)
            m.widthPixels to m.heightPixels
        }
    }

    private fun gesture(points: List<Pair<Float, Float>>, ms: Long) {
        val (w, h) = size()
        val path = Path()
        points.forEachIndexed { i, (x, y) ->
            val px = x.coerceIn(0f, 1f) * (w - 1)
            val py = y.coerceIn(0f, 1f) * (h - 1)
            if (i == 0) path.moveTo(px, py) else path.lineTo(px, py)
        }
        dispatchGesture(GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, ms.coerceIn(1, 3000))).build(), null, null)
    }

    private fun typeText(text: String): Boolean {
        val node = rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: return false
        val current = if (node.isShowingHintText) "" else node.text?.toString().orEmpty()
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, current + text)
        }
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    companion object {
        @Volatile private var instance: ControlService? = null

        /** Accessibility services may open screens from the background. */
        fun startActivity(intent: android.content.Intent): Boolean {
            val service = instance ?: return false
            return runCatching { service.startActivity(intent); true }.getOrDefault(false)
        }

        fun perform(action: String, args: JSONObject): CommandResult {
            val service = instance ?: return CommandResult(false, "Turn on “DUMBOO screen control” on the phone: " +
                "Settings → Accessibility → DUMBOO Mobile.")
            if (!ScreenShare.active) return CommandResult(false, "Share the phone's screen first.")
            when (action) {
                "tap" -> {
                    val p = args.optDouble("x").toFloat() to args.optDouble("y").toFloat()
                    service.gesture(listOf(p), if (args.optBoolean("long")) 800 else 60)
                }
                "swipe" -> service.gesture(listOf(
                    args.optDouble("x1").toFloat() to args.optDouble("y1").toFloat(),
                    args.optDouble("x2").toFloat() to args.optDouble("y2").toFloat()), args.optLong("ms", 300))
                "global" -> {
                    val g = when (args.optString("action")) {
                        "back" -> GLOBAL_ACTION_BACK; "home" -> GLOBAL_ACTION_HOME
                        "recents" -> GLOBAL_ACTION_RECENTS; "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
                        "quick_settings" -> GLOBAL_ACTION_QUICK_SETTINGS
                        "lock" -> if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) GLOBAL_ACTION_LOCK_SCREEN
                        else return CommandResult(false, "Locking needs Android 9 or newer.")
                        else -> return CommandResult(false, "Unknown button.")
                    }
                    service.performGlobalAction(g)
                }
                "type" -> if (!service.typeText(args.optString("text").take(1000))) {
                    return CommandResult(false, "Tap a text box on the phone first.")
                }
                else -> return CommandResult(false, "Unknown screen action.")
            }
            return CommandResult(true, "Done.")
        }
    }
}

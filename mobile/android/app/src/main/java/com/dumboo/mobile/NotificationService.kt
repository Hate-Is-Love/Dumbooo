package com.dumboo.mobile

import android.app.Notification
import android.app.RemoteInput
import android.content.Intent
import android.os.Bundle
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap

/** Whether the app's screen is showing (set by MainActivity). */
object App {
    @Volatile var inForeground = false
    /** The visible DUMBOO screen, so commands can open apps straight away while it's on screen. */
    @Volatile var activity: java.lang.ref.WeakReference<android.app.Activity>? = null
    /** An app had to be offered as a notification: ask for "display over other apps" next time. */
    @Volatile var needsOverlay = false
}

/**
 * Shares this phone's notifications with the laptop and replies to messages from it (Phase 3). Android only runs
 * this after the owner grants "Notification access" in the phone's settings, and it only sends anything while
 * “Show my notifications on the laptop” is on.
 */
class NotificationService : NotificationListenerService() {

    override fun onListenerConnected() {
        instance = this
        Bridge.sendFeatures()
        if (Features.usable(this, "notifications")) {
            runCatching { activeNotifications }.getOrNull()?.takeLast(15)?.forEach { onNotificationPosted(it) }
        }
    }

    override fun onListenerDisconnected() {
        if (instance === this) instance = null
        posted.clear()
        Bridge.sendFeatures()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (!Features.usable(this, "notifications") || !worthSharing(sbn)) return
        posted[sbn.key] = sbn
        val extras = sbn.notification.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = (extras.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: extras.getCharSequence(Notification.EXTRA_TEXT))
            ?.toString().orEmpty()
        if (title.isBlank() && text.isBlank()) return
        Bridge.sendNotification(JSONObject()
            .put("key", sbn.key.take(300))
            .put("app", appLabel(sbn.packageName))
            .put("title", title.take(200))
            .put("text", text.take(500))
            .put("can_reply", replyAction(sbn.notification) != null))
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        if (posted.remove(sbn.key) != null && Features.usable(this, "notifications")) {
            Bridge.sendNotification(JSONObject().put("key", sbn.key.take(300)).put("removed", true))
        }
    }

    private fun worthSharing(sbn: StatusBarNotification): Boolean {
        val n = sbn.notification
        return sbn.packageName != packageName && !sbn.isOngoing &&
            (n.flags and Notification.FLAG_GROUP_SUMMARY) == 0 &&
            n.category != Notification.CATEGORY_TRANSPORT && n.category != Notification.CATEGORY_PROGRESS
    }

    private fun appLabel(pkg: String): String = runCatching {
        packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
    }.getOrDefault(pkg)

    companion object {
        @Volatile private var instance: NotificationService? = null
        private val posted = ConcurrentHashMap<String, StatusBarNotification>()

        private fun replyAction(n: Notification): Notification.Action? = n.actions?.firstOrNull { action ->
            action.remoteInputs?.any { it.allowFreeFormInput } == true
        }

        fun reply(key: String, text: String): CommandResult {
            val service = instance ?: return CommandResult(false, "Notification access is off on the phone.")
            val sbn = posted[key] ?: return CommandResult(false, "That message is no longer on the phone.")
            val action = replyAction(sbn.notification)
                ?: return CommandResult(false, "That app doesn't allow replying from the notification.")
            if (text.isBlank()) return CommandResult(false, "Nothing to send.")
            val intent = Intent()
            val results = Bundle()
            action.remoteInputs.filter { it.allowFreeFormInput }.forEach { results.putCharSequence(it.resultKey, text) }
            RemoteInput.addResultsToIntent(action.remoteInputs, intent, results)
            action.actionIntent.send(service, 0, intent)
            return CommandResult(true, "Reply sent.")
        }

        fun dismiss(key: String): CommandResult {
            val service = instance ?: return CommandResult(false, "Notification access is off on the phone.")
            service.cancelNotification(key)
            return CommandResult(true, "Dismissed.")
        }
    }
}

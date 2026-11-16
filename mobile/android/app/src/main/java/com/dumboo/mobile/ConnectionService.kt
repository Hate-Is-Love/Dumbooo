package com.dumboo.mobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/**
 * Keeps the link to DUMBOO open while the app is in the background, so reminders arrive as notifications.
 * Android shows a small persistent notification while it runs; "Disconnect" in the app stops it.
 */
class ConnectionService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannels(this)
        ServiceCompat.startForeground(this, ONGOING_ID, ongoing("Connected to DUMBOO"),
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else 0)
        Bridge.start(this)
        scope.launch { Bridge.reminders.collect { notifyReminder(it) } }
        scope.launch {
            Bridge.state.collect { state ->
                val text = when (state) {
                    is LinkState.Ready -> "Connected to DUMBOO as ${state.user}"
                    is LinkState.Connecting -> "Connecting to the laptop…"
                    is LinkState.Failed -> state.message
                    LinkState.Offline -> "Laptop offline · retrying"
                    LinkState.NotPaired -> { stopSelf(); "Not paired" }
                }
                NotificationManagerCompat.from(this@ConnectionService).let { manager ->
                    if (manager.areNotificationsEnabled()) {
                        try { manager.notify(ONGOING_ID, ongoing(text)) } catch (_: SecurityException) { }
                    }
                }
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        scope.cancel()
        Bridge.stop()
        super.onDestroy()
    }

    private fun openApp(): PendingIntent = PendingIntent.getActivity(this, 0,
        Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE)

    private fun ongoing(text: String): Notification = NotificationCompat.Builder(this, CHANNEL_LINK)
        .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
        .setContentTitle("DUMBOO Mobile")
        .setContentText(text)
        .setOngoing(true)
        .setSilent(true)
        .setContentIntent(openApp())
        .build()

    private fun notifyReminder(reminder: Reminder) {
        val notification = NotificationCompat.Builder(this, CHANNEL_REMINDERS)
            .setSmallIcon(android.R.drawable.ic_popup_reminder)
            .setContentTitle(if (reminder.missed) "Missed: ${reminder.title}" else reminder.title)
            .setContentText(reminder.text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(reminder.text))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setAutoCancel(true)
            .setContentIntent(openApp())
            .build()
        try {
            NotificationManagerCompat.from(this).notify(reminder.title.hashCode(), notification)
        } catch (_: SecurityException) {
            // notifications not allowed: the reminder still shows in the app
        }
    }

    companion object {
        private const val ONGOING_ID = 1
        const val CHANNEL_LINK = "link"
        const val CHANNEL_REMINDERS = "reminders"

        fun createChannels(context: Context) {
            val manager = context.getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_LINK, "Connection", NotificationManager.IMPORTANCE_MIN))
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_REMINDERS, "Reminders", NotificationManager.IMPORTANCE_HIGH))
        }

        fun start(context: Context) {
            context.startForegroundService(Intent(context, ConnectionService::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, ConnectionService::class.java))
        }
    }
}

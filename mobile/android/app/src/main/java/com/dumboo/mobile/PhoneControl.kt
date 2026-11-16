package com.dumboo.mobile

import android.Manifest
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.SearchManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.AlarmClock
import android.provider.ContactsContract
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.provider.MediaStore
import android.provider.Settings
import android.view.KeyEvent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject

/**
 * What the laptop may do to this phone (Phase 3). Every feature is OFF until switched on in Settings → "What the
 * laptop may do", and most also need Android's own permission. The laptop is told which features are on; every
 * command is checked again here before anything happens.
 */
object Features {
    val ALL = listOf("notifications", "clipboard", "files", "calls", "music", "find_phone", "open_apps", "screen")

    val LABELS = mapOf(
        "notifications" to "Show my notifications on the laptop and reply from it",
        "clipboard" to "Clipboard: copy text between phone and laptop",
        "files" to "Files: send and receive files",
        "calls" to "Calls: the laptop can start a call",
        "music" to "Music & volume: play, pause, skip, volume",
        "find_phone" to "Find my phone: the laptop can make it ring",
        "open_apps" to "Open apps on this phone",
        "screen" to "Screen: share this screen with the laptop and let it tap",
    )

    private fun prefs(context: Context) = context.getSharedPreferences("dumboo_features", Context.MODE_PRIVATE)

    fun on(context: Context, feature: String): Boolean = prefs(context).getBoolean(feature, false)

    fun set(context: Context, feature: String, on: Boolean) {
        prefs(context).edit().putBoolean(feature, on).apply()
        Bridge.sendFeatures()
    }

    /** What actually works now: switched on here AND Android's permission is there. */
    fun usable(context: Context, feature: String): Boolean = on(context, feature) && when (feature) {
        "notifications" -> notificationAccess(context)
        "calls" -> granted(context, Manifest.permission.CALL_PHONE)
        else -> true
    }

    fun snapshot(context: Context): JSONObject =
        JSONObject().apply { ALL.forEach { put(it, usable(context, it)) } }

    fun granted(context: Context, permission: String) =
        ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED

    fun notificationAccess(context: Context): Boolean =
        NotificationManagerCompat.getEnabledListenerPackages(context).contains(context.packageName)

    fun controlServiceOn(context: Context): Boolean {
        val enabled = Settings.Secure.getString(context.contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
        val me = ComponentName(context, ControlService::class.java).flattenToString()
        return enabled.split(':').any { it.equals(me, ignoreCase = true) }
    }

    /** Android blocks apps in the background from opening screens (calls, apps) unless this is allowed. */
    fun canOpenFromBackground(context: Context) = Settings.canDrawOverlays(context) || controlServiceOn(context)
}

data class CommandResult(val ok: Boolean, val message: String, val data: JSONObject = JSONObject())

/** Runs the laptop's commands on this phone. */
object PhoneControl {
    private val main = Handler(Looper.getMainLooper())
    private var player: MediaPlayer? = null
    private var savedAlarmVolume = -1
    private const val RING_NOTIFICATION = 40
    private const val ACTION_NOTIFICATION = 41
    const val CHANNEL_ACTIONS = "laptop_actions"

    private val NEEDS = mapOf(
        "call" to "calls", "dial" to "calls", "play" to "music", "media" to "music", "volume" to "music",
        "ring" to "find_phone", "stop_ring" to "find_phone", "open_app" to "open_apps", "clipboard.set" to "clipboard",
        "reply" to "notifications", "dismiss" to "notifications", "screen.start" to "screen", "screen.stop" to "screen",
        "tap" to "screen", "swipe" to "screen", "global" to "screen", "type" to "screen",
        "torch" to "open_apps", "alarm" to "open_apps", "timer" to "open_apps", "navigate" to "open_apps",
        "search" to "open_apps", "sms" to "calls",
    )

    /**
     * Commands the owner just spoke or typed into DUMBOO Mobile, for this phone ("call mom", "set an alarm for 7").
     * The "what the laptop may do" switches limit the laptop, not the owner, so these skip them, but only within
     * 90 seconds of the owner sending a message from this phone. Android's own permissions still apply.
     */
    private val OWN = setOf("call", "dial", "play", "media", "volume", "open_app", "torch", "alarm", "timer",
        "navigate", "search", "sms")

    fun run(context: Context, action: String, args: JSONObject): CommandResult {
        if (action == "status") return CommandResult(true, "", status(context))
        val feature = NEEDS[action] ?: return CommandResult(false, "This phone doesn't know “$action”.")
        val own = args.optBoolean("own") && action in OWN && Bridge.ownerAskedRecently()
        if (!own && !Features.usable(context, feature)) {
            return CommandResult(false, "“${feature.replace('_', ' ')}” is switched off on the phone " +
                "(DUMBOO Mobile → Settings → What the laptop may do).")
        }
        return try {
            when (action) {
                "call" -> call(context, args.optString("who"), own, args.optString("pick"))
                "dial" -> dial(context, args.optString("who"), args.optString("who"), own)
                "torch" -> torch(context, args.optBoolean("on"))
                "alarm" -> launch(context, Intent(AlarmClock.ACTION_SET_ALARM)
                    .putExtra(AlarmClock.EXTRA_HOUR, args.optInt("hour")).putExtra(AlarmClock.EXTRA_MINUTES, args.optInt("minute"))
                    .putExtra(AlarmClock.EXTRA_MESSAGE, "DUMBOO").putExtra(AlarmClock.EXTRA_SKIP_UI, true),
                    "Set an alarm", "Alarm set for %02d:%02d.".format(args.optInt("hour"), args.optInt("minute")))
                "timer" -> launch(context, Intent(AlarmClock.ACTION_SET_TIMER)
                    .putExtra(AlarmClock.EXTRA_LENGTH, args.optInt("seconds")).putExtra(AlarmClock.EXTRA_MESSAGE, "DUMBOO")
                    .putExtra(AlarmClock.EXTRA_SKIP_UI, true), "Start a timer", "Timer started.")
                "navigate" -> launch(context, Intent(Intent.ACTION_VIEW,
                    Uri.parse("google.navigation:q=" + Uri.encode(args.optString("place")))).let {
                        if (it.resolveActivity(context.packageManager) != null) it
                        else Intent(Intent.ACTION_VIEW, Uri.parse("geo:0,0?q=" + Uri.encode(args.optString("place"))))
                    }, "Directions to ${args.optString("place")}", "Directions to ${args.optString("place")}.")
                "search" -> launch(context, Intent(Intent.ACTION_VIEW, Uri.parse(
                    "https://www.youtube.com/results?search_query=" + Uri.encode(args.optString("query")))),
                    "Search YouTube", "Searching YouTube for ${args.optString("query")}.")
                "sms" -> sms(context, args.optString("who"), args.optString("text"), args.optString("pick"))
                "play" -> play(context, args.optString("query"), args.optString("video"))
                "media" -> media(context, args.optString("key"))
                "volume" -> volume(context, args.optString("direction"))
                "ring" -> ring(context)
                "stop_ring" -> { stopRing(context); CommandResult(true, "Stopped ringing.") }
                "open_app" -> openApp(context, args.optString("name"), args.optString("pick"))
                "clipboard.set" -> setClipboard(context, args.optString("text"))
                "reply" -> NotificationService.reply(args.optString("key"), args.optString("text"))
                "dismiss" -> NotificationService.dismiss(args.optString("key"))
                "screen.start" -> ScreenShare.ask(context)
                "screen.stop" -> { ScreenShare.stop(context); CommandResult(true, "Stopped sharing the phone screen.") }
                else -> ControlService.perform(action, args)
            }
        } catch (e: SecurityException) {
            CommandResult(false, "Android didn't allow that: ${e.message ?: "permission missing"}.")
        } catch (e: Exception) {
            CommandResult(false, "The phone couldn't do that: ${e.message ?: e.javaClass.simpleName}.")
        }
    }

    // -- status ----------------------------------------------------------------------------------------------------
    fun status(context: Context): JSONObject {
        val battery = context.getSystemService(BatteryManager::class.java)
        val audio = context.getSystemService(AudioManager::class.java)
        val connectivity = context.getSystemService(ConnectivityManager::class.java)
        val caps = connectivity.getNetworkCapabilities(connectivity.activeNetwork)
        val network = when {
            caps == null -> "offline"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "mobile data"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN) -> "vpn"
            else -> "online"
        }
        return JSONObject()
            .put("battery", battery.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY))
            .put("charging", battery.isCharging)
            .put("network", network)
            .put("ringer", when (audio.ringerMode) {
                AudioManager.RINGER_MODE_SILENT -> "silent"; AudioManager.RINGER_MODE_VIBRATE -> "vibrate"
                else -> "normal" })
            .put("model", "${Build.MANUFACTURER} ${Build.MODEL}".take(60))
    }

    // -- opening screens from the background -------------------------------------------------------------------------
    /** Start an activity; if Android blocks it because the app is in the background, offer it as a notification. */
    internal fun launch(context: Context, intent: Intent, title: String, done: String): CommandResult {
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val activity = App.activity?.get()
        if (App.inForeground && activity != null) {             // DUMBOO is on screen: just open it
            main.post { runCatching { activity.startActivity(intent) } }
            return CommandResult(true, done)
        }
        if (android.provider.Settings.canDrawOverlays(context)) {
            BackgroundLauncher.start(context, intent)             // "display over other apps" is allowed
            return CommandResult(true, done)
        }
        if (Features.controlServiceOn(context) && ControlService.startActivity(intent)) {
            return CommandResult(true, done)
        }
        val pending = PendingIntent.getActivity(context, title.hashCode(), intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(context, CHANNEL_ACTIONS)
            .setSmallIcon(android.R.drawable.ic_menu_send)
            .setContentTitle("From your laptop")
            .setContentText("$title · tap to continue")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(pending)
            .build()
        notify(context, ACTION_NOTIFICATION, n)
        App.needsOverlay = true
        return CommandResult(true, "$title: tap the notification on the phone. To open things straight away, " +
            "allow DUMBOO Mobile to “display over other apps” (the app asks next time you open it).")
    }

    private fun notify(context: Context, id: Int, n: android.app.Notification) {
        try { NotificationManagerCompat.from(context).notify(id, n) } catch (_: SecurityException) { }
    }

    // -- calls -------------------------------------------------------------------------------------------------------
    private fun call(context: Context, who: String, own: Boolean = false, pick: String = ""): CommandResult {
        if (pick.contains('|')) return dial(context, pick.substringAfter('|'), pick.substringBefore('|'), own)
        if (who.isBlank()) return CommandResult(false, "Who should I call?")
        if (!Features.granted(context, Manifest.permission.READ_CONTACTS)) {
            return CommandResult(false, "Allow DUMBOO Mobile to read contacts (Settings → Calls) to call by name.")
        }
        val matches = findContacts(context, who)
        if (matches.isEmpty()) return CommandResult(false, "No contact called “$who” on the phone.")
        if (matches.size > 1) return choices("I found ${matches.size} matches for “$who”.", matches)
        val (name, number, _) = matches.first()
        return dial(context, number, name, own)
    }

    /** Several matches: DUMBOO reads them out and you pick one by voice ("the second one"). */
    private fun choices(question: String, items: List<Triple<String, String, String>>): CommandResult {
        val arr = org.json.JSONArray()
        items.take(6).forEach { (name, value, label) -> arr.put(JSONObject().put("id", "$name|$value").put("label", label)) }
        return CommandResult(false, question, JSONObject().put("choices", arr))
    }

    private fun dial(context: Context, number: String, name: String, own: Boolean = false): CommandResult {
        val clean = number.filter { it.isDigit() || it == '+' }
        if (clean.length < 3) return CommandResult(false, "That isn't a phone number.")
        if (!Features.granted(context, Manifest.permission.CALL_PHONE)) {
            if (!own) return CommandResult(false, "Allow phone calls for DUMBOO Mobile (Settings → Calls).")
            return launch(context, Intent(Intent.ACTION_DIAL, Uri.fromParts("tel", clean, null)), "Call $name",
                "Opened the dialer for $name: tap Call. (Switch on “Calls” in Settings to call straight away.)")
        }
        val intent = Intent(Intent.ACTION_CALL, Uri.fromParts("tel", clean, null))
        return launch(context, intent, "Call $name", "Calling $name…")
    }

    /** Opens a text to the contact with the message filled in; the owner taps Send (nothing is sent silently). */
    private fun sms(context: Context, who: String, text: String, pick: String = ""): CommandResult {
        val digits = who.filter { it.isDigit() || it == '+' }
        val (name, number) = if (pick.contains('|')) {
            pick.substringBefore('|') to pick.substringAfter('|')
        } else if (digits.length >= 3 && digits.length >= who.count { !it.isWhitespace() } - 1) {
            who to digits
        } else {
            if (!Features.granted(context, Manifest.permission.READ_CONTACTS)) {
                return CommandResult(false, "Allow DUMBOO Mobile to read contacts (Settings → Calls) to text by name.")
            }
            val matches = findContacts(context, who)
            if (matches.isEmpty()) return CommandResult(false, "No contact called “$who” on the phone.")
            if (matches.size > 1) return choices("I found ${matches.size} matches for “$who”.", matches)
            matches.first().let { it.first to it.second }
        }
        val intent = Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + Uri.encode(number))).putExtra("sms_body", text)
        return launch(context, intent, "Text $name", "Opened a text to $name with your message: tap Send.")
    }

    private fun torch(context: Context, on: Boolean): CommandResult {
        val cameras = context.getSystemService(CameraManager::class.java)
        val id = cameras.cameraIdList.firstOrNull {
            cameras.getCameraCharacteristics(it).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
        } ?: return CommandResult(false, "This phone has no torch.")
        cameras.setTorchMode(id, on)
        return CommandResult(true, if (on) "Torch on." else "Torch off.")
    }

    private fun findContact(context: Context, who: String): Pair<String, String>? =
        findContacts(context, who).firstOrNull()?.let { it.first to it.second }

    /** The best-matching contacts as (name, number, label): several only when they match equally well. */
    private fun findContacts(context: Context, who: String): List<Triple<String, String, String>> {
        val wanted = who.trim().lowercase()
        val cursor = context.contentResolver.query(ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            arrayOf(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME, ContactsContract.CommonDataKinds.Phone.NUMBER,
                ContactsContract.CommonDataKinds.Phone.TYPE, ContactsContract.CommonDataKinds.Phone.LABEL),
            null, null, null) ?: return emptyList()
        val scored = mutableListOf<Pair<Int, Triple<String, String, String>>>()
        cursor.use {
            while (it.moveToNext()) {
                val name = it.getString(0) ?: continue
                val number = it.getString(1) ?: continue
                val lower = name.lowercase()
                val score = when {
                    lower == wanted -> 3
                    lower.split(' ').any { part -> part == wanted } -> 2
                    lower.contains(wanted) -> 1
                    else -> 0
                }
                if (score == 0) continue
                val type = ContactsContract.CommonDataKinds.Phone.getTypeLabel(context.resources, it.getInt(2),
                    it.getString(3) ?: "").toString().lowercase()
                scored += score to Triple(name, number, "$name, $type, ending ${number.filter { c -> c.isDigit() }.takeLast(4)}")
            }
        }
        val best = scored.maxOfOrNull { it.first } ?: return emptyList()
        return scored.filter { it.first == best }.map { it.second }
            .distinctBy { it.first.lowercase() + it.second.filter { c -> c.isDigit() }.takeLast(9) }
    }

    // -- music -------------------------------------------------------------------------------------------------------
    private fun play(context: Context, query: String, video: String = ""): CommandResult {
        if (query.isBlank()) return media(context, "play")
        if (video.matches(Regex("[A-Za-z0-9_-]{11}"))) {
            // The laptop found the song: open it so it starts playing at once (YouTube Music, else YouTube).
            val pm = context.packageManager
            val music = Intent(Intent.ACTION_VIEW, Uri.parse("https://music.youtube.com/watch?v=$video"))
                .setPackage("com.google.android.apps.youtube.music")
            val youtube = Intent(Intent.ACTION_VIEW, Uri.parse("vnd.youtube:$video"))
            val intent = listOf(music, youtube).firstOrNull { it.resolveActivity(pm) != null }
                ?: Intent(Intent.ACTION_VIEW, Uri.parse("https://www.youtube.com/watch?v=$video"))
            return launch(context, intent, "Play $query", "Playing “$query”.")
        }
        val search = Intent(MediaStore.INTENT_ACTION_MEDIA_PLAY_FROM_SEARCH)
            .putExtra(SearchManager.QUERY, query)
            .putExtra(MediaStore.EXTRA_MEDIA_FOCUS, "vnd.android.cursor.item/*")
        val intent = if (search.resolveActivity(context.packageManager) != null) search
        else Intent(Intent.ACTION_VIEW, Uri.parse("https://www.youtube.com/results?search_query=" + Uri.encode(query)))
        return launch(context, intent, "Play $query", "Playing “$query” on the phone.")
    }

    private fun media(context: Context, key: String): CommandResult {
        val code = when (key) {
            "play" -> KeyEvent.KEYCODE_MEDIA_PLAY; "pause" -> KeyEvent.KEYCODE_MEDIA_PAUSE
            "playpause" -> KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE; "next" -> KeyEvent.KEYCODE_MEDIA_NEXT
            "previous" -> KeyEvent.KEYCODE_MEDIA_PREVIOUS; "stop" -> KeyEvent.KEYCODE_MEDIA_STOP
            else -> return CommandResult(false, "Unknown media key “$key”.")
        }
        val audio = context.getSystemService(AudioManager::class.java)
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, code))
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_UP, code))
        return CommandResult(true, mapOf("play" to "Playing.", "pause" to "Paused.", "next" to "Next track.",
            "previous" to "Previous track.", "stop" to "Stopped.").getOrDefault(key, "Done."))
    }

    private fun volume(context: Context, direction: String): CommandResult {
        val adjust = when (direction) {
            "up" -> AudioManager.ADJUST_RAISE; "down" -> AudioManager.ADJUST_LOWER
            "mute" -> AudioManager.ADJUST_MUTE; "unmute" -> AudioManager.ADJUST_UNMUTE
            else -> return CommandResult(false, "Volume up, down, mute or unmute?")
        }
        val audio = context.getSystemService(AudioManager::class.java)
        audio.adjustStreamVolume(AudioManager.STREAM_MUSIC, adjust, AudioManager.FLAG_SHOW_UI)
        val level = audio.getStreamVolume(AudioManager.STREAM_MUSIC) * 100 /
            audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC).coerceAtLeast(1)
        return CommandResult(true, "Phone volume $level%.", JSONObject().put("volume", level))
    }

    // -- find my phone ----------------------------------------------------------------------------------------------------
    @Synchronized
    private fun ring(context: Context): CommandResult {
        stopRing(context)
        val audio = context.getSystemService(AudioManager::class.java)
        savedAlarmVolume = audio.getStreamVolume(AudioManager.STREAM_ALARM)
        audio.setStreamVolume(AudioManager.STREAM_ALARM, audio.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0)
        val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
        player = MediaPlayer().apply {
            setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
            setDataSource(context, uri)
            isLooping = true
            prepare()
            start()
        }
        val stop = PendingIntent.getService(context, 1,
            Intent(context, ConnectionService::class.java).setAction(ConnectionService.ACTION_STOP_RING),
            PendingIntent.FLAG_IMMUTABLE)
        notify(context, RING_NOTIFICATION, NotificationCompat.Builder(context, CHANNEL_ACTIONS)
            .setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
            .setContentTitle("Your laptop is looking for this phone")
            .setContentText("Tap to stop ringing")
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setOngoing(true)
            .setContentIntent(stop)
            .addAction(0, "STOP", stop)
            .build())
        main.postDelayed({ stopRing(context) }, 90_000)          // never rings forever
        return CommandResult(true, "Your phone is ringing.")
    }

    @Synchronized
    fun stopRing(context: Context) {
        player?.let { runCatching { it.stop() }; it.release() }
        player = null
        if (savedAlarmVolume >= 0) {
            runCatching {
                context.getSystemService(AudioManager::class.java)
                    .setStreamVolume(AudioManager.STREAM_ALARM, savedAlarmVolume, 0)
            }
            savedAlarmVolume = -1
        }
        context.getSystemService(NotificationManager::class.java).cancel(RING_NOTIFICATION)
    }

    // -- apps and clipboard ------------------------------------------------------------------------------------------------
    private fun openApp(context: Context, name: String, pick: String = ""): CommandResult {
        val pm = context.packageManager
        if (pick.contains('|')) {
            val intent = pm.getLaunchIntentForPackage(pick.substringAfter('|'))
                ?: return CommandResult(false, "${pick.substringBefore('|')} can't be opened.")
            return launch(context, intent, "Open ${pick.substringBefore('|')}", "Opened ${pick.substringBefore('|')}.")
        }
        val wanted = name.trim().lowercase().removeSuffix(" app")
        if (wanted.isBlank()) return CommandResult(false, "Which app?")
        val apps = pm.queryIntentActivities(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), 0)
            .map { it.activityInfo.packageName to it.loadLabel(pm).toString() }.distinctBy { it.first }
        val exact = apps.filter { it.second.lowercase() == wanted }
        val matches = exact.ifEmpty { apps.filter { it.second.lowercase().contains(wanted) } }
            .ifEmpty { apps.filter { wanted.split(' ').all { w -> w in it.second.lowercase() } } }
        if (matches.isEmpty()) return CommandResult(false, "No app called “$name” on the phone.")
        if (matches.size > 1) return choices("I found ${matches.size} apps for “$name”.",
            matches.map { Triple(it.second, it.first, it.second) })
        val (pkg, label) = matches.first()
        val intent = pm.getLaunchIntentForPackage(pkg) ?: return CommandResult(false, "$label can't be opened.")
        return launch(context, intent, "Open $label", "Opened $label.")
    }

    private fun setClipboard(context: Context, text: String): CommandResult {
        if (text.isEmpty()) return CommandResult(false, "Nothing to copy.")
        main.post {
            context.getSystemService(ClipboardManager::class.java)
                .setPrimaryClip(ClipData.newPlainText("From the laptop", text.take(20000)))
        }
        return CommandResult(true, "Copied to the phone's clipboard.")
    }
}

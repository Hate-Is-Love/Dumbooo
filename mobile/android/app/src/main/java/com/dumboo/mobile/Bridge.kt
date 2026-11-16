package com.dumboo.mobile

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Build
import android.util.Base64
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

sealed class LinkState {
    object NotPaired : LinkState()
    object Offline : LinkState()
    data class Connecting(val host: String) : LinkState()
    data class Ready(val user: String, val host: String) : LinkState()
    data class Failed(val message: String) : LinkState()
}

data class ChatItem(val id: Long, val fromMe: Boolean, val text: String, val pending: Boolean = false,
                    val error: Boolean = false, val heard: String? = null)

data class AgendaTask(val title: String, val due: String?, val hasTime: Boolean, val overdue: Boolean,
                      val priority: String, val remindAt: String?)

data class AgendaEvent(val title: String, val startsAt: String, val reminder: Boolean)

data class Agenda(val tasks: List<AgendaTask>, val events: List<AgendaEvent>, val loadedAt: Long)

data class Reminder(val title: String, val text: String, val missed: Boolean)

data class Spoken(val text: String)

/** One picture of the laptop screen, with the laptop's real resolution (for mapping taps to clicks). */
data class ScreenFrame(val bitmap: Bitmap, val width: Int, val height: Int)

/**
 * The secure link to DUMBOO on the laptop (docs/PHONE_BRIDGE.md): a WebSocket over TLS that trusts only the paired
 * laptop's certificate, then a signed challenge that proves this is the paired phone. Reconnects by itself.
 */
object Bridge {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val ids = AtomicLong(1)
    private var socket: WebSocket? = null
    private var reconnectJob: Job? = null
    private var appContext: Context? = null
    private var pairing: PairingOffer? = null
    private var pairingResult: ((String?) -> Unit)? = null
    private var wanted = false
    private var hostIndex = 0
    private var backoffS = 2L

    private val _state = MutableStateFlow<LinkState>(LinkState.Offline)
    val state: StateFlow<LinkState> = _state.asStateFlow()
    private val _chat = MutableStateFlow<List<ChatItem>>(emptyList())
    val chat: StateFlow<List<ChatItem>> = _chat.asStateFlow()
    private val _agenda = MutableStateFlow<Agenda?>(null)
    val agenda: StateFlow<Agenda?> = _agenda.asStateFlow()
    private val _reminders = MutableSharedFlow<Reminder>(extraBufferCapacity = 16)
    val reminders: SharedFlow<Reminder> = _reminders.asSharedFlow()
    private val _speak = MutableSharedFlow<Spoken>(extraBufferCapacity = 8)
    val speak: SharedFlow<Spoken> = _speak.asSharedFlow()
    private val voiceIds = mutableSetOf<Long>()
    private val _laptopControl = MutableStateFlow(false)
    /** True when the laptop allows this phone to control it (switched on there, with the password). */
    val laptopControl: StateFlow<Boolean> = _laptopControl.asStateFlow()
    private val _screen = MutableStateFlow<ScreenFrame?>(null)
    val screen: StateFlow<ScreenFrame?> = _screen.asStateFlow()
    @Volatile private var screenWanted = false
    private var screenWidth = 1024

    // -- connecting ---------------------------------------------------------------------------------------------
    fun start(context: Context) {
        appContext = context.applicationContext
        if (Store.laptop(context) == null) {
            _state.value = LinkState.NotPaired
            return
        }
        wanted = true
        if (socket == null) open()
    }

    fun stop() {
        wanted = false
        reconnectJob?.cancel()
        socket?.close(1000, "bye")
        socket = null
        _state.value = if (appContext?.let { Store.laptop(it) } == null) LinkState.NotPaired else LinkState.Offline
    }

    /** Pair with the laptop from its QR code. [done] gets null on success or a message to show. */
    fun pair(context: Context, offer: PairingOffer, done: (String?) -> Unit) {
        appContext = context.applicationContext
        stop()
        DeviceKeys.ensure()
        pairing = offer
        pairingResult = done
        wanted = true
        hostIndex = 0
        open()
    }

    private fun client(fingerprint: String): OkHttpClient {
        val trust = PinnedTrustManager(fingerprint)
        return OkHttpClient.Builder()
            .sslSocketFactory(trust.socketFactory(), trust)
            .hostnameVerifier { _, _ -> true }      // identity is checked by the certificate pin instead
            .connectTimeout(8, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.SECONDS)
            .pingInterval(25, TimeUnit.SECONDS)
            .build()
    }

    private fun open() {
        val context = appContext ?: return
        val offer = pairing
        val laptop = Store.laptop(context)
        val hosts = offer?.hosts ?: laptop?.hosts ?: return
        val port = offer?.port ?: laptop!!.port
        val fingerprint = offer?.fingerprint ?: laptop!!.fingerprint
        if (hosts.isEmpty()) {
            fail("No laptop address. Add one in Settings.")
            return
        }
        val host = hosts[hostIndex % hosts.size]
        _state.value = LinkState.Connecting(host)
        val request = Request.Builder().url("wss://$host:$port/").build()
        socket = client(fingerprint).newWebSocket(request, Listener(host, fingerprint))
    }

    private fun scheduleReconnect() {
        if (!wanted) return
        reconnectJob?.cancel()
        reconnectJob = scope.launch {
            delay(backoffS * 1000)
            backoffS = (backoffS * 2).coerceAtMost(60)
            hostIndex++
            open()
        }
    }

    private fun fail(message: String) {
        _state.value = LinkState.Failed(message)
    }

    private class Listener(val host: String, val fingerprint: String) : WebSocketListener() {
        override fun onMessage(webSocket: WebSocket, text: String) {
            val msg = try { JSONObject(text) } catch (e: Exception) { return }
            handle(webSocket, host, fingerprint, msg)
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (webSocket !== socket) return
            socket = null
            val message = t.message ?: "connection failed"
            pairingResult?.let {
                // While pairing, try the next address from the QR code before giving up.
                val offer = pairing
                if (offer != null && hostIndex < offer.hosts.size - 1) {
                    hostIndex++
                    open()
                    return
                }
                pairing = null
                pairingResult = null
                wanted = false
                fail("Couldn't reach the laptop: $message")
                it("Couldn't reach the laptop ($message). Check that both are on the same network.")
                return
            }
            fail(if (message.contains("fingerprint")) message else "Laptop not reachable. Retrying…")
            scheduleReconnect()
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            if (webSocket !== socket) return
            socket = null
            if (code == 1008 && reason == "removed") {
                appContext?.let { Store.forget(it) }
                wanted = false
                _state.value = LinkState.NotPaired
                return
            }
            _state.value = LinkState.Offline
            scheduleReconnect()
        }
    }

    // -- protocol --------------------------------------------------------------------------------------------------
    private fun handle(ws: WebSocket, host: String, fingerprint: String, msg: JSONObject) {
        when (msg.optString("t")) {
            "hello" -> {
                val nonce = Base64.decode(msg.getString("nonce"), Base64.DEFAULT)
                val sig = DeviceKeys.signChallenge(nonce, fingerprint)
                val offer = pairing
                val out = if (offer != null) {
                    JSONObject().put("t", "pair").put("code", offer.code).put("name", deviceName())
                        .put("pub", Base64.encodeToString(DeviceKeys.publicKeyDer(), Base64.NO_WRAP)).put("sig", sig)
                } else {
                    JSONObject().put("t", "auth").put("device", Store.laptop(appContext!!)!!.deviceId).put("sig", sig)
                }
                ws.send(out.toString())
            }
            "ready" -> {
                backoffS = 2
                val user = msg.optString("user")
                val offer = pairing
                if (offer != null) {
                    Store.saveLaptop(appContext!!, PairedLaptop(offer.hosts, offer.port, offer.fingerprint,
                        msg.getInt("device"), offer.laptopName, user))
                    pairing = null
                    pairingResult?.invoke(null)
                    pairingResult = null
                } else {
                    // Remember which address worked, so it's tried first next time.
                    val context = appContext!!
                    val laptop = Store.laptop(context)
                    if (laptop != null && laptop.hosts.firstOrNull() != host) {
                        Store.setHosts(context, listOf(host) + laptop.hosts.filter { it != host })
                    }
                }
                _state.value = LinkState.Ready(user, host)
                _laptopControl.value = msg.optJSONObject("caps")?.optBoolean("laptop_control") == true
                requestAgenda()
                if (screenWanted && _laptopControl.value) startScreen(screenWidth)
            }
            "error" -> {
                val code = msg.optString("code")
                val message = msg.optString("message", "Error")
                if (pairing != null && pairingResult != null) {
                    pairingResult?.invoke(message)
                    pairing = null
                    pairingResult = null
                    wanted = false
                } else if (code in setOf("unknown_device", "bad_signature")) {
                    appContext?.let { Store.forget(it) }
                    wanted = false
                    _state.value = LinkState.NotPaired
                } else if (code in setOf("not_allowed", "rate_limit", "bad_request", "internal")) {
                    addDumboo(message, error = true)
                } else {
                    fail(message)
                }
            }
            "transcript" -> {
                val id = msg.optLong("id")
                _chat.update { list -> list.map { if (it.id == id) it.copy(text = msg.optString("text"), heard = "🎙") else it } }
            }
            "answer.delta" -> upsertAnswer(msg.optLong("id"), msg.optString("text"), pending = true)
            "answer" -> {
                val id = msg.optLong("id")
                upsertAnswer(id, msg.optString("text"), pending = false)
                val speech = msg.optString("speech")
                if (speech.isNotBlank() && (id in voiceIds || appContext?.let { Store.speakAnswers(it) } == true)) {
                    _speak.tryEmit(Spoken(speech))
                }
                voiceIds.remove(id)
            }
            "answer.error" -> upsertAnswer(msg.optLong("id"), msg.optString("message", "Failed"), pending = false,
                error = true)
            "agenda" -> _agenda.value = parseAgenda(msg)
            "caps" -> {
                _laptopControl.value = msg.optJSONObject("caps")?.optBoolean("laptop_control") == true
                if (!_laptopControl.value) { screenWanted = false; _screen.value = null }
            }
            "screen.frame" -> {
                val bytes = Base64.decode(msg.getString("img"), Base64.DEFAULT)
                BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.let {
                    _screen.value = ScreenFrame(it, msg.optInt("w"), msg.optInt("h"))
                }
                // confirm the frame: the laptop sends the next one only then (no backlog on a slow network)
                ws.send(JSONObject().put("t", "screen.ack").put("seq", msg.optInt("seq")).toString())
            }
            "reminder" -> _reminders.tryEmit(Reminder(msg.optString("title"), msg.optString("text"),
                msg.optBoolean("missed")))
        }
    }

    private fun upsertAnswer(questionId: Long, text: String, pending: Boolean, error: Boolean = false) {
        val answerId = -questionId                 // the answer to message n is item -n
        _chat.update { list ->
            if (list.any { it.id == answerId }) {
                list.map { if (it.id == answerId) it.copy(text = text, pending = pending, error = error) else it }
            } else {
                list + ChatItem(answerId, fromMe = false, text = text, pending = pending, error = error)
            }
        }
    }

    private fun addDumboo(text: String, error: Boolean = false) {
        _chat.update { it + ChatItem(-ids.incrementAndGet(), fromMe = false, text = text, error = error) }
    }

    private fun parseAgenda(msg: JSONObject): Agenda {
        val tasks = msg.getJSONArray("tasks").let { a ->
            List(a.length()) { i ->
                val t = a.getJSONObject(i)
                AgendaTask(t.getString("title"), t.optString("due").takeIf { it.isNotBlank() && it != "null" },
                    t.optBoolean("has_time"), t.optBoolean("overdue"), t.optString("priority", "medium"),
                    t.optString("remind_at").takeIf { it.isNotBlank() && it != "null" })
            }
        }
        val events = msg.getJSONArray("events").let { a ->
            List(a.length()) { i ->
                val e = a.getJSONObject(i)
                AgendaEvent(e.getString("title"), e.getString("starts_at"), e.optBoolean("reminder"))
            }
        }
        return Agenda(tasks, events, System.currentTimeMillis())
    }

    // -- sending ----------------------------------------------------------------------------------------------------
    private fun ready(): Boolean = _state.value is LinkState.Ready && socket != null

    fun sendChat(text: String) {
        val clean = text.trim()
        if (clean.isEmpty()) return
        val id = ids.incrementAndGet()
        _chat.update { it + ChatItem(id, fromMe = true, text = clean) }
        if (!ready()) {
            upsertAnswer(id, "Not connected to the laptop right now.", pending = false, error = true)
            return
        }
        socket?.send(JSONObject().put("t", "chat").put("id", id).put("text", clean.take(2000)).toString())
    }

    /** A push-to-talk clip: 16 kHz, mono, 16-bit PCM. The laptop transcribes it with Whisper. */
    fun sendVoice(pcm: ByteArray) {
        val id = ids.incrementAndGet()
        _chat.update { it + ChatItem(id, fromMe = true, text = "🎙 …", pending = true) }
        if (!ready()) {
            upsertAnswer(id, "Not connected to the laptop right now.", pending = false, error = true)
            return
        }
        voiceIds += id
        socket?.send(JSONObject().put("t", "voice").put("id", id).put("rate", 16000)
            .put("pcm", Base64.encodeToString(pcm, Base64.NO_WRAP)).toString())
    }

    fun requestAgenda() {
        if (ready()) socket?.send(JSONObject().put("t", "agenda.get").toString())
    }

    fun clearChat() = _chat.update { emptyList() }

    // -- laptop control (Phase 2) -------------------------------------------------------------------------------
    private fun control(msg: JSONObject) {
        if (ready() && _laptopControl.value) socket?.send(msg.toString())
    }

    fun startScreen(width: Int = 1024, fps: Int = 5) {
        screenWanted = true
        screenWidth = width
        control(JSONObject().put("t", "screen.start").put("fps", fps).put("width", width).put("quality", 60))
    }

    fun stopScreen() {
        screenWanted = false
        control(JSONObject().put("t", "screen.stop"))
        _screen.value = null
    }

    /** Click at a position on the laptop screen (0..1 across and down), or where the cursor is (null). */
    fun click(x: Float? = null, y: Float? = null, button: String = "left", double: Boolean = false) {
        val msg = JSONObject().put("t", "input.click").put("button", button).put("double", double)
        if (x != null && y != null) msg.put("x", x.coerceIn(0f, 1f).toDouble()).put("y", y.coerceIn(0f, 1f).toDouble())
        control(msg)
    }

    fun move(dx: Int, dy: Int) {
        if (dx != 0 || dy != 0) control(JSONObject().put("t", "input.move").put("dx", dx).put("dy", dy))
    }

    fun scroll(clicks: Int) {
        if (clicks != 0) control(JSONObject().put("t", "input.scroll").put("clicks", clicks))
    }

    fun key(combo: String) = control(JSONObject().put("t", "input.key").put("combo", combo))

    fun type(text: String) {
        if (text.isNotEmpty()) control(JSONObject().put("t", "input.text").put("text", text.take(500)))
    }

    private fun deviceName(): String =
        "${Build.MANUFACTURER.replaceFirstChar { it.uppercase() }} ${Build.MODEL}".take(60)
}

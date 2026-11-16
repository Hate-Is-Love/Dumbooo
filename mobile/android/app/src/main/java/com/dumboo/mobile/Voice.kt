package com.dumboo.mobile

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.util.Base64
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.ByteArrayOutputStream
import java.util.Locale
import java.util.concurrent.LinkedBlockingQueue
import kotlin.concurrent.thread
import kotlin.math.sqrt

/** What DUMBOO is doing, for the 3D bot and the mic button. */
enum class Mood { IDLE, LISTENING, THINKING, SPEAKING }

object VoiceUi {
    val mood = MutableStateFlow(Mood.IDLE)
    /** 0..1: the voice level while listening or speaking. */
    val level = MutableStateFlow(0f)
    /** What the phone has heard so far, while listening. */
    val heard = MutableStateFlow("")
    private val _listenAgain = MutableSharedFlow<Unit>(extraBufferCapacity = 2)
    /** DUMBOO asked a question (yes/no, "which one?"): start listening again without a tap. */
    val listenAgain: SharedFlow<Unit> = _listenAgain.asSharedFlow()
    fun askToListen() { _listenAgain.tryEmit(Unit) }
}

/**
 * Hearing you: Android's speech recognition (good with Indian English, names and song titles) stops by itself when
 * you stop talking. If the phone has no speech recognition, it records and the laptop's Whisper transcribes it,
 * also stopping by itself after a short silence.
 */
class Listener(private val context: Context) {
    private val main = Handler(Looper.getMainLooper())
    private var recognizer: SpeechRecognizer? = null
    private var fallback: VadRecorder? = null
    val active get() = VoiceUi.mood.value == Mood.LISTENING

    fun start() {
        Voice.stopSpeaking()
        if (!SpeechRecognizer.isRecognitionAvailable(context)) { startFallback(); return }
        main.post {
            val r = recognizer ?: SpeechRecognizer.createSpeechRecognizer(context).also { recognizer = it }
            r.setRecognitionListener(object : RecognitionListener {
                override fun onReadyForSpeech(params: Bundle?) { VoiceUi.mood.value = Mood.LISTENING }
                override fun onBeginningOfSpeech() {}
                override fun onRmsChanged(rmsdB: Float) { VoiceUi.level.value = ((rmsdB + 2f) / 12f).coerceIn(0f, 1f) }
                override fun onBufferReceived(buffer: ByteArray?) {}
                override fun onEndOfSpeech() { VoiceUi.mood.value = Mood.THINKING; VoiceUi.level.value = 0f }
                override fun onError(error: Int) {
                    VoiceUi.level.value = 0f
                    VoiceUi.heard.value = ""
                    if (error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS ||
                        error == SpeechRecognizer.ERROR_CLIENT && VoiceUi.mood.value == Mood.IDLE) {
                        VoiceUi.mood.value = Mood.IDLE
                        return
                    }
                    VoiceUi.mood.value = Mood.IDLE
                }
                override fun onResults(results: Bundle?) {
                    val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
                    VoiceUi.heard.value = ""
                    if (text.isNullOrBlank()) { VoiceUi.mood.value = Mood.IDLE; return }
                    VoiceUi.mood.value = Mood.THINKING
                    Bridge.sendChat(text, spoken = true)
                }
                override fun onPartialResults(partial: Bundle?) {
                    partial?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.let {
                        VoiceUi.heard.value = it
                    }
                }
                override fun onEvent(eventType: Int, params: Bundle?) {}
            })
            val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                .putExtra(RecognizerIntent.EXTRA_LANGUAGE, Store.voiceLanguage(context))
                .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
                .putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 1200L)
                .putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 1000L)
            VoiceUi.heard.value = ""
            VoiceUi.mood.value = Mood.LISTENING
            r.startListening(intent)
        }
    }

    private fun startFallback() {
        val rec = VadRecorder()
        fallback = rec
        VoiceUi.mood.value = Mood.LISTENING
        if (!rec.start { pcm ->
                fallback = null
                if (pcm == null) VoiceUi.mood.value = Mood.IDLE
                else { VoiceUi.mood.value = Mood.THINKING; Bridge.sendVoice(pcm) }
            }) VoiceUi.mood.value = Mood.IDLE
    }

    fun stop() {
        main.post { recognizer?.stopListening() }
        fallback?.finish()
    }

    fun cancel() {
        main.post { recognizer?.cancel() }
        fallback?.cancel()
        VoiceUi.mood.value = Mood.IDLE
        VoiceUi.level.value = 0f
    }

    fun destroy() = main.post { recognizer?.destroy(); recognizer = null }
}

/** Records 16 kHz PCM and stops by itself after ~0.9 s of silence once you've spoken (a simple energy VAD). */
class VadRecorder {
    @Volatile private var running = false
    @Volatile private var cancelled = false

    @SuppressLint("MissingPermission")   // the caller checks RECORD_AUDIO first
    fun start(done: (ByteArray?) -> Unit): Boolean {
        val minSize = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val record = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT, maxOf(minSize, RATE))
        if (record.state != AudioRecord.STATE_INITIALIZED) { record.release(); return false }
        running = true
        record.startRecording()
        thread(name = "VadRecorder") {
            val out = ByteArrayOutputStream()
            val frame = ShortArray(480)                    // 30 ms
            var noise = 300.0
            var spoke = false
            var silentMs = 0
            var totalMs = 0
            while (running && totalMs < 15_000) {
                val n = record.read(frame, 0, frame.size)
                if (n <= 0) continue
                var sum = 0.0
                for (i in 0 until n) { sum += frame[i] * frame[i].toDouble() }
                val rms = sqrt(sum / n)
                VoiceUi.level.value = (rms / 3000.0).toFloat().coerceIn(0f, 1f)
                val bytes = ByteArray(n * 2)
                for (i in 0 until n) { bytes[2 * i] = frame[i].toByte(); bytes[2 * i + 1] = (frame[i].toInt() shr 8).toByte() }
                out.write(bytes)
                totalMs += 30
                val speech = rms > maxOf(500.0, noise * 3)
                if (!spoke && !speech) noise = noise * 0.95 + rms * 0.05
                if (speech) { spoke = true; silentMs = 0 } else if (spoke) silentMs += 30
                if (spoke && silentMs > 900) break
                if (!spoke && totalMs > 6000) break      // nothing said
            }
            running = false
            try { record.stop() } catch (_: IllegalStateException) { }
            record.release()
            VoiceUi.level.value = 0f
            done(if (cancelled || !spoke) null else out.toByteArray())
        }
        return true
    }

    fun finish() { running = false }
    fun cancel() { cancelled = true; running = false }

    companion object { const val RATE = 16000 }
}

/**
 * Speaking: DUMBOO's own voice from the laptop (the same Piper voice), streamed as audio. If the laptop's voice
 * isn't there, or nothing arrives within 4 s, the phone's own voice reads it instead.
 */
object Voice {
    private val main = Handler(Looper.getMainLooper())
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    @Volatile private var currentId = 0L
    private var pendingText = ""
    private var listenAfter = false
    private var gotAudio = false
    private val queue = LinkedBlockingQueue<Pair<Long, ShortArray?>>()
    private var track: AudioTrack? = null
    private var trackRate = 0
    private var player: Thread? = null

    fun init(context: Context) {
        if (tts == null) {
            tts = TextToSpeech(context.applicationContext) { status ->
                ttsReady = status == TextToSpeech.SUCCESS
                if (ttsReady) {
                    tts?.language = Locale("en", "IN").takeIf { tts!!.isLanguageAvailable(it) >= 0 } ?: Locale.ENGLISH
                    tts?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                        override fun onStart(utteranceId: String?) { VoiceUi.mood.value = Mood.SPEAKING }
                        override fun onDone(utteranceId: String?) { finished(utteranceId?.toLongOrNull() ?: 0L) }
                        @Deprecated("Deprecated in Java")
                        override fun onError(utteranceId: String?) { finished(utteranceId?.toLongOrNull() ?: 0L) }
                        override fun onRangeStart(utteranceId: String?, start: Int, end: Int, frame: Int) {
                            VoiceUi.level.value = 0.3f + (end % 7) / 10f
                        }
                    })
                }
            }
        }
        if (player == null) player = thread(name = "DumbooVoice", isDaemon = true) { playLoop() }
    }

    /** Speak an answer; [askAfter]: listen again when done (DUMBOO asked a question). */
    fun say(context: Context, id: Long, text: String, askAfter: Boolean) {
        if (text.isBlank()) return
        stopSpeaking()
        currentId = id
        pendingText = text
        listenAfter = askAfter
        gotAudio = false
        VoiceUi.mood.value = Mood.SPEAKING
        if (Store.laptopVoice(context) && Bridge.isReady()) {
            Bridge.requestSpeech(id, text)
            main.postDelayed({ if (currentId == id && !gotAudio) phoneVoice(id) }, 4000)
        } else {
            phoneVoice(id)
        }
    }

    fun onAudio(id: Long, rate: Int, pcm: String, last: Boolean) {
        if (id != currentId) return
        gotAudio = true
        if (last) { queue.put(id to null); return }
        val bytes = Base64.decode(pcm, Base64.DEFAULT)
        val samples = ShortArray(bytes.size / 2) { i ->
            ((bytes[2 * i].toInt() and 0xff) or (bytes[2 * i + 1].toInt() shl 8)).toShort()
        }
        if (rate != trackRate) ensureTrack(rate)
        queue.put(id to samples)
    }

    fun unavailable(id: Long) { if (id == currentId && !gotAudio) phoneVoice(id) }

    private fun phoneVoice(id: Long) {
        if (id != currentId) return
        gotAudio = true                       // don't fall back twice
        val engine = tts
        if (engine == null || !ttsReady) { finished(id); return }
        engine.speak(pendingText.take(3900), TextToSpeech.QUEUE_FLUSH, null, id.toString())
    }

    @Synchronized
    private fun ensureTrack(rate: Int) {
        track?.release()
        trackRate = rate
        track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(rate)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setBufferSizeInBytes(maxOf(AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT), rate))
            .build().also { it.play() }
    }

    private fun playLoop() {
        while (true) {
            val (id, samples) = queue.take()
            if (id != currentId) continue
            if (samples == null) {
                // wait for what's buffered to play out, then finish
                val t = track
                if (t != null) Thread.sleep(250)
                finished(id)
                continue
            }
            VoiceUi.mood.value = Mood.SPEAKING
            var offset = 0
            while (offset < samples.size && id == currentId) {
                val n = minOf(1024, samples.size - offset)
                var sum = 0.0
                for (i in offset until offset + n) sum += samples[i] * samples[i].toDouble()
                VoiceUi.level.value = (sqrt(sum / n) / 6000.0).toFloat().coerceIn(0f, 1f)
                track?.write(samples, offset, n)
                offset += n
            }
        }
    }

    private fun finished(id: Long) {
        if (id != currentId || id == 0L) return
        currentId = 0L
        VoiceUi.level.value = 0f
        VoiceUi.mood.value = Mood.IDLE
        if (listenAfter) { listenAfter = false; main.postDelayed({ VoiceUi.askToListen() }, 250) }
    }

    fun stopSpeaking() {
        currentId = 0L
        queue.clear()
        tts?.stop()
        track?.let { runCatching { it.pause(); it.flush(); it.play() } }
        if (VoiceUi.mood.value == Mood.SPEAKING) VoiceUi.mood.value = Mood.IDLE
    }

    fun shutdown() { tts?.shutdown(); tts = null }
}

package com.dumboo.mobile

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.speech.tts.TextToSpeech
import java.io.ByteArrayOutputStream
import java.util.Locale
import kotlin.concurrent.thread

/** Push-to-talk: records 16 kHz mono 16-bit PCM while the button is held (at most 20 seconds). */
class PushToTalk {
    private var recorder: AudioRecord? = null
    @Volatile private var running = false
    private var buffer = ByteArrayOutputStream()
    private var worker: Thread? = null

    @SuppressLint("MissingPermission")   // the caller checks RECORD_AUDIO first
    fun start(): Boolean {
        if (running) return true
        val minSize = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val record = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT, maxOf(minSize, RATE))
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            record.release()
            return false
        }
        buffer = ByteArrayOutputStream()
        recorder = record
        running = true
        record.startRecording()
        worker = thread(name = "PushToTalk") {
            val chunk = ByteArray(3200)                         // 100 ms
            while (running && buffer.size() < MAX_BYTES) {
                val n = record.read(chunk, 0, chunk.size)
                if (n > 0) buffer.write(chunk, 0, n)
            }
        }
        return true
    }

    /** Stops and returns the clip, or null if it was too short to be speech. */
    fun stop(): ByteArray? {
        if (!running) return null
        running = false
        worker?.join(500)
        recorder?.run { try { stop() } catch (_: IllegalStateException) { }; release() }
        recorder = null
        val pcm = buffer.toByteArray()
        return if (pcm.size < RATE * 2 * 3 / 10) null else pcm   // under 0.3 s: probably a tap
    }

    companion object {
        const val RATE = 16000
        const val MAX_BYTES = RATE * 2 * 20
    }
}

/** DUMBOO's answers spoken on the phone with the phone's own voice. */
class Speaker(context: Context) : TextToSpeech.OnInitListener {
    private val tts = TextToSpeech(context.applicationContext, this)
    private var ready = false

    override fun onInit(status: Int) {
        ready = status == TextToSpeech.SUCCESS
        if (ready) tts.language = Locale.getDefault().takeIf { tts.isLanguageAvailable(it) >= 0 } ?: Locale.ENGLISH
    }

    fun say(text: String) {
        if (ready) tts.speak(text.take(4000), TextToSpeech.QUEUE_FLUSH, null, "dumboo")
    }

    fun stop() = tts.stop()
    fun shutdown() = tts.shutdown()
}

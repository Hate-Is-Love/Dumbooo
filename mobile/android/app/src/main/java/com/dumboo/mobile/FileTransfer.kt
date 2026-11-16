package com.dumboo.mobile

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.provider.OpenableColumns
import android.util.Base64
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger

/** Files between the phone and the laptop (Phase 3): up to 50 MB, in 512 KB pieces, over the secure link. */
object FileTransfer {
    const val MAX_BYTES = 50L * 1024 * 1024
    private const val CHUNK = 512 * 1024
    private val ids = AtomicInteger(1)
    private val saved = ConcurrentHashMap<String, CompletableDeferred<String>>()

    private class Incoming(val name: String, val size: Long, val file: File, val out: FileOutputStream) {
        var got = 0L
    }
    private val incoming = ConcurrentHashMap<String, Incoming>()
    /** Files this phone asked the laptop for (Files screen): accepted even with "Files" switched off. */
    private val expected = ConcurrentHashMap.newKeySet<String>()

    fun expect(name: String) { expected += safeName(name) }

    /** Files received on this phone, newest first: (name, uri, time). */
    fun received(context: Context): List<Triple<String, String, Long>> {
        val raw = context.getSharedPreferences("dumboo_files", Context.MODE_PRIVATE).getString("received", "[]")!!
        val a = org.json.JSONArray(raw)
        return List(a.length()) { i -> a.getJSONObject(i).let { Triple(it.optString("n"), it.optString("u"), it.optLong("t")) } }
    }

    private fun remember(context: Context, name: String, uri: String) {
        val prefs = context.getSharedPreferences("dumboo_files", Context.MODE_PRIVATE)
        val list = listOf(Triple(name, uri, System.currentTimeMillis())) + received(context).take(49)
        val a = org.json.JSONArray()
        list.forEach { (n, u, t) -> a.put(JSONObject().put("n", n).put("u", u).put("t", t)) }
        prefs.edit().putString("received", a.toString()).apply()
        _changed.tryEmit(Unit)
    }

    private val _changed = kotlinx.coroutines.flow.MutableSharedFlow<Unit>(extraBufferCapacity = 4)
    val changed: kotlinx.coroutines.flow.SharedFlow<Unit> = _changed

    // -- laptop -> phone ----------------------------------------------------------------------------------------------
    fun onMessage(context: Context, msg: JSONObject) {
        val id = msg.optString("id").filter { it.isLetterOrDigit() }.take(40)     // used in a file name below
        when (msg.optString("t")) {
            "file.begin" -> {
                val size = msg.optLong("size")
                val asked = msg.optBoolean("requested") && expected.remove(safeName(msg.optString("name")))
                if (!(asked || Features.usable(context, "files")) || size <= 0 || size > MAX_BYTES) return
                val dir = File(context.cacheDir, "incoming").apply { mkdirs() }
                val file = File(dir, "f$id.part")
                incoming[id] = Incoming(safeName(msg.optString("name")), size, file, FileOutputStream(file))
            }
            "file.chunk" -> {
                val entry = incoming[id] ?: return
                val data = Base64.decode(msg.optString("data"), Base64.DEFAULT)
                entry.got += data.size
                if (entry.got > entry.size) { abort(id); return }
                entry.out.write(data)
            }
            "file.end" -> {
                val reply = msg.optLong("reply")
                val entry = incoming.remove(id)
                val result = when {
                    entry == null -> CommandResult(false, "The file didn't arrive (receiving files is off, or it " +
                        "was too big).")
                    else -> {
                        entry.out.close()
                        if (entry.got != entry.size) {
                            entry.file.delete(); CommandResult(false, "The file arrived incomplete.")
                        } else {
                            val (where, uri) = saveToDownloads(context, entry.file, entry.name)
                            entry.file.delete()
                            remember(context, entry.name, uri)
                            notifySaved(context, entry.name)
                            CommandResult(true, "Saved ${entry.name} in the phone's $where.")
                        }
                    }
                }
                Bridge.sendResult(reply, result)
            }
            "file.saved" -> saved.remove(id)?.complete(msg.optString("name"))
        }
    }

    private fun abort(id: String) {
        incoming.remove(id)?.let { it.out.close(); it.file.delete() }
    }

    fun abortAll() = incoming.keys.toList().forEach { abort(it) }

    private fun safeName(name: String): String =
        name.substringAfterLast('/').substringAfterLast('\\').replace(Regex("[^\\w .()\\[\\]-]+"), "_")
            .trim(' ', '.').ifBlank { "file" }.take(120)

    private fun saveToDownloads(context: Context, file: File, name: String): Pair<String, String> {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/DUMBOO")
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val resolver = context.contentResolver
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IllegalStateException("couldn't create the file in Downloads")
            resolver.openOutputStream(uri)!!.use { out -> file.inputStream().use { it.copyTo(out) } }
            values.clear()
            values.put(MediaStore.Downloads.IS_PENDING, 0)
            resolver.update(uri, values, null, null)
            return "Downloads/DUMBOO folder" to uri.toString()
        }
        val dir = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: context.filesDir
        val target = File(dir, name)
        file.copyTo(target, overwrite = false)
        return "DUMBOO Mobile downloads folder" to Uri.fromFile(target).toString()
    }

    private fun notifySaved(context: Context, name: String) {
        val n = NotificationCompat.Builder(context, PhoneControl.CHANNEL_ACTIONS)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("File from your laptop")
            .setContentText("$name · saved in Downloads/DUMBOO")
            .setAutoCancel(true)
            .build()
        try { NotificationManagerCompat.from(context).notify(name.hashCode(), n) } catch (_: SecurityException) { }
    }

    // -- phone -> laptop ----------------------------------------------------------------------------------------------
    /** Sends a file the owner picked or shared. Returns a message to show. Runs on a background thread. */
    /** [own]: you picked or shared it yourself (then the "Files" switch, which limits the laptop, isn't needed). */
    suspend fun send(context: Context, uri: Uri, own: Boolean = false): String {
        if (!own && !Features.usable(context, "files")) return "Turn on “Files” in DUMBOO Mobile → Settings first."
        if (!Bridge.isReady()) return "Not connected to the laptop."
        var name = "file"
        var size = -1L
        context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE),
            null, null, null)?.use {
            if (it.moveToFirst()) {
                name = it.getString(0) ?: name
                if (!it.isNull(1)) size = it.getLong(1)
            }
        }
        if (size <= 0 || size > MAX_BYTES) return "Files must be up to 50 MB ($name is too big or empty)."
        val id = "P${ids.getAndIncrement()}"
        val done = CompletableDeferred<String>()
        saved[id] = done
        Bridge.send(JSONObject().put("t", "file.begin").put("id", id).put("name", safeName(name)).put("size", size)
            .put("own", own))
        val buffer = ByteArray(CHUNK)
        context.contentResolver.openInputStream(uri)?.use { input ->
            while (true) {
                val n = input.readNBytesCompat(buffer)
                if (n <= 0) break
                while (Bridge.queueSize() > 4L * 1024 * 1024) delay(50)     // don't flood a slow network
                Bridge.send(JSONObject().put("t", "file.chunk").put("id", id)
                    .put("data", Base64.encodeToString(buffer, 0, n, Base64.NO_WRAP)))
            }
        } ?: return "Couldn't read $name."
        Bridge.send(JSONObject().put("t", "file.end").put("id", id))
        val savedAs = withTimeoutOrNull(120_000) { done.await() }
        saved.remove(id)
        return if (savedAs != null) "Sent $savedAs to the laptop (Downloads\\DUMBOO Mobile)."
        else "The laptop didn't confirm $name."
    }

    private fun java.io.InputStream.readNBytesCompat(buffer: ByteArray): Int {
        var total = 0
        while (total < buffer.size) {
            val n = read(buffer, total, buffer.size - total)
            if (n < 0) break
            total += n
        }
        return total
    }
}

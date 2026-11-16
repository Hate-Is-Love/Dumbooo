package com.dumboo.mobile

import android.app.AlertDialog
import android.content.ContentResolver
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Share → “DUMBOO laptop” from any app: files go to the laptop's Downloads, text to its clipboard. Always asks
 * first, so another app can't push things to the laptop without the owner seeing it.
 */
class ShareActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Store.laptop(this) == null) {
            done("Pair DUMBOO Mobile with your laptop first.")
            return
        }
        // Only shared content (content://), never file paths: those could point into this app's private files.
        val uris = streams(intent).filter { it.scheme == ContentResolver.SCHEME_CONTENT }
        val text = intent.getStringExtra(Intent.EXTRA_TEXT)
        val what = when {
            uris.size == 1 -> "this file"
            uris.isNotEmpty() -> "${uris.size.coerceAtMost(10)} files"
            !text.isNullOrBlank() -> "this text (to the laptop's clipboard)"
            else -> { done("Nothing to send."); return }
        }
        AlertDialog.Builder(this)
            .setTitle("Send to your laptop?")
            .setMessage("Send $what to ${Store.laptop(this)?.laptopName ?: "your laptop"}?")
            .setPositiveButton("SEND") { _, _ -> send(uris, text) }
            .setNegativeButton("CANCEL") { _, _ -> finish() }
            .setOnCancelListener { finish() }
            .show()
    }

    private fun send(uris: List<Uri>, text: String?) {
        Toast.makeText(this, "Sending to the laptop…", Toast.LENGTH_SHORT).show()
        lifecycleScope.launch {
            if (!Bridge.isReady()) {
                ConnectionService.start(this@ShareActivity)
                repeat(40) { if (!Bridge.isReady()) delay(250) }
            }
            val message = withContext(Dispatchers.IO) {
                if (uris.isNotEmpty()) uris.take(10).map { FileTransfer.send(this@ShareActivity, it) }.last()
                else Bridge.pushClipboard(text.orEmpty())
            }
            done(message)
        }
    }

    private fun done(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
        finish()
    }

    @Suppress("DEPRECATION")
    private fun streams(intent: Intent): List<Uri> = when (intent.action) {
        Intent.ACTION_SEND -> listOfNotNull(
            if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
            else intent.getParcelableExtra(Intent.EXTRA_STREAM))
        Intent.ACTION_SEND_MULTIPLE ->
            (if (Build.VERSION.SDK_INT >= 33) intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
            else intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM)) ?: emptyList()
        else -> emptyList()
    }
}

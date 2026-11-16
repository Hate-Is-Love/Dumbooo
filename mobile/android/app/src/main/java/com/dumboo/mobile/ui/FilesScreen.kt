package com.dumboo.mobile.ui

import android.content.Intent
import android.net.Uri
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.dumboo.mobile.Bridge
import com.dumboo.mobile.FileTransfer
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.DateFormat
import java.util.Date

/**
 * Files: send files and photos to the laptop, get files from the laptop's Downloads\DUMBOO Mobile folder, and open
 * what you received on this phone.
 */
@Composable
fun FilesScreen() {
    val context = LocalContext.current
    val clipboard = LocalClipboardManager.current
    val scope = rememberCoroutineScope()
    val laptop by Bridge.laptopFiles.collectAsStateWithLifecycle()
    var tick by remember { mutableIntStateOf(0) }
    val received = remember(tick) { FileTransfer.received(context) }
    LaunchedEffect(Unit) {
        Bridge.listLaptopFiles()
        launch { FileTransfer.changed.collect { tick++; Bridge.listLaptopFiles() } }
        Bridge.filesMessage.collect { Toast.makeText(context, it, Toast.LENGTH_LONG).show() }
    }
    fun send(uri: Uri?) {
        if (uri == null) return
        scope.launch {
            Toast.makeText(context, "Sending to the laptop…", Toast.LENGTH_SHORT).show()
            val message = withContext(Dispatchers.IO) { FileTransfer.send(context, uri, own = true) }
            Toast.makeText(context, message, Toast.LENGTH_LONG).show()
            Bridge.listLaptopFiles()
        }
    }
    val pickFile = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { send(it) }
    val pickMedia = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { send(it) }

    LazyColumn(Modifier.fillMaxSize().background(Night), contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Title("FILES")
                Spacer(Modifier.weight(1f))
                IconButton(onClick = { Bridge.listLaptopFiles(); tick++ }) { Icon(Icons.Filled.Refresh, "Refresh", tint = Lilac) }
            }
        }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Action("📄", "File", Modifier.weight(1f)) { pickFile.launch(arrayOf("*/*")) }
                Action("🖼", "Photo / video", Modifier.weight(1f)) {
                    pickMedia.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageAndVideo))
                }
                Action("📋", "Clipboard", Modifier.weight(1f)) {
                    Toast.makeText(context, Bridge.pushClipboard(clipboard.getText()?.text.orEmpty(), own = true),
                        Toast.LENGTH_LONG).show()
                }
            }
        }
        item {
            Text("Sent files land in the laptop's Downloads\\DUMBOO Mobile. Tip: Share → “DUMBOO laptop” from any app.",
                color = Muted, fontSize = 12.sp)
        }
        item { Section("ON THE LAPTOP · tap to download") }
        val files = laptop
        if (files == null) item { Text("Loading…", color = Muted) }
        else if (files.isEmpty()) item { Text("Nothing in the laptop's DUMBOO Mobile folder yet.", color = Muted) }
        else itemsIndexed(files, key = { _, f -> "l" + f.name }) { i, f ->
            Row2(i, icon(f.name), f.name, "${size(f.size)} · ${DateFormat.getDateTimeInstance(DateFormat.SHORT,
                DateFormat.SHORT).format(Date(f.modified * 1000))}", "⬇") {
                Bridge.getLaptopFile(f.name)
                Toast.makeText(context, "Downloading ${f.name}…", Toast.LENGTH_SHORT).show()
            }
        }
        item { Section("ON THIS PHONE · from the laptop") }
        if (received.isEmpty()) item { Text("Files from the laptop appear here (and in Downloads/DUMBOO).", color = Muted) }
        else itemsIndexed(received, key = { _, r -> "r" + r.second + r.third }) { i, (name, uri, time) ->
            Row2(i, icon(name), name, DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT)
                .format(Date(time)), "↗") {
                runCatching {
                    context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(uri))
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
                }.onFailure { Toast.makeText(context, "No app here can open $name.", Toast.LENGTH_SHORT).show() }
            }
        }
    }
}

@Composable
private fun Action(emoji: String, label: String, modifier: Modifier, onClick: () -> Unit) {
    Card(modifier = modifier.clickable(onClick = onClick), shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = Color(0xFF2E2563))) {
        Column(Modifier.fillMaxWidth().padding(vertical = 14.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Text(emoji, fontSize = 26.sp)
            Text(label, color = Color.White, fontSize = 12.sp)
        }
    }
}

@Composable
private fun Section(text: String) = Text(text, color = Lilac, fontFamily = FontFamily.Monospace, fontSize = 12.sp,
    letterSpacing = 2.sp, modifier = Modifier.padding(top = 10.dp))

@Composable
private fun Row2(index: Int, icon: String, title: String, sub: String, action: String, onClick: () -> Unit) {
    val anim = remember { Animatable(0f) }
    LaunchedEffect(Unit) { delay(index * 40L); anim.animateTo(1f, tween(320, easing = FastOutSlowInEasing)) }
    Card(Modifier.fillMaxWidth().graphicsLayer { alpha = anim.value; translationY = (1 - anim.value) * 30f }
        .clickable(onClick = onClick), shape = RoundedCornerShape(14.dp),
        colors = CardDefaults.cardColors(containerColor = Color(0xFF221B42))) {
        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(40.dp).background(Color(0xFF2E2563), RoundedCornerShape(10.dp)),
                contentAlignment = Alignment.Center) { Text(icon, fontSize = 20.sp) }
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(title, color = Color.White, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(sub, color = Muted, fontSize = 12.sp)
            }
            Text(action, color = Lilac, fontSize = 18.sp)
        }
    }
}

private fun icon(name: String) = when (name.substringAfterLast('.', "").lowercase()) {
    "jpg", "jpeg", "png", "gif", "webp", "heic" -> "🖼"
    "mp4", "mkv", "mov", "webm" -> "🎬"
    "mp3", "wav", "m4a", "flac", "ogg" -> "🎵"
    "pdf" -> "📕"
    "doc", "docx", "txt", "md", "rtf" -> "📝"
    "xls", "xlsx", "csv" -> "📊"
    "ppt", "pptx" -> "📽"
    "zip", "rar", "7z" -> "🗜"
    "apk" -> "📦"
    else -> "📄"
}

private fun size(bytes: Long): String = when {
    bytes >= 1L shl 20 -> "%.1f MB".format(bytes / 1048576.0)
    bytes >= 1L shl 10 -> "%.0f KB".format(bytes / 1024.0)
    else -> "$bytes B"
}

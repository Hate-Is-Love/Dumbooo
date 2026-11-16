package com.dumboo.mobile.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.dumboo.mobile.Agenda
import com.dumboo.mobile.Bridge
import com.dumboo.mobile.ChatItem
import com.dumboo.mobile.ConnectionService
import com.dumboo.mobile.LinkState
import com.dumboo.mobile.PairingOffer
import com.dumboo.mobile.PushToTalk
import com.dumboo.mobile.Store
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions

internal val Cyan = Color(0xFF3FE0E8)
internal val Violet = Color(0xFF9B8CFF)
internal val Ink = Color(0xFF071114)
internal val Panel = Color(0xFF0D1B20)
internal val Muted = Color(0xFF7FA6AD)
internal val Danger = Color(0xFFFF6B7A)

private val colors = darkColorScheme(primary = Cyan, onPrimary = Ink, secondary = Violet, background = Ink,
    surface = Panel, onBackground = Color(0xFFDDF4F6), onSurface = Color(0xFFDDF4F6), error = Danger)

@Composable
fun DumbooApp(unlocked: Boolean, pairLink: String?, onPairLinkUsed: () -> Unit, onPaired: () -> Unit,
              onUnlock: () -> Unit) {
    MaterialTheme(colorScheme = colors) {
        Surface(Modifier.fillMaxSize(), color = Ink) {
            val context = LocalContext.current
            val state by Bridge.state.collectAsStateWithLifecycle()
            val paired = state !is LinkState.NotPaired && Store.laptop(context) != null
            when {
                !paired || pairLink != null -> PairScreen(pairLink, onPairLinkUsed, onPaired)
                !unlocked -> LockedScreen(onUnlock)
                else -> MainScreens(state)
            }
        }
    }
}

@Composable
internal fun Title(text: String) = Text(text, color = Cyan, fontFamily = FontFamily.Monospace, fontSize = 13.sp,
    letterSpacing = 3.sp, fontWeight = FontWeight.Bold)

@Composable
private fun LockedScreen(onUnlock: () -> Unit) {
    Column(Modifier.fillMaxSize().padding(32.dp), verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally) {
        Title("DUMBOO MOBILE")
        Spacer(Modifier.height(16.dp))
        Text("Locked", fontSize = 22.sp)
        Spacer(Modifier.height(24.dp))
        Button(onClick = onUnlock) { Text("UNLOCK") }
    }
}

// -- pairing -------------------------------------------------------------------------------------------------------
@Composable
private fun PairScreen(pairLink: String?, onPairLinkUsed: () -> Unit, onPaired: () -> Unit) {
    val context = LocalContext.current
    var status by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var pasted by remember { mutableStateOf("") }

    fun pair(text: String) {
        val offer = try { PairingOffer.parse(text) } catch (e: Exception) {
            status = e.message ?: "That isn't a DUMBOO pairing code."; return
        }
        busy = true
        status = "Connecting to ${offer.laptopName}…"
        Bridge.pair(context, offer) { error ->
            busy = false
            status = error ?: "✓ Paired with ${offer.laptopName}"
            if (error == null) onPaired()
        }
    }

    LaunchedEffect(pairLink) {
        if (pairLink != null) {
            onPairLinkUsed()
            pair(pairLink)
        }
    }
    val scanner = rememberLauncherForActivityResult(ScanContract()) { result ->
        result.contents?.let { pair(it) }
    }
    Column(Modifier.fillMaxSize().padding(24.dp).imePadding(), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Spacer(Modifier.height(24.dp))
        Title("DUMBOO MOBILE")
        Text("Pair with your laptop", fontSize = 24.sp, fontWeight = FontWeight.SemiBold)
        Text("On the laptop open DUMBOO → ACCOUNT → Phone and click “Pair a phone”. Then scan the QR code.",
            color = Muted)
        Button(onClick = {
            scanner.launch(ScanOptions().setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                .setPrompt("Scan the QR code on the laptop").setBeepEnabled(false).setOrientationLocked(false))
        }, enabled = !busy, modifier = Modifier.fillMaxWidth().height(52.dp)) { Text("SCAN QR CODE") }
        OutlinedTextField(value = pasted, onValueChange = { pasted = it }, singleLine = true,
            label = { Text("…or paste the pairing link") }, modifier = Modifier.fillMaxWidth())
        OutlinedButton(onClick = { pair(pasted) }, enabled = !busy && pasted.isNotBlank()) { Text("PAIR") }
        if (busy) CircularProgressIndicator()
        if (status.isNotBlank()) Text(status, color = if (status.startsWith("✓")) Cyan else Muted)
        Spacer(Modifier.weight(1f))
        Text("The link is encrypted and locked to this phone's own key, which never leaves it. Only your laptop " +
            "is trusted, checked by its certificate fingerprint.", color = Muted, fontSize = 12.sp)
    }
}

// -- main screens ------------------------------------------------------------------------------------------------------
@Composable
private fun MainScreens(state: LinkState) {
    var tab by remember { mutableStateOf(0) }
    Scaffold(containerColor = Ink, bottomBar = {
        NavigationBar(containerColor = Panel) {
            NavigationBarItem(selected = tab == 0, onClick = { tab = 0 },
                icon = { Icon(Icons.Filled.Home, null) }, label = { Text("DUMBOO") })
            NavigationBarItem(selected = tab == 1, onClick = { tab = 1; Bridge.requestAgenda() },
                icon = { Icon(Icons.Filled.DateRange, null) }, label = { Text("Agenda") })
            NavigationBarItem(selected = tab == 3, onClick = { tab = 3 },
                icon = { Text("💻", fontSize = 20.sp) }, label = { Text("Laptop") })
            NavigationBarItem(selected = tab == 2, onClick = { tab = 2 },
                icon = { Icon(Icons.Filled.Settings, null) }, label = { Text("Settings") })
        }
    }) { padding ->
        Column(Modifier.padding(padding).fillMaxSize()) {
            StatusBar(state)
            when (tab) {
                0 -> ChatScreen()
                1 -> AgendaScreen()
                3 -> LaptopScreen()
                else -> SettingsScreen(state)
            }
        }
    }
}

@Composable
private fun StatusBar(state: LinkState) {
    val (text, color) = when (state) {
        is LinkState.Ready -> "● ${state.user} · connected" to Cyan
        is LinkState.Connecting -> "○ connecting to ${state.host}…" to Muted
        is LinkState.Failed -> "⚠ ${state.message}" to Danger
        LinkState.Offline -> "○ laptop offline · retrying" to Muted
        LinkState.NotPaired -> "not paired" to Muted
    }
    Row(Modifier.fillMaxWidth().background(Panel).padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Title("DUMBOO")
        Spacer(Modifier.size(12.dp))
        Text(text, color = color, fontSize = 13.sp, maxLines = 1)
    }
}

private fun plain(markdown: String): String = markdown
    .replace(Regex("\\[\\[(\\d+)]]\\([^)]*\\)"), "[$1]")
    .replace(Regex("\\[([^\\]]+)]\\(([^)]+)\\)"), "$1 ($2)")
    .replace("**", "").replace(Regex("(?<![\\w])_([^_]+)_(?![\\w])"), "$1").replace("`", "")
    .replace(Regex("^#+\\s*", RegexOption.MULTILINE), "").replace(Regex("^- ", RegexOption.MULTILINE), "• ")

@Composable
private fun ChatScreen() {
    val context = LocalContext.current
    val chat by Bridge.chat.collectAsStateWithLifecycle()
    var input by remember { mutableStateOf("") }
    var recording by remember { mutableStateOf(false) }
    val ptt = remember { PushToTalk() }
    val listState = rememberLazyListState()
    val askMic = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
    LaunchedEffect(chat.size) { if (chat.isNotEmpty()) listState.animateScrollToItem(chat.size - 1) }

    Column(Modifier.fillMaxSize().imePadding()) {
        LazyColumn(Modifier.weight(1f).fillMaxWidth().padding(horizontal = 12.dp), state = listState,
            verticalArrangement = Arrangement.spacedBy(8.dp)) {
            item { Spacer(Modifier.height(8.dp)) }
            if (chat.isEmpty()) item {
                Text("Ask DUMBOO anything, or hold the mic and speak.\nTry “what's on my plate today?” or " +
                    "“remind me in 10 minutes to stretch”.", color = Muted, modifier = Modifier.padding(16.dp))
            }
            items(chat, key = { it.id }) { Bubble(it) }
            item { Spacer(Modifier.height(8.dp)) }
        }
        Row(Modifier.fillMaxWidth().background(Panel).padding(8.dp), verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(value = input, onValueChange = { input = it }, modifier = Modifier.weight(1f),
                placeholder = { Text(if (recording) "Listening… release to send" else "Message DUMBOO") },
                maxLines = 4, keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = { Bridge.sendChat(input); input = "" }))
            if (input.isNotBlank()) {
                IconButton(onClick = { Bridge.sendChat(input); input = "" }) { Icon(Icons.AutoMirrored.Filled.Send, "Send", tint = Cyan) }
            } else {
                Box(Modifier.padding(start = 8.dp).size(52.dp).clip(CircleShape)
                    .background(if (recording) Danger else Cyan)
                    .pointerInput(Unit) {
                        detectTapGestures(onPress = {
                            if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) !=
                                PackageManager.PERMISSION_GRANTED) {
                                askMic.launch(Manifest.permission.RECORD_AUDIO)
                                return@detectTapGestures
                            }
                            recording = ptt.start()
                            tryAwaitRelease()
                            recording = false
                            ptt.stop()?.let { Bridge.sendVoice(it) }
                        })
                    }, contentAlignment = Alignment.Center) {
                    Text("🎙", fontSize = 22.sp)
                }
            }
        }
    }
}

@Composable
private fun Bubble(item: ChatItem) {
    val shape = RoundedCornerShape(14.dp)
    Row(Modifier.fillMaxWidth(), horizontalArrangement = if (item.fromMe) Arrangement.End else Arrangement.Start) {
        Card(shape = shape, modifier = Modifier.widthIn(max = 320.dp), colors = CardDefaults.cardColors(
            containerColor = when {
                item.error -> Color(0xFF3A1A20)
                item.fromMe -> Color(0xFF123A40)
                else -> Panel
            })) {
            Column(Modifier.padding(12.dp)) {
                if (!item.fromMe) Text("DUMBOO", color = Cyan, fontSize = 10.sp, letterSpacing = 2.sp)
                Text((item.heard?.let { "$it " } ?: "") + plain(item.text) + if (item.pending) " ▍" else "",
                    color = if (item.error) Danger else MaterialTheme.colorScheme.onSurface)
            }
        }
    }
}

@Composable
private fun AgendaScreen() {
    val agenda by Bridge.agenda.collectAsStateWithLifecycle()
    LazyColumn(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Title("AGENDA")
                Spacer(Modifier.weight(1f))
                IconButton(onClick = { Bridge.requestAgenda() }) { Icon(Icons.Filled.Refresh, "Refresh", tint = Cyan) }
            }
        }
        val a: Agenda? = agenda
        if (a == null) {
            item { Text("Loading from the laptop…", color = Muted) }
            return@LazyColumn
        }
        item { Text("Coming up", fontWeight = FontWeight.SemiBold) }
        if (a.events.isEmpty()) item { Text("Nothing scheduled in the next 7 days.", color = Muted) }
        items(a.events) { e ->
            Card(colors = CardDefaults.cardColors(containerColor = Panel)) {
                Row(Modifier.fillMaxWidth().padding(12.dp)) {
                    Text(e.startsAt.replace("T", "  "), color = Cyan, fontFamily = FontFamily.Monospace,
                        fontSize = 13.sp)
                    Spacer(Modifier.size(12.dp))
                    Text((if (e.reminder) "⏰ " else "📅 ") + e.title)
                }
            }
        }
        item { Spacer(Modifier.height(8.dp)); Text("Tasks", fontWeight = FontWeight.SemiBold) }
        if (a.tasks.isEmpty()) item { Text("No pending tasks.", color = Muted) }
        items(a.tasks) { t ->
            Card(colors = CardDefaults.cardColors(containerColor = Panel)) {
                Column(Modifier.fillMaxWidth().padding(12.dp)) {
                    Text((if (t.priority == "high") "❗ " else "") + t.title)
                    val due = t.due?.let { d -> if (t.hasTime) d.replace("T", " ") else d.substringBefore("T") }
                    val details = listOfNotNull(due?.let { "due $it" }, t.remindAt?.let { "reminder ${it.replace("T", " ")}" })
                    if (details.isNotEmpty()) Text(details.joinToString(" · "),
                        color = if (t.overdue) Danger else Muted, fontSize = 13.sp)
                }
            }
        }
    }
}

@Composable
private fun SettingsScreen(state: LinkState) {
    val context = LocalContext.current
    val laptop = Store.laptop(context)
    var hosts by remember { mutableStateOf(laptop?.hosts?.joinToString(", ") ?: "") }
    var speak by remember { mutableStateOf(Store.speakAnswers(context)) }
    var lock by remember { mutableStateOf(Store.appLock(context)) }
    var confirmUnpair by remember { mutableStateOf(false) }
    Column(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Title("SETTINGS")
        if (laptop != null) {
            Text("Paired with ${laptop.laptopName} as ${laptop.userName}")
            Text("Laptop fingerprint ${laptop.fingerprint.take(16)}…", color = Muted, fontSize = 12.sp,
                fontFamily = FontFamily.Monospace)
        }
        OutlinedTextField(value = hosts, onValueChange = { hosts = it }, label = { Text("Laptop addresses") },
            supportingText = { Text("Tried in order. Add the laptop's Tailscale address to connect from anywhere.") },
            modifier = Modifier.fillMaxWidth())
        OutlinedButton(onClick = {
            Store.setHosts(context, hosts.split(",").map { it.trim() }.filter { it.isNotEmpty() })
            Bridge.stop(); Bridge.start(context)
        }) { Text("SAVE & RECONNECT") }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Speak DUMBOO's answers", modifier = Modifier.weight(1f))
            Switch(checked = speak, onCheckedChange = { speak = it; Store.setSpeakAnswers(context, it) })
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Lock the app with fingerprint / face", modifier = Modifier.weight(1f))
            Switch(checked = lock, onCheckedChange = { lock = it; Store.setAppLock(context, it) })
        }
        OutlinedButton(onClick = { Bridge.clearChat() }) { Text("CLEAR CHAT ON THIS PHONE") }
        Spacer(Modifier.weight(1f))
        if (!confirmUnpair) {
            OutlinedButton(onClick = { confirmUnpair = true }, modifier = Modifier.fillMaxWidth()) {
                Text("UNPAIR THIS PHONE", color = Danger)
            }
        } else {
            Text("Unpair? This phone's key is deleted. Also remove it on the laptop (ACCOUNT → Phone).",
                color = Danger)
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Button(onClick = {
                    ConnectionService.stop(context); Bridge.stop(); Store.forget(context); Bridge.start(context)
                }) { Text("UNPAIR") }
                OutlinedButton(onClick = { confirmUnpair = false }) { Text("CANCEL") }
            }
        }
        Text(if (state is LinkState.Ready) "Connected via ${state.host}" else "", color = Muted, fontSize = 12.sp)
    }
}

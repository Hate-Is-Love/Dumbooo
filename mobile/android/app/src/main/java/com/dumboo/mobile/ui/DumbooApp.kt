package com.dumboo.mobile.ui

import android.net.Uri
import android.content.Intent
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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
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
            NavigationBarItem(selected = tab == 4, onClick = { tab = 4 },
                icon = { Text("📁", fontSize = 20.sp) }, label = { Text("Files") })
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
                4 -> FilesScreen()
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

@Composable
private fun SettingsScreen(state: LinkState) {
    val context = LocalContext.current
    val laptop = Store.laptop(context)
    var hosts by remember { mutableStateOf(laptop?.hosts?.joinToString(", ") ?: "") }
    var speak by remember { mutableStateOf(Store.speakAnswers(context)) }
    var lock by remember { mutableStateOf(Store.appLock(context)) }
    var laptopVoice by remember { mutableStateOf(Store.laptopVoice(context)) }
    var language by remember { mutableStateOf(Store.voiceLanguage(context)) }
    var confirmUnpair by remember { mutableStateOf(false) }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)) {
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
            Column(Modifier.weight(1f)) {
                Text("DUMBOO's own voice (from the laptop)")
                Text("The same voice as on the laptop. Off: the phone's voice.", color = Muted, fontSize = 12.sp)
            }
            Switch(checked = laptopVoice, onCheckedChange = { laptopVoice = it; Store.setLaptopVoice(context, it) })
        }
        Text("Listening language", color = Muted, fontSize = 12.sp)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("en-IN" to "English (India)", "hi-IN" to "Hindi", "en-US" to "English (US)").forEach { (tag, label) ->
                androidx.compose.material3.FilterChip(selected = language == tag,
                    onClick = { language = tag; Store.setVoiceLanguage(context, tag) }, label = { Text(label, fontSize = 12.sp) })
            }
        }
        AnywhereCard()
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Lock the app with fingerprint / face", modifier = Modifier.weight(1f))
            Switch(checked = lock, onCheckedChange = { lock = it; Store.setAppLock(context, it) })
        }
        OutlinedButton(onClick = { Bridge.clearChat() }) { Text("CLEAR CHAT ON THIS PHONE") }
        PhoneSettings()
        Spacer(Modifier.height(12.dp))
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

/** Use DUMBOO from anywhere: both devices on Tailscale (free, private, no router changes). */
@Composable
private fun AnywhereCard() {
    val context = LocalContext.current
    val laptopReady by Bridge.laptopTailscale.collectAsStateWithLifecycle()
    val phoneHas = remember {
        runCatching { context.packageManager.getPackageInfo("com.tailscale.ipn", 0); true }.getOrDefault(false)
    }
    Card(colors = CardDefaults.cardColors(containerColor = Color(0xFF221B42))) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Title("USE DUMBOO ANYWHERE")
            Text(when {
                laptopReady && phoneHas -> "✓ Ready. The laptop and this phone are on Tailscale: DUMBOO connects on " +
                    "mobile data too. Keep Tailscale switched on on both."
                laptopReady -> "The laptop is on Tailscale. Install Tailscale on this phone and sign in with the same " +
                    "account. That's it."
                else -> "Away from home Wi-Fi, DUMBOO connects through Tailscale (free, private, end-to-end " +
                    "encrypted; your router is never opened). Install it on the laptop (tailscale.com/download) and " +
                    "on this phone, and sign in to both with the same account."
            }, fontSize = 13.sp)
            if (!phoneHas) OutlinedButton(onClick = {
                runCatching {
                    context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("market://details?id=com.tailscale.ipn"))
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                }.onFailure {
                    context.startActivity(Intent(Intent.ACTION_VIEW,
                        Uri.parse("https://play.google.com/store/apps/details?id=com.tailscale.ipn"))
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                }
            }) { Text("GET TAILSCALE FOR THIS PHONE") }
            else if (!laptopReady) Text("Tailscale is on this phone. Now install it on the laptop.", color = Muted,
                fontSize = 12.sp)
        }
    }
}

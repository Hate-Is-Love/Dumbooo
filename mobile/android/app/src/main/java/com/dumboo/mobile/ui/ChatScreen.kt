package com.dumboo.mobile.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateDpAsState
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
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
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.dumboo.mobile.App
import com.dumboo.mobile.Bridge
import com.dumboo.mobile.ChatItem
import com.dumboo.mobile.Features
import com.dumboo.mobile.Listener
import com.dumboo.mobile.Mood
import com.dumboo.mobile.Voice
import com.dumboo.mobile.VoiceUi

internal val Lilac = Color(0xFFC9B8FF)
internal val Night = Color(0xFF1B1533)

/** Talk to DUMBOO: the 3D bot, a tap-to-talk mic that stops when you stop talking, and the conversation. */
@Composable
fun ChatScreen() {
    val context = LocalContext.current
    val chat by Bridge.chat.collectAsStateWithLifecycle()
    val mood by VoiceUi.mood.collectAsStateWithLifecycle()
    val heard by VoiceUi.heard.collectAsStateWithLifecycle()
    val level by VoiceUi.level.collectAsStateWithLifecycle()
    var input by remember { mutableStateOf("") }
    val listener = remember { Listener(context) }
    val listState = rememberLazyListState()
    var refresh by remember { mutableIntStateOf(0) }
    LifecycleResumeEffect(Unit) { refresh++; onPauseOrDispose { } }
    DisposableEffect(Unit) { onDispose { listener.cancel(); listener.destroy() } }

    fun micAllowed() = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
        PackageManager.PERMISSION_GRANTED
    val askMic = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        if (ok) listener.start()
    }
    fun talk() {
        when {
            !micAllowed() -> askMic.launch(Manifest.permission.RECORD_AUDIO)
            mood == Mood.LISTENING -> listener.stop()
            mood == Mood.SPEAKING -> { Voice.stopSpeaking(); listener.start() }
            else -> listener.start()
        }
    }
    LaunchedEffect(Unit) { VoiceUi.listenAgain.collect { if (micAllowed()) listener.start() } }
    LaunchedEffect(chat.size) { if (chat.isNotEmpty()) listState.animateScrollToItem(chat.size - 1) }

    val botHeight by animateDpAsState(if (chat.isEmpty()) 340.dp else 230.dp, tween(600, easing = FastOutSlowInEasing),
        label = "botHeight")

    Column(Modifier.fillMaxSize().background(Night).imePadding()) {
        // Let DUMBOO open apps by itself (otherwise Android makes it a notification to tap)
        val needsOverlay = remember(refresh) { !Features.canOpenFromBackground(context) }
        AnimatedVisibility(needsOverlay, enter = fadeIn(), exit = fadeOut()) {
            Row(Modifier.fillMaxWidth().background(Color(0xFF2A2150)).clickable {
                context.startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:${context.packageName}")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            }.padding(horizontal = 14.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(if (App.needsOverlay) "⚡ So apps and songs open straight away: allow “Display over other apps”."
                    else "⚡ Let DUMBOO open apps, calls and songs instantly", color = Lilac, fontSize = 13.sp,
                    modifier = Modifier.weight(1f))
                Text("ALLOW", color = Cyan, fontSize = 13.sp, fontFamily = FontFamily.Monospace)
            }
        }
        // -- the bot -------------------------------------------------------------------------------------------
        Box(Modifier.fillMaxWidth().height(botHeight)) {
            BotView(Modifier.fillMaxSize(), onChest = { talk() })
            Column(Modifier.align(Alignment.BottomCenter).padding(bottom = 10.dp),
                horizontalAlignment = Alignment.CenterHorizontally) {
                val label = when (mood) {
                    Mood.LISTENING -> if (heard.isBlank()) "Listening…" else "“$heard”"
                    Mood.THINKING -> "Thinking…"
                    Mood.SPEAKING -> "Speaking · tap the mic to interrupt"
                    Mood.IDLE -> if (chat.isEmpty()) "Tap the mic (or my chest) and talk" else ""
                }
                AnimatedVisibility(label.isNotEmpty(), enter = fadeIn(), exit = fadeOut()) {
                    Text(label, color = Color.White.copy(alpha = 0.85f), fontSize = 14.sp, textAlign = TextAlign.Center,
                        modifier = Modifier.widthIn(max = 320.dp).background(Color(0x66000000), RoundedCornerShape(12.dp))
                            .padding(horizontal = 12.dp, vertical = 6.dp))
                }
            }
        }
        // -- conversation ----------------------------------------------------------------------------------------
        LazyColumn(Modifier.weight(1f).fillMaxWidth().padding(horizontal = 12.dp), state = listState,
            verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (chat.isEmpty()) item {
                Text("Try “call mom”, “play Arijit Singh”, “set an alarm for 7”, “what's on my plate today?” or " +
                    "“remind me in 10 minutes to stretch”.", color = Muted, textAlign = TextAlign.Center,
                    modifier = Modifier.fillMaxWidth().padding(16.dp))
            }
            items(chat, key = { it.id }) { Bubble(it) }
            item { Spacer(Modifier.height(4.dp)) }
        }
        // -- input -------------------------------------------------------------------------------------------------
        Row(Modifier.fillMaxWidth().background(Panel).padding(10.dp), verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(value = input, onValueChange = { input = it }, modifier = Modifier.weight(1f),
                placeholder = { Text("Message DUMBOO") }, maxLines = 4,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = { Bridge.sendChat(input); input = "" }))
            Spacer(Modifier.size(10.dp))
            if (input.isNotBlank()) {
                IconButton(onClick = { Bridge.sendChat(input); input = "" }) {
                    Icon(Icons.AutoMirrored.Filled.Send, "Send", tint = Cyan)
                }
            } else {
                MicButton(mood, level) { talk() }
            }
        }
    }
}

@Composable
private fun MicButton(mood: Mood, level: Float, onTap: () -> Unit) {
    val pulse = rememberInfiniteTransition(label = "pulse")
    val breathe by pulse.animateFloat(1f, 1.18f, infiniteRepeatable(tween(900), RepeatMode.Reverse), label = "breathe")
    val ring by animateFloatAsState(if (mood == Mood.LISTENING) 1.15f + level * 0.6f else 1f, tween(90), label = "ring")
    val color by animateColorAsState(when (mood) {
        Mood.LISTENING -> Color(0xFFFF6B9A); Mood.THINKING -> Violet; Mood.SPEAKING -> Lilac; Mood.IDLE -> Cyan
    }, label = "micColor")
    Box(Modifier.size(64.dp), contentAlignment = Alignment.Center) {
        Box(Modifier.size(58.dp).scale(if (mood == Mood.THINKING) breathe else ring).clip(CircleShape)
            .background(color.copy(alpha = 0.25f)))
        Box(Modifier.size(52.dp).clip(CircleShape)
            .background(Brush.radialGradient(listOf(color, color.copy(alpha = 0.75f))))
            .border(2.dp, Color.White.copy(alpha = 0.15f), CircleShape)
            .clickable(onClick = onTap), contentAlignment = Alignment.Center) {
            Text(when (mood) { Mood.LISTENING -> "■"; Mood.SPEAKING -> "🔊"; else -> "🎙" }, fontSize = 22.sp,
                color = Ink)
        }
    }
}

@Composable
private fun Bubble(item: ChatItem) {
    val appear = remember { Animatable(0f) }
    LaunchedEffect(Unit) { appear.animateTo(1f, tween(320, easing = FastOutSlowInEasing)) }
    val shape = if (item.fromMe) RoundedCornerShape(16.dp, 16.dp, 4.dp, 16.dp) else RoundedCornerShape(16.dp, 16.dp, 16.dp, 4.dp)
    Row(Modifier.fillMaxWidth().graphicsLayer { alpha = appear.value; translationY = (1 - appear.value) * 40f },
        horizontalArrangement = if (item.fromMe) Arrangement.End else Arrangement.Start) {
        Card(shape = shape, modifier = Modifier.widthIn(max = 320.dp), colors = CardDefaults.cardColors(
            containerColor = when {
                item.error -> Color(0xFF3A1A20)
                item.fromMe -> Color(0xFF3B2F7A)
                else -> Color(0xFF241D45)
            })) {
            Column(Modifier.padding(12.dp)) {
                if (!item.fromMe) Text("DUMBOO", color = Lilac, fontSize = 10.sp, letterSpacing = 2.sp)
                Text((item.heard?.let { "$it " } ?: "") + plainText(item.text) + if (item.pending) " ▍" else "",
                    color = if (item.error) Danger else MaterialTheme.colorScheme.onSurface)
            }
        }
    }
}

internal fun plainText(markdown: String): String = markdown
    .replace(Regex("\\[\\[(\\d+)]]\\([^)]*\\)"), "[$1]")
    .replace(Regex("\\[([^\\]]+)]\\(([^)]+)\\)"), "$1 ($2)")
    .replace("**", "").replace(Regex("(?<![\\w])_([^_]+)_(?![\\w])"), "$1").replace("`", "")
    .replace(Regex("^#+\\s*", RegexOption.MULTILINE), "").replace(Regex("^- ", RegexOption.MULTILINE), "• ")

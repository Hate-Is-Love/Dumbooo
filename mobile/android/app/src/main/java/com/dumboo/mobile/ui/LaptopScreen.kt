package com.dumboo.mobile.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.dumboo.mobile.Bridge

/**
 * Phase 2: control the laptop. The live screen (tap = click, double tap = double click, long press = right click),
 * a touchpad (drag = move the pointer, tap = click), scrolling, typing, special keys and media shortcuts.
 * Everything is refused by the laptop unless laptop control is switched on there for this phone.
 */
@Composable
fun LaptopScreen() {
    val allowed by Bridge.laptopControl.collectAsStateWithLifecycle()
    if (!allowed) {
        Column(Modifier.fillMaxSize().padding(24.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Title("LAPTOP CONTROL")
            Text("Laptop control is off for this phone.", fontSize = 18.sp)
            Text("To allow it, open DUMBOO on the laptop → ACCOUNT → Phone, select this phone and tick “Allow " +
                "laptop control”. You'll be asked for your DUMBOO password there. The laptop shows a notice " +
                "whenever this phone uses its screen, mouse or keyboard.", color = Muted)
        }
        return
    }
    val frame by Bridge.screen.collectAsStateWithLifecycle()
    var showing by remember { mutableStateOf(false) }
    var text by remember { mutableStateOf("") }
    var bigPad by remember { mutableStateOf(false) }
    if (bigPad) {
        // the whole screen is the touchpad
        Column(Modifier.fillMaxSize().padding(10.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Touchpad(Modifier.fillMaxWidth().weight(1f))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = { Bridge.click() }, modifier = Modifier.weight(1f).height(56.dp)) { Text("LEFT") }
                OutlinedButton(onClick = { Bridge.click(button = "right") }, modifier = Modifier.weight(1f).height(56.dp)) { Text("RIGHT") }
                Button(onClick = { bigPad = false }, modifier = Modifier.height(56.dp)) { Text("DONE") }
            }
        }
        return
    }
    DisposableEffect(Unit) { onDispose { if (showing) Bridge.stopScreen() } }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Title("LAPTOP")
            Spacer(Modifier.weight(1f))
            Button(onClick = {
                showing = !showing
                if (showing) Bridge.startScreen(1024) else Bridge.stopScreen()
            }) { Text(if (showing) "HIDE SCREEN" else "SHOW SCREEN") }
        }
        // -- live screen ----------------------------------------------------------------------------------------
        val current = frame
        if (showing) {
            if (current == null) {
                Box(Modifier.fillMaxWidth().aspectRatio(16f / 10f).background(Panel), contentAlignment = Alignment.Center) {
                    Text("Waiting for the laptop screen…", color = Muted)
                }
            } else {
                var size by remember { mutableStateOf(IntSize.Zero) }
                Image(bitmap = current.bitmap.asImageBitmap(), contentDescription = "Laptop screen",
                    contentScale = ContentScale.Fit,
                    modifier = Modifier.fillMaxWidth()
                        .aspectRatio(current.width.toFloat() / current.height.coerceAtLeast(1))
                        .border(1.dp, Cyan.copy(alpha = 0.4f))
                        .pointerInput(current.width, current.height) {
                            size = IntSize(this.size.width, this.size.height)
                            detectTapGestures(
                                onTap = { p -> Bridge.click(p.x / size.width, p.y / size.height) },
                                onDoubleTap = { p -> Bridge.click(p.x / size.width, p.y / size.height, double = true) },
                                onLongPress = { p -> Bridge.click(p.x / size.width, p.y / size.height, "right") },
                            )
                        })
                Text("Tap to click · double tap to open · long press for right click", color = Muted, fontSize = 12.sp)
            }
        }
        // -- touchpad ----------------------------------------------------------------------------------------------
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Touchpad", color = Muted, fontSize = 12.sp, modifier = Modifier.weight(1f))
            OutlinedButton(onClick = { bigPad = true }) { Text("⛶ FULL SCREEN") }
        }
        Touchpad(Modifier.fillMaxWidth().height(340.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = { Bridge.click() }, modifier = Modifier.weight(1f)) { Text("LEFT") }
            OutlinedButton(onClick = { Bridge.click(button = "right") }, modifier = Modifier.weight(1f)) { Text("RIGHT") }
        }
        // -- keyboard -------------------------------------------------------------------------------------------------
        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(value = text, onValueChange = { text = it }, modifier = Modifier.weight(1f),
                placeholder = { Text("Type on the laptop") }, singleLine = true)
            Spacer(Modifier.width(8.dp))
            Button(onClick = { Bridge.type(text); text = "" }, enabled = text.isNotEmpty()) { Text("TYPE") }
        }
        KeyRow(listOf("Esc" to "esc", "Tab" to "tab", "Enter" to "enter", "⌫" to "backspace", "Del" to "delete"))
        KeyRow(listOf("←" to "left", "↑" to "up", "↓" to "down", "→" to "right", "Win" to "win"))
        KeyRow(listOf("Copy" to "ctrl+c", "Paste" to "ctrl+v", "Undo" to "ctrl+z", "Alt+Tab" to "alt+tab",
            "Save" to "ctrl+s"))
        // -- shortcuts -------------------------------------------------------------------------------------------------
        Text("Shortcuts", color = Muted, fontSize = 12.sp)
        KeyRow(listOf("🔉" to "volumedown", "🔊" to "volumeup", "🔇" to "volumemute", "⏯" to "playpause",
            "⏭" to "nexttrack"))
        KeyRow(listOf("Desktop" to "win+d", "Tasks" to "win+tab", "Snip" to "win+shift+s", "Lock 🔒" to "win+l"))
        Spacer(Modifier.height(12.dp))
    }
}

@Composable
private fun KeyRow(keys: List<Pair<String, String>>) {
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.fillMaxWidth()) {
        keys.forEach { (label, combo) ->
            OutlinedButton(onClick = { Bridge.key(combo) }, modifier = Modifier.weight(1f),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 2.dp, vertical = 8.dp)) {
                Text(label, fontSize = 12.sp, maxLines = 1)
            }
        }
    }
}

/**
 * Touchpad with scrolling on the right edge. Faster swipes move the pointer further (pointer acceleration); moves are
 * added up and sent ~120 times a second, so the pointer follows the finger without lag.
 */
@Composable
private fun Touchpad(modifier: Modifier) {
    Row(modifier, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Box(Modifier.weight(1f).fillMaxHeight().background(Panel, RoundedCornerShape(16.dp))
            .border(1.dp, Cyan.copy(alpha = 0.18f), RoundedCornerShape(16.dp))
            .pointerInput(Unit) {
                detectDragGestures { change, drag ->
                    change.consume()
                    val speed = kotlin.math.hypot(drag.x, drag.y)
                    val gain = 1.4f + (speed / 9f).coerceAtMost(3.2f)       // slow = precise, fast = far
                    Bridge.move(drag.x * gain, drag.y * gain)
                }
            }
            .pointerInput(Unit) {
                detectTapGestures(onTap = { Bridge.click() }, onDoubleTap = { Bridge.click(double = true) },
                    onLongPress = { Bridge.click(button = "right") })
            }, contentAlignment = Alignment.Center) {
            Text("drag to move · tap to click · hold for right click", color = Muted, fontSize = 12.sp)
        }
        Box(Modifier.width(56.dp).fillMaxHeight().background(Panel, RoundedCornerShape(16.dp))
            .pointerInput(Unit) {
                var carry = 0f
                detectDragGestures { change, drag ->
                    change.consume()
                    carry += drag.y
                    val clicks = (carry / 36f).toInt()
                    if (clicks != 0) {
                        Bridge.scroll(-clicks)
                        carry -= clicks * 36f
                    }
                }
            }, contentAlignment = Alignment.Center) {
            Text("⇕", color = Cyan, fontSize = 22.sp)
        }
    }
}

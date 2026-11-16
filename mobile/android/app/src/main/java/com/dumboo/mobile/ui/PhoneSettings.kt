package com.dumboo.mobile.ui

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.LifecycleResumeEffect
import com.dumboo.mobile.Bridge
import com.dumboo.mobile.Features
import com.dumboo.mobile.FileTransfer
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Settings → “What the laptop may do” (Phases 3-4): every feature starts OFF. Switching one on asks for Android's
 * permission where one is needed. Also “Share with the laptop”: send this phone's clipboard or a file.
 */
@Composable
fun PhoneSettings() {
    val context = LocalContext.current
    val clipboard = LocalClipboardManager.current
    val scope = rememberCoroutineScope()
    var tick by remember { mutableIntStateOf(0) }       // re-read permissions when coming back from Android settings
    LifecycleResumeEffect(Unit) {
        tick++
        Bridge.sendFeatures()
        onPauseOrDispose { }
    }
    val askCalls = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        tick++
        Bridge.sendFeatures()
    }
    val pickFile = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) scope.launch {
            Toast.makeText(context, "Sending…", Toast.LENGTH_SHORT).show()
            val message = withContext(Dispatchers.IO) { FileTransfer.send(context, uri) }
            Toast.makeText(context, message, Toast.LENGTH_LONG).show()
        }
    }
    fun open(intent: Intent) = runCatching { context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }

    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Title("SHARE WITH THE LAPTOP")
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            OutlinedButton(onClick = {
                val text = clipboard.getText()?.text.orEmpty()
                Toast.makeText(context, Bridge.pushClipboard(text), Toast.LENGTH_LONG).show()
            }) { Text("📋 SEND CLIPBOARD") }
            OutlinedButton(onClick = { pickFile.launch(arrayOf("*/*")) }) { Text("📤 SEND A FILE") }
        }
        Text("Or use Share → “DUMBOO laptop” in any app. Files land in the laptop's Downloads\\DUMBOO Mobile.",
            color = Muted, fontSize = 12.sp)

        Title("WHAT THE LAPTOP MAY DO")
        Text("All off until you switch them on. The laptop asks, this phone decides.", color = Muted, fontSize = 12.sp)
        key(tick) {
            Features.ALL.forEach { feature ->
                var on by remember { mutableIntStateOf(if (Features.on(context, feature)) 1 else 0) }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(Features.LABELS.getValue(feature))
                        if (on == 1 && !Features.usable(context, feature)) {
                            Text("Needs Android's permission: switch it off and on again.", color = Danger,
                                fontSize = 12.sp)
                        }
                    }
                    Switch(checked = on == 1, onCheckedChange = { want ->
                        Features.set(context, feature, want)
                        on = if (want) 1 else 0
                        if (want) when (feature) {
                            "calls" -> askCalls.launch(arrayOf(Manifest.permission.CALL_PHONE,
                                Manifest.permission.READ_CONTACTS))
                            "notifications" -> if (!Features.notificationAccess(context)) {
                                Toast.makeText(context, "Turn on DUMBOO Mobile in the list", Toast.LENGTH_LONG).show()
                                open(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
                            }
                        }
                    })
                }
            }
            val needsScreens = listOf("calls", "music", "open_apps", "screen").any { Features.on(context, it) }
            if (needsScreens && !Features.canOpenFromBackground(context)) {
                Text("So calls, music and apps can open while DUMBOO Mobile is in the background, allow it to " +
                    "“display over other apps”. Without it you'll get a notification to tap instead.",
                    color = Muted, fontSize = 12.sp)
                OutlinedButton(onClick = {
                    open(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:${context.packageName}")))
                }) { Text("ALLOW OPENING FROM THE BACKGROUND") }
            }
            if (Features.on(context, "screen") && !Features.controlServiceOn(context)) {
                Text("To let the laptop tap and type (not just watch), turn on “DUMBOO screen control” in " +
                    "Accessibility. It only works while you're sharing the screen.", color = Muted, fontSize = 12.sp)
                OutlinedButton(onClick = { open(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }) {
                    Text("OPEN ACCESSIBILITY SETTINGS")
                }
            }
        }
    }
}

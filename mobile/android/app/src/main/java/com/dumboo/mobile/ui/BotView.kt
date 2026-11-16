package com.dumboo.mobile.ui

import android.annotation.SuppressLint
import android.graphics.Color
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.dumboo.mobile.Mood
import com.dumboo.mobile.VoiceUi
import kotlinx.coroutines.delay

/**
 * DUMBOO's interactive 3D mini bot (three.js, bundled in the app; nothing is loaded from the internet).
 * Drag to spin it, tap its head or antenna; tapping its chest calls [onChest] (start listening). It listens,
 * thinks and talks along with DUMBOO.
 */
@SuppressLint("SetJavaScriptEnabled")
@Composable
fun BotView(modifier: Modifier, onChest: () -> Unit) {
    val context = LocalContext.current
    val mood by VoiceUi.mood.collectAsStateWithLifecycle()
    val web = remember {
        WebView(context).apply {
            setBackgroundColor(Color.parseColor("#1b1533"))
            settings.javaScriptEnabled = true
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            settings.domStorageEnabled = false
            webViewClient = object : WebViewClient() {
                // only the bundled page; never navigate anywhere else
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest) = true
            }
            addJavascriptInterface(object {
                @JavascriptInterface
                fun onBotTap(part: String) {
                    if (part == "chest" || part == "body") post { onChest() }
                }
            }, "Android")
            loadUrl("file:///android_asset/bot/bot.html")
        }
    }
    LaunchedEffect(mood) {
        web.evaluateJavascript("window.bot && bot.setState('${mood.name.lowercase()}')", null)
    }
    LaunchedEffect(Unit) {
        var last = -1f
        while (true) {
            val level = VoiceUi.level.value
            if (kotlin.math.abs(level - last) > 0.02f || (level == 0f && last != 0f)) {
                web.evaluateJavascript("window.bot && bot.setLevel(${"%.3f".format(java.util.Locale.US, level)})", null)
                last = level
            }
            delay(if (mood == Mood.IDLE) 250 else 50)
        }
    }
    AndroidView(factory = { web }, modifier = modifier)
}

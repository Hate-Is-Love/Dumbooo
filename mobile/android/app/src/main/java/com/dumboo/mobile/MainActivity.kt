package com.dumboo.mobile

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_STRONG
import androidx.biometric.BiometricManager.Authenticators.DEVICE_CREDENTIAL
import androidx.biometric.BiometricPrompt
import androidx.compose.runtime.mutableStateOf
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.lifecycleScope
import com.dumboo.mobile.ui.DumbooApp
import kotlinx.coroutines.launch

class MainActivity : FragmentActivity() {
    private val unlocked = mutableStateOf(false)
    private val pendingPairLink = mutableStateOf<String?>(null)
    private val askNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ConnectionService.createChannels(this)
        Voice.init(this)
        pendingPairLink.value = pairLink(intent)
        if (Store.laptop(this) != null) startLink()
        setContent {
            DumbooApp(
                unlocked = unlocked.value,
                pairLink = pendingPairLink.value,
                onPairLinkUsed = { pendingPairLink.value = null },
                onPaired = { startLink() },
                onUnlock = { requestUnlock() },
            )
        }
        requestUnlock()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        pairLink(intent)?.let { pendingPairLink.value = it }
    }

    override fun onStart() {
        super.onStart()
        App.inForeground = true
        App.activity = java.lang.ref.WeakReference(this)
        Bridge.sendFeatures()          // permissions may have changed in the phone's settings
    }

    override fun onStop() {
        App.inForeground = false
        App.activity = null
        super.onStop()
    }

    override fun onDestroy() {
        Voice.stopSpeaking()
        super.onDestroy()
    }

    private fun pairLink(intent: Intent?): String? =
        intent?.data?.takeIf { it.scheme == "dumboo" && it.host == "pair" }?.toString()

    private fun startLink() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED) {
            askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        ConnectionService.start(this)
    }

    /** App lock: fingerprint/face or the phone's PIN, if the phone has one and the lock is switched on. */
    private fun requestUnlock() {
        val authenticators = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) BIOMETRIC_STRONG or DEVICE_CREDENTIAL
        else BiometricManager.Authenticators.BIOMETRIC_WEAK or DEVICE_CREDENTIAL
        val possible = BiometricManager.from(this).canAuthenticate(authenticators) == BiometricManager.BIOMETRIC_SUCCESS
        if (!Store.appLock(this) || !possible || Store.laptop(this) == null) {
            unlocked.value = true
            return
        }
        val prompt = BiometricPrompt(this, ContextCompat.getMainExecutor(this),
            object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                    unlocked.value = true
                }
            })
        prompt.authenticate(BiometricPrompt.PromptInfo.Builder()
            .setTitle("Unlock DUMBOO Mobile")
            .setSubtitle("Your phone can talk to your laptop")
            .setAllowedAuthenticators(authenticators)
            .build())
    }
}

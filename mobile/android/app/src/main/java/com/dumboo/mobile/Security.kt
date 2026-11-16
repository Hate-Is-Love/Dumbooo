package com.dumboo.mobile

import android.content.Context
import android.net.Uri
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.Signature
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.security.spec.ECGenParameterSpec
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.X509TrustManager

/**
 * This phone's identity: an EC P-256 key pair created inside the Android Keystore. The private key never leaves
 * the Keystore (hardware-backed where the phone has it) and can't be exported or backed up.
 */
object DeviceKeys {
    private const val ALIAS = "dumboo_device_key"
    private val keyStore: KeyStore get() = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    fun ensure() {
        if (keyStore.containsAlias(ALIAS)) return
        val generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore")
        generator.initialize(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .build()
        )
        generator.generateKeyPair()
    }

    /** The public key as X.509 SubjectPublicKeyInfo (DER), what the laptop stores when pairing. */
    fun publicKeyDer(): ByteArray {
        ensure()
        return keyStore.getCertificate(ALIAS).publicKey.encoded
    }

    /** Signs DUMBOO-AUTH-v1|nonce|laptop-fingerprint: proves this is the paired phone, for this laptop only. */
    fun signChallenge(nonce: ByteArray, laptopFingerprint: String): String {
        ensure()
        val message = ByteArrayOutputStream().apply {
            write("DUMBOO-AUTH-v1|".toByteArray(Charsets.US_ASCII))
            write(nonce)
            write("|".toByteArray(Charsets.US_ASCII))
            write(laptopFingerprint.toByteArray(Charsets.US_ASCII))
        }.toByteArray()
        val signer = Signature.getInstance("SHA256withECDSA")
        signer.initSign(keyStore.getKey(ALIAS, null) as PrivateKey)
        signer.update(message)
        return Base64.encodeToString(signer.sign(), Base64.NO_WRAP)
    }

    fun delete() {
        if (keyStore.containsAlias(ALIAS)) keyStore.deleteEntry(ALIAS)
    }
}

/**
 * Trusts exactly one certificate: the laptop's, identified by the SHA-256 fingerprint from the pairing QR code.
 * No certificate authority can vouch for anything else, so a fake Wi-Fi or a man in the middle is rejected.
 */
class PinnedTrustManager(private val fingerprint: String) : X509TrustManager {
    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        val leaf = chain?.firstOrNull() ?: throw CertificateException("no certificate")
        val actual = sha256Hex(leaf.encoded)
        if (!MessageDigest.isEqual(actual.toByteArray(), fingerprint.lowercase().toByteArray())) {
            throw CertificateException("This isn't your paired laptop (certificate fingerprint changed).")
        }
    }

    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) =
        throw CertificateException("not used")

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()

    fun socketFactory(): SSLSocketFactory =
        SSLContext.getInstance("TLS").apply { init(null, arrayOf(this@PinnedTrustManager), null) }.socketFactory

    companion object {
        fun sha256Hex(data: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }
    }
}

/** What the laptop's QR code contains: addresses, port, certificate fingerprint and the one-time code. */
data class PairingOffer(val hosts: List<String>, val port: Int, val fingerprint: String, val code: String,
                        val laptopName: String) {
    companion object {
        fun parse(text: String): PairingOffer {
            val uri = Uri.parse(text.trim())
            require(uri.scheme == "dumboo" && uri.host == "pair") { "That isn't a DUMBOO pairing code." }
            val data = uri.getQueryParameter("d") ?: throw IllegalArgumentException("The code is incomplete.")
            val json = JSONObject(String(Base64.decode(data, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)))
            require(json.optInt("v") == 1) { "This code is from a newer DUMBOO. Update the app." }
            val hosts = json.getJSONArray("h").let { array -> List(array.length()) { array.getString(it) } }
            val fp = json.getString("fp")
            require(fp.length == 64 && fp.all { it in "0123456789abcdef" }) { "The code's fingerprint is invalid." }
            return PairingOffer(hosts, json.getInt("p"), fp, json.getString("c"), json.optString("n", "laptop"))
        }
    }
}

/** The paired laptop, kept in private app storage (excluded from backups). */
data class PairedLaptop(val hosts: List<String>, val port: Int, val fingerprint: String, val deviceId: Int,
                        val laptopName: String, val userName: String)

object Store {
    private fun prefs(context: Context) = context.getSharedPreferences("dumboo", Context.MODE_PRIVATE)

    fun laptop(context: Context): PairedLaptop? {
        val p = prefs(context)
        val id = p.getInt("device_id", -1)
        if (id < 0) return null
        return PairedLaptop(p.getString("hosts", "")!!.split(",").filter { it.isNotBlank() }, p.getInt("port", 8765),
            p.getString("fingerprint", "")!!, id, p.getString("laptop_name", "laptop")!!,
            p.getString("user_name", "")!!)
    }

    fun saveLaptop(context: Context, laptop: PairedLaptop) {
        prefs(context).edit()
            .putString("hosts", laptop.hosts.joinToString(","))
            .putInt("port", laptop.port)
            .putString("fingerprint", laptop.fingerprint)
            .putInt("device_id", laptop.deviceId)
            .putString("laptop_name", laptop.laptopName)
            .putString("user_name", laptop.userName)
            .apply()
    }

    fun setHosts(context: Context, hosts: List<String>) =
        prefs(context).edit().putString("hosts", hosts.joinToString(",")).apply()

    fun forget(context: Context) {
        prefs(context).edit().clear().apply()
        DeviceKeys.delete()
    }

    fun speakAnswers(context: Context) = prefs(context).getBoolean("speak", true)
    fun setSpeakAnswers(context: Context, on: Boolean) = prefs(context).edit().putBoolean("speak", on).apply()
    fun appLock(context: Context) = prefs(context).getBoolean("app_lock", true)
    /** Speak with DUMBOO's own voice from the laptop (otherwise the phone's voice). */
    fun laptopVoice(context: Context) = prefs(context).getBoolean("laptop_voice", true)
    fun setLaptopVoice(context: Context, on: Boolean) = prefs(context).edit().putBoolean("laptop_voice", on).apply()
    /** Speech recognition language, e.g. en-IN (Indian English, good with Hindi names and songs). */
    fun voiceLanguage(context: Context): String = prefs(context).getString("voice_language", "en-IN")!!
    fun setVoiceLanguage(context: Context, tag: String) = prefs(context).edit().putString("voice_language", tag).apply()
    fun setAppLock(context: Context, on: Boolean) = prefs(context).edit().putBoolean("app_lock", on).apply()
}

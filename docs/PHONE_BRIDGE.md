# DUMBOO Mobile: phone bridge design

DUMBOO Mobile is an Android app (Kotlin) that connects to DUMBOO on the laptop. This document describes the
connection, the security model and the message protocol. It is built in phases; **Phase 1** (this version) covers
pairing, talking to DUMBOO by text and voice, the agenda and reminders on the phone.

| Phase | Adds |
|---|---|
| 1 | Secure pairing; chat and push-to-talk with DUMBOO; tasks and schedule; reminders as phone notifications |
| 2 | Phone → laptop control: DUMBOO commands, laptop screen with touchpad and keyboard |
| 3 | Laptop → phone: notifications and replies, files and clipboard, calls, music, find my phone |
| 4 | Full phone screen control from the laptop (Accessibility + screen sharing, approved on the phone) |
| 5 | Anywhere over the internet (Tailscale), security review |

## Connection

```
 DUMBOO Mobile ──── TLS 1.2+ WebSocket (wss://<laptop>:8765) ────► bridge/ in DUMBOO
   private key in the Android Keystore                              laptop certificate (self-signed)
   pins the laptop certificate's SHA-256                            knows each phone's public key
```

* The laptop runs a WebSocket server (`bridge/server.py`) on port **8765** (setting `bridge.port`), only while the
  phone link is switched on (`bridge.enabled`, off by default). Windows asks once to allow it through the firewall.
* On the same Wi-Fi the phone uses the laptop's local address. From anywhere (Phase 5) both devices join a private
  **Tailscale** network and the phone uses the laptop's Tailscale address (100.x.y.z); the router is never opened.

## Security model

1. **Laptop identity.** On first use DUMBOO creates an EC P-256 key and a self-signed certificate
   (`data/bridge/`). The private key file is encrypted; its password is protected with Windows DPAPI.
2. **Pairing (QR code).** The signed-in user clicks *Pair a phone* on the ACCOUNT page. DUMBOO shows a QR code with
   the laptop's addresses, port, the certificate's SHA-256 fingerprint and a **one-time code** (40 bits, valid for
   2 minutes, at most 5 tries, then the window closes). Pairing is only possible while that window is open.
3. **Phone identity.** The app creates an EC P-256 key pair inside the **Android Keystore** (hardware-backed where
   available, never exportable). Pairing sends the public key; the laptop stores it with the user it belongs to.
4. **Every connection is authenticated.**
   * TLS: the app accepts the laptop only if the certificate's SHA-256 matches the fingerprint from the QR code
     (certificate pinning, no certificate authorities involved).
   * The laptop sends a random 32-byte challenge; the phone signs
     `DUMBOO-AUTH-v1|<challenge>|<laptop fingerprint>` with its Keystore key (ECDSA SHA-256). The laptop checks the
     signature with the stored public key. Binding the fingerprint into the signature means a signature can't be
     replayed to a different server.
5. **Least privilege.** Each paired phone has capabilities, all checked on the laptop: `chat`, `voice`, `agenda`,
   `notifications` (Phase 1, on by default) and `laptop_control` (Phase 2, **off** by default). Until laptop control
   is enabled, phone messages can't run automation actions or change security settings; DUMBOO says so.
6. **Its own session.** An authenticated phone gets its own DUMBOO session for the paired user, ended when the phone
   disconnects. The phone has its own conversation, so it never interrupts or reads the laptop's.
7. **Revocation and audit.** Phones are listed on the ACCOUNT page and can be removed instantly (open connections
   are closed). Pairing, connections, refusals and removals are recorded in `device_events`.
8. **Limits.** Frames are at most 2 MB, voice clips at most 20 s, at most 20 messages per 10 seconds per phone, and
   unauthenticated connections are closed after 10 seconds.
9. **On the phone.** The app can be locked with fingerprint/face or the device PIN.

## Messages

All frames are JSON text. `t` is the message type.

**Handshake**

| Direction | Message |
|---|---|
| laptop → phone | `{"t":"hello","v":1,"nonce":"<base64>"}` |
| phone → laptop (first time) | `{"t":"pair","code":"ABCD-EFGH","name":"Pixel 8","pub":"<base64 SPKI DER>","sig":"<base64>"}` |
| phone → laptop | `{"t":"auth","device":12,"sig":"<base64>"}` |
| laptop → phone | `{"t":"ready","device":12,"user":"Roshan","caps":{...}}` or `{"t":"error","code":"...","message":"..."}` |

**After `ready`**

| Direction | Message | Meaning |
|---|---|---|
| phone → laptop | `{"t":"chat","id":1,"text":"what's on my plate today"}` | Ask DUMBOO |
| phone → laptop | `{"t":"voice","id":2,"rate":16000,"pcm":"<base64 16-bit mono>"}` | Push-to-talk clip |
| laptop → phone | `{"t":"transcript","id":2,"text":"..."}` | What DUMBOO heard |
| laptop → phone | `{"t":"answer.delta","id":1,"text":"..."}` | The answer so far (streaming) |
| laptop → phone | `{"t":"answer","id":1,"text":"<markdown>","speech":"...","kind":"agenda"}` | Final answer |
| laptop → phone | `{"t":"answer.error","id":1,"message":"..."}` | The request failed |
| phone → laptop | `{"t":"agenda.get"}` | Ask for tasks and events |
| laptop → phone | `{"t":"agenda","tasks":[...],"events":[...]}` | Tasks and upcoming events |
| laptop → phone | `{"t":"reminder","title":"...","text":"...","kind":"task","at":"..."}` | A reminder is due |
| either | `{"t":"ping"}` / `{"t":"pong"}` | Keep-alive |

## Files

* Laptop: `bridge/identity.py` (certificate), `bridge/devices.py` (pairing codes, paired phones, audit),
  `bridge/protocol.py` (messages and limits), `bridge/server.py` (WebSocket server, one handler per phone),
  `bridge/link.py` (one phone's conversation, voice, agenda and reminders), `dashboard/widgets/phone_section.py`.
* Phone: `mobile/android/` (Kotlin, Jetpack Compose).
* Tests: `tests/test_bridge.py` (real TLS WebSocket connections in-process; no phone needed).

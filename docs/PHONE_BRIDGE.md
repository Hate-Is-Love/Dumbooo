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

## Phase 2: laptop control

Switched on per phone with the `laptop_control` capability. Allowing it needs the account password
(`DeviceRegistry.set_capability(..., password)`); turning it off doesn't. Live phones receive
`{"t":"caps","caps":{...}}` at once. Every message below also needs the user's `automation.input` permission.
Laptop actions from phone chat (automation, security changes, opening or saving through research) are allowed only
while the capability is on (`AssistantCore._phone_may_control` → `PhoneBridge.allows_control`).

| Direction | Message | Meaning |
|---|---|---|
| phone → laptop | `{"t":"screen.start","fps":5,"width":1024,"quality":60}` | Start the screen picture (at most 8 fps, 320–1600 px wide) |
| laptop → phone | `{"t":"screen.frame","seq":3,"w":2560,"h":1600,"img":"<base64 JPEG>"}` | One frame; `w`/`h` are the laptop's real size |
| phone → laptop | `{"t":"screen.ack","seq":3}` | Frame received; the next one is sent only after this (or after 2 s) |
| phone → laptop | `{"t":"screen.stop"}` | Stop |
| phone → laptop | `{"t":"input.click","x":0.5,"y":0.25,"button":"left","double":false}` | Click at a position given as 0–1 across and down; omit x/y to click where the pointer is |
| phone → laptop | `{"t":"input.move","dx":12,"dy":-4}` | Touchpad: move the pointer (kept on the main screen) |
| phone → laptop | `{"t":"input.scroll","clicks":-3}` | Scroll under the pointer (at most 15) |
| phone → laptop | `{"t":"input.key","combo":"ctrl+c"}` | Key or combination; only known key names, including media keys (`volumeup`, `playpause` …) |
| phone → laptop | `{"t":"input.text","text":"hello"}` | Type into the window in front (at most 500 characters) |

The laptop publishes `PHONE_CONTROL` (banner and notification) and audits `remote_control`, at most every 20 s while
in use, and also when the screen picture stops. The `input.*` and `screen.ack` messages have their own rate limit
(400 per 10 s). Code: `bridge/remote.py` (screen frames with mss + OpenCV JPEG; input through the existing
`automation/platform` desktop layer).

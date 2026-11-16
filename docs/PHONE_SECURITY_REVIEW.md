# DUMBOO Mobile: security review

Scope: the phone link in `bridge/` (laptop) and the Android app in `mobile/android/`, Phases 1–5. Reviewed on
2026-11-16 against the code in this repository. This is a self-review by the developer, not an independent audit.

## What is protected

| Asset | Where it lives |
|---|---|
| Control of the laptop (mouse, keyboard, screen, DUMBOO commands) | laptop |
| Control of the phone (calls, apps, screen taps) | phone |
| Private content: chats with DUMBOO, notifications and replies, clipboard, files, both screens | in transit and on both devices |
| The DUMBOO account and its security settings | laptop |

## Who might attack

1. **Someone on the same network** (café Wi-Fi, a shared flat) who can see or change traffic.
2. **Someone on the internet** who finds the laptop's port.
3. **Another app on the phone**, without root.
4. **Someone holding the unlocked phone** or sitting at the unlocked laptop for a moment.
5. **A paired phone that has been compromised.**

## Controls

| # | Control | Stops |
|---|---|---|
| C1 | TLS 1.2+ for everything. The app pins the laptop certificate's SHA-256 from the QR code. Plain-text traffic is forbidden by `network_security_config.xml` | 1 |
| C2 | Pairing needs the QR code's one-time code: 40 bits, 2 minutes, 5 tries, only while the owner has the window open | 1, 2 |
| C3 | Each phone proves itself with an Android Keystore key (EC P-256, never exported, never backed up), signing a fresh 32-byte challenge bound to the laptop's fingerprint | 1, 2 |
| C4 | Connections from public internet addresses are refused before any message is read. Allowed: private ranges, loopback, link-local and Tailscale (100.64.0.0/10, fd7a:115c:a1e0::/48). `bridge.allow_public` turns this off and is false by default | 2 |
| C5 | Laptop control is **off** per phone; turning it on needs the account password. Gates in Automation, Guardian and Research refuse phone-sourced actions until then | 4, 5 |
| C6 | Phone control: every feature (notifications, clipboard, files, calls, music, find my phone, apps, screen) is **off** until the owner switches it on in the app, and the phone checks it again for every command. Android's own permissions (notification access, call permission, Accessibility, screen-capture consent) are on top | 4, 5 |
| C7 | Screen taps only work while the phone's screen is being shared. Android asks for consent every time sharing starts and shows that it is running | 4, 5 |
| C8 | Visible use: a laptop banner and notification while a phone controls it; a phone notification with STOP while its screen is shared or it rings | 4, 5 |
| C9 | Limits: 2 MB per message, 20 messages per 10 s (400 for touch input), 50 MB per file, at most 4 files arriving at once, 1 GB received per connection, 10 s to authenticate | 2, 5 |
| C10 | File names from the other side are reduced to a plain name (no folders, no `..`); files are never opened automatically | 5 |
| C11 | Removing a phone on the laptop closes its connection at once; pairing, connections, refusals, control, commands and files are written to `device_events` | 5 |
| C12 | The app's components that the system binds (notification listener, Accessibility) require the system's bind permissions; the screen-share screen and services are not exported | 3 |
| C13 | Share → DUMBOO laptop asks the owner before sending and accepts only `content://` items | 3 |
| C14 | Optional fingerprint/face/PIN lock for the app. App data is excluded from backups | 4 |

## Findings

Issues found while reviewing the Phase 3–5 code. All are fixed in this version.

| # | Finding | Severity | Fix |
|---|---|---|---|
| F1 | The phone used the laptop's transfer id directly in a temporary file name. A crafted id containing `../` could write a `.part` file elsewhere inside the app's private storage. Exploiting it needed an authenticated laptop | Low | The id is reduced to letters and digits (`FileTransfer.kt`) |
| F2 | Any app on the phone could open the share screen directly and send its own files or text to the laptop without the owner noticing. `file://` items could point at DUMBOO's private files | Medium | The share screen now asks first and accepts only `content://` items (`ShareActivity.kt`) |
| F3 | A compromised paired phone could keep sending 50 MB files until the laptop's disk was full | Low | At most 4 files at once and 1 GB per connection (`bridge/phone.py`) |
| F4 | The bridge accepted connections from any address. Exposing the port to the internet relied on pairing and keys alone | Medium | Public addresses are refused unless `bridge.allow_public` is set (C4) |
| F5 | (Phase 1) On Windows, TLS 1.3 session tickets raced with the first message and could drop connections | Reliability | `num_tickets = 0` |

## Remaining risks (accepted)

* **A compromised paired phone is trusted for what it shares.** It can claim every feature is on and send
  notifications, files or clipboard text to the laptop. Clipboard text from the phone replaces the laptop's
  clipboard, and a banner says so. Don't paste text you didn't copy, and remove a lost phone at once (ACCOUNT → Phone).
* **Whoever photographs the QR code within 2 minutes can pair.** Show it only to your own phone. The window
  closes after one pairing.
* **Notifications appear on the laptop screen.** The previews show sender and text for 8 seconds. Leave
  "notifications" off if other people can see your laptop.
* **Voice commands on the laptop reach the phone.** Anyone DUMBOO accepts at the laptop can say "call mom" or "find
  my phone". DUMBOO's own sign-in and guardian rules apply.
* **The 100.64.0.0/10 range is also used for carrier-grade NAT.** Tailscale uses it too, so it is allowed.
  Customers of the same internet provider may share that space. Pairing and keys (C2, C3) still apply.
* **Opening screens from the background.** If the owner allows DUMBOO Mobile to "display over other apps", the
  laptop can start calls and open apps without a tap on the phone. Without it, each such request becomes a
  notification to tap.
* **Owner commands on the phone (1.1).** Phone commands spoken in the app skip the "what the laptop may do"
  switches. The phone allows this only for everyday actions (calls, music, apps, alarms, torch, maps, texts) and
  only within 90 s of its owner sending a message. A compromised laptop could use that window to place a call or
  open an app; texts are only pre-filled, never sent.
* **Not independently audited.** There is no fuzzing yet, and real-device testing so far is by the owner only.

## Tests that cover this

* `tests/test_bridge.py`: pairing, wrong codes, impostor keys, replay, removal, capabilities, the password gate, flood
  limits.
* `tests/test_phone_link.py`: features switched off on the phone, unknown actions, file-name traversal, oversized
  files, too many files, screen frames only when shared, the public-address filter.

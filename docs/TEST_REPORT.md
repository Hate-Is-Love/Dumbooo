# Phase 1 verification report (2026-10-21)

Machine: Windows 11 (build 26200), Python 3.11.9, Intel Smart Sound mic array,
Realtek speakers, Noise TWO Bluetooth headset.

## Automated tests: `python -m pytest`, 167 passed, 0 skipped

| File | Covers |
|---|---|
| test_state_machine.py | transitions, refusals, shutdown from any state, thread safety, event bus order and error isolation |
| test_settings.py | defaults, validation (11 invalid cases), persistence, corrupt file, session overrides |
| test_tasks.py | CRUD, validation, ordering, SQL-injection safety, persistence across restart, migrations |
| test_schedules.py | CRUD, date/time validation, today/upcoming, reminders (on time, once, missed, re-armed) |
| test_vad.py | segment start/end, pre-roll, natural pauses, silence end, max duration, short-blip rejection, listen timeout, hysteresis; **real Silero** on silence/noise/hum and **real Piper speech** at 3 volumes and in noise; noise suppression (≥10 dB noise reduction, <3 dB speech loss) |
| test_audio_pipeline.py | single microphone stream, re-chunking, overflow, mute, missing mic, resampler, mode routing, interruption carry-over, mic disconnect + retry; interruption: echo ignored, user barge-in detected, loud noise ignored, cough ignored, grace period |
| test_wake_word.py | phrase variants and near-misses, follow-up extraction, duplicate/cooldown prevention, engine auto-selection, openWakeWord chunking/threshold, spotter burst handling |
| test_speech.py | STT success/failure/empty/hallucination/model-load failure; TTS lifecycle events, prompt stop, no overlap, missing voice, synthesis failure, **real Piper** synthesis |
| test_orchestrator.py | 24 interaction flows incl. wake→listen→process→speak→idle, interruption, timeouts, auto-sleep, errors and recovery |
| test_application.py | headless startup, typed round trip, no-mic operation, text-only mode, graceful shutdown (no leftover threads), live settings, real system metrics, missing battery |
| test_dashboard.py | every core state renders (<50 ms/frame), animation pauses when hidden, window interaction, tasks/system panels, sleep hides window while the wake listener stays on, ✕ = sleep, reminder banner |

## Checks run on the real hardware (by the developer, not by you)

| Check | Result |
|---|---|
| Real microphone opens (default MME at 16 kHz; WASAPI device falls back to 48 kHz + resampler) | OK. 61/62 frames per 2 s, 0 dropped |
| Real speaker playback (Bluetooth headphones, WASAPI at 48 kHz) | OK |
| Core start → all services ready | 7 s cold (models load in the background; UI not blocked) |
| Typed message → speech starts | 0.21 s |
| STOP during speech → audio stops | 0.016 s |
| Shutdown | mic closed, no DUMBOO threads left |
| Second launch | wakes the running instance; no second microphone stream |
| NS + VAD cost | 0.6 ms per 32 ms frame (~2 % of one core) |
| Full voice chain on synthesized speech fed through a virtual mic in real time: “Hey Dumboo.” … “Add milk to my shopping list.” | wake detected 0.6 s after the phrase; transcript exact; reply spoken; back to IDLE |
| Whisper spelling of the name | `base.en`: “Dumbo” every time; `tiny.en`: inconsistent → `base.en` chosen + phonetic matching |

## Not verified: needs you (README section 8)

Your own voice saying the wake phrase at a distance, false-wake rate over
time, real barge-in over loudspeakers (acoustic echo), noise suppression with
a real fan/TV, USB-mic unplugging, and reminder notifications appearing in
Windows. These all need a person and a real room.

---

# Module 2 verification (AI conversation engine, 2026-10-21)

## Automated tests: `python -m pytest`, **246 passed, 0 skipped** (Module 1 + Module 2)

No test contacts a real AI provider, uses an API key, or costs money.

| File | Tests | Covers |
|---|---|---|
| test_ai_providers.py | 24 | The **real `google-genai` and `openai` SDKs** pointed at a local imitation server: request shape (history roles, system prompt, key in header not URL, `store=false`), streaming (Gemini SSE, OpenAI Responses events), daily free-tier quota vs per-minute limit (Gemini quota IDs), `insufficient_quota` (OpenAI), invalid key, unknown model, overloaded, timeout, connection refused, empty / truncated / safety-blocked answers, unsupported temperature/thinking/reasoning auto-dropped once, cancellation mid-stream, health check via models.get/retrieve |
| test_ai_controller.py | 31 | context window (turns + characters, never splits a turn), long-message shortening, prompt (concise, voice vs text style, sent once), voice + text sharing context, streaming events, duplicate filter, latest-message-wins cancellation (no mixed tokens), cancel discards answer, retries with backoff for temporary errors only and bounded, no retry for auth/quota/model/bad request, long retry hints left to the user, mid-stream failure keeps the partial text, empty answer, retry button, missing key, unexpected bug without traceback, provider switching keeps context, non-streaming mode, Markdown-to-speech, sentence chunker identical for any chunk size |
| test_ai_config.py | 14 | Gemini default, provider selection, model names from settings, missing-key messages, `.env.example` placeholders ignored, invalid settings rejected, env overrides (parsed, applied per run, never saved), `.env` load/save, keys masked in descriptions, **keys redacted from log files** (messages, headers, tracebacks), keys never written to settings.json, app starts without a key |
| test_orchestrator.py | 29 | (updated) full voice→AI→speech flow; STREAMING before SPEAKING; SPEAKING only on real playback start; typed then spoken follow-up share context; new message cancels streaming answer; STOP cancels generation and speech; duplicate submissions; non-streaming mode; Markdown answer spoken clean; clear forgets context; error → RETRY recovers |
| test_dashboard.py | 7 | (new) processing animation right after submission; streaming text grows in the chat; SPEAKING only after TTS starts and ends with IDLE; provider failure shows ERROR state + RETRY that works; text-only mode converses without opening mic or speaker |

## Checks run on this laptop

| Check | Result |
|---|---|
| Full voice chain with real Whisper + real Piper, synthesized speech through a virtual mic, offline echo provider with streaming | "Hey Dumboo" detected; "Add milk to my shopping list." transcribed exactly; PROCESSING → STREAMING (0.05 s after the request) → SPEAKING when audio started → IDLE |
| Real dashboard launch with default config (Gemini, no key) | starts cleanly; AI row says the key is missing; text features keep working |
| `python scripts/ai_check.py --health-only` | correctly reports "Gemini API key is missing" (exit code 2) |
| Animation cost (CPU renderer, 1000×760 core, intensity 0.8) | ~10 ms/frame idle (20 fps), ~14–21 ms/frame thinking/speaking (24–30 fps); stops when hidden |

## Live verification with your Gemini key (2026-10-21)

All requests went to the real Gemini API with the user's free-tier key.

| Check | Result |
|---|---|
| Health check (`models.get`, no generation) | OK: Gemini 3.8 Flash available, max output 65,536 tokens |
| "What is 15 × 8?" (gemini-3.8-flash) | "120" (concise, correct) |
| Follow-up with history, raw adapter | correct step-by-step explanation; history format accepted |
| Gemini 3.8 Flash availability | **several HTTP 503 "model is currently experiencing high demand"** that day; DUMBOO retried twice with backoff, then showed the error + RETRY (as designed) |
| Full DUMBOO core, typed "Tell me about Python in one sentence." (gemini-3.6-flash) | one-sentence answer, streamed, spoken; PROCESSING → STREAMING → SPEAKING (on real playback start) → IDLE |
| Spoken follow-up "What are its main advantages?" | correctly answered about **Python**, so typed→voice context works live |
| Time to first text | 1.1–12.6 s across 3.5-flash-lite / 3.6-flash / 3.8-flash; dominated by free-tier server load, no model consistently faster |
| Formatting | Flash-Lite once answered maths in LaTeX → system prompt now forbids LaTeX, and speech cleanup reads any LaTeX as words |

Total: about 15 small requests of the free quota were used for these checks.
OpenAI was **not** tested live (no OpenAI key).

---

# Module 3 verification (accounts, history, memory — 2026-10-21)

## Automated tests: `python -m pytest`, **346 passed, 0 skipped** (Modules 1–3)

No test uses a real AI provider or the network; all use temporary databases.

| File | Tests | Covers (numbers = the requested test list) |
|---|---|---|
| test_auth.py | 34 | 1 owner setup + single owner (also enforced by a DB index), no default account; 2 login; 3 identical generic error for wrong password / unknown user; 4 duplicate usernames (case-insensitive) and format; 5 Argon2id hashes, no plaintext anywhere, tokens stored only as hashes; 6 disabled accounts rejected and signed out; 7 logout revokes; forged/tampered sessions; inactivity timeout and expiry; remembered sessions; 8 lockout incl. unknown usernames, unlock after the window, purge; 9 normal users denied every owner action; owner-only permissions; re-auth required and expiring; temporary password + forced change; password change revokes other sessions; owner cannot be disabled/deleted; ownership transfer; registration off by default; user list without hashes; account deletion removes all data |
| test_isolation.py | 7 | 10 conversations, 11 memories, 12 updates/deletes/clears of another user's records (service and DB trigger), 13 identity only from the session (forged display fields, guessed tokens, foreign source ids), 14 export only own data + permission, 15 switching accounts clears context; the second user's AI request contains nothing of the first user's |
| test_history_memory.py | 31 | 16 messages + provider metadata, 17 voice/text same user and conversation, streamed answer = one row, nothing saved when signed out (AI not called); 18 failed (with kind) / interrupted (partial saved as interrupted) / cancelled; 19 retry reuses the message, idempotent saves, no fabricated empty answer; 20 history + memory survive restart (sign-in required); 21 ranked search, stop-words; rename/archive/new chat/clear with confirmation; deleting a conversation keeps memories; retention; 22 explicit memory (handled locally, linked to its message); 23 duplicates; 24 corrections supersede; 25 forget / forget that / erasure + audit; list and clear with confirmation; sensitive confirmation; "call me"; auto-extraction only of clear statements; compound sentences split; 26 relevant retrieval; 27 "not saved" instead of guessing; 28 another user's memories never in prompts; 29 secrets refused and never retrieved; memory and AI permissions |
| test_db_migrations.py | 9 | 30 v1 → v2 keeps every row, backup written (old version), owner adopts rows; no re-migration; failed migration rolls back fully; newer schema refused; 31 foreign keys, check constraints, one active memory per key; 32 database errors reported without SQL or data; backup + integrity check; FTS stays in sync with edits/deletes |
| test_dashboard_accounts.py | 10 | 33 real windows offscreen: owner setup from the UI (scanner scanning → granted), wrong then right password (denied → shell), LOCK clears the screen and context; HISTORY list/open/search/export/delete; CONTINUE restores context; MEMORY add/edit/delete/clear + secret refusal + chat command updates the page; retrieval drives the core animation; ACCOUNT preferences + voice toggle; ADMIN create/reset (re-auth)/disable/security settings/integrity check; normal users see no owner pages (after the forced password change); per-user tasks page |
| Modules 1–2 files | 255 | updated to the per-user services; all still pass (item 34: no paid API anywhere in the suite) |

## Checks on this laptop

| Check | Result |
|---|---|
| Migration of a **copy** of the real v1 database (`data/dumboo.db`) | v1 → v2, backup written first, FTS5 enabled, integrity ok, reopening does not migrate again |
| Real GUI start against that copy | migration + backup in-app, owner setup screen shown, microphone opened, no errors |
| **Live Gemini** (user's key, gemini-3.6-flash, scratch database): "Remember that my project is called DUMBOO." → restart → sign in → "What is my project called?" | "Your project is called DUMBOO, Sir." (retrieval found 1 fact); the request took 27.8 s on the free tier |
| Same question as a second user (Anya) | "I don't have a project name saved for you, Anya." (no cross-user leak, nothing invented) |
| Screens rendered: owner setup, identity scan, access denied, retrieval animation, history, memory, admin | all render; fixed during testing: white sign-in background, crowded top bar, compound-fact extraction bug |

## Not verified

* The real `data/dumboo.db` was **not** migrated during development; that happens (with an automatic backup) the first time this version starts.
* OpenAI was not tested live (no key). Real-room voice use with several people is a manual check.

---

# Module 4 verification (laptop automation, 2026-10-21)

## Automated tests: `python -m pytest`, **474 passed, 0 skipped** (Modules 1–4; 128 new)

No test touches the real desktop, the network or a paid API: a fake desktop, fake UI Automation,
fake browser and scripted AI provider are used, and real files are only created in a temporary
sandbox folder.

| File | Tests | Covers |
|---|---|---|
| test_automation_policy.py | 14 | sign-in required; unknown capabilities refused (no `shell.run`); unknown/invalid arguments refused; per-capability permissions (normal users: no dev commands, no admin); low/medium/high risk and confirmation; configuration can't lower risk below a capability's floor; planner-supplied "risk: low" ignored; auto-approve only for medium; path guard (outside folders, `..` traversal, DUMBOO data, `.env`, network paths, alternate data streams, the permitted root itself); regression: protected folders enforced even if permitted; permitting a protected folder refused; URL validation; secrets never typed, stored in history, logged or remembered |
| test_automation_files.py | 16 | search by name/extension/folder; never outside permitted folders; content search (text + .docx); create folder/file verified; no silent overwrite (replace = high risk + backup); executables never created or opened; rename/move need confirmation and are verified (extension kept); name conflicts skipped and reported; declining a delete changes nothing; delete goes to the Recycle Bin; organise preview → apply → undo (skips files still downloading); duplicates reported, not deleted; latest project, recent documents; edits show a diff and keep a backup; Python syntax check; summaries use the real document text marked as data |
| test_automation_apps.py | 17 | approved app launch verified by its window; open with a file/project; cmd/PowerShell/regedit/unknown apps refused; blocked executables can't be registered; missing app reported; launch failure and "process but no window" reported as failed/unverified, never completed; minimise/restore/focus failure; close never discards unsaved work, force-close is high risk; typing only into the verified window, stops when focus changes; regression: "open Notepad and type" never types into a restored document; typing into unknown apps asks; shortcut allow-list (win+r etc. refused, shift+delete high); copy verified by the clipboard; clicking via accessibility, consequential buttons high risk; screen reading and finding controls; screen text sent to the AI as untrusted data; screenshot only after consent and never with "never"; capture failure reported |
| test_automation_tasks.py | 13 | multi-step order and verification; failure mid-sequence → partially completed, later steps skipped; step timeout; emergency stop prevents later steps and reports completed vs cancelled; voice "stop everything" cancels running and queued tasks; STOP button stops automation; running commands killed on cancel, real exit codes; confirmation timeout; step-to-step references; planner never executes arbitrary commands (rejected twice, no task created); planner plan approved before anything runs; planner clarification; plan with a protected target refused before starting |
| test_automation_memory.py | 13 | saved project path retrieved, verified and used; "this folder" from the open Explorer window; missing/outdated paths not used; nothing invented without memory; several matches → question; saved website and folder used; memory scoped to the signed-in user; task history private (list/get/delete/confirm by another user refused); ordinary tasks write no memories; workflows saved and replayed with approval; "continue the task" re-checks; corrections supersede, deletion works; URLs with passwords refused |
| test_automation_system_web.py | 18 | metrics measured (no invented battery); top programs grouped, Windows processes separated; slow-laptop diagnosis with live numbers and labelled saved notes; alerts need sustained load and have a cooldown; unavailable sensors; website opened in the user's browser and verified; unreachable site not opened; DUMBOO-browser navigation success/failure; search results, open result, tabs; download verified by size and never opened, executables refused, HTTP 404 reported; passwords never filled, form fill asks, submit is high risk; tests really run (real pytest) with real pass/fail summary; installs ask first, unknown templates refused; project without venv reported; prepare-project workflow; generated code previewed, then created and syntax-checked; traceback explanation uses the real output; scanned PDFs reported honestly |
| test_automation_integration.py | 37 | typed and voice commands use the same controller (no AI call), normal chat unchanged; automation exchanges saved in conversation history; spoken confirmation keeps listening for "yes"; task history persists across restart; tasks interrupted by a crash marked after restart; sign-out cancels the user's tasks; export includes automation history; service status; 20 example commands from the specification recognised; 8 chat/memory sentences **not** hijacked; multi-clause requests keep context ("open my project and run its tests") |
| Modules 1–3 files | 346 | unchanged and passing (application status test now also expects the automation service) |

## Checks on this laptop (real desktop, scratch database, user's data untouched)

| Check | Result |
|---|---|
| Real window list (Win32) | Claude, Antigravity IDE and Edge windows listed with correct processes |
| "open notepad and type: …" (real Notepad, real UI Automation) | **First run found a real problem**: Windows 11 Notepad reopened the user's saved tabs and the text went into the active tab `V0_2_COMPLETE_SUMMARY (1).txt` (unsaved edit only; the file on disk was verified unchanged, 28 Sep). The tab was restored (closed without saving, reopened clean, all 21 user tabs intact). **Fixed**: a new blank document is opened first, or nothing is typed. |
| Same, after the fix | **Second real problem**: text garbled ("Hello ssss…") when several characters went in one `SendInput` call. Measured: one character per call at 15 ms is exact (and paste is exact). **Fixed** (one character per call). Verification now compares the whole text: the garbled run was correctly reported as **failed**, not completed. Final run: exact text in a new tab, "read back … matches exactly". All test tabs removed afterwards; the user's 21 tabs verified intact. |
| Screen reading (UIA) on Notepad | visible text, buttons and tab names read; "File" menu found with coordinates |
| Ctrl+A / copy / minimise / switch / close on Notepad | all performed and verified (clipboard changed, window states, window gone) |
| DUMBOO's browser (Playwright + installed Edge) | live Bing results read (6 shown), "open the first result" → "Our Documentation \| Python.org" (fixed: waits for Bing's redirect page), tab list, PDF download 13,264 bytes = server size, not opened; non-existent domain → "the site name could not be found" |
| System status on this laptop | CPU, RAM (97% used at that moment), disk C: 260 GB free, battery 100% charging, Ethernet + internet 15 ms, OS/CPU info |
| **Live Gemini planner** (gemini-3.6-flash, user's key) | "back up my college folder … into a new folder called Backup" → valid 2-step plan using only registered actions → approval → both steps verified, files present. Then the **free-tier daily quota ran out**: the next planner and traceback requests returned the clear quota message (no crash, nothing executed). The traceback explanation was therefore only tested with the scripted AI. |
| Dashboard (offscreen render with Windows fonts) | ASSISTANT: confirmation banner, step ring + "AWAITING CONFIRMATION 1/2 verified", ⛔ STOP ALL. AUTOMATION: confirmation card with exact file list, APPROVE/DENY, step list, history with verification and changed files, apps & folders. Fixed: crowded top bar (subtitle hidden, tighter tabs, minimum width 1380) and the "APPS & FOLDERS" tab label. |

## Not verified

* Voice end-to-end with the real microphone (the voice path is the same code as typed input and is
  covered by tests; the spoken "yes" follow-up was tested with the fake microphone).
* Live Gemini beyond the first plan (daily quota exhausted); OpenAI not tested live (no key).
* Clicking buttons in real apps (only finding them was done live; invoking is covered by tests).
* `dev.run` installs into a real project `.venv` (tests ran real pytest; installs were only
  confirmed/declined).
* Resource alerts popping up on the real dashboard (logic tested with fake measurements).

---

# Module 5 verification (web search, research and live news, 2026-10-22)

## Automated tests: `python -m pytest`, **539 passed, 0 skipped** (Modules 1–5; 65 new)

No test uses the network or a paid API. The tests use:
* fake search providers;
* fake web pages served by an `httpx.MockTransport` (including `robots.txt`, redirects, 403s,
  PDFs and a page with an injected instruction);
* a fake link resolver;
* a scripted AI that returns the JSON each step expects.

Exports go to a temporary sandbox folder.

| File | Tests | Covers |
|---|---|---|
| test_research_search.py | 18 | Search answers:<br>• answers cite only retrieved sources<br>• duplicates merged<br>• empty results reported without an AI answer<br>• search failures reported honestly<br>• a chosen provider without its key is reported, never silently switched<br>• search switched off<br>• ordinary chat **not** researched; time-sensitive questions researched automatically<br><br>Page reading and safety:<br>• inaccessible pages marked "snippet only"<br>• `robots.txt` respected<br>• local/private addresses never fetched (also after redirects)<br><br>Providers:<br>• Tavily parsing, with the key sent only in a header<br>• HTTP error, timeout and network error mapping<br>• Brave web + news parsing, Google News RSS parsing, GDELT parsing and its text errors<br>• client identifies itself as DUMBOO |
| test_research_news.py | 10 | • news with dates, period and links<br>• publication date from page metadata, never from the AI<br>• an event date not in the sources is dropped<br>• "today" window; old news never shown as new<br>• news failure not hidden<br>• one story from several publishers grouped<br>• period parsing<br>• future/old dates filtered<br>• only declared dates trusted<br>• docs without a declared date are "date unknown" |
| test_research_analysis.py | 10 | • multi-step research: plan → searches → report<br>• plan without AI splits comparisons<br>• AI unavailable → extractive answer, labelled<br>• citation service rejects unknown ids and URLs<br>• syndicated copies are not independent<br>• fact-check verdicts and evidence<br>• "verify that answer" uses the conversation<br>• unresolvable "this course" asks instead of guessing<br>• one organisation = one origin; GitHub labels<br>• speech text and unanswered points |
| test_research_memory_security.py | 11 | Memory:<br>• personal requests use relevant saved memories; unrelated ones don't<br>• other users never see research or memories<br>• save/delete findings<br>• saved research reopened, refreshed and compared<br>• history listing and search<br><br>Security:<br>• prompt injection in a page is only data<br>• AI output can't trigger automation<br>• API keys never reach logs, prompts or storage<br>• the `research.web` permission is required<br>• "open the source" goes through the automation policy |
| test_research_integration.py | 9 | • voice and typed requests use the same controller<br>• research runs in the background and can be cancelled<br>• progress follows the real stages<br>• failures shown accurately<br>• history survives a restart<br>• follow-ups use the previous answer<br>• "show the sources" without a new search<br>• export goes through automation<br>• existing modules unchanged |
| test_dashboard_research.py | 3 | • RESEARCH page runs and shows the answer with sources<br>• HISTORY and SAVED FINDINGS tabs<br>• SETTINGS saves through the settings service |
| test_automation_files.py | +1 | a write blocked by Windows Controlled Folder Access gets a clear explanation |
| Modules 1–4 files | 474 | unchanged and passing (the application status test now also expects "research") |

## Live checks on this laptop (keyless sources; scratch database; user's data untouched)

| Check | Result |
|---|---|
| "latest news about cybersecurity" | Ran in 40–80 s. Google News links were resolved to the original articles: NCSC, BBC, Times of India, Cybersecurity Dive. The pages were read, and publishers and declared dates shown. GDELT junk and press-release wires were filtered out. |
| Web search "official Python documentation" | About 9 s through the headless Edge on Bing; docs.python.org cited as official. |
| Fact-check "Python 3.11 is 10x faster than 3.10" | About 30 s. Found the Python Insider post of 24 Oct 2022; verdict **partly supported** (10–60 % faster, not 10×). |
| Deep research report | Report with executive summary, findings and source comparison; all citations valid. |
| "open the official documentation for pandas" | Opened in the browser through Module 4's `web.open`, and verified. |
| robots.txt / 403 pages | Labelled "snippet only", not bypassed. |
| Dates | Found and fixed during the checks: the htmldate library *guessed* wrong dates for several pages, so only declared metadata or dates in the URL are used now. |
| AI synthesis | The user's gemini-3.6-flash **daily free quota was exhausted**. The extractive fallback answer was shown correctly ("no AI summary"). Real AI news synthesis with valid citations was verified with a test-only override `TEST_GEMINI_MODEL=gemini-3.5-flash-lite`; the user's settings were not changed. |
| Export | Writing to Documents was **blocked by Windows Controlled Folder Access** (it is on, on this laptop). DUMBOO now explains this; nothing was written to Documents. |
| Dashboard (offscreen render with Windows fonts) | Two problems found and fixed in the ASSISTANT chat bubble: it overflowed a narrow window, and links were dark blue. Both fixed and re-rendered; links are now cyan. The RESEARCH page tabs, progress and core label "RESEARCHING" were also shown. |

## Not verified

* Tavily and Brave live (no keys). Their parsing and errors are tested with recorded-format fakes.
* Voice end-to-end with the real microphone. It uses the same path as typed input, which is
  covered by tests.
* The real `data/dumboo.db` is still at schema v3. The v4 migration (new `research_sessions`
  table) runs, with an automatic backup, the first time this version starts.

**Update (2026-10-22, live run):** the v4 migration ran on the real database, with the backup
`dumboo-20261022-122445-before-v3-to-v4.db`. A live news research run in the real app
completed in 53 s with an AI summary and 6 dated sources (5 read); all citations were valid.
The run also exposed an idle-timer bug: DUMBOO fell asleep the instant a job of more than 60 s
finished. This is fixed, with a regression test in `test_orchestrator.py`.

---

# Wake word reliability ("Hey DUMBOO", 2026-10-22)

The user reported that the wake word didn't work every time. The log showed 16 past
detections: 10 with score 0.81 and 6 with 0.95, against a threshold of 0.75. Rejected
attempts left no trace in the log.

**Measured with a harness.** The harness took synthetic "Hey Dumboo" phrases: 8 phrasings × 3
speeds × 3 noise levels, so 72 samples. It also took 26 look-alike or confusing negatives:
"Hey Jumbo", "Hey Dumbledore", "Hey dumb dumb", reversed speech, noise bursts and claps, 234
samples in total. All of them went through the real pipeline: noise suppression → Silero VAD →
spotter.

| | Detected | Clean | Moderate noise (15 dB) | Loud noise (5 dB) | False wakes |
|---|---|---|---|---|---|
| Before | 46/72 (64%) | 20/24 | 16/24 | 10/24 | 0/144 easy negatives; the old matcher also accepted "Hey dumb dumb" and "Dumb old boo" |
| After | **60/72 (83%)** | **24/24** | **22/24** | **14/24** | **0/234** |

**Causes found and fixed** (`audio/wake_word.py`):
1. **Long commands were discarded.** A wake phrase followed by a command in one breath
   ("Hey DUMBOO, open Notepad and type …") made a burst longer than 4 s, and such bursts were
   thrown away. That was 0 of 9 detected. Now the first 2.5 s of every burst is checked. A burst
   that starts with the phrase is transcribed in full for the command, and one longer than 10 s
   wakes DUMBOO without a cut-off command.
2. **Random decoding.** faster-whisper retries unsure clips with random sampling (temperature
   fallback), so the same audio woke DUMBOO on one try and not the next, and a check sometimes
   took 8 s. The spotter now decodes once, at temperature 0.
3. **Misheard name.** In noise, the invented name is heard as "dumpling", "Dumbledore", "I Dumbo"
   or "Bye Dumbo". A second pass now gives Whisper the name as a hint. Its answer is trusted only
   if Whisper is at least 20% sure of the name word. Measured: look-alikes scored 0.00–0.07 and
   real phrases 0.15–0.85. If the plain pass clearly heard a different similar word (≥ 0.45, such
   as "Jumbo"), the hint is ignored.
4. **"dumb" counted as "dumboo".** The consonant-only comparison treated them as the same, so
   now a different final sound (vowel vs consonant) is penalised.
5. **Phrases dropped while busy.** A burst that arrived while the previous one was being
   transcribed was dropped. The newest one now waits its turn.
6. Whisper hears the original audio instead of the noise-suppressed audio (the suppression
   artefacts hurt recognition). Noise suppression still steers the VAD.
7. Near misses are logged as a score only, never the words.

**Speed on this laptop** (memory ~94% used): a clear phrase is decided in about 1.5 s, and an
unclear one (two passes) in about 3 s.

**Tests:** 8 new tests in `test_wake_word.py` (long command, very long speech, one check per
long burst, hint rescue, unsure hint rejected, similar word wins, burst while busy, deterministic
decoding) and 2 new non-matching phrases. **548 passed.**

**Not verified:** a real voice. The harness uses one synthetic voice. Check with
`python scripts/mic_check.py --wake --seconds 60` (README section 8).

# Voice and accents (2026-10-22)

The user's request: no accent barrier. DUMBOO's voice should be understood by everyone, and
DUMBOO should understand every English accent.

**DUMBOO's voice.** Five Piper voices each read 12 typical DUMBOO answers (times, dates,
numbers, names, tech words). Whisper base.en and small.en transcribed the audio, and the score
is the share of words they got wrong.

| Voice | Words wrong (base.en / small.en) | Synthesis time vs audio length |
|---|---|---|
| **en_US-lessac-medium (current)** | **1.0% / 0.0%** | **0.04** |
| en_US-lessac-high | 1.5% / 0.0% | 0.30 |
| en_GB-alan-medium | 3.6% / 1.3% | 0.08 |
| en_US-amy-medium | 5.5% / 1.6% | 0.06 |
| en_US-ryan-high | 4.2% / 3.0% | 0.34 |

Result: the current neutral General-American voice is the clearest and the fastest, so it is
kept. The candidate voices were deleted afterwards.

**Understanding accents.** 12 typical commands were spoken by the 8 English Windows voices:
Indian English (Heera, Ravi), British (George, Susan, Hazel) and American (David, Zira, Mark).
Each command was tested clean and with noise at 15 dB, 192 transcriptions per model.

| Accent | base.en clean / noisy | small.en clean / noisy |
|---|---|---|
| Indian English | 3.0% / 9.8% | 1.5% / 3.7% |
| British | 3.8% / 6.6% | 1.9% / 3.1% |
| American | 4.9% / 5.9% | 1.5% / 1.5% |
| **All** | **5.6%** | **2.2%** |
| Time per command (this laptop) | 1.0 s | 3.1 s |

Notes on these results:
* Single-beam decoding gave the same accuracy (1.9%) and no speed gain.
* A name hint in the prompt didn't help (2.4%).
* small.en costs about 320 MB more memory.

**Change:** commands now use `small.en`; you can switch back to base.en in SETTINGS → Whisper
model. The wake spotter stays on the faster `base.en`.

**Not verified:** a real human speaker with an Indian accent. The Windows voices are a synthetic
stand-in.

---

# Several AI providers, everyday features and the 3D blob (2026-10-22)

## AI providers and switching
* **Tests:** 18 new tests in `test_ai_fallback.py`:
  * route order;
  * extra keys;
  * placeholders ignored;
  * switching on quota, rate limit, network trouble and bad requests;
  * cooldowns;
  * no switching after text has started or around a safety block;
  * shorter timeouts for backups;
  * the switch is announced in the chat;
  * the Groq adapter and its errors;
  * ADD KEY slots.
* **Live (user's Groq + Gemini keys):**
  * Groq gpt-oss-120b: first words in 0.32–0.64 s.
  * First answer of a session: 0.32 s after the background warm-up (1.9 s without it).
  * Broken key → real key: switched, 0.49 s in total.
  * Broken Groq key → Gemini: switched, 3.2 s.
* **Found live:** the OpenAI SDK rejected the Groq-only `include_reasoning` argument, so every Groq
  request failed; Gemini answered every time through the switching layer. It now goes in
  `extra_body`, with a regression test.

## Everyday assistant, learning, slang
* `test_everyday.py` (28):
  * 12 spoken time phrasings;
  * time and date with no AI call;
  * a voice reminder saved, listed and cancelled;
  * a missing time is asked for;
  * a due reminder is announced and snoozed;
  * cancel all (asks first);
  * "remind" is not "remember";
  * weather for a named city and for the saved home city, asking when there is none, rain, and
    failure;
  * translation script, pronunciation and phrasings;
  * a weather question with "in" is not a translation;
  * "what did we talk about today".
* `test_learning.py` (6):
  * facts learned and used later;
  * only messages about the user are sent;
  * sensitive facts are never learned;
  * learning can be switched off;
  * an AI failure is harmless;
  * rate limit.
* `test_automation_slang.py` (56): 46 casual commands now understood (before: 15 of 45); 9 chat
  sentences not hijacked; a media search URL.
* `test_schedules.py`: missed reminders are delivered once (marked missed) within 12 h; older ones
  are skipped.
* **Live (Groq):**
  * Translations in about 0.5 s each:
    * Hindi: सुप्रभात, आप कैसे हैं? / Suprabhaat, aap kaise hain?
    * Marathi: रेल्वे स्टेशन कुठे आहे?
    * Spanish: Muchas gracias por tu ayuda.
    * Hindi → English and Marathi → English.
    * Found and fixed: a Hindi → English result was labelled "Hindi".
  * "next Thursday after lunch" → Thu 29 Oct 13:00.
  * Learning:
    * "preparing for GATE 2027, study late at night" → 2 facts.
    * "sister Priya is a doctor, hate spicy food" → 2 facts.
    * "high blood pressure" → nothing (sensitive).
  * Weather Pune: 28 °C, overcast. Tokyo time correct.
* **Bugs found and fixed:**
  * "go to sleep" was planned as a window switch.
  * "remind me at 1" meant 1 AM.
  * The "8 tomorrow" date regex swallowed "tomorrow morning".
  * "what did we talk about today" missed messages saved in the same second.
  * Italic notes rendered underlined, like links (Qt reads `_x_` as underline).

## 3D blob
* `test_dashboard_blob.py` (5):
  * thousands of lit dots, cyan on top and violet below;
  * louder voice × higher sensitivity → more swell;
  * every phase still paints;
  * click = TALK, drag spins and is not a click, a click outside does nothing, blob off = old orb;
  * settings.
* **Rendered offscreen** in all phases and in the full window. Performance: the first version cost
  26.5 ms per core frame; after switching to numpy splatting, 17.4 ms (without the blob: 10.5 ms).

## Totals
**662 passed.** `test_voice_and_text_use_the_same_controller` failed intermittently under full-suite
load (it passes alone 3/3); its two 5 s waits were raised to 15 s.

## Not verified
* The blob on the real screen with the real microphone (rendered offscreen with simulated levels).
* A reminder being spoken by the real TTS at its time (event and announcement path covered by tests).

---

# Face unlock and greeting (2026-10-22)

* **Hardware found:** Integrated IR Webcam with Windows Hello. The user chose DUMBOO's own face
  recognition (OpenCV YuNet + SFace) over Windows Hello.
* **Installed:** `opencv-python-headless` 4.14.0 and the YuNet (0.2 MB) and SFace (38.7 MB) models.
  The models load, and the webcam opens at 640×480.
* `test_face_unlock.py` (20 tests):
  * Sign-in check: recognise → turn → back succeeds; a still photo times out; an unknown face is
    refused; another person can't finish someone else's check; clear instructions.
  * Setup: collects front and both sides; ignores a second person.
  * Storage: setup needs the password; data is stored DPAPI-encrypted; a stranger doesn't match.
  * Lockout: 5 failures pause face unlock; a password sign-in lifts the pause.
  * Owner switch and removal.
  * Greeting: introduction once, then short (with the reminder count and "Face recognised"); can
    be switched off.
  * Login page with a fake camera: signs in and releases the camera; the button is hidden until
    face unlock is set up.
  * The real models see no face in an empty picture.
* **Bugs found by the tests and fixed:**
  * After a finished check, one more camera frame crashed it.
  * The lockout compared timestamps to the second; a password sign-in now clears the failures.
* **Rendered:** the sign-in page mid-scan, with the camera picture in the scanner ring, the face
  brackets aligned, the progress arc and CAMERA ON.
* **Not verified:** a real face. Setup and sign-in with the user's own face, the head-turn
  thresholds in real light, and how well recognition copes with glasses or lighting changes.
  This needs the user at the camera.


---

# Module 7: security & privacy guardian (2026-10-23)

## Unit tests

`tests/test_guardian.py` has 46 tests. Every one passes. They use fakes for PowerShell, the
registry, the platform, HTTP clients and winget, so none of them touches the real system or a
paid API. They cover:

* **Overview**
  * Defender, firewall, UAC and updates are read into PASS/WARNING/FAIL.
  * A PowerShell permission error becomes UNKNOWN.
  * An unsupported OS gives NOT_SUPPORTED.
* **Startup entries**
  * They are listed from Run keys, Startup folders, tasks and services.
  * Already-quoted task paths are handled (regression test).
* **Investigation**
  * SHA-256 is correct.
  * Secrets in command lines are redacted.
  * An unsigned file in AppData gives "Suspicious indicators".
  * A missing file is flagged.
  * A signed system file gives "No obvious concern".
  * `explorer.exe` in `C:\Windows` is not called a disguise.
  * Indicators aren't repeated.
* **Threat intelligence**
  * Off by default, and the report says NOT_CHECKED with the reason.
  * VirusTotal and MalwareBazaar responses are parsed as malicious, clean or unknown.
  * Only the hash is sent.
  * Only a MALICIOUS result gives "known malicious".
* **Link check**
  * Lookalikes (`paypa1.com`, `amaz0n-support…`), IP hosts, `@` tricks and shorteners are caught.
  * Regional domains (`amazon.in`) are not flagged.
  * Redirect tracing refuses private addresses, never runs page scripts, stops after the hop limit and reads at most 64 KB.
* **Updates**
  * The winget table is parsed.
  * KEV matches are labelled "version not verified".
* **Integrity**
  * Baseline then compare finds modified, missing and new files.
  * Refused folders are rejected.
* **Remediation**
  * Disabling needs "yes".
  * "No" cancels the action.
  * A user without `security.manage` is refused.
  * A backup is stored, the change is verified, restoring gives back the exact value, and every attempt is audited.
  * Machine-wide entries return not_supported with manual steps.
* **Isolation:** one user can't see another user's scans, scopes or actions.
* **Assistant**
  * Security phrases are routed to the guardian.
  * Other text passes through to the existing modules.
  * A scan can be stopped.
* **AI explanation:** if the AI's answer contains numbers that aren't in the evidence, DUMBOO
  shows the structured report instead.

The full suite has **730 passed** (Python 3.11.9, Windows 11).
`test_voice_and_text_use_the_same_controller` used to fail now and then because of a timing
race in the test itself. It now waits for each reply before sending the next message, and it
passed 30 runs in a row.

## Checked on this PC (read-only, standard user)

These checks were run on the user's real PC, using a scratch database instead of the real one.

* **Overview** finished in about 14 s.
  * Most checks passed (antivirus, firewall, UAC, Windows updates).
  * The checks that need administrator rights showed UNKNOWN, as designed.
  * WARNING: some app updates available.
* **Startup:** several dozen entries.
  * Before the quoting fix, 6 scheduled tasks were wrongly flagged as "file doesn't exist". After the fix, none are flagged.
* **Investigations:**
  * OneDrive and explorer.exe: valid Microsoft signatures.
  * A signed third-party app in AppData\Local: "No obvious concern", medium confidence. Threat intelligence was not checked because lookups are off.
* **Network:** dozens of connections from many programs; a few programs listening.
* **Advisories:** a couple to review, matched by app name and labelled "version not verified".
* **Link check:** `paypa1.com` → High concern.
* **SECURITY page:** all 9 tabs were rendered with real data at the minimum window size (1460 px wide) and
  checked from screenshots.

**Bugs found on the real PC and fixed:**
* explorer.exe was flagged as a disguise.
* amazon.in was flagged as a lookalike.
* `amaz0n-support` was missed.
* Quoted task paths gave false "file doesn't exist" flags.
* An indicator was shown twice.
* The "not checked" reason for threat intelligence was too generic.
* Closed connections were shown with "?" as the program.
* Loopback addresses were called "local network".
* Speech said "0 have …".
* At 1460 px the top bar clipped the logo and some tab names (SECURITY was the 13th tab). The tab
  buttons are now 4 px narrower and never squeezed; a long user name is cut instead (full name on hover).
* The network list was filled with connections between programs on this PC; they are now listed
  last and counted.
* A monitored folder wasn't found again when written differently (e.g. the short `NAME~1` form).
* The INTEGRITY table hid the path and turned every "T" in it into a space; the scan history showed
  raw Python text; the UPDATES button showed "_ADVISORIES".

## Not verified

* Disabling and restoring a **real** startup entry. This is deliberately not done on the user's
  PC, so it is tested with a fake registry only.
* Real VirusTotal and MalwareBazaar lookups (no keys configured; the responses are mocked).
* Redirect tracing against live sites.
* The voice commands spoken through the microphone (they were tested as text through the same
  path).
* Running as administrator (BitLocker and Secure Boot), and Linux or macOS.


---

# Tasks, schedule and reminders by voice and text (2026-10-23)

## Unit tests

`tests/test_agenda.py`: 33 tests, all passing. They use a fixed clock (Thursday 22 Oct 2026,
2 PM) and a fake AI. They cover:
* 15 slang, text-speak and Hinglish phrasings being turned into plain commands.
* Tasks:
  * a deadline with a time gets a reminder 30 minutes before;
  * a deadline with only a day gets a reminder at 9 AM;
  * listing, overdue tasks, done and undo;
  * moving a task also moves its reminder.
* Schedule: add (with a custom reminder), list, move by time only, "am I free", "when is", cancel,
  and asking for the time when it's missing.
* Reminders saved on the right page:
  * a deadline goes to TASKS;
  * a plain timed reminder goes to SCHEDULE;
  * a reminder for an existing task goes on that task, with no duplicate.
* The agenda answer, in time order, includes overdue tasks and tasks without a date.
* A task reminder is delivered once, with the right spoken text; "snooze" and "done" then act on that task.
* Five ordinary requests still go to the AI chat:
  * "make a presentation on …"
  * "plan a trip to Goa for 3 days"
  * "what is the capital of France"
  * "I have a meeting with my boss and I'm nervous"
  * "how do I schedule a meeting in Outlook"
* The AI reads an unusual phrasing as an "add task" command.
* Each user's tasks stay private.
* The TASKS page dialog saves the due time and the reminder.

Full suite: **767 passed** (766 in the full run, then the fixed dialog test).

## Checked with real commands

These were run through the real conversation path, with the fake AI:
* About 40 phrasings in one sweep.
* The TASKS, SCHEDULE and home pages, and the task dialog, rendered at 1460 px with real data.

**Bugs found by the sweep and fixed:**
* "What's on my to-do list" was not recognised.
* "Your for tomorrow tasks" heading.
* "Moved … to on Tuesday".
* "Kal ka plan kya hai" was not understood.
* "Plan a trip to Goa for 3 days" became an event at 3 PM; "plan" no longer adds events.
* "Tell me a joke" right after a weather question was taken as a city name (an older bug in the weather follow-up).

`test_app_runs_without_microphone` checked for an event before the event bus had delivered it. It
failed even with all of this work removed, so it was a timing race in the test; it now waits for
the event.

## Not verified

* Speaking these commands into the real microphone. They were tested as text through the same path.
* The AI fallback with a real AI model; the tests use a fake that returns fixed JSON.
* A task reminder going off and being spoken live.

---

# Fixes from the live session of 2026-10-23 (security, accuracy, latency)

Each fix below came from the real log and saved conversation of that morning.

* **Face sign-in failed in different lighting.** It said "Face not recognised" three times and timed out once. The 0.42 match threshold is unchanged. After each fully confirmed sign-in (recognised in 3 frames, then the head-turn check), up to 2 frames are kept as extra samples. Safeguards:
  * a frame is kept only if it matches the user's original 8 setup samples, so the profile can't drift;
  * a frame that also matches another account is never kept;
  * near-copies are skipped;
  * at most 12 learned samples are kept, and the setup samples are never replaced.

  A failed attempt now logs its best match score.
* **The sign-in screen hid itself in the tray after 60 s.** DUMBOO no longer auto-sleeps while nobody is signed in.
* **A reminder fired about 50 s early with no subject ("Reminder: Reminder").**
  * Times are now rounded up to a whole minute, never down.
  * "Remind me after two minutes" asks what the reminder is for.
  * A reminder with no subject is spoken as "Here's the reminder you asked for".
  * Every delivery is logged, saying whether it was spoken, held until DUMBOO was free, or shown only.
* **The AI said "I'm not able to create reminders" and claimed to save facts it didn't save.** The system prompt now lists what DUMBOO can do and forbids claiming saves.
* **Memory commands were misread.**
  * "Save that in long-term memory, remember it every time when I log in" now saves the previous message, not these words.
  * "My owner's name is Roshan, save it in long-term memory" saves the fact before the comma.
* **Wrong learned facts (a wrong name, and a family detail from a misheard recording).** Messages that look misheard by speech recognition (heavy repetition) are no longer sent for learning. A name is learned only from "my name is …".
* **Slow transcription (about 2.2 s per command, 7.5 s once).** Whisper now uses up to 8 CPU threads (17 % faster, same text) and skips word timestamps it doesn't need (30-40 % faster). On 12 test clips the only differences were punctuation and one better word ("cricket" instead of "Cricut"). Retries on unclear audio are capped at 2 instead of 5.
* **Slow news (42 s).** Reading pages now has a time budget: 12 s for news and quick searches, 20 s for fact checks, 30 s for research. Pages not read in time keep their search snippet and are marked "snippet only". Each page is read into a copy, so a page that arrives late can't change the answer.

New tests:
* face learning (3);
* never-early and no-subject reminders (2);
* "save that" memory commands (2);
* misheard speech and name guards (1);
* a slow page can't hold up the answer (1).

Full suite: **778 passed**.

Not verified live yet: the speed gain on the user's laptop, and face sign-in in different lighting over several days.

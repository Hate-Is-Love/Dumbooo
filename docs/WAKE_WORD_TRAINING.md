# Training a custom “Hey DUMBOO” wake-word model

## Why this is needed

No wake-word engine ships a pretrained model for “Hey DUMBOO”. openWakeWord’s
pretrained models are for phrases like “hey jarvis” and “alexa”. Using one of
those would silently change your wake phrase, which DUMBOO refuses to do.

Until you train a model, DUMBOO uses its built-in **Whisper phrase spotter**:

| | Whisper phrase spotter (default) | Custom openWakeWord model |
|---|---|---|
| Setup | none | ~1 hour, mostly waiting |
| CPU while the room is quiet | ~0 % (VAD only) | ~1–2 % of one core |
| CPU per speech burst | ~0.5–1 s on a laptop CPU (Whisper `base.en`) | negligible |
| Robustness in noise / TV | moderate | good |
| False wakes | possible when someone says “Dumbo” | rare after training |
| Wake + command in one breath | yes (“Hey DUMBOO, what’s next?”) | wake only |

The spotter only transcribes short bursts of speech (0.3–4 s) that Silero VAD
detects. It never runs speech recognition continuously, and it never saves audio.

## Training with openWakeWord (free, no coding)

openWakeWord’s author provides a Google Colab notebook that trains a model
from **synthetic speech**. You don’t need to record yourself.

1. Open the openWakeWord GitHub page: <https://github.com/dscripka/openWakeWord>.
   In the README, find **“Training New Models”** and open the
   *automatic_model_training* Colab notebook (needs a free Google account).
2. In the notebook, set the target phrase to **`hey dumboo`**.
   - Also add a phonetic spelling that the speech synthesiser pronounces well,
     for example `hey dum boo`. Listen to the sample clips the notebook
     generates and adjust the spelling until they sound right.
3. Keep the default number of training examples for a first model. Run all
   cells. The full run takes about 30–60 minutes on Colab’s free GPU.
4. Download the resulting **`hey_dumboo.onnx`** file (pick ONNX, not TFLite,
   because DUMBOO runs on Windows).
5. Copy it to:

   ```
   DUMBOO\models\wakeword\hey_dumboo.onnx
   ```

6. Make sure openWakeWord’s feature models are installed (once):

   ```bash
   python scripts/download_models.py --skip-whisper --skip-piper
   ```

7. Restart DUMBOO. With `wake_word.engine = "auto"` (the default), the
   Diagnostics page should show `wake_word_custom_model: found`. The Core
   telemetry **WAKE** row should read `listening · custom model hey_dumboo.onnx`.

## Tuning

- **Settings → Wake word → Sensitivity**: the openWakeWord detection
  threshold is `1 - sensitivity` (default 0.5). Lower the sensitivity if you get
  false wakes. Raise it if DUMBOO misses you.
- To improve a model, add **your own recordings** as extra positive examples.
  Add recordings of your room (TV, music, typing) as negative examples. The
  notebook explains how; ~20 recordings of you saying the phrase help a lot.
- Licence note: openWakeWord’s code is Apache-2.0. Models you train from the
  notebook’s synthetic data are yours to use. Read the notebook’s notes about
  the licences of the background-noise datasets it downloads.

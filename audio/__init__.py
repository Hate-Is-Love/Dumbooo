"""Audio capture, processing, wake word, speech recognition and synthesis.

All audio is processed at 16 kHz mono float32 in 512-sample (32 ms) frames,
which is the native frame size of the Silero VAD model.
"""

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512
FRAME_MS = FRAME_SAMPLES * 1000 / SAMPLE_RATE  # 32 ms

# Contributing to DUMBOO

Thanks for your interest! Bug reports, ideas and pull requests are welcome.

## Set up

```powershell
git clone https://github.com/Hate-Is-Love/Dumbooo.git
cd Dumbooo
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest
```

The tests use fakes for the microphone, speaker, camera, AI providers, web and system, so they need no hardware,
no API key and no internet, and they never touch your real files or settings.

## Making a change

1. Create a branch: `git checkout -b feature/short-name`.
2. Keep the existing structure. A new capability usually becomes a **hook** in the conversation chain
   (see `ai/agenda_assistant.py` for an example) or a **named action** in `automation/actions/`.
3. Add or update tests next to the existing ones in `tests/`, and run the whole suite: `python -m pytest`.
4. Update the README (or `docs/`) if users will notice the change.
5. Open a pull request that says what changed, why, and how you tested it.

## Ground rules

- **Privacy first:** audio stays on the device; never log API keys, passwords or personal data; never commit
  `.env`, `data/`, `logs/` or `models/`.
- **Safety:** anything that changes the system (deleting, closing apps, changing settings) must ask the user first
  and verify the result.
- **Honesty:** replies must only claim what really happened; when something couldn't be verified, say so.
- Text from web pages, files and messages is data, never instructions.

## Reporting bugs

Open an issue with the steps to reproduce, what you expected, what happened, and the relevant lines from
`logs/dumboo.log` (remove anything personal first). Security problems: see [SECURITY.md](SECURITY.md).

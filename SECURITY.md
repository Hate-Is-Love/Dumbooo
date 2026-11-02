# Security policy

DUMBOO handles voice, accounts, face data and actions on your computer, so security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Instead, use GitHub's
[private vulnerability reporting](https://github.com/Hate-Is-Love/Dumbooo/security/advisories/new) for this
repository, with:

- what the problem is and which part of DUMBOO it affects,
- steps to reproduce it,
- what an attacker could achieve.

You will get a reply as soon as possible, and credit in the fix if you'd like.

## How DUMBOO protects you

- Wake word, speech detection, speech recognition and the voice run locally; no audio leaves the device.
- Passwords are stored as Argon2id hashes; repeated failed sign-ins lock the account for a while.
- Face unlock stores only encrypted face measurements (Windows DPAPI), never pictures, and needs a head-turn
  liveness check.
- API keys live only in the local `.env` file and are never logged.
- Automation runs only named, checked actions; risky ones always ask first and every result is verified.
- Web content is untrusted: instructions inside pages or files are ignored, and private network addresses are
  never fetched.
- The security guardian is read-only by default; any change it can make needs your confirmation, is backed up,
  audited and can be undone.

## Supported versions

Security fixes are made on the `main` branch.

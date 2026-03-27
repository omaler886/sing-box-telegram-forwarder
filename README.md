# sing-box Telegram Forwarder

This repository watches the upstream `yelnoo/sing-box` GitHub Actions build and forwards selected build artifacts to your Telegram bot.

## Forwarded Targets

- Windows amd64
- Android arm64
- OpenWrt x86_64 (`.apk` and `.ipk` when present)
- OpenWrt aarch64_generic (`.apk` and `.ipk` when present)

## Repository Layout

```text
.
|-- .github/workflows/forward-sing-box-to-telegram.yml
|-- scripts/forward_sing_box_to_telegram.py
|-- state/state.json
|-- .gitignore
`-- README.md
```

## Required GitHub Secrets

- `TELEGRAM_BOT_TOKEN`: your Telegram bot token
- `TELEGRAM_CHAT_ID`: your Telegram user id or chat id
- `SOURCE_ARTIFACT_TOKEN`: optional, only needed if you want to download artifacts through the official GitHub API instead of `nightly.link`

## How It Works

1. GitHub Actions runs every 10 minutes.
2. The script checks `yelnoo/sing-box` `build.yml` for new successful `stable` or `unstable` push builds.
3. It downloads the matching artifacts.
4. It sends the selected files to your Telegram chat through `sendDocument`.
5. It updates `state/state.json` so the same run is not sent twice.

## First-Time Setup

1. Create a new GitHub repository.
2. Upload all files from this directory to that repository root.
3. Add the required GitHub Secrets.
4. Open Telegram and send one message to your bot.
5. Run the workflow once with `workflow_dispatch` to confirm Telegram delivery.

## Notes

- The first successful run only forwards the latest upstream build, so old history will not flood your Telegram.
- The workflow uses `nightly.link` by default because public GitHub Actions artifacts need authentication on the official API download endpoint.
- If `nightly.link` becomes unstable for you, add `SOURCE_ARTIFACT_TOKEN` and switch the environment to API download mode.

## Local Dry Run

PowerShell:

```powershell
$env:DRY_RUN='1'
$env:MAX_RUNS='1'
python .\scripts\forward_sing_box_to_telegram.py
Remove-Item Env:DRY_RUN, Env:MAX_RUNS
```

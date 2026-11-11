# GTA 6 Shorts Clipper

Turns GTA 6 streams **from creators who have allowed clipping** into vertical shorts, sends every short through human review, and publishes one every 4 hours to YouTube Shorts and TikTok.

```
stream URL ─► permission check ─► download ─► audio + chat spike scan ─► Claude picks the best moments
          ─► render 9:16 (gameplay + your footage + captions + credit) ─► YOU review ─► 4-hourly publishing
```

## ⚠️ About copyright

A split screen, a different format, or extra footage underneath does **not** make an unlicensed clip legal, and it doesn't get past Content ID. What keeps a clip channel alive is **permission from each streamer**:

- Many streamers allow clipping publicly or run paid clipping programs. Look on their About page, Discord or website, or ask them by email or DM.
- Record each permission in `config/creators.yaml`. The pipeline won't download a stream from a channel that isn't listed there with `granted: true` and a `source`.
- Every short shows the creator credit and links their channel in the description.
- **GTA in-game radio is licensed music** and a common cause of Content ID claims. Claude flags audible music in `risk_notes`, and the review checklist asks about it again.
- Rockstar's policy generally allows GTA gameplay videos. The streamer's face, voice and commentary belong to the streamer.

## One-time setup (do this before launch day)

```bash
./scripts/setup_mac.sh          # Homebrew, ffmpeg, uv, gh, Python 3.12 venv
source .venv/bin/activate
# put ANTHROPIC_API_KEY in .env
clipper doctor                  # shows what's still missing
```

### Platform accounts. Start these NOW, approval takes time.

| Platform | Steps | Catch |
|---|---|---|
| **YouTube** | Google Cloud Console → enable *YouTube Data API v3* → OAuth client (Desktop) → save JSON to `secrets/youtube_client_secret.json` → `clipper auth youtube` | Uploads from an **unaudited** API project are locked to private. Submit the *YouTube API Services audit* form today. Until it's approved, publish each video by hand in YouTube Studio. |
| **TikTok** | developers.tiktok.com → app with Login Kit + Content Posting API → keys in `.env` → `clipper auth tiktok` | Before TikTok audits the app, direct posts are forced private. Set `platforms.tiktok.mode: inbox` in the meantime so clips land in your TikTok **drafts** and you tap Post. |

### Your bottom footage

Put your **own** videos (your own GTA gameplay, your facecam reacting, licensed b-roll) in `assets/bottom/`. Or set `render.layout: blur` to use no bottom footage.

### Turn on the 4-hour publisher

```bash
./scripts/install_scheduler.sh  # runs `clipper publish-due` every 15 min via launchd
```

YouTube clips are uploaded up to 48h ahead as private with a `publishAt` time, so they go live on schedule even if your Mac is asleep. TikTok's API has no scheduling, so TikTok posts go out on the first run after each slot. Your Mac needs to be awake for that (System Settings → Battery → prevent sleep when plugged in).

## Daily workflow

```bash
clipper check  <stream-url>     # optional: is this channel allowed? (no download)
clipper process <stream-url>    # download → highlights → render (VOD only, not while live)
clipper review                  # http://127.0.0.1:5050: watch, edit text, tick checklist, approve
clipper status                  # what's queued for which slot, any upload failures
```

When you approve a clip it takes the next free 4-hour slot (00:00, 04:00, 08:00… in `schedule.timezone`). To publish 6 a day you need 6 approved clips a day.

## How highlights are found

1. **Signals** (`clipper/signals.py`): per-second loudness, plus chat message rate and hype words (`lol`, `kekw`, `no way`, `💀`…) from the YouTube live-chat replay. The top 20 windows go forward.
2. **Claude** (`clipper/analyze.py`): gets a Whisper transcript and 2 still frames of each window, then returns the best 15–59s cuts with titles, captions, hashtags and copyright risk notes. Each stream costs a few cents to a few tens of cents in API usage.
3. **Render** (`clipper/render.py`): 1080×1920, word-synced captions, credit overlay.

Tuning knobs are in `config/settings.yaml`: number of candidates, clip length, Whisper model size, layout, crop focus, schedule.

Twitch VODs work (yt-dlp), but chat replay is only pulled for YouTube, so Twitch highlights rely on audio spikes alone.

## Layout

```
clipper/
  cli.py          commands
  config.py       settings + creator allowlist / permission gate
  download.py     yt-dlp metadata check, then download
  signals.py      audio + chat spike detection
  transcribe.py   faster-whisper on candidate windows only
  analyze.py      Claude picks moments (structured JSON)
  render.py       ffmpeg 9:16 render
  review_app.py   Flask review UI with copyright checklist
  scheduler.py    4-hour slots, publish-due
  uploaders/      youtube.py, tiktok.py
config/           settings.yaml, creators.yaml
scripts/          setup_mac.sh, install_scheduler.sh
tests/
```

## Tests

```bash
pytest -q
```

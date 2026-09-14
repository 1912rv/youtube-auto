# YouTube Chess Puzzle Shorts

This project fetches a Lichess chess puzzle, renders a vertical 9:16 Short, generates voiceover and sound effects, and optionally uploads the video to YouTube through the YouTube Data API v3.

The video includes:

- A hard puzzle target of `2000+` rating.
- A 10-second challenge countdown.
- The original puzzle perspective kept on the solution frame.
- A highlighted best-move arrow.
- Voiceover, move sound, and a generated cinematic background sound bed.
- A call to action asking viewers to comment their next move.

Generated audio, tokens, and video files are stored outside the repository by default. No web pages are downloaded locally.

## Requirements

- Python 3.11 or newer
- FFmpeg available on the system `PATH`
- Internet access to Lichess, Edge-TTS, and YouTube API services
- A Google Cloud OAuth client configured for the YouTube Data API v3

Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Preview Locally

Preview mode never authenticates with YouTube and never uploads:

```powershell
python script.py --preview
```

The preview is written to:

```text
%TEMP%\youtube-auto\temp_output_short.mp4
%TEMP%\youtube-auto\preview_frame.jpg
%TEMP%\youtube-auto\preview_background.wav
```

Use `--no-upload` for a normal render that should also skip YouTube upload. The GitHub workflow uploads only from a manual run or its schedule; pushing code does not upload a video.

## Game Commentary Videos

Create a move-by-move narrated video from a PGN file or a public game URL:

```powershell
python game_video.py demo_game.pgn -o game-demo.mp4
python game_video.py "https://lichess.org/GAME_ID" -o lichess-game.mp4
```

The video length grows with the number of moves. Lichess export URLs are supported directly. Chess.com and ChessBase pages must expose PGN text; otherwise export the game as a `.pgn` file and pass that file. PNG/JPG inputs are supported as visual position scenes, but move commentary requires PGN because a screenshot does not contain the full move history.

To use a different output directory:

```powershell
$env:OUTPUT_DIR = "C:\path\to\output"
python script.py --preview
```

## Local YouTube Upload

For unattended local uploads, set `YOUTUBE_TOKEN_JSON` to the complete authorized OAuth token JSON. The Google account used to authorize the token determines the YouTube channel.

```powershell
$env:YOUTUBE_TOKEN_JSON = Get-Content "C:\path\youtube-token.json" -Raw
python script.py
```

For the first interactive authorization only, use the OAuth client JSON as an environment variable. Do not commit a client-secret file:

```powershell
$env:YOUTUBE_CLIENT_SECRET_JSON = Get-Content "C:\path\client_secret.json" -Raw
python script.py
```

Complete the browser login with the Google account for the intended YouTube channel. The local refresh token is stored in the temporary output directory.

## GitHub Actions Setup

The workflow is located at `.github/workflows/youtube.yml`.

1. Open the repository on GitHub.
2. Go to **Settings**, **Secrets and variables**, then **Actions**.
3. Create a repository secret named `YOUTUBE_TOKEN_JSON`.
4. Set its value to a complete authorized YouTube OAuth token JSON.

The token must have the `https://www.googleapis.com/auth/youtube.upload` scope and must belong to the YouTube channel where the Shorts should be published. Never put the client secret or token directly in the workflow or source code.

## Actions Behavior

Manual workflow runs upload directly. There is no preview step in GitHub Actions. Use `python script.py --preview` locally when you want to inspect a video without uploading.

Scheduled runs publish automatically at these India Standard Time slots:

| IST | UTC cron time |
| --- | --- |
| 12:00 AM | 6:30 PM previous day |
| 4:00 AM | 10:30 PM previous day |
| 6:00 AM | 12:30 AM |
| 12:00 PM | 6:30 AM |
| 7:00 PM | 1:30 PM |

GitHub Actions cron uses UTC and can start a few minutes late. Scheduled runs require a fresh Lichess `/next` puzzle rated `2000+`. The daily fallback is disabled in Actions, so a run fails instead of uploading a repeated daily puzzle when Lichess is rate-limited or does not return a new hard puzzle.

## Configuration

The following environment variables are supported:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OUTPUT_DIR` | System temporary directory | Generated media and local token location |
| `MIN_PUZZLE_RATING` | `2000` | Minimum target rating for selected puzzles |
| `PUZZLE_FETCH_ATTEMPTS` | `3` | Number of `/next` requests per run |
| `ALLOW_DAILY_FALLBACK` | `true` | Allow the daily puzzle when `/next` is unavailable; Actions sets this to `false` |
| `PIECE_STYLE` | `assets` | Board pieces: Lichess-style image set, or `unicode`/`letters` for the fallback renderers |
| `BACKGROUND_VOLUME` | `0.35` | Background music mix level; `0.30`-`0.40` keeps narration clear |

## Troubleshooting

- **HTTP 429 from Lichess:** wait before retrying. Do not repeatedly start the workflow; scheduled runs will try again at the next slot.
- **Missing FFmpeg:** install FFmpeg and ensure `ffmpeg` runs from PowerShell or the Actions runner.
- **YouTube authentication error:** recreate `YOUTUBE_TOKEN_JSON` using an account with YouTube upload permission.
- **No upload on a manual run:** check that the `YOUTUBE_TOKEN_JSON` repository secret exists and contains a valid refresh token.

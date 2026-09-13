# YouTube auto uploader

This script fetches one Lichess puzzle, creates a vertical MP4, and optionally uploads it to YouTube. It does not download web pages or store generated media in the repository; temporary files are written to the system temporary directory.

## Local run

Install FFmpeg and the Python packages:

```powershell
python -m pip install -r requirements.txt
python script.py --preview
```

This creates a preview MP4 without authenticating or uploading. Review `temp_output_short.mp4` in the system temporary directory before publishing.

To upload locally, set `YOUTUBE_TOKEN_JSON` to the JSON for an authorized YouTube OAuth token. For a first browser-based login, set `YOUTUBE_CLIENT_SECRET_JSON` to the OAuth client JSON as an environment variable; do not add a client-secret file to this repository.

## GitHub Actions

Add a repository secret named `YOUTUBE_TOKEN_JSON`. A manual workflow run previews the video and stores it as a downloadable Actions artifact. Set the manual `publish` input to `true` only after reviewing it. Scheduled runs require a fresh Lichess `/next` puzzle rated `2000+`; they do not reuse the daily fallback. Five Shorts upload daily at 00:00 (midnight), 04:00, 06:00, 12:00 (noon), and 19:00 IST. These correspond to 18:30, 22:30, 00:30, 06:30, and 13:30 UTC.
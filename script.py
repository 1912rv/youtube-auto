#!/usr/bin/env python3
"""
chess_short_pipeline.py

Complete working pipeline for Lichess puzzle Shorts:
1. Fetches daily/random puzzle data from Lichess API
2. Draws vertical 9:16 video frames (CairoSVG + Pillow)
3. Generates neural voiceovers via Edge-TTS & move sound effects via Scipy
4. Composes MP4 video using MoviePy 2.x
5. Automatically uploads via Official YouTube Data API v3 (videos.insert)
"""

import os
import io
import sys
import json
import math
import random
import pickle
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import chess
import chess.pgn
import chess.svg
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from scipy.io import wavfile
import edge_tts

# MoviePy 2.x Import Standard
import moviepy as mp

# Google YouTube Data API v3 SDK
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# ---------------- CONFIGURATION ----------------
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", Path(tempfile.gettempdir()) / "youtube-auto"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_PICKLE_FILE = str(OUTPUT_DIR / "token.pickle")
PUZZLE_HISTORY_FILE = OUTPUT_DIR / "puzzle_history.json"

# OAuth 2.0 Scope for YouTube Video Uploads
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

WIDTH, HEIGHT = 1080, 1920
BOARD_SIZE = 1000
MIN_PUZZLE_SECONDS = 10.0
MIN_SOLUTION_SECONDS = 4.0
MIN_PUZZLE_RATING = int(os.environ.get("MIN_PUZZLE_RATING", "2000"))
PUZZLE_FETCH_ATTEMPTS = int(os.environ.get("PUZZLE_FETCH_ATTEMPTS", "3"))
ALLOW_DAILY_FALLBACK = os.environ.get("ALLOW_DAILY_FALLBACK", "true").lower() == "true"
FPS = 30
VOICE_NAME = "en-US-ChristopherNeural"
BACKGROUND_VOLUME = float(os.environ.get("BACKGROUND_VOLUME", "0.35"))
PIECE_STYLE = os.environ.get("PIECE_STYLE", "assets").lower()
PIECE_ASSET_DIR = Path(__file__).with_name("piece_assets")

HOOKS = [
    # Original
    "🔥 ONLY 1% CAN SOLVE THIS",
    "♟️ FIND THE BEST MOVE",
    "🧠 CAN YOU SOLVE THIS?",
    "😈 MOST PLAYERS MISS THIS",
    "⏳ YOU HAVE 5 SECONDS",
    
    # New Additions
    "👑 GRANDMASTERS SEE THIS INSTANTLY",
    "😱 LOOKS WINNING... BUT IT'S A TRAP",
    "⚡ 3 SECONDS TO SAVE THE GAME",
    "💣 THERE IS ONLY ONE WINNING MOVE",
    "🎯 PROOF YOU HAVE 2000+ ELO",
    "☠️ BRUTAL CHECKMATE INCOMING",
    "🔥 SACRIFICE EVERYTHING FOR THE WIN",
    "🚨 QUICK! SPOT THE DIRTY TACTIC",
]

def move_commentary(board_before, move, san, ply_number):
    piece = board_before.piece_at(move.from_square)
    actor = "White" if board_before.turn == chess.WHITE else "Black"
    if board_before.is_castling(move):
        idea = "castles to bring the king to safety and connect the rooks"
    elif board_before.is_capture(move):
        captured = board_before.piece_at(move.to_square)
        captured_name = chess.piece_name(captured.piece_type) if captured else "a piece"
        idea = f"captures {captured_name}, changing the material balance"
    elif piece and piece.piece_type == chess.PAWN:
        idea = "takes space and improves the position"
    else:
        idea = "improves the piece and keeps the position under control"
    if san.endswith("#"):
        idea = "delivers checkmate"
    elif san.endswith("+"):
        idea = "comes with check and forces a response"
    piece_name = chess.piece_name(piece.piece_type) if piece else "move"
    return f"Move {(ply_number + 1) // 2}. {actor} plays {san}. This {piece_name} {idea}."

# ---------------- MOVIEPY 2.X HELPER COMPATIBILITY ----------------
def set_clip_duration(clip, duration):
    return clip.with_duration(duration) if hasattr(clip, "with_duration") else clip.set_duration(duration)

def set_clip_start(clip, start_time):
    return clip.with_start(start_time) if hasattr(clip, "with_start") else clip.set_start(start_time)

def set_clip_audio(video_clip, audio_clip):
    return video_clip.with_audio(audio_clip) if hasattr(video_clip, "with_audio") else video_clip.set_audio(audio_clip)

def duck_audio_during_voice(audio_clip, voice_intervals, duck_level=0.28):
    """Lower the music only while a voiceover interval is active."""
    def apply_ducking(get_frame, time_value):
        times = np.asarray(time_value)
        active = np.zeros(times.shape, dtype=bool)
        for start, end in voice_intervals:
            active |= (times >= start) & (times < end)
        factor = np.where(active, duck_level, 1.0)
        frames = get_frame(time_value)
        if times.ndim == 0:
            return frames * float(factor)
        return frames * factor[..., np.newaxis]

    return audio_clip.transform(apply_ducking)

def format_srt_time(seconds):
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{milliseconds:03}"

def write_srt(filename, captions):
    with open(filename, "w", encoding="utf-8") as subtitle_file:
        for index, (start, end, text) in enumerate(captions, start=1):
            subtitle_file.write(
                f"{index}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{text}\n\n"
            )

# ---------------- AUDIO GENERATORS ----------------
import asyncio

async def generate_edge_tts(text, output_file):
    communicate = edge_tts.Communicate(text, VOICE_NAME)
    await communicate.save(output_file)

def generate_voiceover_file(text, output_file):
    asyncio.run(generate_edge_tts(text, output_file))

def convert_san_to_speech(side_text, san_move):
    if not san_move or san_move == "N/A":
        return f"{side_text}. Can you find the best move in 5 seconds?", "Time is up! No solution found."

    san_clean = san_move.replace("+", " check").replace("#", " checkmate")
    piece_map = {"K": "King ", "Q": "Queen ", "R": "Rook ", "B": "Bishop ", "N": "Knight "}

    if san_clean and san_clean[0] in piece_map:
        piece = piece_map[san_clean[0]]
        move_body = san_clean[1:]
        spoken_move = (piece + "takes " + move_body.replace("x", "")) if "x" in move_body else (piece + "to " + move_body)
    else:
        spoken_move = san_clean.replace("x", " takes ") if "x" in san_clean else ("pawn to " + san_clean)

    puzzle_script = f"{side_text}. Can you find the best move in 5 seconds?"
    solution_script = f"Time is up! The best move is {spoken_move}."
    return puzzle_script, solution_script

def generate_chess_move_sound(filename="chess_move.wav", sample_rate=44100):
    duration = 0.12
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    envelope = np.exp(-35 * t)
    tone = (0.7 * np.sin(2 * np.pi * 220 * t) + 0.3 * np.sin(2 * np.pi * 440 * t)) * envelope
    noise = np.random.uniform(-0.1, 0.1, len(t)) * np.exp(-60 * t)
    mix = tone + noise
    
    max_val = np.max(np.abs(mix))
    if max_val > 0:
        mix = mix / max_val

    audio_int16 = (mix * 32767 * 0.7).astype(np.int16)
    wavfile.write(filename, sample_rate, np.column_stack((audio_int16, audio_int16)))
    return filename

def generate_background_sound(filename="background.wav", sample_rate=44100, duration=20.0):
    """Create a calm, entertaining royalty-free chess-short music bed."""
    sample_count = int(sample_rate * duration)
    time_axis = np.arange(sample_count) / sample_rate
    beat = 96 / 60
    bar = np.floor(time_axis * beat / 4).astype(int) % 4
    roots = np.array((261.63, 196.00, 220.00, 174.61))
    root = roots[bar]
    pad = (
        np.sin(2 * np.pi * root * time_axis)
        + np.sin(2 * np.pi * root * 1.25 * time_axis)
        + np.sin(2 * np.pi * root * 1.5 * time_axis)
        + 0.35 * np.sin(2 * np.pi * root * 2 * time_axis)
    ) / 3.35
    bass = np.sin(2 * np.pi * root * 0.5 * time_axis)
    step = np.floor(time_axis * beat * 2).astype(int) % 8
    melody_intervals = np.array((2.0, 2.5, 3.0, 2.5, 2.25, 2.5, 3.5, 2.5))
    melody = np.sin(2 * np.pi * root * melody_intervals[step] * time_axis)
    pluck_decay = np.exp(-8 * ((time_axis * beat * 2) % 1))
    soft_kick = np.sin(2 * np.pi * 82 * time_axis) * np.exp(-18 * ((time_axis * beat) % 1))
    envelope = np.minimum(1.0, time_axis / 1.5) * np.minimum(1.0, (duration - time_axis) / 1.5)
    pulse = 0.85 + 0.15 * np.sin(2 * np.pi * beat * time_axis)
    mix = (0.032 * pad + 0.018 * bass + 0.009 * melody * pluck_decay + 0.006 * soft_kick) * pulse
    mix *= np.maximum(0, envelope)
    left = mix * (1.0 + 0.035 * np.sin(2 * np.pi * 0.17 * time_axis))
    right = mix * (1.0 - 0.035 * np.sin(2 * np.pi * 0.17 * time_axis))
    audio_int16 = (np.column_stack((left, right)) * 32767).astype(np.int16)
    wavfile.write(filename, sample_rate, audio_int16)
    return filename

# ---------------- LICHESS API CLIENT ----------------
def fetch_puzzle_from_lichess():
    url = "https://lichess.org/api/puzzle/next"
    headers = {"Accept": "application/json", "User-Agent": "ChessShortsBot/5.0"}
    candidates = []
    try:
        seen_ids = set(json.loads(PUZZLE_HISTORY_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        seen_ids = set()

    for attempt in range(PUZZLE_FETCH_ATTEMPTS):
        if attempt:
            time.sleep(1.1)
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 429:
            break
        response.raise_for_status()
        puzzle_json = response.json()
        puzzle_id = puzzle_json.get("puzzle", {}).get("id")
        if puzzle_id in seen_ids:
            continue
        rating = puzzle_json.get("puzzle", {}).get("rating")
        if isinstance(rating, int):
            candidates.append((rating, puzzle_json))
            if rating >= MIN_PUZZLE_RATING:
                break

    if candidates:
        rating, selected = max(candidates, key=lambda candidate: candidate[0])
    else:
        if not ALLOW_DAILY_FALLBACK:
            raise RuntimeError("No new Lichess puzzle was available; daily fallback is disabled.")
        fallback = requests.get(
            "https://lichess.org/api/puzzle/daily",
            headers=headers,
            timeout=30,
        )
        fallback.raise_for_status()
        selected = fallback.json()
        rating = selected.get("puzzle", {}).get("rating")
        puzzle_id = selected.get("puzzle", {}).get("id")
        if puzzle_id in seen_ids:
            raise RuntimeError("Lichess returned a puzzle already used by this project; try again later.")
        print(f"⚠️ Using Lichess daily fallback rated {rating}; target was {MIN_PUZZLE_RATING}+")

    puzzle_id = selected.get("puzzle", {}).get("id")
    if not puzzle_id:
        raise ValueError("Lichess puzzle response did not include an ID.")
    history = list(seen_ids | {puzzle_id})[-100:]
    PUZZLE_HISTORY_FILE.write_text(json.dumps(history), encoding="utf-8")
    if candidates:
        print(f"🎯 Selected new hard puzzle rated {rating} (target: {MIN_PUZZLE_RATING}+)")
    return selected

def get_board_from_puzzle_json(puzzle_json):
    solution = puzzle_json.get("puzzle", {}).get("solution", [])
    initial_ply = puzzle_json.get("puzzle", {}).get("initialPly", 0)
    fen = puzzle_json.get("fen")

    if fen:
        fen_board = chess.Board(fen)
        if not solution or chess.Move.from_uci(solution[0]) in fen_board.legal_moves:
            return fen_board, solution

    pgn_text = puzzle_json.get("game", {}).get("pgn", "")
    if not pgn_text:
        raise ValueError("No FEN or PGN found in puzzle payload.")
        
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    moves = list(game.mainline_moves())

    candidate_plies = [initial_ply, initial_ply - 1, initial_ply + 1]
    first_move = solution[0] if solution else None
    
    for ply in candidate_plies:
        if 0 <= ply <= len(moves):
            board = game.board()
            for mv in moves[:ply]:
                board.push(mv)
            if first_move:
                try:
                    if chess.Move.from_uci(first_move) in board.legal_moves:
                        return board, solution
                except Exception:
                    pass
            else:
                return board, solution

    board = game.board()
    for mv in moves[:initial_ply]:
        board.push(mv)
    return board, solution

# ---------------- BOARD & GRAPHICS RENDERER ----------------
def get_font(size=48):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf"
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size=size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

def get_piece_font(size=48):
    candidates = [
        "C:\\Windows\\Fonts\\seguisym.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                pass
    return get_font(size)

def get_piece_image(symbol, size):
    cache_key = (symbol, size)
    if cache_key not in get_piece_image.cache:
        asset_name = ("w" if symbol.isupper() else "b") + symbol.upper()
        asset_path = PIECE_ASSET_DIR / f"{asset_name}.png"
        if not asset_path.exists():
            return None
        with Image.open(asset_path) as source:
            get_piece_image.cache[cache_key] = source.convert("RGBA").resize(
                (size, size), Image.Resampling.LANCZOS
            )
    return get_piece_image.cache[cache_key]

get_piece_image.cache = {}

def draw_centered_text(draw, text, y, font, width, fill="white", stroke_width=3, stroke_fill="black"):
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    x = (width - text_w) // 2
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)

def fit_text(text, limit=48):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        if current and len(current) + len(word) + 1 > limit:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines

def generate_board_pil(board, arrows=None, size_px=BOARD_SIZE, perspective=None):
    image = Image.new("RGB", (size_px, size_px), "#F0D9B5")
    draw = ImageDraw.Draw(image)
    square_size = size_px // 8
    light_square = "#F0D9B5"
    dark_square = "#B58863"
    piece_font = get_font(int(square_size * 0.62)) if PIECE_STYLE == "letters" else get_piece_font(int(square_size * 0.78))
    letter_pieces = {
        "P": "P", "N": "N", "B": "B", "R": "R", "Q": "Q", "K": "K",
        "p": "P", "n": "N", "b": "B", "r": "R", "q": "Q", "k": "K",
    }
    unicode_pieces = {
        "P": "♙", "N": "♘", "B": "♗", "R": "♖", "Q": "♕", "K": "♔",
        "p": "♟", "n": "♞", "b": "♝", "r": "♜", "q": "♛", "k": "♚",
    }
    pieces = letter_pieces if PIECE_STYLE == "letters" else unicode_pieces
    perspective = board.turn if perspective is None else perspective

    for file_index in range(8):
        for rank_index in range(8):
            if perspective == chess.WHITE:
                x = file_index * square_size
                y = (7 - rank_index) * square_size
            else:
                x = (7 - file_index) * square_size
                y = rank_index * square_size
            draw.rectangle(
                (x, y, x + square_size, y + square_size),
                fill=light_square if (file_index + rank_index) % 2 == 0 else dark_square,
            )

    for square, piece in board.piece_map().items():
        file_index = chess.square_file(square)
        rank_index = chess.square_rank(square)
        if perspective == chess.WHITE:
            x = file_index * square_size
            y = (7 - rank_index) * square_size
        else:
            x = (7 - file_index) * square_size
            y = rank_index * square_size
        if PIECE_STYLE == "assets":
            piece_image = get_piece_image(piece.symbol(), square_size)
            if piece_image is not None:
                image.paste(piece_image, (x, y), piece_image)
                continue

        label = pieces[piece.symbol()]
        bbox = draw.textbbox((0, 0), label, font=piece_font, stroke_width=2)
        text_x = x + (square_size - (bbox[2] - bbox[0])) / 2 - bbox[0]
        text_y = y + (square_size - (bbox[3] - bbox[1])) / 2 - bbox[1]
        piece_fill = "#F8F8F8" if piece.color == chess.WHITE else "#171717"
        piece_stroke = "#171717" if piece.color == chess.WHITE else "#F8F8F8"
        draw.text((text_x, text_y), label, font=piece_font, fill=piece_fill, stroke_width=3, stroke_fill=piece_stroke)

    draw.rectangle((0, 0, size_px - 1, size_px - 1), outline="#111111", width=8)

    for arrow in arrows or []:
        start_file = chess.square_file(arrow.tail)
        start_rank = chess.square_rank(arrow.tail)
        end_file = chess.square_file(arrow.head)
        end_rank = chess.square_rank(arrow.head)
        if perspective == chess.WHITE:
            start = ((start_file + 0.5) * square_size, (7 - start_rank + 0.5) * square_size)
            end = ((end_file + 0.5) * square_size, (7 - end_rank + 0.5) * square_size)
        else:
            start = ((7 - start_file + 0.5) * square_size, (start_rank + 0.5) * square_size)
            end = ((7 - end_file + 0.5) * square_size, (end_rank + 0.5) * square_size)
        draw.line((start, end), fill="#00E676", width=max(8, square_size // 16))

    return image

def create_reel_frame_array(
    board_pil,
    hook_text,
    side_text,
    footer_text,
    is_solution=False,
    countdown=None,
    info_text=None,
    caption=None,
):
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (15, 15, 18))
    board_resized = board_pil.resize((BOARD_SIZE, BOARD_SIZE), Image.Resampling.LANCZOS)
    canvas.paste(board_resized, ((WIDTH - BOARD_SIZE) // 2, 440))

    draw = ImageDraw.Draw(canvas)
    draw_centered_text(draw, hook_text, 90, get_font(68), WIDTH, fill="#FFD700")
    draw_centered_text(draw, side_text, 240, get_font(58), WIDTH, fill="#FFFFFF")
    if info_text:
        draw_centered_text(draw, info_text, 315, get_font(28), WIDTH, fill="#A8B3C7")
    draw_centered_text(draw, footer_text, 1550, get_font(54), WIDTH, fill="#00FF7F" if is_solution else "#FFFFFF")
    draw_centered_text(draw, "WAS YOUR MOVE RIGHT?" if is_solution else "COMMENT YOUR NEXT MOVE", 1620, get_font(38), WIDTH, fill="#CCCCCC")

    action_font = get_font(28)
    like_box = (260, 1465, 500, 1517)
    subscribe_box = (580, 1465, 820, 1517)
    draw.rounded_rectangle(like_box, radius=18, fill="#E84855")
    draw.rounded_rectangle(subscribe_box, radius=18, fill="#FFFFFF")
    draw.text((like_box[0] + 24, like_box[1] + 10), "♥  LIKE", font=action_font, fill="#FFFFFF")
    bell_fill = "#171717"
    draw.ellipse((598, 1474, 622, 1497), fill=bell_fill)
    draw.rectangle((594, 1489, 626, 1502), fill=bell_fill)
    draw.ellipse((604, 1499, 616, 1508), fill=bell_fill)
    draw.text((640, subscribe_box[1] + 10), "SUBSCRIBE", font=action_font, fill=bell_fill)

    if countdown is not None:
        draw_centered_text(draw, f"TIME: {countdown}s", 350, get_font(42), WIDTH, fill="#FF6B6B")
        progress_width = int((countdown / MIN_PUZZLE_SECONDS) * 760)
        draw.rounded_rectangle((160, 1740, 920, 1770), radius=15, fill="#333333")
        draw.rounded_rectangle((160, 1740, 160 + progress_width, 1770), radius=15, fill="#FF6B6B")

    if caption:
        caption_font = get_font(30)
        draw.rounded_rectangle((90, 1800, 990, 1890), radius=18, fill="#000000")
        for line_index, line in enumerate(fit_text(caption, limit=48)[:2]):
            draw_centered_text(
                draw,
                line,
                1808 + line_index * 34,
                caption_font,
                WIDTH,
                fill="#FFFFFF",
                stroke_width=1,
                stroke_fill="#000000",
            )
    
    return np.array(canvas)

# ---------------- GOOGLE YOUTUBE DATA API V3 AUTH & UPLOAD ----------------
def get_youtube_authenticated_service():
    creds = None
    token_json = os.environ.get("YOUTUBE_TOKEN_JSON")
    if token_json:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    elif os.path.exists(TOKEN_PICKLE_FILE):
        with open(TOKEN_PICKLE_FILE, 'rb') as token:
            creds = pickle.load(token)

    # Refresh token if expired or authenticate for first time
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_secret_json = os.environ.get("YOUTUBE_CLIENT_SECRET_JSON")
            if not client_secret_json:
                raise FileNotFoundError(
                    "Set YOUTUBE_TOKEN_JSON for automation, or set "
                    "YOUTUBE_CLIENT_SECRET_JSON for the first local OAuth login."
                )
            flow = InstalledAppFlow.from_client_config(json.loads(client_secret_json), SCOPES)
            creds = flow.run_local_server(port=0)

        # Save credentials for future execution
        with open(TOKEN_PICKLE_FILE, 'wb') as token:
            pickle.dump(creds, token)

    return build('youtube', 'v3', credentials=creds)

def upload_video_google_api(video_path, title, description, tags=None):
    try:
        print("🔐 Authenticating with YouTube Data API v3...")
        youtube = get_youtube_authenticated_service()

        body = {
            'snippet': {
                'title': title,
                'description': description,
                'tags': tags or ['chess', 'shorts', 'chesstactics', 'puzzles'],
                'categoryId': '24'  # Category: Entertainment / Gaming
            },
            'status': {
                'privacyStatus': 'public',
                'selfDeclaredMadeForKids': False
            }
        }

        print("📦 Uploading video via YouTube API...")
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype='video/mp4')
        
        request = youtube.videos().insert(
            part=','.join(body.keys()),
            body=body,
            media_body=media
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"⏳ Upload progress: {int(status.progress() * 100)}%")

        video_id = response.get('id')
        print(f"✅ Video successfully uploaded! Video ID: {video_id}")
        print(f"🔗 URL: https://youtube.com/shorts/{video_id}")
        return True

    except Exception as e:
        print(f"❌ YouTube Data API v3 Upload failed: {e}")
        return False

# ---------------- MAIN PIPELINE ----------------
def run_pipeline():
    print("🧩 Fetching puzzle from Lichess...")
    puzzle_json = fetch_puzzle_from_lichess()
    board, solution_moves = get_board_from_puzzle_json(puzzle_json)
    puzzle_data = puzzle_json.get("puzzle", {})
    puzzle_id = puzzle_data.get("id", "unknown")
    puzzle_rating = puzzle_data.get("rating", "unrated")
    info_text = f"Puzzle rating: {puzzle_rating}"

    hook = random.choice(HOOKS)
    side_name = "White to move" if board.turn == chess.WHITE else "Black to move"
    side_text = "WHITE TO MOVE" if board.turn == chess.WHITE else "BLACK TO MOVE"
    
    # 1. Render initial board frame
    puzzle_board_pil = generate_board_pil(board)

    # 2. Render each valid solution move as its own scene.
    solution_frames = []
    solution_captions = []
    solution_speech = []
    first_solution_san = "N/A"
    solution_board = board.copy()
    for move_index, move_uci in enumerate(solution_moves or [], start=1):
        try:
            move = chess.Move.from_uci(move_uci)
            if move not in solution_board.legal_moves:
                break
            san = solution_board.san(move)
            if not solution_frames:
                first_solution_san = san
            is_player_move = solution_board.turn == board.turn
            move_side = "WHITE" if solution_board.turn == chess.WHITE else "BLACK"
            commentary = move_commentary(solution_board, move, san, move_index)
            if is_player_move:
                solution_hook = "BEST MOVE"
                solution_side = f"{move_side} PLAYS"
                _, spoken_line = convert_san_to_speech(side_name, san)
            else:
                solution_hook = "OPPONENT REPLY"
                solution_side = f"{move_side} REPLIES"
                spoken_line = f"The opponent replies with {san}."
            arrow = chess.svg.Arrow(move.from_square, move.to_square, color="#00E676")
            solution_board.push(move)
            solution_frames.append(
                create_reel_frame_array(
                    generate_board_pil(solution_board, arrows=[arrow], perspective=board.turn),
                    solution_hook,
                    solution_side,
                    footer_text=f"Move {move_index}: {san}",
                    is_solution=True,
                    info_text=info_text,
                    caption=commentary,
                )
            )
            solution_captions.append(commentary)
            solution_speech.append(spoken_line)
        except (ValueError, chess.IllegalMoveError):
            break

    if not solution_frames:
        solution_frames = [
            create_reel_frame_array(
                puzzle_board_pil,
                "SOLUTION",
                side_text,
                footer_text="No solution move available",
                is_solution=True,
                info_text=info_text,
                caption="No solution was returned for this puzzle.",
            )
        ]
        solution_captions = ["No solution was returned for this puzzle."]
        solution_speech = ["No solution was returned for this puzzle."]

    preview_mode = "--preview" in sys.argv
    if preview_mode:
        Image.fromarray(solution_frames[-1]).save(OUTPUT_DIR / "preview_frame.jpg", quality=92)

    # 3. Audio & Voice Generation
    p_script, _ = convert_san_to_speech(side_name, first_solution_san)
    s_script = " ".join(solution_speech)
    
    temp_p_audio = str(OUTPUT_DIR / "temp_p.mp3")
    temp_s_audio = str(OUTPUT_DIR / "temp_s.mp3")
    chess_sfx = generate_chess_move_sound(str(OUTPUT_DIR / "chess_move.wav"))
    background_file = OUTPUT_DIR / ("preview_background.wav" if preview_mode else "background.wav")
    background_audio = generate_background_sound(str(background_file))
    temp_video = str(OUTPUT_DIR / "temp_output_short.mp4")

    print("🎙️ Synthesizing Voiceovers with Edge-TTS...")
    generate_voiceover_file(p_script, temp_p_audio)
    generate_voiceover_file(s_script, temp_s_audio)

    # 4. Assemble video and audio clips, closing all source handles afterwards.
    raw_v1 = mp.AudioFileClip(temp_p_audio)
    raw_v2 = mp.AudioFileClip(temp_s_audio)
    raw_sfx = mp.AudioFileClip(chess_sfx)
    raw_background = mp.AudioFileClip(background_audio)
    v1 = v2 = sfx = background = clip1 = clip2 = final_video = None
    try:
        v1 = raw_v1.with_start(0.2)
        puzzle_duration = max(MIN_PUZZLE_SECONDS, v1.duration + 0.4)
        solution_duration = max(MIN_SOLUTION_SECONDS, raw_v2.duration + 0.4)
        v2 = raw_v2.with_start(puzzle_duration + 0.2)
        sfx = raw_sfx.with_start(puzzle_duration)
        voice_intervals = [
            (0.2, min(puzzle_duration, 0.2 + raw_v1.duration)),
            (puzzle_duration + 0.2, min(puzzle_duration + solution_duration, puzzle_duration + 0.2 + raw_v2.duration)),
        ]
        background = duck_audio_during_voice(
            raw_background.with_volume_scaled(BACKGROUND_VOLUME), voice_intervals
        )

        puzzle_clips = []
        remaining_duration = puzzle_duration
        for countdown in range(math.ceil(puzzle_duration), 0, -1):
            segment_duration = min(1.0, remaining_duration)
            puzzle_frame = create_reel_frame_array(
                puzzle_board_pil,
                hook,
                side_text,
                footer_text="Find the move!",
                countdown=countdown,
                info_text=info_text,
                caption=p_script,
            )
            puzzle_clips.append(mp.ImageClip(puzzle_frame).with_duration(segment_duration))
            remaining_duration -= segment_duration
            if remaining_duration <= 0:
                break

        clip1 = mp.concatenate_videoclips(puzzle_clips, method="compose")
        solution_frame_duration = solution_duration / len(solution_frames)
        solution_clips = [
            mp.ImageClip(frame).with_duration(solution_frame_duration)
            for frame in solution_frames
        ]
        clip2 = mp.concatenate_videoclips(solution_clips, method="compose")
        final_video = mp.concatenate_videoclips([clip1, clip2], method="compose")
        final_video = final_video.with_audio(mp.CompositeAudioClip([background, v1, v2, sfx]))
        subtitle_entries = [(0.2, min(puzzle_duration, 0.2 + raw_v1.duration), p_script)]
        for index, caption in enumerate(solution_captions):
            start = puzzle_duration + (index * solution_frame_duration)
            subtitle_entries.append((start, start + solution_frame_duration, caption))
        write_srt(OUTPUT_DIR / "temp_output_short.srt", subtitle_entries)

        print(f"🎬 Rendering Short MP4 video for move: '{first_solution_san}'...")
        final_video.write_videofile(
            temp_video,
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            audio_fps=44100,
            bitrate="8M",
            preset="medium",
            ffmpeg_params=["-crf", "18"],
            logger=None,
        )

    finally:
        for clip in [raw_v1, raw_v2, raw_sfx, raw_background, final_video, clip1, clip2]:
            if clip is not None and hasattr(clip, "close"):
                clip.close()

        for temporary_file in [temp_p_audio, temp_s_audio, chess_sfx, background_audio]:
            if preview_mode and temporary_file == background_audio:
                continue
            if os.path.exists(temporary_file):
                os.remove(temporary_file)

    # 5. Upload step via Official YouTube Data API v3
    if preview_mode or "--no-upload" in sys.argv:
        print(f"✅ Video created at '{temp_video}'. Upload skipped via --no-upload flag.")
        if preview_mode:
            print(f"🖼️ Still preview created at '{OUTPUT_DIR / 'preview_frame.jpg'}'.")
            print(f"🎵 Background sound preview created at '{background_file}'.")
        return

    yt_title = f"Can You Solve This Chess Puzzle? {puzzle_id} | {datetime.now(timezone.utc):%Y-%m-%d} #Shorts"
    yt_description = f"Can you find the best move for {side_text}?\n\nPuzzle rating: {puzzle_rating}\n\n#chess #shorts #chesstactics #puzzles"
    
    try:
        upload_success = upload_video_google_api(temp_video, yt_title, yt_description)
    finally:
        if os.path.exists(temp_video):
            os.remove(temp_video)

    if upload_success:
        print("✨ Process completed successfully!")

if __name__ == "__main__":
    run_pipeline()
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
import random
import pickle
from pathlib import Path
from io import BytesIO

import requests
import chess
import chess.pgn
import chess.svg
import cairosvg
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
BASE_DIR = Path(__file__).resolve().parent
CLIENT_SECRETS_FILE = next(
    (str(path) for path in sorted(BASE_DIR.glob("client_secret*.json")) if path.is_file()),
    str(BASE_DIR / "client_secret.json"),
)
TOKEN_PICKLE_FILE = str(BASE_DIR / "token.pickle")

# OAuth 2.0 Scope for YouTube Video Uploads
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

WIDTH, HEIGHT = 1080, 1920
BOARD_SIZE = 1000
MIN_PUZZLE_SECONDS = 7.0
MIN_SOLUTION_SECONDS = 4.0
FPS = 30
VOICE_NAME = "en-US-ChristopherNeural"

HOOKS = [
    "🔥 ONLY 1% CAN SOLVE THIS",
    "♟️ FIND THE BEST MOVE",
    "🧠 CAN YOU SOLVE THIS?",
    "😈 MOST PLAYERS MISS THIS",
    "⏳ YOU HAVE 5 SECONDS",
]

# ---------------- MOVIEPY 2.X HELPER COMPATIBILITY ----------------
def set_clip_duration(clip, duration):
    return clip.with_duration(duration) if hasattr(clip, "with_duration") else clip.set_duration(duration)

def set_clip_start(clip, start_time):
    return clip.with_start(start_time) if hasattr(clip, "with_start") else clip.set_start(start_time)

def set_clip_audio(video_clip, audio_clip):
    return video_clip.with_audio(audio_clip) if hasattr(video_clip, "with_audio") else video_clip.set_audio(audio_clip)

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
    duration = 0.15
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    freq = 450 * np.exp(-40 * t) + 80
    tone = np.sin(2 * np.pi * freq * t) * np.exp(-30 * t)
    noise = np.random.uniform(-1, 1, len(t)) * np.exp(-50 * t)
    mix = (tone * 0.7) + (noise * 0.3)
    
    max_val = np.max(np.abs(mix))
    if max_val > 0:
        mix = mix / max_val

    audio_int16 = (mix * 32767 * 0.8).astype(np.int16)
    wavfile.write(filename, sample_rate, np.column_stack((audio_int16, audio_int16)))
    return filename

# ---------------- LICHESS API CLIENT ----------------
def fetch_puzzle_from_lichess():
    url = "https://lichess.org/api/puzzle/next"
    headers = {"Accept": "application/json", "User-Agent": "ChessShortsBot/5.0"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()

def get_board_from_puzzle_json(puzzle_json):
    solution = puzzle_json.get("puzzle", {}).get("solution", [])
    initial_ply = puzzle_json.get("puzzle", {}).get("initialPly", 0)
    fen = puzzle_json.get("fen")

    if fen:
        return chess.Board(fen), solution

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

def draw_centered_text(draw, text, y, font, width, fill="white", stroke_width=3, stroke_fill="black"):
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    x = (width - text_w) // 2
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)

def generate_board_pil(board, arrows=None, size_px=BOARD_SIZE):
    arrows = arrows or []
    orientation = chess.WHITE if board.turn == chess.WHITE else chess.BLACK
    svg = chess.svg.board(board=board, size=900, orientation=orientation, arrows=arrows)
    png_bytes = cairosvg.svg2png(bytestring=svg.encode('utf-8'), output_width=size_px, output_height=size_px)
    return Image.open(BytesIO(png_bytes)).convert("RGB")

def create_reel_frame_array(board_pil, hook_text, side_text, footer_text, is_solution=False):
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (15, 15, 18))
    board_resized = board_pil.resize((BOARD_SIZE, BOARD_SIZE), Image.Resampling.LANCZOS)
    canvas.paste(board_resized, ((WIDTH - BOARD_SIZE) // 2, 440))

    draw = ImageDraw.Draw(canvas)
    draw_centered_text(draw, hook_text, 90, get_font(68), WIDTH, fill="#FFD700")
    draw_centered_text(draw, side_text, 240, get_font(58), WIDTH, fill="#FFFFFF")
    draw_centered_text(draw, footer_text, 1530, get_font(54), WIDTH, fill="#00FF7F" if is_solution else "#FFFFFF")
    draw_centered_text(draw, "🔥 Did you find it?" if is_solution else "👇 Drop your move in the comments", 1660, get_font(38), WIDTH, fill="#CCCCCC")
    
    return np.array(canvas)

# ---------------- GOOGLE YOUTUBE DATA API V3 AUTH & UPLOAD ----------------
def get_youtube_authenticated_service():
    creds = None
    # Check if authorization token exists locally
    if os.path.exists(TOKEN_PICKLE_FILE):
        with open(TOKEN_PICKLE_FILE, 'rb') as token:
            creds = pickle.load(token)

    # Refresh token if expired or authenticate for first time
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRETS_FILE):
                raise FileNotFoundError(
                    f"❌ Client secret file '{CLIENT_SECRETS_FILE}' not found! "
                    "Download client_secret.json from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
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

    hook = random.choice(HOOKS)
    side_name = "White to move" if board.turn == chess.WHITE else "Black to move"
    side_text = "⚪ WHITE TO MOVE" if board.turn == chess.WHITE else "⚫ BLACK TO MOVE"
    
    # 1. Render initial board frame
    puzzle_board_pil = generate_board_pil(board)
    puzzle_frame_np = create_reel_frame_array(puzzle_board_pil, hook, side_text, footer_text="Find the move! ♟️", is_solution=False)

    # 2. Execute move and render solution frame
    readable_move = "N/A"
    sol_board = board.copy()
    if solution_moves:
        try:
            mv = chess.Move.from_uci(solution_moves[0])
            if mv in sol_board.legal_moves:
                readable_move = sol_board.san(mv)
                arrow = chess.svg.Arrow(mv.from_square, mv.to_square, color="#00E676")
                sol_board.push(mv)
                sol_board_pil = generate_board_pil(sol_board, arrows=[arrow])
            else:
                sol_board_pil = puzzle_board_pil
        except Exception:
            sol_board_pil = puzzle_board_pil
    else:
        sol_board_pil = puzzle_board_pil

    sol_frame_np = create_reel_frame_array(sol_board_pil, "✅ SOLUTION", side_text, footer_text=f"Best Move: {readable_move}", is_solution=True)

    # 3. Audio & Voice Generation
    p_script, s_script = convert_san_to_speech(side_name, readable_move)
    
    temp_p_audio = "temp_p.mp3"
    temp_s_audio = "temp_s.mp3"
    chess_sfx = generate_chess_move_sound("chess_move.wav")
    temp_video = "temp_output_short.mp4"

    print("🎙️ Synthesizing Voiceovers with Edge-TTS...")
    generate_voiceover_file(p_script, temp_p_audio)
    generate_voiceover_file(s_script, temp_s_audio)

    # 4. Assemble Video & Audio Clips
    v1 = set_clip_start(mp.AudioFileClip(temp_p_audio), 0.2)
    v2 = mp.AudioFileClip(temp_s_audio)
    sfx = mp.AudioFileClip(chess_sfx)

    puzzle_duration = max(MIN_PUZZLE_SECONDS, v1.duration + 0.4)
    solution_duration = max(MIN_SOLUTION_SECONDS, v2.duration + 0.4)

    sfx = set_clip_start(sfx, puzzle_duration)
    v2 = set_clip_start(v2, puzzle_duration + 0.2)

    clip1 = set_clip_duration(mp.ImageClip(puzzle_frame_np), puzzle_duration)
    clip2 = set_clip_duration(mp.ImageClip(sol_frame_np), solution_duration)
    
    final_video = mp.concatenate_videoclips([clip1, clip2], method="compose")
    composite_audio = mp.CompositeAudioClip([v1, v2, sfx])
    final_video = set_clip_audio(final_video, composite_audio)

    print(f"🎬 Rendering Short MP4 video for move: '{readable_move}'...")
    final_video.write_videofile(temp_video, fps=FPS, codec="libx264", audio_codec="aac", preset="fast", logger=None)

    # Clean up open video file descriptors
    clip1.close(); clip2.close()
    v1.close(); v2.close(); sfx.close()
    final_video.close()

    # 5. Upload step via Official YouTube Data API v3
    if "--no-upload" in sys.argv:
        print(f"✅ Video created at '{temp_video}'. Upload skipped via --no-upload flag.")
        return

    yt_title = f"Can You Solve This Chess Puzzle? 🧩 #Shorts"
    yt_description = f"Can you find the best move for {side_text}?\n\nBest Move: {readable_move}\n\n#chess #shorts #chesstactics #puzzles"
    
    upload_success = upload_video_google_api(temp_video, yt_title, yt_description)
    
    if upload_success:
        for tmp in [temp_p_audio, temp_s_audio, chess_sfx, temp_video]:
            if os.path.exists(tmp):
                os.remove(tmp)
        print("✨ Process completed successfully!")

if __name__ == "__main__":
    run_pipeline()
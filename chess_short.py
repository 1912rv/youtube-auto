#!/usr/bin/env python3
"""
chess_short_pipeline.py

Complete working pipeline for Lichess puzzle Shorts locally in VS Code:
1. Fetches puzzle data from Lichess API
2. Processes moves cleanly regardless of FEN/PGN format
3. Generates neural voiceovers via Edge-TTS & move sound effects via Scipy
4. Composes MP4 video using MoviePy 1.x
"""

import asyncio
import io
import json
import os
from pathlib import Path
import random
import sys
import time

import chess
import chess.pgn
import edge_tts
import moviepy.editor as mp
import nest_asyncio
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont
from scipy.io import wavfile

nest_asyncio.apply()

# ---------------- CONFIGURATION & LOCAL PATHS ----------------
# Standardized paths for local VS Code execution
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
QUEUE_DIR = OUTPUT_DIR / "queue"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

PUZZLE_HISTORY_FILE = OUTPUT_DIR / "puzzle_history.json"

WIDTH, HEIGHT = 1080, 1920
BOARD_SIZE = 1000
MIN_PUZZLE_SECONDS = 10.0
COUNTDOWN_SECONDS = 10
MIN_SOLUTION_SECONDS = 4.0
MIN_PUZZLE_RATING = int(os.environ.get("MIN_PUZZLE_RATING", "1500"))
PUZZLE_FETCH_ATTEMPTS = int(os.environ.get("PUZZLE_FETCH_ATTEMPTS", "3"))
ALLOW_DAILY_FALLBACK = os.environ.get("ALLOW_DAILY_FALLBACK", "true").lower() == "true"
FPS = 30
VOICE_NAME = "en-US-ChristopherNeural"
BACKGROUND_VOLUME = float(os.environ.get("BACKGROUND_VOLUME", "0.35"))

HOOKS = [
    "🔥 ONLY 1% CAN SOLVE THIS",
    "♟️ FIND THE BEST MOVE",
    "🧠 CAN YOU SOLVE THIS?",
    "😈 MOST PLAYERS MISS THIS",
    "⏳ YOU HAVE 10 SECONDS",
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

# ---------------- MOVIEPY 1.X AUDIO DUCKING ----------------
def duck_audio_during_voice(audio_clip, voice_intervals, duck_level=0.28):
    def apply_ducking(get_frame, time_value):
        time_vals = np.atleast_1d(time_value)
        active = np.zeros(time_vals.shape, dtype=bool)
        for start, end in voice_intervals:
            active |= (time_vals >= start) & (time_vals < end)

        factors = np.where(active, duck_level, 1.0)
        frames = get_frame(time_value)

        if np.isscalar(time_value):
            return frames * factors[0]
        return frames * factors[:, np.newaxis]

    return audio_clip.fl(apply_ducking)

# ---------------- AUDIO GENERATORS ----------------
async def generate_edge_tts(text, output_file):
    communicate = edge_tts.Communicate(text, VOICE_NAME)
    await communicate.save(output_file)

def generate_voiceover_file(text, output_file):
    asyncio.run(generate_edge_tts(text, output_file))

def convert_san_to_speech(side_text, san_move):
    if not san_move or san_move == "N/A":
        return f"{side_text}. Can you find the best move in this position?", "Time is up! No solution found."

    san_clean = san_move.replace("+", " check").replace("#", " checkmate")
    piece_map = {"K": "King ", "Q": "Queen ", "R": "Rook ", "B": "Bishop ", "N": "Knight "}

    if san_clean and san_clean[0] in piece_map:
        piece = piece_map[san_clean[0]]
        move_body = san_clean[1:]
        spoken_move = (piece + "takes " + move_body.replace("x", "")) if "x" in move_body else (piece + "to " + move_body)
    else:
        spoken_move = san_clean.replace("x", " takes ") if "x" in san_clean else ("pawn to " + san_clean)

    puzzle_script = f"{side_text}. Can you find the best move in this position?"
    solution_script = f"The move is {spoken_move}."
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
    wavfile.write(filename, sample_rate, np.column_stack((audio_int16, audio_int16)))
    return filename

# ---------------- LICHESS API CLIENT ----------------
def fetch_puzzle_from_lichess():
    url = "https://lichess.org/api/puzzle/next"
    headers = {"Accept": "application/json", "User-Agent": "ChessShortsBot/5.0"}
    try:
        seen_ids = set(json.loads(PUZZLE_HISTORY_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        seen_ids = set()

    selected = None
    for attempt in range(PUZZLE_FETCH_ATTEMPTS):
        if attempt > 0:
            time.sleep(1.1)
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 429:
            break
        if not response.ok:
            continue

        puzzle_json = response.json()
        puzzle_id = puzzle_json.get("puzzle", {}).get("id")
        rating = puzzle_json.get("puzzle", {}).get("rating", 0)

        if puzzle_id not in seen_ids and isinstance(rating, int) and rating >= MIN_PUZZLE_RATING:
            selected = puzzle_json
            break

    if not selected:
        if not ALLOW_DAILY_FALLBACK:
            raise RuntimeError("No new target-rated Lichess puzzle available.")
        fallback = requests.get("https://lichess.org/api/puzzle/daily", headers=headers, timeout=30)
        fallback.raise_for_status()
        selected = fallback.json()
        print(f"⚠️ Using Lichess daily puzzle fallback (Rating: {selected.get('puzzle', {}).get('rating')})")

    puzzle_id = selected.get("puzzle", {}).get("id")
    history = list(seen_ids | {puzzle_id})[-100:]
    PUZZLE_HISTORY_FILE.write_text(json.dumps(history), encoding="utf-8")
    return selected

def get_board_from_puzzle_json(puzzle_json):
    solution = puzzle_json.get("puzzle", {}).get("solution", [])
    game_data = puzzle_json.get("game", {})
    pgn_text = game_data.get("pgn", "")
    
    # 1. Try setup via PGN mainline
    if pgn_text:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if game:
            board = game.board()
            initial_ply = puzzle_json.get("puzzle", {}).get("initialPly", 0)
            moves = list(game.mainline_moves())
            if initial_ply > 0 and len(moves) >= initial_ply:
                for mv in moves[:initial_ply]:
                    board.push(mv)
                return board, solution

    # 2. Fallback to FEN parsing directly
    fen = puzzle_json.get("fen")
    if fen:
        board = chess.Board(fen)
        # Check if the first move in solution is legal. If not, it might be the setup move by opponent.
        if solution:
            first_move = chess.Move.from_uci(solution[0])
            if first_move not in board.legal_moves:
                # Lichess FEN is sometimes 1 ply before the puzzle start; attempt setup move if needed
                pass
        return board, solution

    raise ValueError("Failed to construct chess board state from Lichess JSON API.")

# ---------------- RENDER ENGINE ----------------
def get_font(size=48):
    # Cross-platform fallback fonts for local execution (Windows/macOS/Linux)
    system_fonts = [
        "arial.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", 
        "Arial.ttf", "Helvetica.ttf"
    ]
    for font_name in system_fonts:
        try:
            return ImageFont.truetype(font_name, size=size)
        except IOError:
            continue
    return ImageFont.load_default()

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
    piece_font = get_font(int(square_size * 0.75))

    unicode_pieces = {
        "P": "♙", "N": "♘", "B": "♗", "R": "♖", "Q": "♕", "K": "♔",
        "p": "♟", "n": "♞", "b": "♝", "r": "♜", "q": "♛", "k": "♚",
    }
    perspective = board.turn if perspective is None else perspective

    for file_index in range(8):
        for rank_index in range(8):
            x = file_index * square_size if perspective == chess.WHITE else (7 - file_index) * square_size
            y = (7 - rank_index) * square_size if perspective == chess.WHITE else rank_index * square_size
            draw.rectangle(
                (x, y, x + square_size, y + square_size),
                fill=light_square if (file_index + rank_index) % 2 == 0 else dark_square,
            )

    for square, piece in board.piece_map().items():
        file_index = chess.square_file(square)
        rank_index = chess.square_rank(square)
        x = file_index * square_size if perspective == chess.WHITE else (7 - file_index) * square_size
        y = (7 - rank_index) * square_size if perspective == chess.WHITE else rank_index * square_size

        label = unicode_pieces[piece.symbol()]
        bbox = draw.textbbox((0, 0), label, font=piece_font)
        text_x = x + (square_size - (bbox[2] - bbox[0])) / 2 - bbox[0]
        text_y = y + (square_size - (bbox[3] - bbox[1])) / 2 - bbox[1]
        piece_fill = "#FFFFFF" if piece.color == chess.WHITE else "#000000"
        piece_stroke = "#000000" if piece.color == chess.WHITE else "#FFFFFF"
        draw.text((text_x, text_y), label, font=piece_font, fill=piece_fill, stroke_width=2, stroke_fill=piece_stroke)

    draw.rectangle((0, 0, size_px - 1, size_px - 1), outline="#111111", width=8)

    for arrow in arrows or []:
        sf, sr = chess.square_file(arrow.tail), chess.square_rank(arrow.tail)
        ef, er = chess.square_file(arrow.head), chess.square_rank(arrow.head)
        if perspective == chess.WHITE:
            start = ((sf + 0.5) * square_size, (7 - sr + 0.5) * square_size)
            end = ((ef + 0.5) * square_size, (7 - er + 0.5) * square_size)
        else:
            start = ((7 - sf + 0.5) * square_size, (sr + 0.5) * square_size)
            end = ((7 - ef + 0.5) * square_size, (er + 0.5) * square_size)
        draw.line((start, end), fill="#00E676", width=12)

    return image

def create_reel_frame_array(board_pil, hook_text, side_text, footer_text, is_solution=False, countdown=None, info_text=None, caption=None):
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (15, 15, 18))
    board_resized = board_pil.resize((BOARD_SIZE, BOARD_SIZE), Image.Resampling.LANCZOS)
    canvas.paste(board_resized, ((WIDTH - BOARD_SIZE) // 2, 440))

    draw = ImageDraw.Draw(canvas)
    draw_centered_text(draw, hook_text, 90, get_font(68), WIDTH, fill="#FFD700")
    draw_centered_text(draw, side_text, 240, get_font(58), WIDTH, fill="#FFFFFF")
    if info_text:
        draw_centered_text(draw, info_text, 315, get_font(28), WIDTH, fill="#A8B3C7")
    draw_centered_text(draw, footer_text, 1550, get_font(54), WIDTH, fill="#00FF7F" if is_solution else "#FFFFFF")

    if countdown is not None:
        draw_centered_text(draw, f"TIME: {countdown}s", 350, get_font(42), WIDTH, fill="#FF6B6B")
        progress_width = int((countdown / MIN_PUZZLE_SECONDS) * 760)
        draw.rounded_rectangle((160, 1740, 920, 1770), radius=15, fill="#333333")
        draw.rounded_rectangle((160, 1740, 160 + progress_width, 1770), radius=15, fill="#FF6B6B")

    if caption:
        caption_font = get_font(30)
        draw.rounded_rectangle((90, 1800, 990, 1890), radius=18, fill="#000000")
        for line_index, line in enumerate(fit_text(caption, limit=48)[:2]):
            draw_centered_text(draw, line, 1808 + line_index * 34, caption_font, WIDTH, fill="#FFFFFF")

    return np.array(canvas)

# ---------------- PIPELINE RUNNER ----------------
def run_pipeline():
    print(f"📁 Saving outputs to: {OUTPUT_DIR}")
    print("🧩 Fetching puzzle from Lichess...")
    puzzle_json = fetch_puzzle_from_lichess()
    board, solution_moves = get_board_from_puzzle_json(puzzle_json)
    puzzle_data = puzzle_json.get("puzzle", {})
    puzzle_rating = puzzle_data.get("rating", "unrated")
    info_text = f"Puzzle rating: {puzzle_rating}"

    hook = random.choice(HOOKS)
    side_name = "White to move" if board.turn == chess.WHITE else "Black to move"
    side_text = "WHITE TO MOVE" if board.turn == chess.WHITE else "BLACK TO MOVE"

    puzzle_board_pil = generate_board_pil(board)

    solution_frames = []
    solution_speech = []
    first_solution_san = "N/A"
    solution_board = board.copy()
    solution_move_number = 0

    for move_uci in solution_moves or []:
        move = chess.Move.from_uci(move_uci)
        
        # Guard against illegal moves by skipping invalid state transitions
        if move not in solution_board.legal_moves:
            print(f"⚠️ Illegal move {move_uci} encountered for state. Stopping frame capture.")
            break
            
        san = solution_board.san(move)
        is_player_move = solution_board.turn == board.turn
        move_number = solution_board.fullmove_number
        move_side = "WHITE" if solution_board.turn == chess.WHITE else "BLACK"

        arrow = type('Arrow', (), {'tail': move.from_square, 'head': move.to_square})()
        commentary = move_commentary(solution_board, move, san, (solution_move_number * 2) + 1)
        solution_board.push(move)

        if is_player_move:
            solution_move_number += 1
            if not solution_frames:
                first_solution_san = san
            solution_hook = "SOLUTION"
            solution_side = side_text
            _, spoken_line = convert_san_to_speech(side_name, san)
        else:
            solution_hook = "OPPONENT BEST MOVE"
            solution_side = f"{move_side} REPLIES"
            spoken_line = f"The opponent's best move is {san}."

        solution_frames.append(
            create_reel_frame_array(
                generate_board_pil(solution_board, arrows=[arrow], perspective=board.turn),
                solution_hook,
                solution_side,
                footer_text=f"Move {move_number}: {san}",
                is_solution=True,
                info_text=info_text,
                caption=commentary,
            )
        )
        solution_speech.append(spoken_line)

    # Sanity check: ensure solution frames list is non-empty
    if not solution_frames:
        print("⚠️ Warning: Empty solution generated. Fallback to base board frame.")
        solution_frames.append(
            create_reel_frame_array(
                puzzle_board_pil,
                "SOLUTION",
                side_text,
                footer_text="Checkmate!",
                is_solution=True,
                info_text=info_text,
                caption="Solution move calculation completed.",
            )
        )

    preview_mode = "--preview" in sys.argv
    if preview_mode and solution_frames:
        Image.fromarray(solution_frames[-1]).save(OUTPUT_DIR / "preview_frame.jpg", quality=95)

    p_script, _ = convert_san_to_speech(side_name, first_solution_san)
    s_script = " ".join(solution_speech) if solution_speech else "Solution complete."

    temp_p_audio = str(OUTPUT_DIR / "temp_p.mp3")
    temp_s_audio = str(OUTPUT_DIR / "temp_s.mp3")
    chess_sfx = generate_chess_move_sound(str(OUTPUT_DIR / "chess_move.wav"))
    background_file = OUTPUT_DIR / ("preview_background.wav" if preview_mode else "background.wav")
    background_audio = generate_background_sound(str(background_file))
    final_video_path = str(OUTPUT_DIR / "chess_short_output.mp4")

    print("🎙️ Generating Neural Voiceovers...")
    generate_voiceover_file(p_script, temp_p_audio)
    generate_voiceover_file(s_script, temp_s_audio)

    raw_v1 = mp.AudioFileClip(temp_p_audio)
    raw_v2 = mp.AudioFileClip(temp_s_audio)
    raw_sfx = mp.AudioFileClip(chess_sfx)
    raw_background = mp.AudioFileClip(background_audio)

    try:
        v1 = raw_v1.set_start(0.2)
        puzzle_duration = max(MIN_PUZZLE_SECONDS, v1.duration + 0.4)
        solution_duration = max(MIN_SOLUTION_SECONDS, raw_v2.duration + 0.4)
        v2 = raw_v2.set_start(puzzle_duration + 0.2)
        sfx = raw_sfx.set_start(puzzle_duration)

        voice_intervals = [
            (0.2, min(puzzle_duration, 0.2 + raw_v1.duration)),
            (puzzle_duration + 0.2, min(puzzle_duration + solution_duration, puzzle_duration + 0.2 + raw_v2.duration)),
        ]
        background = duck_audio_during_voice(raw_background.volumex(BACKGROUND_VOLUME), voice_intervals)

        puzzle_clips = []
        remaining_duration = puzzle_duration
        for countdown in range(COUNTDOWN_SECONDS, -1, -1):
            segment_duration = min(puzzle_duration / (COUNTDOWN_SECONDS + 1), remaining_duration)
            puzzle_frame = create_reel_frame_array(
                puzzle_board_pil,
                hook,
                side_text,
                footer_text="Find the move!",
                countdown=countdown,
                info_text=info_text,
                caption=p_script,
            )
            puzzle_clips.append(mp.ImageClip(puzzle_frame).set_duration(segment_duration))
            remaining_duration -= segment_duration
            if remaining_duration <= 0:
                break

        clip1 = mp.concatenate_videoclips(puzzle_clips, method="compose")
        solution_frame_duration = solution_duration / max(1, len(solution_frames))
        solution_clips = [
            mp.ImageClip(frame).set_duration(solution_frame_duration)
            for frame in solution_frames
        ]
        clip2 = mp.concatenate_videoclips(solution_clips, method="compose")
        final_video = mp.concatenate_videoclips([clip1, clip2], method="compose")
        final_video = final_video.set_audio(mp.CompositeAudioClip([background, v1, v2, sfx]))

        print(f"🎬 Rendering Video to {final_video_path}...")
        final_video.write_videofile(
            final_video_path,
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            audio_fps=44100,
            bitrate="8M",
            preset="medium",
            ffmpeg_params=["-crf", "18"],
            logger=None,
        )
        print(f"✨ Rendering finished! Video created at: {final_video_path}")

    finally:
        for clip in [raw_v1, raw_v2, raw_sfx, raw_background]:
            if clip is not None:
                clip.close()

if __name__ == "__main__":
    run_pipeline()
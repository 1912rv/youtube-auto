#!/usr/bin/env python3
"""Create a narrated game video from a PGN, public game URL, or board image."""

import argparse
import asyncio
import io
import os
import re
import sys
import tempfile
from pathlib import Path

import chess
import chess.pgn
import chess.svg
import edge_tts
import moviepy as mp
import numpy as np
import requests
from PIL import Image, ImageDraw

import script as board_renderer


VOICE_NAME = "en-US-ChristopherNeural"
MOVE_SECONDS = 2.8
INTRO_SECONDS = 4.0
OUTRO_SECONDS = 5.0
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
GAME_BOARD_SIZE = 820
GAME_VIDEO_FPS = int(os.environ.get("GAME_VIDEO_FPS", "24"))


def extract_game_id(url):
    match = re.search(r"/(?:game/export/)?([A-Za-z0-9]{6,16})(?:[/?#]|$)", url)
    if not match:
        raise ValueError("Could not find a game ID in the supplied URL.")
    return match.group(1)


def fetch_pgn(source):
    if source.startswith(("http://", "https://")):
        if "lichess.org" in source:
            game_id = extract_game_id(source)
            response = requests.get(
                f"https://lichess.org/game/export/{game_id}",
                headers={"Accept": "application/x-chess-pgn", "User-Agent": "ChessVideoCreator/1.0"},
                timeout=30,
            )
        else:
            response = requests.get(source, headers={"User-Agent": "ChessVideoCreator/1.0"}, timeout=30)
        response.raise_for_status()
        text = response.text
        if "[Event" not in text or "[Result" not in text:
            raise ValueError(
                "This page did not expose PGN text. Export the game as PGN and pass that .pgn file instead."
            )
        return text

    return Path(source).read_text(encoding="utf-8")


def load_game(source):
    if Path(source).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return None, Image.open(source).convert("RGB")
    pgn_text = fetch_pgn(source)
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("No readable chess game was found in the supplied PGN.")
    return game, None


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


def fit_text(text, limit=62):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > limit:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines[:3]


def create_game_frame(board, title, move_label, commentary, arrows=None, source_image=None, is_final=False):
    canvas = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (15, 15, 18))
    if source_image is None:
        board_image = board_renderer.generate_board_pil(
            board, arrows=arrows, size_px=GAME_BOARD_SIZE, perspective=chess.WHITE
        )
    else:
        board_image = source_image.copy()
        board_image.thumbnail((GAME_BOARD_SIZE, GAME_BOARD_SIZE))
        framed = Image.new("RGB", (GAME_BOARD_SIZE, GAME_BOARD_SIZE), "#111111")
        framed.paste(board_image, ((GAME_BOARD_SIZE - board_image.width) // 2, (GAME_BOARD_SIZE - board_image.height) // 2))
        board_image = framed
    canvas.paste(board_image, (80, 130))

    draw = ImageDraw.Draw(canvas)
    title_font = board_renderer.get_font(58)
    move_font = board_renderer.get_font(44)
    body_font = board_renderer.get_font(36)
    draw.text((980, 120), title, font=title_font, fill="#FFD700")
    draw.text((980, 235), move_label, font=move_font, fill="#FFFFFF")
    text_y = 370
    for line in fit_text(commentary):
        draw.text((980, text_y), line, font=body_font, fill="#FFFFFF")
        text_y += 54
    footer = "COMMENT YOUR FAVORITE MOMENT" if is_final else "LIKE  |  SUBSCRIBE"
    draw.text((980, 820), footer, font=body_font, fill="#00FF7F" if is_final else "#FF6670")
    return np.array(canvas)


def build_game_frames(game, image_source=None):
    board = game.board() if game else chess.Board()
    frames = []
    narration = []
    if game:
        headers = game.headers
        title = headers.get("White", "White") + " vs " + headers.get("Black", "Black")
        frames.append(create_game_frame(board, title, "The game begins", "Watch the plan develop move by move."))
        narration.append(f"Welcome to {title}. Watch the key ideas develop move by move.")
        for ply_number, move in enumerate(game.mainline_moves()):
            san = board.san(move)
            commentary = move_commentary(board, move, san, ply_number)
            board.push(move)
            frames.append(
                create_game_frame(
                    board,
                    title,
                    f"{(ply_number + 2) // 2}. {san}",
                    commentary,
                    arrows=[chess.svg.Arrow(move.from_square, move.to_square)] if hasattr(chess, "svg") else None,
                    is_final=board.outcome() is not None,
                )
            )
            narration.append(commentary)
        return frames, narration, len(frames) * MOVE_SECONDS + OUTRO_SECONDS

    frames.append(create_game_frame(chess.Board(), "Game Position", "Visual analysis", "This video is based on the supplied game image.", source_image=image_source))
    return frames, ["Here is the supplied chess game image. Export the game as PGN for move by move commentary."], INTRO_SECONDS


async def create_voiceover(text, output_file):
    await edge_tts.Communicate(text, VOICE_NAME).save(output_file)


def render_video(source, output, no_voice=False):
    game, image_source = load_game(source)
    frames, narration, duration = build_game_frames(game, image_source)
    clips = [mp.ImageClip(frame).with_duration(INTRO_SECONDS if index == 0 else MOVE_SECONDS) for index, frame in enumerate(frames)]
    video = mp.concatenate_videoclips(clips, method="compose")
    temp_audio = None
    audio = None
    try:
        if not no_voice:
            temp_audio = Path(tempfile.mkstemp(suffix=".mp3")[1])
            asyncio.run(create_voiceover(" ".join(narration), str(temp_audio)))
            audio = mp.AudioFileClip(str(temp_audio))
            video = video.with_audio(audio)
        video.write_videofile(
            output,
            fps=GAME_VIDEO_FPS,
            codec="libx264",
            audio_codec="aac" if not no_voice else None,
            preset="ultrafast",
            threads=4,
            logger=None,
        )
    finally:
        if audio is not None:
            audio.close()
        video.close()
        if temp_audio and temp_audio.exists():
            try:
                temp_audio.unlink()
            except PermissionError:
                pass
    print(f"Created {output} ({duration:.1f}s target, {len(frames) - 1} moves).")


def main():
    parser = argparse.ArgumentParser(description="Create a narrated chess game video from PGN, URL, or image.")
    parser.add_argument("source", help="PGN path, Lichess/Chess.com/ChessBase URL, or PNG/JPG board image")
    parser.add_argument("-o", "--output", default="game-video.mp4")
    parser.add_argument("--no-voice", action="store_true", help="Render silently without Edge-TTS")
    args = parser.parse_args()
    render_video(args.source, args.output, args.no_voice)


if __name__ == "__main__":
    main()
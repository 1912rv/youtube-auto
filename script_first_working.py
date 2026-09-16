#!/usr/bin/env python3
"""Stable puzzle-only version of the working YouTube Shorts pipeline.

The implementation is shared with script.py so the original working model has
one source of truth for fetching, rendering, audio, and upload behavior.
"""

import sys
from pathlib import Path

import script as _pipeline


# Public pipeline surface kept here for callers that want the first model name.
OUTPUT_DIR = _pipeline.OUTPUT_DIR
SCOPES = _pipeline.SCOPES
WIDTH = _pipeline.WIDTH
HEIGHT = _pipeline.HEIGHT
BOARD_SIZE = _pipeline.BOARD_SIZE
MIN_PUZZLE_SECONDS = _pipeline.MIN_PUZZLE_SECONDS
MIN_SOLUTION_SECONDS = _pipeline.MIN_SOLUTION_SECONDS
FPS = _pipeline.FPS
HOOKS = _pipeline.HOOKS

fetch_puzzle_from_lichess = _pipeline.fetch_puzzle_from_lichess
get_board_from_puzzle_json = _pipeline.get_board_from_puzzle_json
generate_board_pil = _pipeline.generate_board_pil
create_reel_frame_array = _pipeline.create_reel_frame_array
convert_san_to_speech = _pipeline.convert_san_to_speech
generate_voiceover_file = _pipeline.generate_voiceover_file
upload_video_google_api = _pipeline.upload_video_google_api


def run_first_working_model(arguments=None):
    """Run the complete puzzle Shorts pipeline without game-video features."""
    original_arguments = sys.argv
    try:
        sys.argv = [str(Path(__file__))] + list(arguments or [])
        _pipeline.run_pipeline()
    finally:
        sys.argv = original_arguments


def main():
    run_first_working_model(sys.argv[1:])


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""The read-only Settings view of a queued or running job."""

import shlex
import subprocess
import sys
from pathlib import Path

from fastflix.encoders.common.helpers import Command
from fastflix.models.encode import AudioTrack, SubtitleTrack, VVCSettings
from fastflix.models.video import Video
from fastflix.widgets.windows.job_settings import job_settings_text


def test_job_settings_text_shows_settings_tracks_and_exact_commands():
    video = Video(source=Path("in.mkv"))
    settings = video.video_settings
    settings.output_path = Path("out/movie.mkv")
    settings.video_encoder_settings = VVCSettings(preset="slower", qp="27.5", mctf_level_tilt=150)
    video.audio_tracks = [
        AudioTrack(
            index=1,
            outdex=1,
            codec="truehd",
            language="eng",
            title="Atmos",
            channels=8,
            conversion_codec="libopus",
            conversion_bitrate="128k",
            downmix="7.1",
        ),
        AudioTrack(index=2, outdex=2, codec="ac3", language="fra"),
        AudioTrack(index=3, outdex=3, codec="dts", language="ger", enabled=False),
    ]
    video.subtitle_tracks = [SubtitleTrack(index=4, outdex=4, language="eng", subtitle_type="text", burn_in=True)]
    first = ["ffmpeg", "-i", "in.mkv", "-vvenc-params", "MCTFLevelTilt=150", "C:/a b/out.mkv"]
    settings.conversion_commands = [Command(command=first), Command(command="echo second")]

    text = job_settings_text(video)

    assert "Encoder: VVC" in text
    for line in ("preset: slower", "qp: 27.5", "mctf_level_tilt: 150"):
        assert f"  {line}\n" in text
    assert "#1 eng Atmos: truehd -> libopus, bitrate 128k, 7.1" in text
    assert "#2 fra: ac3 -> copy" in text
    assert "dts" not in text  # disabled track
    assert "#4 eng text, burn-in" in text
    # a list command reads as the command line it runs, not as a Python list
    shown = subprocess.list2cmdline(first) if sys.platform == "win32" else shlex.join(first)
    assert text.endswith(f"Commands:\n  1. {shown}\n  2. echo second")

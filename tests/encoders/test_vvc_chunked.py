# -*- coding: utf-8 -*-
from pathlib import Path
from unittest import mock

from pytest import approx

from fastflix.encoders.vvc import chunked
from fastflix.encoders.vvc.command_builder import build
from fastflix.models.encode import VVCSettings
from fastflix.models.video import VideoSettings

from tests.conftest import create_fastflix_instance

BEGINNING = [
    "ffmpeg", "-y", "-ss", "5", "-to", "25", "-i", "in.mkv", "-max_muxing_queue_size", "1024",
    "-filter_complex", "[0:0]crop=3840:1600:0:280,setsar=1:1[v]", "-map", "[v]", "-c:v", "libvvenc",
    "-pix_fmt", "yuv420p10le",
]  # fmt: skip
ENDING = ["-map_metadata", "-1", "-map", "0:1", "-c:1", "libopus", "out.mkv"]


def _build(**kwargs):
    settings = dict(qp=22, bitrate=None, preset="slower", tier="main", threads=24, chunks=6, chunk_threads=4)
    settings.update(kwargs)
    fastflix = create_fastflix_instance(
        encoder_settings=VVCSettings(**settings),
        video_settings=VideoSettings(remove_hdr=True, maxrate=None, bufsize=None, output_path=Path("out.mkv")),
    )
    fastflix.config.vvc_ffmpeg = None
    with (
        mock.patch("fastflix.encoders.vvc.command_builder.generate_all") as generate_all,
        mock.patch("fastflix.encoders.vvc.command_builder.find_mkvmerge", return_value="mkvmerge"),
    ):
        generate_all.return_value = (list(BEGINNING), list(ENDING), [])
        return build(fastflix)


def test_chunked_command_takes_the_single_encode_apart():
    commands = _build()
    assert len(commands) == 1
    command = commands[0].command
    assert command[1].endswith("chunked.py")
    plan = chunked.decode_plan(command[2])
    assert (plan["chunks"], plan["threads"]) == (6, 4)
    # the user's trim stays on the source input of the join (audio, subtitles), the chunks cut the video themselves
    assert plan["final_inputs"] == ["-ss", "5", "-to", "25", "-i", "in.mkv"]
    assert plan["video_args"] == ["-filter_complex", "[0:0]crop=3840:1600:0:280,setsar=1:1[v]", "-map", "[v]"]
    assert plan["encode_args"][:2] == ["-c:v", "libvvenc"] and "-vvenc-params" in plan["encode_args"]
    assert "-threads" not in plan["encode_args"]  # every chunk gets the chunk threads instead of the 24
    assert plan["final_args"][:2] == ["-max_muxing_queue_size", "1024"] and plan["final_args"][-1] == "out.mkv"


def test_chunking_only_at_constant_qp():
    commands = _build(qp=None, bitrate="6000k")
    assert [c.name for c in commands] == ["First pass bitrate", "Second pass bitrate"]


def test_split_lands_between_two_frames_before_the_cut():
    frames = [10.0, 10.04, 10.08, 10.12]
    assert chunked.snap_split(frames, chunked.nearest_cut(10.0, [10.08, 13.0], window=1.0)) == approx(10.06)
    # no cut in the window: the even split point, moved to between two frames
    assert chunked.snap_split(frames, chunked.nearest_cut(10.05, [20.0], window=1.0)) == approx(10.06)
    assert chunked.snap_split(frames, 9.0) is None  # no frame before it in the window


def test_trim_starts_the_video_chain():
    plan = {"video_args": ["-filter_complex", "[0:0]crop=10:10:0:0[v]", "-map", "[v]"]}
    args = chunked.trimmed_video_args(plan, 1.5, 3.25)
    assert args[1] == "[0:0]trim=start=1.500000:end=3.250000,setpts=PTS-STARTPTS,crop=10:10:0:0[v]"
    assert (
        chunked.trimmed_video_args({"video_args": ["-map", "0:0"]}, None, 2.0)[-1]
        == "trim=end=2.000000,setpts=PTS-STARTPTS"
    )


def test_side_file_stream_numbers_move_down_without_video():
    args = ["-map", "0:1", "-c:1", "libopus", "-metadata:s:2", "title=x", "-c:s", "copy", "-map_chapters", "0", "o.mkv"]
    assert chunked.without_video_stream(args) == [
        "-map", "0:1", "-c:0", "libopus", "-metadata:s:1", "title=x", "-c:s", "copy", "-map_chapters", "0", "o.mkv",
    ]  # fmt: skip


def test_progress_time_counts_every_chunks_frames():
    # each chunk's own out_time trails its frames; the summed time must reach the end when all frames are done
    encoders = [mock.Mock(values={}), mock.Mock(values={})]
    encoders[0].number = {"frame": 100, "total_size": 0, "out_time_us": 3e6}.get
    encoders[1].number = {"frame": 140, "total_size": 0, "out_time_us": 5e6}.get
    assert "frame=240 " in (line := chunked.progress_line(encoders, started=0.0, frame_duration=1 / 24))
    assert "time=00:00:10.00 " in line


def test_join_check_finds_a_lost_and_a_doubled_frame():
    assert chunked.join_problems([0.0, 0.04, 0.08], 0.04) == []
    assert len(chunked.join_problems([0.0, 0.04, 0.12, 0.125], 0.04)) == 2

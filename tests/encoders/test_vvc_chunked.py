# -*- coding: utf-8 -*-
from pathlib import Path
from unittest import mock

from pytest import approx

from fastflix.encoders.common.helpers import Command
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


def test_raw_commands_show_the_steps_the_driver_runs():
    command = _build()[0]
    plan = chunked.decode_plan(command.command[2])
    names = [name for name, _ in command.steps]
    assert names[0].startswith("Each of the 6 chunk encoders (4 threads)") and names[2:] == ["Join the chunks", "Mux"]
    encoder = command.steps[0][1]
    assert encoder[encoder.index("-ss") + 1] == "<chunk start> - 10 s"
    assert "[0:0]trim=start=<chunk start>:end=<chunk end>,setpts=PTS-STARTPTS,crop=" in " ".join(encoder)
    assert encoder[encoder.index("-c:v") :][: len(plan["encode_args"])] == plan["encode_args"]
    assert command.steps[1][1] == chunked.side_command(plan, chunked.work_folder(plan) / chunked.SIDE_FILE_NAME)
    video_only = {**plan, "final_args": ["out.mkv"]}
    assert [name for name, _ in chunked.preview_steps(video_only)][1:] == ["Join the chunks", "Mux"]
    assert Command(command=["ffmpeg"]).steps == []  # queued before the field existed


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


def test_size_estimate_projects_the_video_and_adds_the_whole_side_file():
    # 240 of 2400 frames (10 of 100 s) encoded into 1 MB; the audio/subtitles file of the whole range is 5 MB
    encoder = mock.Mock(values={})
    encoder.number = {"frame": 240, "total_size": 1e6}.get
    line = chunked.progress_line([encoder], started=0.0, frame_duration=1 / 24, side=(5e6, 100.0))
    kbits = float(line.split("bitrate=")[1].split("k")[0])
    assert 100.0 * kbits / 8000 == approx(10e6 / 1e6 + 5e6 / 1e6, rel=1e-3)  # FastFlix: length x bitrate, in MB
    assert "size=5859KiB " in line  # what is on disk: video so far + side file


class _FakeProcess:
    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code


def _side_file(tmp_path, plan, code=0):
    def start(command, error_file):
        Path(command[-1]).write_bytes(b"audio")
        encoder = mock.Mock(process=_FakeProcess(code))
        encoder.last_errors.return_value = ["error"]
        encoder.number = {"out_time_us": 100e6, "total_size": 5}.get
        return encoder

    with (
        mock.patch.object(chunked, "ChunkEncoder", side_effect=start) as encoders,
        mock.patch.object(chunked, "record_processes"),
        mock.patch.object(chunked, "log"),
    ):
        side = chunked.SideFile({"ffprobe": "ffprobe", **plan}, tmp_path, 100.0)
        return side, side.poll(), encoders.call_count


def test_side_file_is_made_once_and_again_when_its_inputs_change(tmp_path):
    source = tmp_path / "in.mkv"
    source.write_bytes(b"a")
    plan = {"ffmpeg": "ffmpeg", "final_inputs": ["-i", str(source)], "final_args": ["-map", "0:1", "out.mkv"]}
    assert _side_file(tmp_path, plan)[1:] == (True, 1)
    assert _side_file(tmp_path, plan)[1:] == (True, 0)  # kept on a resume: no FFmpeg
    source.write_bytes(b"changed")
    assert _side_file(tmp_path, plan)[1:] == (True, 1)
    assert _side_file(tmp_path, {**plan, "final_args": ["-map", "0:2", "out.mkv"]})[1:] == (True, 1)
    side, finished, started = _side_file(tmp_path, {**plan, "final_args": ["out.mkv"]})  # video only
    assert (side.path, finished, started, side.estimate()) == (None, True, 0, (0, 100.0))
    assert _side_file(tmp_path, {**plan, "final_args": ["-map", "0:3", "out.mkv"]}, code=1)[1] is False
    assert _side_file(tmp_path, {**plan, "final_args": ["-map", "0:3", "out.mkv"]})[1:] == (True, 1)  # not kept


def test_running_side_file_is_projected_to_the_end_once_past_the_headers(tmp_path):
    side = chunked.SideFile({"final_args": ["out.mkv"]}, tmp_path, 100.0)
    side.path, side.encoder = tmp_path / "side.mkv", mock.Mock()
    side.encoder.number = {"out_time_us": 10e6, "total_size": 2e6}.get
    assert side.estimate() == (20e6, 100.0)  # 2 MB in 10 of 100 s
    side.encoder.number = {"out_time_us": 1e6, "total_size": 1e6}.get
    assert side.estimate() == (0, 100.0)  # 1 % in: chapters and headers would inflate it


def test_salvage_keeps_the_frames_coded_before_the_last_usable_keyframe():
    d = 1 / 24
    # decode order of a random-access stream (frame numbers): keyframe 0, frames 4, 2, 1, 3, then keyframe 8 whose
    # leading pictures 5-7 are coded after it, then 6 and 5 written before the encoder was killed
    order = [(0, True), (4, False), (2, False), (1, False), (3, False), (8, True), (6, False), (5, False)]
    packets = [(frame * d, key) for frame, key in order]
    assert chunked.salvage_cut(packets, d) == 5  # frames 0-4; the part encoded next starts at frame 5
    # frame 3 missing before the keyframe: nothing whole to keep
    assert chunked.salvage_cut([p for p in packets if p[0] != 3 * d], d) == 0
    assert chunked.salvage_cut(packets[:5], d) == 0  # no second keyframe yet


def test_work_folder_is_the_same_for_the_same_job():
    plan = {
        "resume_root": "r", "ffmpeg": "missing-ffmpeg", "source": "in.mkv", "start": None, "end": None, "chunks": 4,
        "video_args": ["-map", "0:0"], "encode_args": ["-c:v", "libvvenc", "-qp", "28"], "threads": 6, "output": "a.mkv",
    }  # fmt: skip
    folder = chunked.work_folder(plan)
    assert chunked.work_folder({**plan, "threads": 4, "output": "b.mkv"}) == folder
    assert chunked.work_folder({**plan, "encode_args": ["-c:v", "libvvenc", "-qp", "30"]}) != folder
    assert chunked.work_folder({"work_dir": "old"}) == Path("old")  # queued before resume existed


def test_time_left_from_the_average_speed_and_open_edges():
    assert chunked.seconds_left(100, 400, elapsed=50.0) == approx(150.0)  # 2 frames/s, 300 left
    assert chunked.seconds_left(0, 400, elapsed=50.0) is None  # no frames yet: not judged
    assert chunked.part_seconds((0, Path("p.mkv"), None, 30.0), (10.0, 100.0)) == 20.0
    assert chunked.part_seconds((1, Path("q.mkv"), 30.0, None), (10.0, 100.0)) == 70.0


def test_a_chunk_cut_where_its_kept_frames_end_is_finished(tmp_path):
    part = chunked.part_path(tmp_path, 0, 0)
    part.write_bytes(b"x")
    chunked.write_json(
        chunked.marker_path(part), {"frames": 25, "complete": False, "trim_start": 10.0, "last_pts": 1.0}
    )
    with (
        mock.patch.object(chunked, "first_frame_at", return_value=10.0),
        mock.patch.object(chunked, "frame_times", return_value=[10.96, 11.0, 11.04, 11.08]),
    ):
        # the next part would start halfway between the last kept frame (11.0) and the next (11.04)
        plan = {"ffprobe": "ffprobe", "source": "in.mkv"}
        assert chunked.resume_chunk(plan, tmp_path, 0, 10.0, 11.02, 0.0, 0.04) == ([(part, 25)], None)
        parts, next_part = chunked.resume_chunk(plan, tmp_path, 0, 10.0, 20.0, 0.0, 0.04)
    assert next_part == (chunked.part_path(tmp_path, 0, 1), approx(11.02))


def test_an_earlier_runs_edges_are_read_as_ranges_and_joined_in_video_order(tmp_path):
    chunked.write_json(tmp_path / "state.json", {"edges": [None, 10.0, 20.0, None]})
    with mock.patch.object(chunked, "log"):
        state = chunked.chunk_state({}, tmp_path, (0.0, 30.0), 0.0)
    assert state == {"ranges": [[None, 10.0], [10.0, 20.0], [20.0, None]], "resplit": False}
    # chunk 1 was cut at 12.5 and its rest split into chunks 3 and 4
    assert chunked.join_order([[None, 10.0], [10.0, 12.5], [20.0, None], [12.5, 16.0], [16.0, 20.0]]) == [0, 1, 3, 4, 2]


class _FakeEncoder:
    """Exits after a set number of polls (None: runs until killed), with a set frame count."""

    script = []  # per encoder started: (polls until exit or None, frames)

    def __init__(self, command, error_file):
        self.left, frames = _FakeEncoder.script.pop(0)
        self.code = None
        self.number = {"frame": frames, "total_size": 0}.get
        self.reader = mock.Mock()
        self.process = self  # stands in for its own process too

    @property
    def returncode(self):
        return self.code

    def wait(self):
        pass

    def poll(self):
        if self.code is None and self.left is not None:
            self.left -= 1
            if self.left <= 0:
                self.code = 0
        return self.code

    def kill(self):
        self.code = -9


def _encode_tail(tmp_path, script, tail_range, side=None):
    _FakeEncoder.script = list(script)
    jobs = [(0, chunked.part_path(tmp_path, 0, 0), None, 50.0), (1, chunked.part_path(tmp_path, 1, 0), 50.0, None)]
    plan = {"ffmpeg": "ffmpeg", "source": "in.mkv", "video_args": [], "encode_args": [], "threads": 2, "chunks": 4}
    with (
        mock.patch.object(chunked, "ChunkEncoder", _FakeEncoder),
        mock.patch.object(chunked, "record_processes"),
        mock.patch.object(chunked, "log"),
        mock.patch.object(chunked.time, "sleep"),
        mock.patch.object(chunked.time, "monotonic", side_effect=[0.0] + [60.0] * 50),
        mock.patch.object(chunked, "part_shortfall", return_value=0.0),
    ):
        return chunked.encode_parts(plan, jobs, 2, 0.0, 0.04, 0, 0, tmp_path, (0.0, 100.0), tail_range, side)


def test_the_last_chunk_running_is_stopped_for_a_resplit_only_with_much_left(tmp_path):
    # chunk 0 finishes; chunk 1 (50 s = 1250 frames) did 100 frames in 60 s: about 11.5 min left at that pace
    with mock.patch.object(chunked, "RESPLIT_MIN_SECONDS", 600):
        assert _encode_tail(tmp_path, [(2, 1250), (None, 100)], (0.0, 100.0)) == ("resplit", 1)
    assert chunked.read_json(chunked.marker_path(chunked.part_path(tmp_path, 0, 0)))["complete"]  # chunk 0 kept
    with mock.patch.object(chunked, "RESPLIT_MIN_SECONDS", 900):  # under 15 min: it finishes on its own
        assert _encode_tail(tmp_path, [(2, 1250), (5, 100)], (0.0, 100.0)) == ("done", None)
    with mock.patch.object(chunked, "RESPLIT_MIN_SECONDS", 600):  # already split once in this job: never again
        assert _encode_tail(tmp_path, [(2, 1250), (5, 100)], None) == ("done", None)


def test_a_failed_side_file_stops_the_chunk_encoders(tmp_path):
    side = mock.Mock(running=lambda: [], estimate=lambda: (0, 100.0))
    side.poll.return_value = False  # its FFmpeg exited with an error
    assert _encode_tail(tmp_path, [(None, 10), (None, 10)], None, side) == ("failed", None)
    assert not list(tmp_path.glob("*.json"))  # no chunk marked as finished


def test_a_part_that_ended_early_gets_no_marker_and_is_continued(tmp_path):
    plan = {"ffprobe": "ffprobe"}
    job = (0, chunked.part_path(tmp_path, 0, 0), None, 50.0)  # 0 to 50 s of a range starting at 0
    encoder = mock.Mock(number={"frame": 250}.get)
    with mock.patch.object(chunked, "log"):
        with mock.patch.object(chunked, "packet_list", return_value=[(n * 0.04, n == 0) for n in range(250)]):
            assert not chunked.write_marker(plan, encoder, job, (0.0, 100.0), 0.04)  # 10 of 50 s: a read error
        with mock.patch.object(chunked, "packet_list", return_value=[(n * 0.04, n == 0) for n in range(1250)]):
            assert chunked.part_shortfall(plan, job, (0.0, 100.0), 0.04) == approx(0.0)
            # the last part runs to the source's end: not judged when that end could not be read
            assert chunked.part_shortfall(plan, (1, job[1], 50.0, None), (0.0, None), 0.04) == 0.0
    assert chunked.read_json(chunked.marker_path(job[1])) is None

    # the driver goes round again: resume salvages the early part and the next part encodes the rest
    first, second = chunked.part_path(tmp_path, 0, 0), chunked.part_path(tmp_path, 0, 1)
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    rounds = [([[]], [(0, first, None, 50.0)]), ([[(first, 200)]], [(0, second, 8.02, 50.0)])]

    def encode_parts(plan, jobs, *args):
        if jobs[0][1] == second:
            chunked.write_json(chunked.marker_path(second), {"frames": 1050, "complete": True})
        return "done", None

    state = {"ranges": [[None, 50.0]], "resplit": True}
    with (
        mock.patch.object(chunked, "resume_chunks", side_effect=rounds),
        mock.patch.object(chunked, "encode_parts", side_effect=encode_parts),
        mock.patch.object(chunked, "log"),
    ):
        done = chunked.encode_chunks({}, tmp_path, state, (0.0, 50.0), (0.0, 50.0), 0.0, 0.04, True, None)
    assert done == ([[(first, 200), (second, 1050)]], None)
    # a round that keeps no more frames than the one before: the source cannot be read on, the job stops
    rounds = [([[(first, 200)]], [(0, second, 8.02, 50.0)])] * 2
    chunked.marker_path(second).unlink()  # this time the next part ends early too
    with (
        mock.patch.object(chunked, "resume_chunks", side_effect=rounds),
        mock.patch.object(chunked, "encode_parts", return_value=("done", None)),
        mock.patch.object(chunked, "log"),
    ):
        assert chunked.encode_chunks({}, tmp_path, state, (0.0, 50.0), (0.0, 50.0), 0.0, 0.04, True, None) == (
            None,
            None,
        )


def test_side_file_whose_audio_ended_early_is_made_again(tmp_path):
    source = tmp_path / "in.mkv"
    source.write_bytes(b"a")
    plan = {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "final_inputs": ["-i", str(source)], "final_args": ["-map", "0:1", "out.mkv"]}  # fmt: skip
    ends = []  # out_time of each FFmpeg run, in seconds

    def start(command, error_file):
        Path(command[-1]).write_bytes(b"audio")
        encoder = mock.Mock(process=_FakeProcess(0))
        encoder.number = {"out_time_us": ends.pop(0) * 1e6}.get
        return encoder

    def run(audio, *out_times):
        ends[:] = out_times
        (tmp_path / chunked.SIDE_FILE_NAME).unlink(missing_ok=True)
        with (
            mock.patch.object(chunked, "ChunkEncoder", side_effect=start) as encoders,
            mock.patch.object(chunked, "record_processes"),
            mock.patch.object(chunked, "log"),
            mock.patch.object(chunked, "run_quiet", return_value=mock.Mock(stdout=audio)),
        ):
            side = chunked.SideFile(plan, tmp_path, 1000.0)
            while (finished := side.poll()) is None:
                pass
        return finished, encoders.call_count

    assert run("1\n", 300.0, 995.0) == (True, 2)  # cut short by a read error, made again
    assert run("1\n", 300.0, 300.0, 300.0) == (True, 3)  # really ends early: kept after 2 more tries
    assert run("", 300.0) == (True, 1)  # subtitles only: they end where they end


def test_join_check_finds_a_lost_and_a_doubled_frame():
    assert chunked.join_problems([0.0, 0.04, 0.08], 0.04) == []
    assert len(chunked.join_problems([0.0, 0.04, 0.12, 0.125], 0.04)) == 2

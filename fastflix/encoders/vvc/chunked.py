# -*- coding: utf-8 -*-
"""Chunked VVC encoding: the video is split at scene cuts into chunks that are encoded at the same time, each with a
few threads (VVenC scales poorly past a few threads), then joined and muxed with the source's audio and subtitles.

Run by FastFlix's queue worker as one command: ``python chunked.py <plan>`` (plan from command_builder, JSON in
base64, see encode_plan). Cancel and pause act on the whole process tree, so they reach every chunk encoder. Progress
is printed as FFmpeg-style "frame=" lines, which FastFlix's status panel reads.

Frame exactness: every chunk is cut in the filter graph by source PTS (trim, start inclusive, end exclusive) with the
input timestamps kept absolute (-copyts), at split times halfway between two frames, so each source frame goes to
exactly one chunk. Each chunk starts at PTS 0; mkvmerge appends the chunks (FFmpeg 9 loses VVC timestamps when it
stream-copies Matroska), and a check of the joined video's frame times catches any lost or doubled frame.
"""

import base64
import json
import re
import subprocess
import sys
import threading
import time
from bisect import bisect_left
from pathlib import Path

import psutil

SCENE_THRESHOLD = 10  # scdet's score (0-100) above which a frame starts a new scene; FFmpeg's default
MAX_WINDOW_SECONDS = 30  # how far from the even split point a scene cut may be taken
SEEK_MARGIN_SECONDS = 10  # the coarse input seek lands this much before the chunk; trim cuts exactly
PROGRESS_INTERVAL_SECONDS = 2
MKVMERGE_WARNING = 1  # mkvmerge's exit code when it finished with warnings
# FastFlix runs this driver with pythonw, which has no console: without this flag Windows opens a visible console
# window for every FFmpeg, ffprobe and mkvmerge it starts (and closing one kills that chunk)
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def log(message):
    print(message, file=sys.stderr, flush=True)


def run_quiet(command):
    return subprocess.run(
        command, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL, creationflags=NO_WINDOW
    )


# ---- Choosing the split points (pure, unit-tested) ----


def even_split_times(start, end, chunks):
    return [start + (end - start) * k / chunks for k in range(1, chunks)]


def nearest_cut(target, cuts, window):
    """The scene cut nearest the target within the window, or the target itself when there is none."""
    near = [cut for cut in cuts if abs(cut - target) <= window]
    return min(near, key=lambda cut: abs(cut - target)) if near else target


def snap_split(frame_times, target):
    """The split time halfway between the first frame at or after the target and the frame before it, so that no frame
    lies near the split and trim puts every frame on exactly one side. frame_times: sorted PTS of the window's frames.
    None when the window has no frame on both sides of the target."""
    index = bisect_left(frame_times, target)
    if index == 0 or index >= len(frame_times):
        return None
    return (frame_times[index - 1] + frame_times[index]) / 2


# ---- Reading the source ----


def stream_start_time(ffprobe, source):
    result = run_quiet(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=start_time", "-of", "csv=p=0", source]
    )  # fmt: skip
    try:
        return float(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0.0


def frame_times(ffprobe, source, start, length):
    """Sorted PTS (seconds) of the video frames from the absolute time start for length seconds, read from the
    packets (no decode). ffprobe starts at the keyframe before start, so the list can begin a little earlier."""
    result = run_quiet(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{max(start, 0):.6f}%+{length:.6f}",
            "-show_entries", "packet=pts_time", "-of", "csv=p=0", source,
        ]
    )  # fmt: skip
    times = []
    for line in result.stdout.splitlines():
        try:
            times.append(float(line.strip().rstrip(",")))
        except ValueError:
            pass
    return sorted(set(times))


def scene_cuts(ffmpeg, source, start, length):
    """Absolute times of the scene cuts FFmpeg's scdet finds from start for length seconds (decoded small)."""
    result = run_quiet(
        [
            ffmpeg, "-hide_banner", "-nostdin", "-copyts", "-ss", f"{max(start, 0):.6f}", "-t", f"{length:.6f}",
            "-i", source, "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", f"scale=480:-2,scdet=threshold={SCENE_THRESHOLD}", "-f", "null", "-",
        ]
    )  # fmt: skip
    cuts = []
    for line in result.stderr.splitlines():
        if "lavfi.scd.time:" in line:
            try:
                cuts.append(float(line.rsplit("lavfi.scd.time:", 1)[1].split()[0]))
            except (ValueError, IndexError):
                pass
    return cuts


def find_splits(plan, start, end, start_offset):
    """Split times (absolute PTS) at the scene cuts nearest the even split points; the windows are read at once."""
    chunks = plan["chunks"]
    window = min(MAX_WINDOW_SECONDS, (end - start) / (4 * chunks))
    targets = even_split_times(start, end, chunks)
    results = [None] * len(targets)

    def analyse(index, target):
        # FFmpeg's -ss counts from the file's start time; ffprobe's intervals are absolute like the targets
        cuts = scene_cuts(plan["ffmpeg"], plan["source"], target - window - start_offset, 2 * window)
        times = frame_times(plan["ffprobe"], plan["source"], target - window, 2 * window)
        cut = nearest_cut(target, cuts, window)
        results[index] = (snap_split(times, cut), cut != target)

    workers = [threading.Thread(target=analyse, args=(i, target)) for i, target in enumerate(targets)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    splits = []
    for target, (split, at_cut) in zip(targets, results):
        if split is None or (splits and split <= splits[-1]):
            log(f"Chunked encode: no usable split near {target:.3f} s, one chunk fewer")
            continue
        splits.append(split)
        log(f"Chunked encode: split at {split:.3f} s ({'scene cut' if at_cut else 'no scene cut near, even split'})")
    return splits


# ---- Encoding ----


def trimmed_video_args(plan, trim_start, trim_end):
    """The chunk's video arguments with trim + setpts as the first filters of the video chain."""
    parts = []
    if trim_start is not None:
        parts.append(f"start={trim_start:.6f}")
    if trim_end is not None:
        parts.append(f"end={trim_end:.6f}")
    trim = f"trim={':'.join(parts)},setpts=PTS-STARTPTS" if parts else "setpts=PTS-STARTPTS"
    args = list(plan["video_args"])
    if "-filter_complex" in args:
        index = args.index("-filter_complex") + 1
        label, rest = args[index].split("]", 1)
        args[index] = f"{label}]{trim},{rest}"
    else:
        args += ["-vf", trim]
    return args


def chunk_command(plan, trim_start, trim_end, start_offset, output):
    command = [plan["ffmpeg"], "-hide_banner", "-nostdin", "-y", "-copyts"]
    if trim_start is not None:
        command += ["-ss", f"{max(trim_start - start_offset - SEEK_MARGIN_SECONDS, 0):.6f}"]
    command += ["-i", plan["source"]]
    command += trimmed_video_args(plan, trim_start, trim_end)
    command += plan["encode_args"] + ["-threads", str(plan["threads"])]
    command += ["-an", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1"]
    command += ["-nostats", "-progress", "pipe:1", str(output)]
    return command


class ChunkEncoder:
    """One chunk's FFmpeg, its -progress values read on a thread, its stderr kept in a file for errors."""

    def __init__(self, command, error_file):
        self.error_file = error_file
        self.values = {}
        with open(error_file, "w") as errors:
            self.process = psutil.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=errors,
                stdin=subprocess.DEVNULL,
                text=True,
                errors="replace",
                creationflags=NO_WINDOW,
            )
        self.reader = threading.Thread(target=self.read_progress, daemon=True)
        self.reader.start()

    def read_progress(self):
        for line in self.process.stdout:
            key, _, value = line.strip().partition("=")
            self.values[key] = value

    def number(self, key):
        try:
            return float(self.values.get(key, 0) or 0)
        except ValueError:
            return 0.0

    def last_errors(self, lines=15):
        try:
            return Path(self.error_file).read_text(errors="replace").splitlines()[-lines:]
        except OSError:
            return []


def progress_line(encoders, started, frame_duration):
    """One FFmpeg-style line for all chunks: the frames and encoded time add up, so FastFlix's ETA and size work.
    The time is counted from the frames: each chunk's own out_time trails its frames by the encoder's reorder delay,
    which summed over the chunks left the bar short of the end."""
    frames = sum(e.number("frame") for e in encoders)
    seconds = frames * frame_duration
    size = sum(e.number("total_size") for e in encoders)
    elapsed = max(time.monotonic() - started, 1e-3)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    bitrate = f"{size * 8 / seconds / 1000:.1f}kbits/s" if seconds > 0 else "N/A"
    speed = f"{seconds / elapsed:.3g}x" if seconds > 0 else "N/A"
    return (
        f"frame={int(frames)} fps={frames / elapsed:.2f} q=-0.0 size={int(size / 1024)}KiB "
        f"time={int(hours):02d}:{int(minutes):02d}:{secs:05.2f} bitrate={bitrate} speed={speed}"
    )


def match_priority(encoders):
    """The chunk encoders follow the driver's priority, which FastFlix sets and may change while it runs."""
    try:
        own = psutil.Process().nice()
    except psutil.Error:
        return
    for encoder in encoders:
        try:
            if encoder.process.nice() != own:
                encoder.process.nice(own)
        except psutil.Error:
            pass


def encode_chunks(plan, ranges, start_offset, work_dir, frame_duration):
    encoders, outputs = [], []
    for index, (trim_start, trim_end) in enumerate(ranges):
        output = work_dir / f"chunk_{index:03d}.mkv"
        command = chunk_command(plan, trim_start, trim_end, start_offset, output)
        log(f"Chunk {index + 1}/{len(ranges)}: {subprocess.list2cmdline(command)}")
        encoders.append(ChunkEncoder(command, work_dir / f"chunk_{index:03d}.log"))
        outputs.append(output)

    started = time.monotonic()
    while any(e.process.poll() is None for e in encoders):
        failed = [e for e in encoders if e.process.poll() not in (None, 0)]
        if failed:
            for encoder in encoders:
                if encoder.process.poll() is None:
                    encoder.process.kill()
            log("Chunked encode: a chunk encoder failed:")
            for line in failed[0].last_errors():
                log(line)
            return None
        match_priority(encoders)
        log(progress_line(encoders, started, frame_duration))
        time.sleep(PROGRESS_INTERVAL_SECONDS)
    if any(e.process.returncode != 0 for e in encoders):
        bad = next(e for e in encoders if e.process.returncode != 0)
        log("Chunked encode: a chunk encoder failed:")
        for line in bad.last_errors():
            log(line)
        return None
    for encoder in encoders:
        encoder.reader.join(timeout=5)  # the last progress block holds the chunk's frame count
    log(progress_line(encoders, started, frame_duration))
    return outputs, int(sum(e.number("frame") for e in encoders))


# ---- Joining ----


def run_step(name, command, warning_exit_code=None):
    """Run one join step, logging its command and, on failure, its last error lines. mkvmerge exits with 1 for
    warnings only (warning_exit_code)."""
    log(f"Chunked encode: {name}: {subprocess.list2cmdline(command)}")
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW
    )
    if result.returncode in (0, warning_exit_code):
        return True
    for line in (result.stderr or result.stdout).splitlines()[-15:]:
        log(line)
    return False


STREAM_SPECIFIER = re.compile(r"^(-[A-Za-z_]+(?::s)?):(\d+)$")


def without_video_stream(args):
    """The single-encode output options with the output stream numbers moved down by one: the side file that carries
    the audio, subtitles and chapters has no video stream 0 (e.g. -c:1 -> -c:0, -metadata:s:2 -> -metadata:s:1)."""
    shifted = []
    for arg in args:
        match = STREAM_SPECIFIER.match(arg)
        shifted.append(f"{match.group(1)}:{int(match.group(2)) - 1}" if match else arg)
    return shifted


def join_and_mux(plan, outputs, work_dir):
    """FFmpeg cannot stream-copy VVC between Matroska files without losing timestamps (FFmpeg 9), so mkvmerge appends
    the chunks; FFmpeg writes the audio, subtitles and chapters (with the user's trim) into a side file, and mkvmerge
    puts video and side file together."""
    video = work_dir / "video.mkv"
    appended = [part for output in outputs for part in ("+", str(output))][1:]
    if not run_step("joining the chunks", [plan["mkvmerge"], "-q", "-o", str(video), *appended], MKVMERGE_WARNING):
        return False
    sources = [str(video)]
    final_args = plan["final_args"][:-1]  # without the output file
    if "-map" in final_args:
        side = work_dir / "audio_subtitles.mkv"
        command = [plan["ffmpeg"], "-hide_banner", "-nostdin", "-y", *plan["final_inputs"]]
        command += without_video_stream(final_args) + [str(side)]
        if not run_step("audio, subtitles and chapters", command):
            return False
        sources.append(str(side))
    return run_step("muxing", [plan["mkvmerge"], "-q", "-o", plan["output"], *sources], MKVMERGE_WARNING)


def join_problems(frame_times_out, frame_duration):
    """Gaps (a lost frame) or overlaps (a doubled frame) in the joined video's frame times."""
    problems = []
    for before, after in zip(frame_times_out, frame_times_out[1:]):
        step = after - before
        if step < frame_duration * 0.5 or step > frame_duration * 1.5:
            problems.append(f"{before:.3f} s -> {after:.3f} s")
    return problems


def check_output(plan, output, frame_duration, expected_frames):
    """Every frame once, at the source's pace: as many distinct frame times as frames, no gap or overlap."""
    times = frame_times(plan["ffprobe"], output, 0, 10**9)
    problems = join_problems(times, frame_duration)
    if len(times) != expected_frames:
        problems.insert(0, f"{len(times)} distinct frame times for {expected_frames} encoded frames")
    if problems:
        log(f"Chunked encode: the joined video has gaps or doubled frames: {', '.join(problems[:10])}")
        return False
    log(f"Chunked encode: joined video checked, {len(times)} frames, no gaps or doubled frames")
    return True


# ---- Driver ----


def run(plan):
    work_dir = Path(plan["work_dir"])
    work_dir.mkdir(parents=True, exist_ok=True)
    start_offset = stream_start_time(plan["ffprobe"], plan["source"])
    start = plan["start"] or 0.0
    end = plan["end"] or plan["duration"]

    # Absolute source PTS of the chunk edges; None = the file's own first or last frame
    first_trim = start + start_offset if plan["start"] else None
    last_trim = end + start_offset if plan["end"] else None
    head = frame_times(plan["ffprobe"], plan["source"], start + start_offset, 5)
    if len(head) < 2:
        log("Chunked encode: could not read the source's frame times")
        return 1
    # The source's pace, for the progress time and the join check; averaged, as Matroska stores times in whole ms
    frame_duration = (head[-1] - head[0]) / (len(head) - 1)

    edges = [first_trim] + find_splits(plan, start + start_offset, end + start_offset, start_offset) + [last_trim]
    encoded = encode_chunks(plan, list(zip(edges, edges[1:])), start_offset, work_dir, frame_duration)
    if encoded is None:
        return 1
    outputs, frames = encoded
    if not join_and_mux(plan, outputs, work_dir):
        return 1
    if not check_output(plan, plan["output"], frame_duration, frames):
        return 1
    for temporary in [*outputs, work_dir / "video.mkv", work_dir / "audio_subtitles.mkv"]:
        temporary.unlink(missing_ok=True)
    return 0


def encode_plan(plan) -> str:
    """The plan as one command-line argument without quotes or backslashes, which shells and argument parsers keep."""
    return base64.urlsafe_b64encode(json.dumps(plan).encode("utf-8")).decode("ascii")


def decode_plan(argument: str):
    return json.loads(base64.urlsafe_b64decode(argument.encode("ascii")).decode("utf-8"))


if __name__ == "__main__":
    sys.exit(run(decode_plan(sys.argv[1])))

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

Resume: the work folder is named after the source, range, encoder options and FFmpeg build, so a job run again after
a crash or cancel finds it. Each chunk is encoded in parts that are never overwritten; a finished part gets a marker.
A part without one is what a killed encoder left: the frames before its last keyframe are kept (cut by mkvmerge,
then checked), and the next part starts at the source frame after them, so a crash loses at most about one intra
period per chunk.
"""

import base64
import hashlib
import json
import os
import re
import shutil
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
# The last chunk still encoding is stopped and its rest split again when it has more than this left (once per job)
RESPLIT_MIN_SECONDS = 15 * 60
MKVMERGE_WARNING = 1  # mkvmerge's exit code when it finished with warnings
CLUSTER_TIME_LIMIT_MS = 1000  # small Matroska clusters: a killed encoder's file loses at most the last second written
TIME_TOLERANCE = 0.002  # Matroska stores times in whole milliseconds
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
    packets (no decode). ffprobe starts at the keyframe before start, so the list can begin a little earlier. It also
    counts the length from that keyframe, so a short interval could end before start + length: it reads
    SEEK_MARGIN_SECONDS more and drops what lies past the end."""
    read = f"{max(start, 0):.6f}%+{length + SEEK_MARGIN_SECONDS:.6f}"
    result = run_quiet(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", read,
            "-show_entries", "packet=pts_time", "-of", "csv=p=0", source,
        ]
    )  # fmt: skip
    times = []
    for line in result.stdout.splitlines():
        try:
            time_s = float(line.strip().rstrip(","))
        except ValueError:
            continue
        if time_s <= start + length:
            times.append(time_s)
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
    command += ["-cluster_time_limit", str(CLUSTER_TIME_LIMIT_MS)]
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


def progress_line(encoders, started, frame_duration, done_frames=0, done_size=0, side=(0, 0)):
    """One FFmpeg-style line for all chunks: the frames and encoded time add up, so FastFlix's ETA and size work.
    The time is counted from the frames: each chunk's own out_time trails its frames by the encoder's reorder delay,
    which summed over the chunks left the bar short of the end. Frames kept from an earlier run count towards the
    time but not the speed, which is this run's, so the time left stays right on a resume.

    side = (size of the audio/subtitles file, length of the whole range): that file is already complete, so its rate
    is spread over the whole range, and FastFlix's estimate (length x bitrate) is the video so far projected to the
    end plus the whole side file."""
    new_frames = sum(e.number("frame") for e in encoders)
    frames = done_frames + new_frames
    seconds = frames * frame_duration
    video_size = done_size + sum(e.number("total_size") for e in encoders)
    side_size, total_seconds = side
    size = video_size + side_size
    side_rate = side_size * 8 / total_seconds if total_seconds > 0 else 0
    elapsed = max(time.monotonic() - started, 1e-3)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    bitrate = f"{(video_size * 8 / seconds + side_rate) / 1000:.1f}kbits/s" if seconds > 0 else "N/A"
    speed = f"{new_frames * frame_duration / elapsed:.3g}x" if new_frames > 0 else "N/A"
    return (
        f"frame={int(frames)} fps={new_frames / elapsed:.2f} q=-0.0 size={int(size / 1024)}KiB "
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


def part_seconds(job, range_times):
    """Length of a job's part (chunk, part path, trim start, trim end); a None trim is the range's own first or last
    time, range_times = (first, last)."""
    _, _, trim_start, trim_end = job
    first, last = range_times
    return (last if trim_end is None else trim_end) - (first if trim_start is None else trim_start)


def seconds_left(frames, expected_frames, elapsed):
    """The encoder's time left at its average speed so far; None while it has no frames yet (look-ahead filling)."""
    if frames <= 0 or elapsed <= 0:
        return None
    return max(expected_frames - frames, 0) / (frames / elapsed)


def write_marker(encoder, job):
    """The finished part's marker; its frame count is the encoder's last progress block."""
    _, output, trim_start, _ = job
    encoder.reader.join(timeout=5)
    frames = int(encoder.number("frame"))
    write_json(marker_path(output), {"frames": frames, "complete": True, "trim_start": trim_start})


def encode_parts(
    plan, jobs, chunk_count, start_offset, frame_duration, done_frames, done_size, work_dir, tail_range, side=(0, 0)
):
    """Encodes the next part of each given chunk at once; jobs: (chunk, part path, trim start, trim end). Each
    finished part gets its marker. Returns ("done", None), ("failed", None) or ("resplit", chunk). side: see
    progress_line.

    tail_range (the range's first and last time, None: no re-split) lets the last encoder still running be judged
    once, when another of these encoders has finished: with more than RESPLIT_MIN_SECONDS left at its average speed,
    it is stopped and its rest is to be split over plan["chunks"] chunks, so the end of the job does not wait for
    one encoder on a few threads while the rest of the CPU idles."""
    encoders = []
    for chunk, output, trim_start, trim_end in jobs:
        command = chunk_command(plan, trim_start, trim_end, start_offset, output)
        log(f"Chunk {chunk + 1}/{chunk_count}: {subprocess.list2cmdline(command)}")
        encoders.append(ChunkEncoder(command, output.with_suffix(".log")))
    record_encoders(work_dir, encoders)

    started = time.monotonic()
    tail_judged = tail_range is None or len(encoders) < 2
    while any(e.process.poll() is None for e in encoders):
        failed = [e for e in encoders if e.process.poll() not in (None, 0)]
        if failed:
            for encoder in encoders:
                if encoder.process.poll() is None:
                    encoder.process.kill()
            log("Chunked encode: a chunk encoder failed:")
            for line in failed[0].last_errors():
                log(line)
            return "failed", None
        running = [i for i, e in enumerate(encoders) if e.process.poll() is None]
        if not tail_judged and len(running) == 1:
            last, job = encoders[running[0]], jobs[running[0]]
            expected = part_seconds(job, tail_range) / frame_duration
            left = seconds_left(last.number("frame"), expected, time.monotonic() - started)
            if left is not None:
                tail_judged = True
                log(f"Chunked encode: chunk {job[0] + 1} is the last one running, about {left / 60:.0f} min left")
                if left > RESPLIT_MIN_SECONDS:
                    log(
                        f"Chunked encode: chunk {job[0] + 1} is stopped, its rest is split over {plan['chunks']} chunks"
                    )
                    last.process.kill()
                    last.process.wait()
                    for encoder, finished in zip(encoders, jobs):
                        if encoder is not last:
                            write_marker(encoder, finished)
                    return "resplit", job[0]
        match_priority(encoders)
        log(progress_line(encoders, started, frame_duration, done_frames, done_size, side))
        time.sleep(PROGRESS_INTERVAL_SECONDS)
    if any(e.process.returncode != 0 for e in encoders):
        bad = next(e for e in encoders if e.process.returncode != 0)
        log("Chunked encode: a chunk encoder failed:")
        for line in bad.last_errors():
            log(line)
        return "failed", None
    for encoder, job in zip(encoders, jobs):
        write_marker(encoder, job)
    log(progress_line(encoders, started, frame_duration, done_frames, done_size, side))
    return "done", None


# ---- Resuming ----


def write_json(path, data):
    """Written under a temporary name and renamed, so a kill never leaves half a file."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data))
    os.replace(temporary, path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def work_folder(plan):
    """The same folder for the same source, range, chunk count, video and encoder options and FFmpeg build, so a job
    run again finds what an earlier run left. Plans queued before resume existed name their folder themselves."""
    if "resume_root" not in plan:
        return Path(plan["work_dir"])
    try:
        stat = Path(plan["ffmpeg"]).stat()
        build = [stat.st_size, int(stat.st_mtime)]
    except OSError:
        build = None
    key = [plan["source"], plan["start"], plan["end"], plan["chunks"], plan["video_args"], plan["encode_args"]]
    key += [plan["ffmpeg"], build]
    return Path(plan["resume_root"]) / hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest()[:16]


def live_process(entry):
    """The process a lock entry names, if it still runs: the same PID and start time, so a reused PID is not it."""
    try:
        process = psutil.Process(entry["pid"])
        return process if process.create_time() == entry["created"] else None
    except (psutil.Error, KeyError, TypeError):
        return None


def process_entry(process):
    return {"pid": process.pid, "created": process.create_time()}


def take_lock(work_dir):
    """False while another live run owns the folder. Encoders an earlier run left running (when only its driver was
    killed) are stopped first: they would still be writing the parts this run salvages."""
    lock = work_dir / "lock.json"
    owner = read_json(lock) or {}
    if owner and live_process(owner):
        return False
    for entry in owner.get("encoders", []):
        if leftover := live_process(entry):
            log(f"Chunked encode: stopping encoder {leftover.pid} left running by the earlier run")
            try:
                leftover.kill()
                leftover.wait(timeout=30)
            except psutil.Error:
                pass
    write_json(lock, process_entry(psutil.Process()))
    return True


def record_encoders(work_dir, encoders):
    """The lock names this run's encoders, for take_lock of a later run."""
    record_processes(work_dir, [e.process for e in encoders])


def record_processes(work_dir, processes):
    """The lock names this run's child processes (psutil.Popen); take_lock of a later run stops those still running."""
    lock = work_dir / "lock.json"
    write_json(lock, {**process_entry(psutil.Process()), "encoders": [process_entry(p) for p in processes]})


def part_path(work_dir, chunk, part):
    return work_dir / f"chunk_{chunk:03d}_part_{part:02d}.mkv"


def marker_path(part):
    return part.with_suffix(".json")


def packet_list(ffprobe, path):
    """(PTS seconds, keyframe) of a file's video packets in file (decode) order; reads a killed encoder's file up to
    its last complete cluster."""
    result = run_quiet(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path)]
    )  # fmt: skip
    packets = []
    for line in result.stdout.splitlines():
        pts, _, flags = line.strip().partition(",")
        try:
            packets.append((float(pts), "K" in flags))
        except ValueError:
            pass
    return packets


def salvage_cut(packets, frame_duration):
    """How many packets, in decode order, to keep of a part a killed encoder left: all before the last keyframe whose
    earlier packets are a whole run of frames from the first, without a gap, and shown before every later packet.
    That keyframe's leading pictures (shown before it, coded after it) then go to the part encoded next. 0: none."""
    if not packets:
        return 0
    first = min(pts for pts, _ in packets)
    for index in range(len(packets) - 1, 0, -1):
        if not packets[index][1]:
            continue
        kept = sorted(pts for pts, _ in packets[:index])
        later = min(pts for pts, _ in packets[index:])
        if kept[0] == first and later > kept[-1] and not join_problems(kept, frame_duration):
            return index
    return 0


def salvage(plan, part, frame_duration):
    """Keeps the frames before the last usable keyframe of a killed encoder's part, checked; returns (frames, PTS of
    the last kept frame) or None, and then the part is deleted."""
    packets = packet_list(plan["ffprobe"], part)
    count = salvage_cut(packets, frame_duration)
    kept = part.with_name(part.stem + "_kept.mkv")
    if count:
        expected = sorted(pts for pts, _ in packets[:count])
        cut = [plan["mkvmerge"], "-q", "-o", str(kept), "--split", f"parts-frames:-{count}", str(part)]
        if run_step(f"keeping the first {count} frames of {part.name}", cut, MKVMERGE_WARNING):
            result = sorted(pts for pts, _ in packet_list(plan["ffprobe"], kept))
            if (
                len(result) == count
                and abs(result[0] - expected[0]) < TIME_TOLERANCE
                and abs(result[-1] - expected[-1]) < TIME_TOLERANCE
                and not join_problems(result, frame_duration)
            ):
                os.replace(kept, part)
                return count, result[-1] - result[0]
            log(f"Chunked encode: the kept frames of {part.name} did not check out, the part is encoded again")
    kept.unlink(missing_ok=True)
    part.unlink(missing_ok=True)
    return None


def first_frame_at(plan, time_from, start_offset):
    """PTS of the source's first frame at or after time_from (None: the file's first frame)."""
    if time_from is None:
        times = frame_times(plan["ffprobe"], plan["source"], start_offset, 2)
        return times[0] if times else start_offset
    times = frame_times(plan["ffprobe"], plan["source"], time_from - 2, 4)
    return next((t for t in times if t >= time_from), time_from)


def resume_chunk(plan, work_dir, chunk, chunk_start, chunk_end, start_offset, frame_duration):
    """The chunk's parts so far, each (path, frames), salvaging one a killed encoder left, and where its next part
    starts: None when the chunk is finished, else (part path, trim start). A chunk whose rest was split off ends
    where its kept frames end, so its next part would start at its end: it is finished."""
    parts = []
    number = 0
    trim_start = chunk_start
    while (part := part_path(work_dir, chunk, number)).exists():
        marker = read_json(marker_path(part))
        if marker is None:
            kept = salvage(plan, part, frame_duration)
            if kept is None:
                break
            marker = {"frames": kept[0], "complete": False, "trim_start": trim_start, "last_pts": kept[1]}
            write_json(marker_path(part), marker)
        parts.append((part, marker["frames"]))
        number += 1
        if marker["complete"]:
            return parts, None
        # The next part starts halfway between the last kept frame and the source frame after it
        last_kept = first_frame_at(plan, marker["trim_start"], start_offset) + marker["last_pts"]
        target = last_kept + frame_duration / 2
        trim_start = snap_split(frame_times(plan["ffprobe"], plan["source"], target - 2, 4), target)
        if trim_start is None:
            raise ResumeError(f"no source frames found after {last_kept:.3f} s to continue chunk {chunk + 1}")
    if None not in (trim_start, chunk_end) and trim_start >= chunk_end - frame_duration / 2:
        return parts, None
    return parts, (part_path(work_dir, chunk, number), trim_start)


class ResumeError(Exception):
    pass


# ---- Joining ----


def run_step(name, command, warning_exit_code=None, work_dir=None):
    """Run one step, logging its command and, on failure, its last error lines. mkvmerge exits with 1 for
    warnings only (warning_exit_code). With work_dir, the process is named in the lock, so a later run stops it if
    this driver is killed while it runs."""
    log(f"Chunked encode: {name}: {subprocess.list2cmdline(command)}")
    process = psutil.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        creationflags=NO_WINDOW,
    )
    if work_dir is not None:
        try:
            record_processes(work_dir, [process])
        except psutil.Error:  # already exited
            pass
    stdout, stderr = process.communicate()
    if process.returncode in (0, warning_exit_code):
        return True
    for line in (stderr or stdout).splitlines()[-15:]:
        log(line)
    return False


def file_stamp(path):
    """A file's path, size and modification time, to tell whether it changed since an earlier run."""
    try:
        stat = Path(path).stat()
        return [str(path), stat.st_size, int(stat.st_mtime)]
    except OSError:
        return [str(path), None, None]


def make_side_file(plan, work_dir):
    """FFmpeg writes the audio, subtitles and chapters (with the user's trim) into a side file, at the job's start so
    the progress line can count its size (see progress_line). Kept for a resume while its command, inputs and FFmpeg
    build are unchanged: the work folder's name does not cover them. Returns its path, None when the output has no
    streams but the video, False on a failure."""
    final_args = plan["final_args"][:-1]  # without the output file
    if "-map" not in final_args:
        return None
    side = work_dir / "audio_subtitles.mkv"
    marker = side.with_suffix(".json")
    command = [plan["ffmpeg"], "-hide_banner", "-nostdin", "-y", *plan["final_inputs"]]
    command += without_video_stream(final_args) + [str(side)]
    inputs = [arg for flag, arg in zip(command, command[1:]) if flag == "-i"]
    record = {"command": command, "files": [file_stamp(path) for path in [plan["ffmpeg"], *inputs]]}
    if side.exists() and read_json(marker) == record:
        log("Chunked encode: the audio, subtitles and chapters file of the earlier run is kept")
        return side
    marker.unlink(missing_ok=True)
    if not run_step("audio, subtitles and chapters", command, work_dir=work_dir):
        return False
    write_json(marker, record)
    return side


STREAM_SPECIFIER = re.compile(r"^(-[A-Za-z_]+(?::s)?):(\d+)$")


def without_video_stream(args):
    """The single-encode output options with the output stream numbers moved down by one: the side file that carries
    the audio, subtitles and chapters has no video stream 0 (e.g. -c:1 -> -c:0, -metadata:s:2 -> -metadata:s:1)."""
    shifted = []
    for arg in args:
        match = STREAM_SPECIFIER.match(arg)
        shifted.append(f"{match.group(1)}:{int(match.group(2)) - 1}" if match else arg)
    return shifted


def join_and_mux(plan, outputs, work_dir, side):
    """FFmpeg cannot stream-copy VVC between Matroska files without losing timestamps (FFmpeg 9), so mkvmerge appends
    the chunks and puts the video and the side file (make_side_file; None: none) together."""
    video = work_dir / "video.mkv"
    appended = [part for output in outputs for part in ("+", str(output))][1:]
    if not run_step("joining the chunks", [plan["mkvmerge"], "-q", "-o", str(video), *appended], MKVMERGE_WARNING):
        return False
    sources = [str(video)] if side is None else [str(video), str(side)]
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
    work_dir = work_folder(plan)
    work_dir.mkdir(parents=True, exist_ok=True)
    if not take_lock(work_dir):
        log(f"Chunked encode: another run is encoding this job in {work_dir}")
        return 1
    log(f"Chunked encode: work folder {work_dir} (kept after a cancel or crash: running the job again resumes it)")
    try:
        return run_locked(plan, work_dir)
    except ResumeError as error:
        log(f"Chunked encode: cannot resume: {error}; delete {work_dir} to start over")
        return 1
    finally:
        (work_dir / "lock.json").unlink(missing_ok=True)


def chunk_state(plan, work_dir, range_times, start_offset):
    """The chunks' ranges, [start, end] in absolute PTS (None = the file's own first or last frame), and whether the
    last chunk's rest was split again: found once and kept in state.json for resuming. A state from before the
    re-split holds the edges only."""
    state_file = work_dir / "state.json"
    state = read_json(state_file) or {}
    if state.get("ranges"):
        log("Chunked encode: resuming, the chunks' split points are those of the earlier run")
        return state
    if state.get("edges"):
        log("Chunked encode: resuming, the chunks' split points are those of the earlier run")
        edges = state["edges"]
    else:
        first, last = range_times
        splits = find_splits(plan, first, last, start_offset)
        edges = [first if plan["start"] else None] + splits + [last if plan["end"] else None]
    state = {"ranges": [list(pair) for pair in zip(edges, edges[1:])], "resplit": False}
    write_json(state_file, state)
    return state


def join_order(ranges):
    """The chunks in the order of the video: a re-split chunk's new pieces come after the others in the list."""
    return sorted(range(len(ranges)), key=lambda chunk: float("-inf") if ranges[chunk][0] is None else ranges[chunk][0])


def resplit(plan, work_dir, state, chunk, range_times, start_offset, frame_duration):
    """Splits the rest of a stopped chunk over plan["chunks"] new chunks at the scene cuts, as at the start: the
    chunk keeps the frames salvaged before its encoder's last keyframe and ends there. Written to state.json at once,
    with "resplit" set, so a resume runs the new chunks and never splits again."""
    ranges = state["ranges"]
    chunk_start, chunk_end = ranges[chunk]
    parts, next_part = resume_chunk(plan, work_dir, chunk, chunk_start, chunk_end, start_offset, frame_duration)
    state = {"ranges": [list(pair) for pair in ranges], "resplit": True}
    if next_part:
        rest_start = next_part[1]
        first = range_times[0] if rest_start is None else rest_start
        last = range_times[1] if chunk_end is None else chunk_end
        edges = [rest_start] + find_splits(plan, first, last, start_offset) + [chunk_end]
        pieces = [list(pair) for pair in zip(edges, edges[1:])]
        log(f"Chunked encode: the rest of chunk {chunk + 1} is split into {len(pieces)} chunks")
        if parts:
            state["ranges"][chunk][1] = rest_start
        else:  # nothing was kept: the chunk itself becomes the first piece
            state["ranges"][chunk] = pieces.pop(0)
        state["ranges"] += pieces
    write_json(work_dir / "state.json", state)
    return state


def encode_chunks(plan, work_dir, state, range_times, start_offset, frame_duration, first_pass, side):
    """Resumes every chunk and encodes what is left. Returns (each chunk's parts, None) when all are encoded,
    (parts, chunk) when the last chunk still running was stopped for a re-split, (None, None) on a failure.
    side: see progress_line."""
    ranges = state["ranges"]
    done, jobs = [], []
    for chunk, (chunk_start, chunk_end) in enumerate(ranges):
        parts, next_part = resume_chunk(plan, work_dir, chunk, chunk_start, chunk_end, start_offset, frame_duration)
        done.append(parts)
        if next_part:
            output, trim_start = next_part
            marker_path(output).unlink(missing_ok=True)  # a stale marker must not mark the new part finished
            jobs.append((chunk, output, trim_start, chunk_end))
    done_frames = sum(frames for parts in done for _, frames in parts)
    if done_frames and first_pass:
        log(f"Chunked encode: resuming with {done_frames} frames kept from the earlier run")
    # A PC without the RAM for every chunk's encoder at once runs them in batches (each 4K encoder holds about 10 GB).
    # The chunks' commands stay the same, so the output does too.
    batch_size = plan.get("max_parallel") or max(len(jobs), 1)
    for first in range(0, len(jobs), batch_size):
        batch = jobs[first : first + batch_size]
        last_batch = first + batch_size >= len(jobs)
        tail_range = range_times if last_batch and not state.get("resplit") else None
        done_frames = sum(frames for parts in done for _, frames in parts)
        done_size = sum(part.stat().st_size for parts in done for part, _ in parts)
        status, stopped = encode_parts(
            plan, batch, len(ranges), start_offset, frame_duration, done_frames, done_size, work_dir, tail_range, side
        )
        if status == "failed":
            return None, None
        for chunk, output, _, _ in batch:
            if chunk != stopped:
                done[chunk].append((output, read_json(marker_path(output))["frames"]))
        if status == "resplit":
            return done, stopped
    return done, None


def run_locked(plan, work_dir):
    start_offset = stream_start_time(plan["ffprobe"], plan["source"])
    start = plan["start"] or 0.0
    end = plan["end"] or plan["duration"]

    head = frame_times(plan["ffprobe"], plan["source"], start + start_offset, 5)
    if len(head) < 2:
        log("Chunked encode: could not read the source's frame times")
        return 1
    # The source's pace, for the progress time and the join check; averaged, as Matroska stores times in whole ms
    frame_duration = (head[-1] - head[0]) / (len(head) - 1)

    range_times = (start + start_offset, end + start_offset)
    state = chunk_state(plan, work_dir, range_times, start_offset)
    side_file = make_side_file(plan, work_dir)
    if side_file is False:
        return 1
    side = (side_file.stat().st_size if side_file else 0, end - start)
    first_pass = True
    while True:
        done, stopped = encode_chunks(
            plan, work_dir, state, range_times, start_offset, frame_duration, first_pass, side
        )
        first_pass = False
        if done is None:
            return 1
        if stopped is None:
            break
        state = resplit(plan, work_dir, state, stopped, range_times, start_offset, frame_duration)

    outputs = [part for chunk in join_order(state["ranges"]) for part, _ in done[chunk]]
    if not join_and_mux(plan, outputs, work_dir, side_file):
        return 1
    if not check_output(plan, plan["output"], frame_duration, sum(frames for parts in done for _, frames in parts)):
        return 1
    shutil.rmtree(work_dir, ignore_errors=True)
    return 0


def encode_plan(plan) -> str:
    """The plan as one command-line argument without quotes or backslashes, which shells and argument parsers keep."""
    return base64.urlsafe_b64encode(json.dumps(plan).encode("utf-8")).decode("ascii")


def decode_plan(argument: str):
    return json.loads(base64.urlsafe_b64decode(argument.encode("ascii")).decode("utf-8"))


if __name__ == "__main__":
    sys.exit(run(decode_plan(sys.argv[1])))

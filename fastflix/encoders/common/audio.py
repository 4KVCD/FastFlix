#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
from functools import lru_cache
from typing import List

logger = logging.getLogger("fastflix")

channel_list = {
    "mono": 1,
    "stereo": 2,
    "2.1": 3,
    "3.0": 3,
    "3.0(back)": 3,
    "3.1": 4,
    "4.0": 4,
    "quad": 4,
    "quad(side)": 4,
    "5.0": 5,
    "5.1": 6,
    "5.1(side)": 6,
    "6.0": 6,
    "6.0(front)": 6,
    "hexagonal": 6,
    "6.1": 7,
    "6.1(front)": 7,
    "7.0": 7,
    "7.0(front)": 7,
    "7.1": 8,
    "7.1(wide)": 8,
    "2.2": 4,
    "3.1.2": 6,
    "4.1": 5,
    "5.0(side)": 5,
    "5.1.2": 8,
    "5.1.4": 10,
    "6.1(back)": 7,
    "7.1(wide-side)": 8,
    "7.1.2": 10,
    "7.1.4": 12,
    "7.2.3": 12,
    "9.1.4": 14,
    "9.1.6": 16,
    "octagonal": 8,
    "cube": 8,
    "hexadecagonal": 16,
    "downmix": 2,
    "22.2": 24,
}


# Layout names in channel_list that FFmpeg does not know, and FFmpeg's name for them
ffmpeg_layout_names = {"2.2": "quad(side)"}


def channels_for_layout(layout, channels=None):
    """Channel count for an ffprobe layout name, falling back to the stream's ``channels`` field."""
    if layout in channel_list:
        return channel_list[layout]
    if isinstance(channels, int) and channels > 0:
        return channels
    return None


# libopus' default channel mapping takes only FFmpeg's Vorbis layouts (mono, stereo, 3.0, quad, 5.0, 5.1, 6.1, 7.1;
# at most 8 channels) and rejects any other, e.g. "Invalid channel layout 5.1(side)", the layout of most AC3 and DTS
# 5.1 tracks. Of those, 5.0 and 6.1 are left out here: in FFmpeg 9.0.2 their channels come back in other positions
# after a libopus round trip (5.0's back left plays as centre), while the layouts below stay in place.
opus_layouts = ("mono", "stereo", "3.0", "quad", "5.1", "7.1")

# The smallest of those layouts that holds each other FFmpeg layout's channels. FFmpeg's aformat converts into it:
# side and back channels stand in for each other, a back centre is split over the two back channels, the
# front-of-centre and top-front pairs are mixed into the front, and an LFE needs a layout with LFE (5.1 or 7.1).
layout_for_opus = {
    "downmix": "stereo",
    "binaural": "stereo",
    "2.1": "5.1",
    "3.0(back)": "quad",
    "4.0": "5.1",
    "quad(side)": "quad",
    "3.1": "5.1",
    "5.0": "5.1",
    "5.0(side)": "5.1",
    "4.1": "5.1",
    "5.1(side)": "5.1",
    "6.0": "7.1",
    "6.0(front)": "quad",
    "3.1.2": "5.1",
    "hexagonal": "5.1",
    "6.1": "7.1",
    "6.1(back)": "5.1",
    "6.1(front)": "5.1",
    "7.0": "7.1",
    "7.0(front)": "5.1",
    "7.1(wide)": "5.1",
    "7.1(wide-side)": "5.1",
    "5.1.2": "5.1",
    "5.1.2(back)": "5.1",
    "octagonal": "7.1",
    "7.1.2": "7.1",
}

# Layouts with top-back or other channels that FFmpeg's conversion has no place for: their sound is dropped
# (checked per channel with a tone in one channel at a time: every other layout keeps all of its channels)
layout_for_opus_losing_channels = {
    "cube": "quad",
    "5.1.4": "5.1",
    "7.1.4": "7.1",
    "7.2.3": "7.1",
    "9.1.4": "7.1",
    "9.1.6": "7.1",
    "hexadecagonal": "7.1",
    "22.2": "7.1",
}

# Outputs FFmpeg cannot put Opus into (mov and m4v muxers refuse it, avi has no tag; m2ts stores it as private data)
no_opus_extensions = (".mov", ".m4v", ".avi", ".m2ts")

# Audio whose layout has no known name: FFmpeg assumes its default layout for the channel count (3: 2.1, 4: 4.0,
# 5: 5.0, 6: 5.1, 7: 6.1, 8: 7.1), so the target is the one for that default layout
opus_layout_for_channel_count = {1: "mono", 2: "stereo", 3: "5.1", 4: "5.1", 5: "5.1", 6: "5.1", 7: "7.1"}


@lru_cache
def warn_once(message: str):
    """Log a warning once: command lines are rebuilt on every settings change."""
    logger.warning(message)


def opus_channel_layout(layout, channels=None) -> str:
    """A channel layout libopus accepts for audio in ``layout`` (an ffprobe layout name) with ``channels`` channels."""
    if layout in opus_layouts:
        return layout
    if layout in layout_for_opus:
        return layout_for_opus[layout]
    if layout in layout_for_opus_losing_channels:
        target = layout_for_opus_losing_channels[layout]
        warn_once(
            f"Opus holds at most 7.1: converting {layout} audio to {target} drops its top-back channels"
            " (and in larger layouts the other channels that do not fit)"
        )
        return target
    count = channels_for_layout(layout, channels) or 2
    return opus_layout_for_channel_count.get(count, "7.1")


# Encoder bitrate ceilings in kbps (ac3enc.c: AC-3 tops out at 640k, E-AC-3 at 128 * sample_rate bits/s = 6144k @48kHz)
max_bitrate_kbps = {"ac3": 640, "eac3": 6144}

lossless = ["flac", "truehd", "alac", "tta", "wavpack", "mlp"]


def audio_quality_converter(quality, codec, channels=2, track_number=1):
    base = [120, 96, 72, 48, 24, 24, 16, 8, 8, 8][quality]
    channels = channels or 2

    match codec:
        case "libopus":
            return f"-vbr:{track_number} on -b:{track_number} {base * channels}k"
        case "aac":
            return f"-q:{track_number} {[2, 1.8, 1.6, 1.4, 1.2, 1, 0.8, 0.6, 0.4, 0.2][quality]}"
        case "libfdk_aac":
            return f"-q:{track_number} {[1, 1, 2, 2, 3, 3, 4, 4, 5, 5][quality]}"
        case "libvorbis" | "vorbis":
            return f"-q:{track_number} {[10, 9, 8, 7, 6, 5, 4, 3, 2, 1][quality]}"
        case "libmp3lame" | "mp3":
            return f"-q:{track_number} {quality}"
        case "ac3" | "eac3" | "truehd":
            bitrate = base * channels * 4
            if codec in max_bitrate_kbps:
                bitrate = min(bitrate, max_bitrate_kbps[codec])
            return f"-b:{track_number} {bitrate}k"
        case _:
            return f"-b:{track_number} {base * channels}k"


AAC_HE_PROFILES = ("aac_he", "aac_he_v2")


def resolve_aac_profile(codec: str, profile, channels=None):
    """Return the AAC profile that is safe to request, or None for the encoder default.

    - HE-AAC / HE-AACv2 are only supported by libfdk_aac. FFmpeg's native ``aac`` encoder fails with
      "Invalid argument" (-22) when asked for them, so the profile is dropped for every other codec.
    - HE-AACv2 (Parametric Stereo) requires exactly 2 channels; for any other channel count it falls back
      to HE-AAC (v1), which supports mono and multichannel.
    """
    if not profile:
        return None
    if codec != "libfdk_aac":
        if profile in AAC_HE_PROFILES:
            logger.warning(f"AAC profile {profile} is only supported by libfdk_aac, ignoring for codec {codec}")
            return None
        return profile
    if profile == "aac_he_v2" and channels is not None and channels != 2:
        logger.warning(f"HE-AACv2 requires stereo output (got {channels} channels), falling back to HE-AAC")
        return "aac_he"
    return profile


def _split_quality(quality_str: str) -> List[str]:
    """Split a quality string like '-vbr:1 on -b:1 120k' into list items."""
    return quality_str.split()


def build_audio(audio_tracks, audio_file_index=0, reverse_video: bool = False, output_suffix: str = "") -> List[str]:
    """output_suffix: the output file's extension; a libopus conversion is copied instead where Opus cannot go."""
    command_list = []
    has_truehd = False
    has_opus = False
    has_dca = False

    for track in audio_tracks:
        if not track.enabled:
            continue
        if track.conversion_codec == "libopus" and output_suffix.lower() in no_opus_extensions:
            warn_once(f"Opus cannot go into {output_suffix} files: audio track {track.index} is copied instead")
            track = track.model_copy(update={"conversion_codec": ""})
        command_list.extend(["-map", f"{audio_file_index}:{track.index}"])
        if track.title:
            command_list.extend([f"-metadata:s:{track.outdex}", f"title={track.title}"])
            command_list.extend([f"-metadata:s:{track.outdex}", f"handler={track.title}"])
        else:
            command_list.extend([f"-metadata:s:{track.outdex}", "title="])
            command_list.extend([f"-metadata:s:{track.outdex}", "handler="])

        if track.language:
            command_list.extend([f"-metadata:s:{track.outdex}", f"language={track.language}"])
        if not track.conversion_codec or track.conversion_codec == "none":
            command_list.extend([f"-c:{track.outdex}", "copy"])
        elif track.conversion_codec:
            if track.conversion_codec == "truehd":
                has_truehd = True
            elif track.conversion_codec == "opus":
                has_opus = True
            elif track.conversion_codec == "dca":
                has_dca = True

            user_downmix = track.downmix if track.downmix and track.downmix != "No Downmix" else None
            user_downmix = ffmpeg_layout_names.get(user_downmix, user_downmix)
            try:
                cl = user_downmix or track.raw_info.channel_layout
            except (AssertionError, KeyError, AttributeError):
                cl = None
                if track.conversion_codec != "libopus":  # libopus always gets a layout from the channel count
                    logger.warning("Could not determine channel layout, not forcing a channel layout filter")

            try:
                source_channels = track.raw_info.get("channels") or track.channels
            except AttributeError:
                source_channels = track.channels
            audio_filters = []
            if reverse_video:
                audio_filters.append("areverse")
            if track.conversion_codec == "libopus":
                requested = cl
                cl = opus_channel_layout(requested, channels_for_layout(requested, source_channels))
                source_layout = track.raw_info.get("channel_layout") if track.raw_info else None
                if (
                    user_downmix in channel_list
                    and user_downmix not in (cl, source_layout)
                    and channel_list[user_downmix] < channel_list[cl]
                ):
                    # A chosen layout with fewer channels than its libopus target (e.g. 7.0 -> 7.1, 4.1 -> 5.1): mix
                    # down to it first, so the target's extra channels stay silent. (The conversion window preselects
                    # the source's own layout, which needs no extra step.)
                    audio_filters.append(f"aformat=channel_layouts={user_downmix}")
                # Spend bitrate (quality mode) on the channels with sound, not on ones the target layout adds
                out_channels = min(channels_for_layout(requested, source_channels) or 8, channel_list[cl])
            else:
                out_channels = channels_for_layout(cl, source_channels)

            downmix = [f"-ac:{track.outdex}", str(channel_list[cl])] if user_downmix and cl in channel_list else []
            if cl in channel_list and track.conversion_codec == "libopus":
                # Float: mixing in 16-bit would scale the whole layout down whenever channels are mixed together
                audio_filters.append(f"aformat=sample_fmts=flt:channel_layouts={cl}")
            elif cl in channel_list:
                audio_filters.append(f"aformat=channel_layouts={cl}")
            channel_layout = [f"-filter:{track.outdex}", ",".join(audio_filters)] if audio_filters else []

            bitrate_parts = []
            if track.conversion_codec not in lossless:
                if track.conversion_bitrate:
                    conversion_bitrate = (
                        track.conversion_bitrate
                        if track.conversion_bitrate.lower().endswith(("k", "m", "g", "kb", "mb", "gb"))
                        else f"{track.conversion_bitrate}k"
                    )
                    bitrate_parts = [f"-b:{track.outdex}", conversion_bitrate]
                else:
                    bitrate_parts = _split_quality(
                        audio_quality_converter(
                            track.conversion_aq or 0,
                            track.conversion_codec,
                            out_channels,
                            track.outdex,
                        )
                    )

            command_list.extend([f"-c:{track.outdex}", track.conversion_codec])
            aac_profile = resolve_aac_profile(track.conversion_codec, track.conversion_profile, out_channels)
            if aac_profile:
                command_list.extend([f"-profile:{track.outdex}", aac_profile])
            command_list.extend(bitrate_parts)
            command_list.extend(downmix)
            command_list.extend(channel_layout)

        if getattr(track, "dispositions", None):
            added = ""
            for disposition, is_set in track.dispositions.items():
                if is_set:
                    added += f"{disposition}+"
            if added:
                command_list.extend([f"-disposition:{track.outdex}", added.rstrip("+")])
            else:
                command_list.extend([f"-disposition:{track.outdex}", "0"])

    if has_truehd or has_opus or has_dca:
        command_list.extend(["-strict", "-2"])
    return command_list

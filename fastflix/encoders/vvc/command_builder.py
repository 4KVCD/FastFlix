# -*- coding: utf-8 -*-
import secrets
import shlex

from fastflix.encoders.common.hdr_metadata import (
    chromaloc_mapping,
    color_matrix_mapping,
    color_primaries_mapping,
    color_transfer_mapping,
    is_hdr_bit_depth,
    parse_master_display,
    valid_color_matrix,
    valid_color_primaries,
    valid_color_transfers,
)
from fastflix.encoders.common.helpers import Command, generate_all, null
from fastflix.models.encode import VVCSettings
from fastflix.models.fastflix import FastFlix


def is_constant_qp(fastflix: FastFlix) -> bool:
    """QP mode without a maxrate. FastFlix's own CuTreePicStrength and NumParallelGOPs go only here: VVenC refuses
    NumParallelGOPs > 0 and capped-QP CuTreePicStrength under rate control."""
    settings: VVCSettings = fastflix.current_video.video_settings.video_encoder_settings
    return settings.qp is not None and not settings.bitrate and not fastflix.current_video.video_settings.maxrate


def is_capped_qp(settings: VVCSettings, constant_qp: bool) -> bool:
    """QP mode with a maxrate (capped CQF), the one rate mode where VVenC refuses the tune's CU-tree picture QPs."""
    return not constant_qp and not settings.bitrate


def tune_active(settings: VVCSettings, constant_qp: bool) -> bool:
    """Tune=grain is sent: ticked and not capped QP. A greyed-out tune changes nothing, FastFlix's own keys stay."""
    return settings.tune_grain and not is_capped_qp(settings, constant_qp)


def flat_inter_qp_needs_qpa_off(settings: VVCSettings, constant_qp: bool) -> bool:
    """FlatInterQP=1 sent without the tune: VVenC refuses it with QPA on (the tune turns QPA off itself).
    PsyRD and PsyRDOQ combine with QPA."""
    return not tune_active(settings, constant_qp) and settings.flat_inter_qp is True


def psy_params(settings: VVCSettings, constant_qp: bool) -> list[str]:
    """PsyRD, PsyRDOQ, LevelLambda and FlatInterQP, in every rate mode; they follow Tune=grain, so they override
    what it set."""
    params = []
    # With the tune on, both are always sent: the user may have moved a slider off the tune's 1.0, even to 0
    tune = tune_active(settings, constant_qp)
    if tune or settings.psy_rd > 0:
        params.append(f"PsyRD={settings.psy_rd:g}")
    if tune or settings.psy_rdoq > 0:
        params.append(f"PsyRDOQ={settings.psy_rdoq:g}")
    if settings.level_lambda is not None:
        params.append(f"LevelLambda={settings.level_lambda:g}")
    if settings.flat_inter_qp is not None:
        params.append(f"FlatInterQP={int(settings.flat_inter_qp)}")
    return params


def generated_vvenc_params(settings: VVCSettings, constant_qp: bool) -> list[str]:
    """The VVenC Key=Value options FastFlix sets from its panel."""
    params = []
    tune = tune_active(settings, constant_qp)
    if tune:
        params.append("Tune=grain")  # first: everything after it overrides the tune
    qp_mode = settings.qp is not None and not settings.bitrate
    if qp_mode and not float(settings.qp).is_integer():
        params.append(f"QP={float(settings.qp):g}")
    # The tune sets block importance, the CU-tree picture QPs and MCTF itself; FastFlix's values would undo it.
    # The longer look-ahead only pays together with the picture QPs, so it is sent only with them (the tune's too).
    if tune:
        params.append(f"CuTreeLookAheadGOPs={settings.cutree_lookahead_gops}")
    else:
        params.append(f"BIM={settings.bim}")
        if constant_qp and settings.bim == 1 and settings.cutree_pic_strength:
            params.append(f"CuTreePicStrength={settings.cutree_pic_strength}")
            params.append(f"CuTreeLookAheadGOPs={settings.cutree_lookahead_gops}")
    if settings.adaptive_intra:
        params.append("AdaptiveIntra=1")
    params.append(f"DecodingRefreshType={settings.refresh_type}")
    if settings.mctf_level_tilt and not tune:
        params.append(f"MCTFLevelTilt={settings.mctf_level_tilt}")
    # Film grain analysis runs inside MCTF, which the tune turns off: VVenC refuses fga with the tune
    if settings.fga and not tune:
        params.append(f"fga={settings.fga}")
        if settings.fga_strength != 100:
            params.append(f"fgastrength={settings.fga_strength / 100:g}")
    if settings.ifp != "auto":
        params.append(f"IFP={1 if settings.ifp == 'on' else 0}")
    if constant_qp and settings.num_parallel_gops != -1:
        params.append(f"NumParallelGOPs={settings.num_parallel_gops}")
    if settings.same_output_any_threads:
        params.append("mtprofile=0")
    return params + psy_params(settings, constant_qp)


def build(fastflix: FastFlix):
    settings: VVCSettings = fastflix.current_video.video_settings.video_encoder_settings

    beginning, ending, output_fps = generate_all(fastflix, "libvvenc")
    if fastflix.config.vvc_ffmpeg:
        beginning[0] = str(fastflix.config.vvc_ffmpeg)

    if settings.tier:
        beginning.extend(["-tier:v", settings.tier])

    if settings.levelidc:
        beginning.extend(["-level", settings.levelidc])

    constant_qp = is_constant_qp(fastflix)
    if not settings.subjopt or flat_inter_qp_needs_qpa_off(settings, constant_qp):
        beginning.extend(["-qpa", "0"])

    beginning.extend(["-period", str(settings.period)])

    if settings.threads > 0:
        beginning.extend(["-threads", str(settings.threads)])

    vvc_params = generated_vvenc_params(settings, constant_qp=constant_qp)

    if not fastflix.current_video.video_settings.remove_hdr:
        # Color primaries/transfer/matrix via FFmpeg flags (libvvenc reads these for VUI)
        if fastflix.current_video.video_settings.color_primaries:
            beginning.extend(["-color_primaries", fastflix.current_video.video_settings.color_primaries])
        elif fastflix.current_video.color_primaries:
            cp = fastflix.current_video.color_primaries
            cp = color_primaries_mapping.get(cp, cp)
            if cp in valid_color_primaries:
                beginning.extend(["-color_primaries", cp])

        if fastflix.current_video.video_settings.color_transfer:
            beginning.extend(["-color_trc", fastflix.current_video.video_settings.color_transfer])
        elif fastflix.current_video.color_transfer:
            ct = fastflix.current_video.color_transfer
            ct = color_transfer_mapping.get(ct, ct)
            if ct in valid_color_transfers:
                beginning.extend(["-color_trc", ct])

        if fastflix.current_video.video_settings.color_space:
            beginning.extend(["-colorspace", fastflix.current_video.video_settings.color_space])
        elif fastflix.current_video.color_space:
            cs = fastflix.current_video.color_space
            cs = color_matrix_mapping.get(cs, cs)
            if cs in valid_color_matrix:
                beginning.extend(["-colorspace", cs])

        if is_hdr_bit_depth(settings.pix_fmt):
            master_display = parse_master_display(fastflix.current_video.master_display)
            if master_display:
                vvc_params.append(f"MasteringDisplayColourVolume={master_display.to_vvenc()}")

            if fastflix.current_video.cll:
                vvc_params.append(f"MaxContentLightLevel={fastflix.current_video.cll}")

        current_chroma_loc = fastflix.current_video.current_video_stream.get("chroma_location")
        if current_chroma_loc in chromaloc_mapping:
            beginning.extend(["-chroma_sample_location", str(chromaloc_mapping[current_chroma_loc])])

    pass_log_file = fastflix.current_video.work_path / f"pass_log_file_{secrets.token_hex(10)}"

    # The user's own params go last, so they override the generated ones
    vvc_params += settings.vvc_params
    vvenc_params = ["-vvenc-params", ":".join(vvc_params)] if vvc_params else []

    extra = shlex.split(settings.extra) if settings.extra else []
    extra_both = shlex.split(settings.extra) if settings.extra and settings.extra_both_passes else []

    if settings.bitrate:
        command_1 = (
            beginning
            + vvenc_params
            + ["-pass", "1", "-passlogfile", str(pass_log_file), "-b:v", settings.bitrate, "-preset:v", settings.preset]
            + extra_both
            + ["-an", "-sn", "-dn"]
            + output_fps
            + ["-f", "mp4", null]
        )
        command_2 = (
            beginning
            + vvenc_params
            + ["-pass", "2", "-passlogfile", str(pass_log_file), "-b:v", settings.bitrate, "-preset:v", settings.preset]
            + extra
            + ending
        )
        return [
            Command(command=command_1, name="First pass bitrate", exe="ffmpeg"),
            Command(command=command_2, name="Second pass bitrate", exe="ffmpeg"),
        ]

    elif settings.qp is not None:
        # FFmpeg's -qp takes whole numbers only; a fractional QP is passed as QP= in -vvenc-params, which wins
        command = (
            beginning
            + vvenc_params
            + ["-qp:v", str(int(float(settings.qp))), "-b:v", "0", "-preset:v", settings.preset]
            + extra
            + ending
        )
        return [Command(command=command, name="Single pass CRF", exe="ffmpeg")]

    else:
        return []

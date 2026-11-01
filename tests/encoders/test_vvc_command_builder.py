# -*- coding: utf-8 -*-
from pathlib import Path
from unittest import mock

from fastflix.encoders.vvc.command_builder import build
from fastflix.models.encode import VVCSettings
from fastflix.models.video import VideoSettings

from tests.conftest import create_fastflix_instance


def _build_with_settings(
    hdr10=False,
    remove_hdr=False,
    maxrate=None,
    vvc_ffmpeg=None,
    **kwargs,
):
    """Helper to build VVC commands with custom settings."""
    defaults = dict(
        qp=22,
        bitrate=None,
        preset="medium",
        tier="main",
    )
    defaults.update(kwargs)
    fastflix = create_fastflix_instance(
        encoder_settings=VVCSettings(**defaults),
        video_settings=VideoSettings(remove_hdr=remove_hdr, maxrate=maxrate, bufsize=None),
        hdr10_metadata=hdr10,
    )
    fastflix.config.vvc_ffmpeg = vvc_ffmpeg
    with mock.patch("fastflix.encoders.vvc.command_builder.generate_all") as mock_gen:
        mock_gen.return_value = (["ffmpeg", "-y", "-i", "input.mkv"], ["output.mkv"], ["-r", "24"])
        result = build(fastflix)
    return result


def test_vvc_basic_qp():
    """Test basic QP encoding with default settings."""
    result = _build_with_settings()
    assert len(result) == 1
    cmd = result[0].command
    assert isinstance(cmd, list)
    assert "-qp:v" in cmd
    assert "22" in cmd
    assert "-preset:v" in cmd
    assert "medium" in cmd


def test_vvc_bitrate_two_pass():
    """Test bitrate mode produces two-pass commands."""
    result = _build_with_settings(qp=None, bitrate="6000k")
    assert len(result) == 2
    cmd1 = result[0].command
    cmd2 = result[1].command
    assert "-b:v" in cmd1
    assert "6000k" in cmd1
    assert "-b:v" in cmd2
    assert "6000k" in cmd2


def test_vvc_tier():
    """Test tier parameter is included."""
    result = _build_with_settings(tier="high")
    cmd = result[0].command
    assert "-tier:v" in cmd
    idx = cmd.index("-tier:v")
    assert cmd[idx + 1] == "high"


def test_vvc_level():
    """Test level parameter is included when set."""
    result = _build_with_settings(levelidc="5.1")
    cmd = result[0].command
    assert "-level" in cmd
    idx = cmd.index("-level")
    assert cmd[idx + 1] == "5.1"


def test_vvc_subjopt_enabled_by_default():
    """Test that QPA is not disabled when subjopt is True (default)."""
    result = _build_with_settings(subjopt=True)
    cmd = result[0].command
    assert "-qpa" not in cmd


def test_vvc_subjopt_disabled():
    """Test that -qpa 0 is emitted when subjopt is False."""
    result = _build_with_settings(subjopt=False)
    cmd = result[0].command
    assert "-qpa" in cmd
    idx = cmd.index("-qpa")
    assert cmd[idx + 1] == "0"


def test_vvc_period():
    """Test intra period parameter is included when set."""
    result = _build_with_settings(period=2)
    cmd = result[0].command
    assert "-period" in cmd
    idx = cmd.index("-period")
    assert cmd[idx + 1] == "2"


def test_vvc_legacy_period_auto_becomes_10_seconds():
    """Older settings stored None (auto) or 0 (rejected by FFmpeg); both load as the 10 s default."""
    for legacy_period in (None, 0, "0", -2):
        cmd = _build_with_settings(period=legacy_period)[0].command
        assert cmd[cmd.index("-period") + 1] == "10"


def test_vvc_threads():
    """Test threads parameter is included when > 0."""
    result = _build_with_settings(threads=8)
    cmd = result[0].command
    assert "-threads" in cmd
    idx = cmd.index("-threads")
    assert cmd[idx + 1] == "8"


def test_vvc_threads_auto():
    """Test threads is not included when 0 (auto)."""
    result = _build_with_settings(threads=0)
    cmd = result[0].command
    assert "-threads" not in cmd


def test_vvc_ifp_choices():
    """IFP auto sends nothing; on and off are sent; the old checkbox's True means on, False auto."""
    for ifp, expected in (("on", "IFP=1"), ("off", "IFP=0"), (True, "IFP=1")):
        assert expected in _params(_build_with_settings(ifp=ifp)[0].command).split(":")
    for ifp in ("auto", False):
        params = _params(_build_with_settings(ifp=ifp)[0].command).split(":")
        assert not any(param.startswith("IFP=") for param in params)


def test_vvc_vvc_params():
    """Test pass-through vvc params."""
    result = _build_with_settings(vvc_params=["rcstatsfile=test"])
    cmd = result[0].command
    assert "-vvenc-params" in cmd
    idx = cmd.index("-vvenc-params")
    assert "rcstatsfile=test" in cmd[idx + 1]


def test_vvc_all_elements_are_strings():
    """Test that all command elements are strings."""
    result = _build_with_settings()
    cmd = result[0].command
    for i, element in enumerate(cmd):
        assert isinstance(element, str), f"Element at index {i} is {type(element).__name__}: {element!r}"


def test_vvc_hdr10_mastering_display():
    """Test HDR10 mastering display metadata is passed to vvenc-params."""
    result = _build_with_settings(hdr10=True, pix_fmt="yuv420p10le")
    cmd = result[0].command
    assert "-vvenc-params" in cmd
    idx = cmd.index("-vvenc-params")
    params = cmd[idx + 1]
    assert "MasteringDisplayColourVolume=" in params


def test_vvc_hdr10_cll():
    """Test HDR10 content light level is passed to vvenc-params."""
    result = _build_with_settings(hdr10=True, pix_fmt="yuv420p10le")
    cmd = result[0].command
    assert "-vvenc-params" in cmd
    idx = cmd.index("-vvenc-params")
    params = cmd[idx + 1]
    assert "MaxContentLightLevel=1000,300" in params


def test_vvc_hdr10_color_primaries():
    """Test HDR10 color primaries are passed as FFmpeg flags."""
    result = _build_with_settings(hdr10=True, pix_fmt="yuv420p10le")
    cmd = result[0].command
    assert "-color_primaries" in cmd
    idx = cmd.index("-color_primaries")
    assert cmd[idx + 1] == "bt2020"


def test_vvc_hdr10_color_trc():
    """Test HDR10 color transfer is passed as FFmpeg flags."""
    result = _build_with_settings(hdr10=True, pix_fmt="yuv420p10le")
    cmd = result[0].command
    assert "-color_trc" in cmd
    idx = cmd.index("-color_trc")
    assert cmd[idx + 1] == "smpte2084"


def test_vvc_hdr10_colorspace():
    """Test HDR10 colorspace is passed as FFmpeg flags."""
    result = _build_with_settings(hdr10=True, pix_fmt="yuv420p10le")
    cmd = result[0].command
    assert "-colorspace" in cmd
    idx = cmd.index("-colorspace")
    assert cmd[idx + 1] == "bt2020nc"


def test_vvc_hdr10_not_included_when_remove_hdr():
    """Test HDR10 metadata is not included when remove_hdr is set."""
    result = _build_with_settings(hdr10=True, remove_hdr=True, pix_fmt="yuv420p10le")
    cmd = result[0].command
    assert "-color_primaries" not in cmd
    if "-vvenc-params" in cmd:
        idx = cmd.index("-vvenc-params")
        params = cmd[idx + 1]
        assert "MasteringDisplayColourVolume" not in params
        assert "MaxContentLightLevel" not in params


def test_vvc_hdr10_not_included_when_8bit():
    """Test HDR10 mastering display is not included for 8-bit output."""
    result = _build_with_settings(hdr10=True, pix_fmt="yuv420p")
    cmd = result[0].command
    # Color signaling should still be present (it's independent of bit depth)
    assert "-color_primaries" in cmd
    # But mastering display / CLL should not be in vvenc-params for 8-bit
    if "-vvenc-params" in cmd:
        idx = cmd.index("-vvenc-params")
        params = cmd[idx + 1]
        assert "MasteringDisplayColourVolume" not in params
        assert "MaxContentLightLevel" not in params


def test_vvc_hdr10_chroma_location():
    """Test chroma location is passed when present."""
    result = _build_with_settings(hdr10=True, pix_fmt="yuv420p10le")
    cmd = result[0].command
    assert "-chroma_sample_location" in cmd
    idx = cmd.index("-chroma_sample_location")
    assert cmd[idx + 1] == "0"  # "left" maps to 0


def test_vvc_hdr10_all_elements_are_strings():
    """Test that all command elements are strings with HDR10 metadata."""
    result = _build_with_settings(hdr10=True, pix_fmt="yuv420p10le")
    cmd = result[0].command
    for i, element in enumerate(cmd):
        assert isinstance(element, str), f"Element at index {i} is {type(element).__name__}: {element!r}"


def _params(cmd):
    return cmd[cmd.index("-vvenc-params") + 1] if "-vvenc-params" in cmd else ""


def _pass_value(cmd):
    return cmd[cmd.index("-pass") + 1]


def test_vvc_two_pass_uses_ffmpeg_pass_flags_not_vvenc_params():
    """libvvenc.c rejects pass/passes/rcstatsfile inside vvenc-params; -pass and -passlogfile must be used."""
    result = _build_with_settings(qp=None, bitrate="6000k")
    cmd1, cmd2 = result[0].command, result[1].command
    assert _pass_value(cmd1) == "1"
    assert _pass_value(cmd2) == "2"
    assert cmd1[cmd1.index("-passlogfile") + 1] == cmd2[cmd2.index("-passlogfile") + 1]
    for cmd in (cmd1, cmd2):
        params = _params(cmd).lower()
        assert "pass=" not in params
        assert "rcstatsfile" not in params


def test_vvc_no_x265_vbv_keys_in_vvenc_params():
    """vbv-maxrate/vbv-bufsize are unknown vvenc options (init fails); -maxrate/-bufsize come from generate_all."""
    fastflix = create_fastflix_instance(
        encoder_settings=VVCSettings(qp=22, bitrate=None, preset="medium", tier="main"),
        video_settings=VideoSettings(remove_hdr=False, maxrate=5000, bufsize=10000),
    )
    with mock.patch("fastflix.encoders.vvc.command_builder.generate_all") as mock_gen:
        mock_gen.return_value = (["ffmpeg", "-y", "-i", "input.mkv"], ["output.mkv"], [])
        cmd = build(fastflix)[0].command
    assert "vbv" not in _params(cmd)


def test_vvc_mastering_display_uses_vvenc_integer_format():
    result = _build_with_settings(hdr10=True, pix_fmt="yuv420p10le")
    params = _params(result[0].command)
    assert "MasteringDisplayColourVolume=" in params
    assert "(" not in params


def test_vvc_hdr_metadata_for_422_10bit():
    result = _build_with_settings(hdr10=True, pix_fmt="yuv422p10le")
    params = _params(result[0].command)
    assert "MasteringDisplayColourVolume=" in params
    assert "MaxContentLightLevel=1000,300" in params


def _param_list(cmd):
    return _params(cmd).split(":")


def test_vvc_default_qp_turns_on_every_new_vvenc_option():
    params = _param_list(_build_with_settings()[0].command)
    assert params == [
        "BIM=1",
        "CuTreePicStrength=2",
        "CuTreeLookAheadGOPs=4",
        "AdaptiveIntra=1",
        "DecodingRefreshType=cra",
        "MCTFLevelTilt=100",
        "fga=2",
        "NumParallelGOPs=4",
    ]
    tilt_off = _param_list(_build_with_settings(mctf_level_tilt=0)[0].command)
    assert not any(param.startswith("MCTFLevelTilt") for param in tilt_off)


def test_vvc_fractional_qp():
    """FFmpeg's -qp is a whole number; the fraction reaches VVenC as QP= (also with a maxrate cap)."""
    for maxrate in (None, 8000):
        cmd = _build_with_settings(qp=27.5, maxrate=maxrate)[0].command
        assert cmd[cmd.index("-qp:v") + 1] == "27"
        assert "QP=27.5" in _param_list(cmd)
    whole = _build_with_settings(qp=27.0)[0].command
    assert not any(param.startswith("QP=") for param in _param_list(whole))


def test_vvc_qp_zero_is_a_qp_encode():
    cmd = _build_with_settings(qp=0)[0].command
    assert cmd[cmd.index("-qp:v") + 1] == "0"


def test_vvc_rate_control_never_gets_cutree_picture_qps_or_parallel_gops():
    """VVenC refuses CuTreePicStrength and NumParallelGOPs > 0 with a target bitrate or a maxrate."""
    for cmd in (
        _build_with_settings(qp=None, bitrate="6000k", num_parallel_gops=4)[1].command,
        _build_with_settings(maxrate=8000, num_parallel_gops=4)[0].command,
    ):
        params = _param_list(cmd)
        assert "BIM=1" in params
        assert not any(param.startswith(("CuTree", "NumParallelGOPs")) for param in params)


def test_vvc_cutree_options_need_bim_1():
    params = _param_list(_build_with_settings(bim=2)[0].command)
    assert "BIM=2" in params
    assert not any(param.startswith("CuTree") for param in params)


def test_vvc_user_params_come_last_and_win():
    params = _param_list(_build_with_settings(vvc_params=["BIM=2"])[0].command)
    assert params[-1] == "BIM=2"


def test_vvc_grain_strength_and_speed_options():
    params = _param_list(
        _build_with_settings(fga=3, fga_strength=50, num_parallel_gops=4, same_output_any_threads=True)[0].command
    )
    assert {"fga=3", "fgastrength=0.5", "NumParallelGOPs=4", "mtprofile=0"} <= set(params)
    assert not any(param.startswith("fga") for param in _param_list(_build_with_settings(fga=0)[0].command))


def test_vvc_ffmpeg_used_for_vvc_encodes():
    cmd = _build_with_settings(vvc_ffmpeg=Path("C:/w/ffvvc/ffmpeg.exe"))[0].command
    assert cmd[0] == str(Path("C:/w/ffvvc/ffmpeg.exe"))

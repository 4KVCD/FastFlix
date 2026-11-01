#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging

from PySide6 import QtCore, QtGui, QtWidgets

from fastflix.encoders.common.setting_panel import SettingPanel
from fastflix.language import t
from fastflix.models.encode import VVCSettings
from fastflix.models.fastflix_app import FastFlixApp


logger = logging.getLogger("fastflix")

presets = ["faster", "fast", "medium", "slow", "slower"]

recommended_bitrates = [
    "150k     (320x240p @ 30fps)",
    "276k     (640x360p @ 30fps)",
    "512k     (640x480p @ 30fps)",
    "1500k   (1280x720p @ 30fps)",
    "2000k   (1280x720p @ 60fps)",
    "3000k   (1920x1080p @ 30fps)",
    "4000k   (1920x1080p @ 60fps)",
    "6000k   (2560x1440p @ 30fps)",
    "9000k   (2560x1440p @ 60fps)",
    "12000k (3840x2160p @ 30fps)",
    "18000k (3840x2160p @ 60fps)",
    "Custom",
]

pix_fmts = [
    "8-bit: yuv420p",
    "10-bit: yuv420p10le",
]

DEFAULT_QP = 22
MAX_QP = 63
MIN_GRAIN_STRENGTH = 1  # percent; 0 would signal no grain after running the whole analysis
MAX_GRAIN_STRENGTH = 399  # VVenC's fgastrength is below 4.0

# Combos whose items carry their setting's value: (setting name, label, [(item text, value)], tooltip).
# The setting name is also the widget name.
value_combos = [
    (
        "bim",
        "Block importance",
        [("CU tree", 1), ("MCTF error", 2), ("Off", 0)],
        "BIM: lowers the QP of blocks that later pictures predict from.\n"
        "CU tree: a dependency model over the look-ahead (best compression).\n"
        "MCTF error: from the motion-compensated filter's error (VVenC's default).",
    ),
    (
        "cutree_pic_strength",
        "Picture QPs from CU tree",
        [("On", 2), ("Off", 0)],
        "CuTreePicStrength: the CU tree also sets each picture's QP.\n"
        "Needs CU tree and constant QP (VVenC refuses it with a bitrate or maxrate).",
    ),
    (
        "cutree_lookahead_gops",
        "CU-tree look-ahead",
        [("4 GOPs (best)", 4), ("2 GOPs", 2), ("1 GOP", 1)],
        "CuTreeLookAheadGOPs: how far ahead the encoder looks to set QPs\n"
        "(better compression; more RAM and delay, 4K: about 5 GB at 1 GOP, 8.2 GB at 4).\n"
        "Only used with picture QPs from the CU tree; capped at the intra period.",
    ),
    (
        "adaptive_intra",
        "Scene-cut intra",
        [("On", True), ("Off", False)],
        "AdaptiveIntra: I pictures at scene cuts where they pay.",
    ),
    (
        "period",
        "Intra period",
        [("1 s", 1), ("2 s", 2), ("3 s", 3), ("5 s", 5), ("10 s", 10)],
        "Seconds between intra pictures. Longer gives smaller files; scene-cut intra adds I pictures at cuts.",
    ),
    (
        "refresh_type",
        "Refresh type",
        [("CRA (open GOP)", "cra"), ("IDR (closed GOP)", "idr")],
        "DecodingRefreshType: CRA codes smaller files; IDR makes every intra picture a clean cut point.",
    ),
    (
        "mctf_level_tilt",
        "MCTF tilt",
        [("On", 100), ("Off", 0)],
        "MCTFLevelTilt: filters the pictures that the rest of the video is predicted from more strongly.\n"
        "Helps grainy or noisy film (about 2-3 % smaller files at equal quality, up to 4 %);\n"
        "on clean content about neutral, clean 3D animation can lose a few tenths of a percent.",
    ),
    (
        "fga",
        "Film grain analysis",
        [("Off", 0), ("1: basic", 1), ("2: plays everywhere", 2), ("3: best grain, VVdeC only", 3)],
        "fga: measures the source's grain and signals it, so decoders can add it back.\n"
        "The coded pictures are the same with or without it.\n"
        "3 is the better grain model, but FFmpeg-based players (mpv, VLC) cannot decode it: use 2 for them.",
    ),
    (
        "threads",
        "Threads",
        [("Auto", 0), ("1", 1), ("2", 2), ("4", 4), ("6", 6), ("8", 8), ("12", 12), ("16", 16), ("24", 24), ("32", 32)],
        "Number of encoding threads. Auto lets VVenC decide.",
    ),
    (
        "ifp",
        "IFP",
        [("Auto", "auto"), ("On", "on"), ("Off", "off")],
        "Inter-frame parallelism: faster with many threads, changes the output slightly. Auto lets VVenC decide.",
    ),
    (
        "num_parallel_gops",
        "Parallel GOPs",
        [("Auto", -1), ("0 (off)", 0), ("1", 1), ("2", 2), ("3", 3), ("4", 4), ("6", 6), ("8", 8)],
        "NumParallelGOPs: how many extra GOPs are encoded at the same time (faster; same output).\n"
        "Each extra GOP keeps 33 more pictures in memory. Auto: VVenC's choice, 2 without IFP, 0 with IFP.\n"
        "Constant QP only (VVenC refuses it with a bitrate or maxrate).",
    ),
]


def get_breaker():
    breaker_line = QtWidgets.QWidget()
    breaker_line.setMaximumHeight(2)
    breaker_line.setStyleSheet("background-color: #ccc; margin: auto 0; padding: auto 0;")
    return breaker_line


def section_title(title):
    """A line with a centred title between groups of settings."""
    layout = QtWidgets.QHBoxLayout()
    layout.addWidget(get_breaker(), stretch=1)
    layout.addWidget(QtWidgets.QLabel(t(title)), alignment=QtCore.Qt.AlignHCenter)
    layout.addWidget(get_breaker(), stretch=1)
    return layout


def qp_text(qp):
    return f"{qp:g}" if qp is not None else str(DEFAULT_QP)


class VVC(SettingPanel):
    profile_name = "vvc"
    hdr10plus_signal = QtCore.Signal(str)
    hdr10plus_ffmpeg_signal = QtCore.Signal(str)

    def __init__(self, parent, main, app: FastFlixApp):
        super().__init__(parent, main, app)
        self.main = main
        self.app = app

        grid = QtWidgets.QGridLayout()

        self.mode = "QP"
        self.updating_settings = False
        self.extract_thread = None

        grid.addLayout(self.init_preset(), 0, 0, 1, 2)
        grid.addLayout(self.init_tier(), 1, 0, 1, 2)
        grid.addLayout(self.init_levels(), 2, 0, 1, 2)
        grid.addLayout(self.init_pix_fmt(), 3, 0, 1, 2)
        grid.addLayout(self.init_modes(), 0, 2, 5, 4)

        combos = {name: self._add_value_combo(name, label, choices, tip) for name, label, choices, tip in value_combos}

        grid.addLayout(section_title("Compression"), 5, 0, 1, 6)
        grid.addLayout(combos["bim"], 6, 0, 1, 2)
        grid.addLayout(combos["adaptive_intra"], 6, 2, 1, 4)
        grid.addLayout(combos["cutree_pic_strength"], 7, 0, 1, 2)
        grid.addLayout(combos["period"], 7, 2, 1, 4)
        grid.addLayout(combos["cutree_lookahead_gops"], 8, 0, 1, 2)
        grid.addLayout(combos["refresh_type"], 8, 2, 1, 4)
        grid.addLayout(combos["fga"], 9, 0, 1, 2)
        grid.addLayout(self.init_fga_strength(), 9, 2, 1, 4)
        grid.addLayout(combos["mctf_level_tilt"], 10, 0, 1, 2)

        grid.addLayout(section_title("Speed"), 11, 0, 1, 6)
        grid.addLayout(combos["threads"], 12, 0, 1, 2)
        grid.addLayout(combos["num_parallel_gops"], 12, 2, 1, 4)
        grid.addLayout(combos["ifp"], 13, 0, 1, 2)
        grid.addLayout(self.init_same_output_any_threads(), 13, 2, 1, 4)

        grid.addLayout(section_title("Advanced"), 14, 0, 1, 6)
        grid.addLayout(self.init_subjopt(), 15, 0, 1, 2)
        grid.addLayout(self.init_vvc_params(), 15, 2, 1, 4)
        grid.addLayout(self.init_max_mux(), 16, 0, 1, 2)

        grid.setRowStretch(17, True)

        grid.addLayout(self._add_custom(), 18, 0, 1, 6)

        self.setLayout(grid)
        self.update_dependent_options()
        self.hide()

    def _add_value_combo(self, name, label, choices, tooltip):
        """A combo whose items carry their setting's value. It stays out of self.opts: the shared restore code
        treats whole-number values as item positions, so these combos are restored by show_value_combos."""
        layout = QtWidgets.QHBoxLayout()
        self.labels[name] = QtWidgets.QLabel(t(label))
        self.labels[name].setToolTip(self.translate_tip(tooltip))
        combo = QtWidgets.QComboBox()
        for text, value in choices:
            combo.addItem(t(text), value)
        combo.setToolTip(self.translate_tip(tooltip))
        self.widgets[name] = combo
        self.select_value(name, self.app.fastflix.config.encoder_opt(self.profile_name, name))
        combo.currentIndexChanged.connect(lambda: self.value_combo_changed())
        layout.addWidget(self.labels[name])
        layout.addWidget(combo)
        return layout

    def select_value(self, name, value):
        """Select the item with this value. A value the list lacks (from an older profile or the queue, e.g. 7 threads)
        gets its own item, so saving does not silently change it."""
        combo = self.widgets[name]
        index = combo.findData(value)
        if index < 0:
            combo.addItem(str(value), value)
            index = combo.count() - 1
        combo.setCurrentIndex(index)

    def show_value_combos(self, value_of):
        for name, *_ in value_combos:
            self.select_value(name, value_of(name))

    def value_combo_changed(self):
        self.update_dependent_options()
        self.main.page_update(build_thumbnail=False)

    def init_tier(self):
        return self._add_combo_box(
            label="Tier",
            tooltip="Set the encoding tier",
            widget_name="tier",
            options=["main", "high"],
            opt="tier",
        )

    def init_levels(self):
        # https://github.com/fraunhoferhhi/vvenc/blob/cf8ba5ed74f8e8c7c9e7b6f81f7fb08bce6241b0/source/Lib/vvenc/vvencCfg.cpp#L159
        return self._add_combo_box(
            label="IDC Level",
            tooltip="Set the IDC level. Auto picks it from the size, frame rate and bitrate; it does not change\n"
            "the compression. 15.5 (unconstrained) is refused by some hardware decoders.",
            widget_name="levelidc",
            options=[
                t("Auto"),
                "1",
                "2",
                "2.1",
                "3",
                "3.1",
                "4",
                "4.1",
                "5",
                "5.1",
                "5.2",
                "6",
                "6.1",
                "6.2",
                "6.3",
                "15.5",
            ],
            opt="levelidc",
        )

    def init_preset(self):
        layout = self._add_combo_box(
            label="Preset",
            widget_name="preset",
            options=presets,
            tooltip=(
                "preset: The slower the preset, the better the compression and quality\n"
                "Slow is the slowest preset personally recommended,\n"
                "presets slower than this result in much smaller gains"
            ),
            connect="default",
            opt="preset",
        )
        return layout

    def init_pix_fmt(self):
        return self._add_combo_box(
            label="Bit Depth",
            tooltip="Pixel Format (requires at least 10-bit for HDR)",
            widget_name="pix_fmt",
            options=pix_fmts,
            connect=lambda: self.setting_change(pix_change=True),
            opt="pix_fmt",
        )

    def init_fga_strength(self):
        layout = self._add_text_box(
            label="Grain strength %",
            widget_name="fga_strength",
            opt="fga_strength",
            tooltip="fgastrength: the signalled grain in percent of the analysed grain (1-399).",
            width=60,
        )
        self.widgets.fga_strength.setValidator(QtGui.QIntValidator(MIN_GRAIN_STRENGTH, MAX_GRAIN_STRENGTH))
        return layout

    def init_same_output_any_threads(self):
        return self._add_check_box(
            label="Same output at any thread count",
            tooltip="mtprofile=0: VVenC's automatic multi-threading (tiles, IFP) is off,\n"
            "so the output does not depend on the number of threads.",
            widget_name="same_output_any_threads",
            opt="same_output_any_threads",
        )

    def init_subjopt(self):
        return self._add_check_box(
            label="Perceptual QPA",
            tooltip="QPA perceptual optimization (enabled by default in vvenc).\nDisabling sends -qpa 0 to vvenc.",
            widget_name="subjopt",
            opt="subjopt",
        )

    def init_max_mux(self):
        return self._add_combo_box(
            label="Max Muxing Queue Size",
            tooltip='max_muxing_queue_size: Raise to fix "Too many packets buffered for output stream" error',
            widget_name="max_mux",
            options=["default", "1024", "2048", "4096", "8192"],
            opt="max_muxing_queue_size",
        )

    def init_modes(self):
        layout = self._add_modes(recommended_bitrates, ["Custom"], qp_name="qp", qp_text_box_only=True)
        qp_validator = QtGui.QDoubleValidator(0, MAX_QP, 2)
        qp_validator.setLocale(QtCore.QLocale.c())  # a dot as the decimal separator
        self.widgets.custom_qp.setValidator(qp_validator)
        self.widgets.custom_qp.setToolTip(t("QP 0-63; decimals such as 27.5 encode between two QPs"))
        self.widgets.custom_qp.setText(qp_text(self.app.fastflix.config.encoder_opt(self.profile_name, "qp")))
        return layout

    def mode_update(self):
        self.widgets.custom_bitrate.setDisabled(self.widgets.bitrate.currentText() != "Custom")
        self.main.build_commands()

    def init_vvc_params(self):
        layout = QtWidgets.QHBoxLayout()
        self.labels.vvc_params = QtWidgets.QLabel(t("Additional vvc params"))
        self.labels.vvc_params.setFixedWidth(200)
        tool_tip = (
            f"{t('Extra vvc params in opt=1:opt2=0 format')},\n"
            f"{t('they override the settings above')}\n"
            f"{t('examples: CuTreeStrength=1.5:IntraPeriod=128')} \n"
        )
        self.labels.vvc_params.setToolTip(tool_tip)
        layout.addWidget(self.labels.vvc_params)
        self.widgets.vvc_params = QtWidgets.QLineEdit()
        self.widgets.vvc_params.setToolTip(tool_tip)
        self.widgets.vvc_params.setText(":".join(self.app.fastflix.config.encoder_opt(self.profile_name, "vvc_params")))
        self.opts["vvc_params"] = "vvc_params"
        self.widgets.vvc_params.textChanged.connect(lambda: self.main.page_update())
        layout.addWidget(self.widgets.vvc_params)
        return layout

    def update_dependent_options(self):
        """Grey out the options VVenC refuses or ignores with the other settings. Only a hint: the maxrate is set on
        another tab, so the command builder's is_constant_qp is what keeps refused options out of the command."""
        video = self.app.fastflix.current_video
        rate_control = self.mode.lower() == "bitrate" or bool(video and video.video_settings.maxrate)
        cutree = self.widgets.bim.currentData() == 1
        picture_qps = not rate_control and cutree
        self.widgets.cutree_pic_strength.setEnabled(picture_qps)
        # The look-ahead only pays together with the picture QPs, so the builder sends it only with them
        self.widgets.cutree_lookahead_gops.setEnabled(
            picture_qps and self.widgets.cutree_pic_strength.currentData() > 0
        )
        self.widgets.num_parallel_gops.setEnabled(not rate_control)
        self.widgets.fga_strength.setEnabled(self.widgets.fga.currentData() != 0)

    def setting_change(self, update=True, pix_change=False):
        if self.updating_settings or not self.main.input_video:
            return
        self.updating_settings = True

        if update:
            self.main.page_update()
        self.updating_settings = False

    def new_source(self):
        if not self.app.fastflix.current_video:
            return
        super().new_source()
        self.setting_change()
        if self.extract_thread:
            try:
                self.extract_thread.terminate()
            except Exception:
                pass

    def checked_qp(self, qp):
        """The QP typed, or the last valid one while the box holds something VVenC would refuse."""
        valid = 0 <= qp <= MAX_QP and not (qp == MAX_QP and not float(qp).is_integer())
        self.widgets.custom_qp.setStyleSheet("" if valid else "color: red")
        if valid:
            return qp
        last_qp = self.app.fastflix.current_video.video_settings.video_encoder_settings.qp
        return last_qp if last_qp is not None else DEFAULT_QP

    def update_video_encoder_settings(self):
        if not self.app.fastflix.current_video:
            return
        self.update_dependent_options()

        vvc_params_text = self.widgets.vvc_params.text().strip()
        level = self.widgets.levelidc.currentText() if self.widgets.levelidc.currentIndex() > 0 else None
        # The box can hold 0 or nothing while typing; VVenC's fgastrength stays below 4.0
        fga_strength = self.widgets.fga_strength.text()
        fga_strength = min(max(int(fga_strength), MIN_GRAIN_STRENGTH), MAX_GRAIN_STRENGTH) if fga_strength else 100

        settings = VVCSettings(
            preset=self.widgets.preset.currentText(),
            max_muxing_queue_size=self.widgets.max_mux.currentText(),
            pix_fmt=self.widgets.pix_fmt.currentText().split(":")[1].strip(),
            tier=self.widgets.tier.currentText(),
            levelidc=level,
            vvc_params=vvc_params_text.split(":") if vvc_params_text else [],
            extra=self.ffmpeg_extras,
            extra_both_passes=self.widgets.extra_both_passes.isChecked(),
            subjopt=self.widgets.subjopt.isChecked(),
            same_output_any_threads=self.widgets.same_output_any_threads.isChecked(),
            fga_strength=fga_strength,
            **{name: self.widgets[name].currentData() for name, *_ in value_combos},
        )

        encode_type, q_value = self.get_mode_settings()
        settings.qp = self.checked_qp(q_value) if encode_type == "qp" else None
        settings.bitrate = q_value if encode_type == "bitrate" else None
        self.app.fastflix.current_video.video_settings.video_encoder_settings = settings

    def update_profile(self):
        super().update_profile()
        self.show_value_combos(lambda name: self.app.fastflix.config.encoder_opt(self.profile_name, name))
        self.widgets.custom_qp.setText(qp_text(self.app.fastflix.config.encoder_opt(self.profile_name, "qp")))
        self.update_dependent_options()

    def reload(self):
        super().reload()
        settings = self.app.fastflix.current_video.video_settings.video_encoder_settings
        self.show_value_combos(lambda name: getattr(settings, name))
        self.widgets.custom_qp.setText(qp_text(settings.qp))
        self.update_dependent_options()

    def set_mode(self, x):
        self.mode = x.text()
        self.update_dependent_options()
        self.main.build_commands()

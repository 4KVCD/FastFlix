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

PSY_SLIDER_STEPS_PER_UNIT = 20  # the psy sliders move in steps of 0.05
PSY_SLIDER_MAX = 3.0  # VVenC takes up to 3.98; past about 2 the defects (streaks, patchy grain) outweigh the grain kept
TUNE_PSY_VALUE = 1.0  # what Tune=grain sets for PsyRD and PsyRDOQ
MAX_LEVEL_LAMBDA = 3.98
QWIDGETSIZE_MAX = 16777215  # Qt's "no maximum" widget size (PySide6 does not export the constant)

PSY_RD_TIP = (
    "PsyRD: keeps the source's fine texture and film grain instead of smoothing it away.\n"
    "Higher keeps more grain at the same QP and costs more bytes; past a point it brings visible defects\n"
    "(brush-stroke streaks, patchy grain). Off = stock VVenC. 1.0 is recommended for film grain,\n"
    "0.5 is lighter (fewer defects), 2.0 keeps the most."
)
PSY_RDOQ_TIP = (
    "PsyRDOQ: the quantizer keeps grain coefficients in I pictures, on the same scale as Psy-RD.\n"
    "Useful on film grain; on clean digital video it is the main source of brush-stroke streaks."
)
TUNE_GRAIN_TIP = (
    "Tune=grain: VVenC's preset for grainy film. It sets Psy-RD and Psy-RDOQ to 1.0 and its own\n"
    "block importance, CU-tree picture QPs, filters and QPA, and turns MCTF off,\n"
    "so those settings above are greyed while it is on. The sliders still override it.\n"
    "Film grain analysis needs MCTF, so it is off with the tune."
)
CAPPED_QP_TIP = "Not with a maxrate in QP mode: VVenC refuses the tune's CU-tree picture QPs there"

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
        "fga_interval",
        "Grain analysed",
        [
            ("Once per GOP", -1),
            ("Every 16 pictures", 4),
            ("Every 8 pictures", 3),
            ("Every 4 pictures", 2),
            ("Every 2 pictures", 1),
            ("Every picture", 0),
        ],
        "FgaIntervalLog2: how often film grain analysis measures the grain (a GOP is 32 pictures).\n"
        "Once per GOP is VVenC's default. More often follows grain changes within a GOP more closely;\n"
        "each extra analysed picture costs a filtered copy, the analysis and, where MCTF does not filter\n"
        "that picture, a motion search, and holds its buffers until its GOP is coded (slower, more RAM).\n"
        "VVenC refuses it with a GOPSize it does not divide or with one-pass look-ahead in your own options.",
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
    (
        "chunks",
        "Chunks",
        [("Off", 0), ("2", 2), ("3", 3), ("4", 4), ("6", 6), ("8", 8), ("12", 12)],
        "Chunked encoding: the video is split at the scene cuts nearest to even split points, the chunks are\n"
        "encoded at the same time by this many encoders, then joined into one file with the audio and subtitles.\n"
        "VVenC uses a few threads per encoder more efficiently than many.\n"
        "Each encoder needs its own RAM; measured at 4K: about 11 GB each at preset slower with\n"
        "Parallel GOPs 1 (4 chunks fill a 64 GB PC), 3.6 GB each at faster with Parallel GOPs 4.\n"
        "Constant QP and MKV output only, and MKVToolNix (mkvmerge) must be installed; not with burned-in\n"
        "subtitles, reverse, a frame rate change, a concatenated source or in the packaged FastFlix\n"
        "(then one encoder is used and the log says why).",
    ),
    (
        "chunk_threads",
        "Threads per chunk",
        [("1", 1), ("2", 2), ("3", 3), ("4", 4), ("6", 6), ("8", 8)],
        "Threads of each chunk encoder (chunks x threads = the CPU threads used).",
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


def column(*layouts):
    """The layouts stacked top-down, pushed to the top of their grid cell."""
    stack = QtWidgets.QVBoxLayout()
    for layout in layouts:
        stack.addLayout(layout)
    stack.addStretch(1)
    return stack


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

        # Three columns of sections side by side: the panel is wide and short in the normal window size
        grid.addLayout(column(self.init_preset(), self.init_pix_fmt()), 0, 0)
        grid.addLayout(column(self.init_tier(), self.init_levels()), 0, 1)
        grid.addLayout(self.init_modes(), 0, 2)

        combos = {name: self._add_value_combo(name, label, choices, tip) for name, label, choices, tip in value_combos}

        compression = column(
            section_title("Compression"),
            *(
                combos[name]
                for name in (
                    "bim",
                    "cutree_pic_strength",
                    "cutree_lookahead_gops",
                    "adaptive_intra",
                    "period",
                    "refresh_type",
                    "mctf_level_tilt",
                )
            ),
        )
        grain = column(
            section_title("Grain / detail retention"),
            self.init_tune_grain(),
            self.init_psy_slider("psy_rd", "Psy-RD", PSY_RD_TIP),
            self.init_psy_slider("psy_rdoq", "Psy-RDOQ", PSY_RDOQ_TIP),
            self.init_level_lambda(),
            self.init_flat_inter_qp(),
            combos["fga"],
            self.init_fga_strength(),
            combos["fga_interval"],
        )
        speed_and_advanced = column(
            section_title("Speed"),
            *(combos[name] for name in ("threads", "num_parallel_gops", "ifp", "chunks", "chunk_threads")),
            self.init_same_output_any_threads(),
            section_title("Advanced"),
            self.init_subjopt(),
            self.init_max_mux(),
        )
        for index, section in enumerate((compression, grain, speed_and_advanced)):
            grid.addLayout(section, 1, index)
            grid.setColumnStretch(index, 1)

        grid.addLayout(self.init_vvc_params(), 2, 0, 1, 3)
        grid.setRowStretch(3, True)
        grid.addLayout(self._add_custom(), 4, 0, 1, 3)

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
            label="Same output, any threads",
            tooltip="mtprofile=0: VVenC's automatic multi-threading (tiles, IFP) is off,\n"
            "so the output does not depend on the number of threads.",
            widget_name="same_output_any_threads",
            opt="same_output_any_threads",
        )

    def init_tune_grain(self):
        layout = self._add_check_box(
            label="Tune for film grain",
            tooltip=TUNE_GRAIN_TIP,
            widget_name="tune_grain",
            opt="tune_grain",
            connect=lambda: self.value_combo_changed(),
        )
        # clicked, not toggled: only the user's tick moves the sliders, not a profile or queue restore
        self.widgets.tune_grain.clicked.connect(lambda checked: checked and self.set_psy_sliders(TUNE_PSY_VALUE))
        return layout

    def init_psy_slider(self, name, label, tooltip):
        """A 0-3 slider in steps of 0.05 with a label showing Off or the value. Kept out of self.opts (the shared
        restore code has no sliders); restored by show_psy_settings."""
        layout = QtWidgets.QHBoxLayout()
        self.labels[name] = QtWidgets.QLabel(t(label))
        self.labels[name].setToolTip(self.translate_tip(tooltip))
        slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        slider.setRange(0, round(PSY_SLIDER_MAX * PSY_SLIDER_STEPS_PER_UNIT))
        slider.setTickPosition(QtWidgets.QSlider.TickPosition.TicksBelow)
        slider.setTickInterval(PSY_SLIDER_STEPS_PER_UNIT)  # a tick at every whole value, 1.0 recommended
        slider.setToolTip(self.translate_tip(tooltip))
        value_label = QtWidgets.QLabel()
        value_label.setFixedWidth(40)
        self.widgets[name] = slider
        self.labels[f"{name}_value"] = value_label
        slider.valueChanged.connect(lambda: self.psy_slider_changed(name))
        self.set_psy_slider(name, self.app.fastflix.config.encoder_opt(self.profile_name, name))
        layout.addWidget(self.labels[name])
        layout.addWidget(slider, stretch=1)
        layout.addWidget(value_label)
        return layout

    def psy_value(self, name) -> float:
        return self.widgets[name].value() / PSY_SLIDER_STEPS_PER_UNIT

    def show_psy_value(self, name):
        value = self.psy_value(name)
        self.labels[f"{name}_value"].setText(f"{value:g}" if value else t("Off"))

    def set_psy_slider(self, name, value):
        slider = self.widgets[name]
        slider.blockSignals(True)
        slider.setValue(round((value or 0) * PSY_SLIDER_STEPS_PER_UNIT))
        slider.blockSignals(False)
        self.show_psy_value(name)

    def set_psy_sliders(self, value):
        for name in ("psy_rd", "psy_rdoq"):
            self.set_psy_slider(name, value)
        self.main.page_update(build_thumbnail=False)

    def psy_slider_changed(self, name):
        self.show_psy_value(name)
        self.main.page_update(build_thumbnail=False)

    def init_level_lambda(self):
        layout = QtWidgets.QHBoxLayout()
        tooltip = (
            "LevelLambda: below 1 keeps small coefficients (fine texture) more often. Empty = the encoder's 1.0,\n"
            "or the tune's 0.5 with the film grain tune."
        )
        self.labels.level_lambda = QtWidgets.QLabel(t("Level lambda"))
        self.labels.level_lambda.setToolTip(self.translate_tip(tooltip))
        self.widgets.level_lambda = QtWidgets.QLineEdit()
        self.widgets.level_lambda.setFixedWidth(60)
        self.widgets.level_lambda.setToolTip(self.translate_tip(tooltip))
        validator = QtGui.QDoubleValidator(0, MAX_LEVEL_LAMBDA, 2)
        validator.setLocale(QtCore.QLocale.c())  # a dot as the decimal separator
        self.widgets.level_lambda.setValidator(validator)
        self.show_level_lambda(self.app.fastflix.config.encoder_opt(self.profile_name, "level_lambda"))
        self.widgets.level_lambda.textChanged.connect(lambda: self.main.page_update(build_thumbnail=False))
        layout.addWidget(self.labels.level_lambda)
        layout.addWidget(self.widgets.level_lambda)
        layout.addStretch(1)
        return layout

    def show_level_lambda(self, value):
        self.widgets.level_lambda.blockSignals(True)
        self.widgets.level_lambda.setText("" if value is None else f"{value:g}")
        self.widgets.level_lambda.blockSignals(False)

    def level_lambda(self):
        """The typed LevelLambda, or None (the encoder's value) while empty or out of VVenC's (0, 3.98] range."""
        try:
            value = float(self.widgets.level_lambda.text())
        except ValueError:
            return None
        return value if 0 < value <= MAX_LEVEL_LAMBDA else None

    def init_flat_inter_qp(self):
        layout = QtWidgets.QHBoxLayout()
        tooltip = (
            "FlatInterQP: the same QP for all inter pictures (the film grain tune turns it on).\n"
            "VVenC needs Perceptual QPA off for it, so On also turns QPA off."
        )
        self.labels.flat_inter_qp = QtWidgets.QLabel(t("Flat inter QPs"))
        self.labels.flat_inter_qp.setToolTip(self.translate_tip(tooltip))
        combo = QtWidgets.QComboBox()
        for text in ("Default", "On", "Off"):
            combo.addItem(t(text))
        combo.setToolTip(self.translate_tip(tooltip))
        self.widgets.flat_inter_qp = combo
        self.show_flat_inter_qp(self.app.fastflix.config.encoder_opt(self.profile_name, "flat_inter_qp"))
        combo.currentIndexChanged.connect(lambda: self.main.page_update(build_thumbnail=False))
        layout.addWidget(self.labels.flat_inter_qp)
        layout.addWidget(combo)
        return layout

    def show_flat_inter_qp(self, value):
        self.widgets.flat_inter_qp.blockSignals(True)
        self.widgets.flat_inter_qp.setCurrentIndex(0 if value is None else (1 if value else 2))
        self.widgets.flat_inter_qp.blockSignals(False)

    def flat_inter_qp(self):
        return {0: None, 1: True, 2: False}[self.widgets.flat_inter_qp.currentIndex()]

    def show_psy_settings(self, value_of):
        """Restore the grain widgets that the shared restore code does not handle (the tune checkbox it does)."""
        for name in ("psy_rd", "psy_rdoq"):
            self.set_psy_slider(name, value_of(name))
        self.show_level_lambda(value_of("level_lambda"))
        self.show_flat_inter_qp(value_of("flat_inter_qp"))

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
        # The shared code creates the mode widgets and their logic; rate_grid lays them out. Its layout is kept until
        # then: dropping it deletes its group boxes and every widget still in them.
        shared_layout = self._add_modes(recommended_bitrates, ["Custom"], qp_name="qp", qp_text_box_only=True)
        qp_validator = QtGui.QDoubleValidator(0, MAX_QP, 2)
        qp_validator.setLocale(QtCore.QLocale.c())  # a dot as the decimal separator
        self.widgets.custom_qp.setValidator(qp_validator)
        self.widgets.custom_qp.setToolTip(t("QP 0-63; decimals such as 27.5 encode between two QPs"))
        self.widgets.custom_qp.setText(qp_text(self.app.fastflix.config.encoder_opt(self.profile_name, "qp")))
        grid = self.rate_grid()
        del shared_layout
        return grid

    def rate_grid(self):
        """The QP and bitrate rows in one grid (radio | bitrate preset | "Custom:" | value | unit), so the two value
        boxes line up and each row is one control tall next to the preset and bit depth rows. The shared _add_modes
        puts each row in its own padded group box, which needs more height and leaves the boxes unaligned."""
        grid = QtWidgets.QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        rows = (
            (self.qp_radio, None, None, self.widgets.custom_qp, None),
            (
                self.bitrate_radio,
                self.widgets.bitrate,
                QtWidgets.QLabel(t("Custom") + ":"),
                self.widgets.custom_bitrate,
                QtWidgets.QLabel("k"),
            ),
        )
        for row, widgets in enumerate(rows):
            for col, widget in enumerate(widgets):
                if widget is not None:
                    # Out of _add_modes' group box (discarded with its layout) into this panel
                    widget.setParent(self)
                    grid.addWidget(widget, row, col)
        for radio in (self.qp_radio, self.bitrate_radio):
            # _add_modes fixes their width; in a shared column they take the width of "Bitrate" instead
            radio.setMinimumWidth(0)
            radio.setMaximumWidth(QWIDGETSIZE_MAX)
        grid.setColumnStretch(1, 1)
        return grid

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
        bitrate_mode = self.mode.lower() == "bitrate"
        rate_control = bitrate_mode or bool(video and video.video_settings.maxrate)
        # VVenC refuses the tune's CU-tree picture QPs with a maxrate in QP mode (capped QP)
        capped_qp = rate_control and not bitrate_mode
        self.widgets.tune_grain.setEnabled(not capped_qp)
        self.widgets.tune_grain.setToolTip(self.translate_tip(CAPPED_QP_TIP if capped_qp else TUNE_GRAIN_TIP))
        # The tune decides these itself; a greyed-out tune (capped QP) is not sent and leaves them to FastFlix
        tune = self.widgets.tune_grain.isChecked() and not capped_qp
        cutree = self.widgets.bim.currentData() == 1
        picture_qps = not rate_control and cutree and not tune
        self.widgets.bim.setEnabled(not tune)
        self.widgets.cutree_pic_strength.setEnabled(picture_qps)
        # The look-ahead only pays together with the picture QPs (the tune turns them on), so it is sent only with them
        self.widgets.cutree_lookahead_gops.setEnabled(
            tune or (picture_qps and self.widgets.cutree_pic_strength.currentData() > 0)
        )
        self.widgets.mctf_level_tilt.setEnabled(not tune)
        self.widgets.subjopt.setEnabled(not tune)
        self.widgets.num_parallel_gops.setEnabled(not rate_control)
        # Chunks need constant QP; while chunking, every encoder runs the chunk threads
        chunked = not rate_control and self.widgets.chunks.currentData() > 1
        self.widgets.chunks.setEnabled(not rate_control)
        self.widgets.chunk_threads.setEnabled(chunked)
        self.widgets.threads.setEnabled(not chunked)
        # Film grain analysis runs inside MCTF, which the tune turns off: VVenC refuses fga with it
        self.widgets.fga.setEnabled(not tune)
        grain_analysis = not tune and self.widgets.fga.currentData() != 0
        self.widgets.fga_strength.setEnabled(grain_analysis)
        self.widgets.fga_interval.setEnabled(grain_analysis)

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
            tune_grain=self.widgets.tune_grain.isChecked(),
            psy_rd=self.psy_value("psy_rd"),
            psy_rdoq=self.psy_value("psy_rdoq"),
            level_lambda=self.level_lambda(),
            flat_inter_qp=self.flat_inter_qp(),
            **{name: self.widgets[name].currentData() for name, *_ in value_combos},
        )

        encode_type, q_value = self.get_mode_settings()
        settings.qp = self.checked_qp(q_value) if encode_type == "qp" else None
        settings.bitrate = q_value if encode_type == "bitrate" else None
        self.app.fastflix.current_video.video_settings.video_encoder_settings = settings

    def update_profile(self):
        super().update_profile()
        self.show_value_combos(lambda name: self.app.fastflix.config.encoder_opt(self.profile_name, name))
        self.show_psy_settings(lambda name: self.app.fastflix.config.encoder_opt(self.profile_name, name))
        self.widgets.custom_qp.setText(qp_text(self.app.fastflix.config.encoder_opt(self.profile_name, "qp")))
        self.update_dependent_options()

    def reload(self):
        super().reload()
        settings = self.app.fastflix.current_video.video_settings.video_encoder_settings
        self.show_value_combos(lambda name: getattr(settings, name))
        self.show_psy_settings(lambda name: getattr(settings, name))
        self.widgets.custom_qp.setText(qp_text(settings.qp))
        self.update_dependent_options()

    def set_mode(self, x):
        self.mode = x.text()
        self.update_dependent_options()
        self.main.build_commands()

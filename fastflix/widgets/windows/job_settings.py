# -*- coding: utf-8 -*-
"""Read-only view of a queued or running job's settings and the exact commands it runs.

Since the loaded source stays editable while the queue runs, the tabs no longer show what the running job
uses; this window does."""

from PySide6 import QtCore, QtGui, QtWidgets

from fastflix.language import t
from fastflix.models.video import Video
from fastflix.widgets.panels.command_panel import _command_to_display_string

__all__ = ["JobSettingsWindow", "job_settings_text"]


def _audio_line(track) -> str:
    title = f" {track.title}" if track.title else ""
    head = f"#{track.index} {track.language or '-'}{title}: {track.codec}"
    if not track.conversion_codec:
        return f"{head} -> copy"
    if track.conversion_bitrate:
        quality = f"bitrate {track.conversion_bitrate}"
    elif track.conversion_aq is not None:
        quality = f"quality {track.conversion_aq}"
    else:
        quality = ""
    keeps_layout = not track.downmix or track.downmix == "No Downmix"  # the audio builder's "no downmix" value
    layout = f"{track.channels} channels" if keeps_layout else track.downmix
    return f"{head} -> {', '.join(x for x in (track.conversion_codec, quality, layout) if x)}"


def _subtitle_line(track) -> str:
    extras = [x for x, on in ((t("burn-in"), track.burn_in), (t("external"), track.external)) if on]
    return f"#{track.index} {track.language or '-'} {track.subtitle_type}" + "".join(f", {x}" for x in extras)


def job_settings_text(video: Video) -> str:
    """Plain text of everything that decides the job's output: encoder settings, tracks and the commands."""
    settings = video.video_settings
    encoder = settings.video_encoder_settings
    lines = [
        f"{t('Source')}: {video.source}",
        f"{t('Output')}: {settings.output_path}",
        f"{t('Encoder')}: {encoder.name if encoder else '-'}",
    ]

    def section(title, entries):
        if entries:
            lines.extend(["", f"{title}:", *(f"  {entry}" for entry in entries)])

    if encoder:
        section(t("Encoder Settings"), [f"{k}: {v}" for k, v in encoder.model_dump().items() if k != "name"])
    section(t("Audio"), [_audio_line(x) for x in video.audio_tracks if x.enabled])
    section(t("Subtitles"), [_subtitle_line(x) for x in video.subtitle_tracks if x.enabled])
    section(
        t("Commands"),
        [f"{i}. {_command_to_display_string(c.command)}" for i, c in enumerate(settings.conversion_commands, 1)],
    )
    return "\n".join(lines)


class JobSettingsWindow(QtWidgets.QWidget):
    """Owned by the main window (closes with it, survives queue rows being rebuilt); deletes itself on close."""

    def __init__(self, video: Video, parent: QtWidgets.QWidget):
        super().__init__(parent, QtCore.Qt.WindowType.Window)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"{t('Settings')}: {video.video_settings.output_path.name}")
        self.resize(900, 600)

        self.text = QtWidgets.QPlainTextEdit(job_settings_text(video))
        self.text.setReadOnly(True)
        self.text.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))

        copy_button = QtWidgets.QPushButton(t("Copy"))
        copy_button.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(self.text.toPlainText()))
        close_button = QtWidgets.QPushButton(t("Close"))
        close_button.clicked.connect(self.close)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(copy_button)
        buttons.addWidget(close_button)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.text)
        layout.addLayout(buttons)
        self.setLayout(layout)

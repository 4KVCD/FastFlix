# -*- coding: utf-8 -*-
from unittest import mock

from PySide6 import QtWidgets

from fastflix.widgets.status_bar import STATE_COMPLETE, STATE_ENCODING, STATE_IDLE, StatusBarWidget


def test_progress_bar_shows_from_the_start_of_an_encode():
    """A slow encode stays under 1 % for minutes: the bar shows 0 % then, and goes once the queue is done."""
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    bar = StatusBarWidget(mock.MagicMock())
    bar.show()
    bar.set_state(STATE_ENCODING, "Encoding: clip")
    bar.set_progress(0)
    assert bar.progress_bar.isVisible() and bar.progress_bar.value() == 0
    bar.set_progress(0)  # end of the queue: reset while still encoding, then the final state
    bar.set_state(STATE_COMPLETE, "All conversions complete")
    assert not bar.progress_bar.isVisible()
    bar.set_state(STATE_IDLE)
    bar.set_progress(0)
    assert not bar.progress_bar.isVisible()

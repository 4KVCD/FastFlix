# -*- coding: utf-8 -*-
"""
Tests for the keep_source_after_encode setting (#677).

Verifies:
- Config field defaults to True
- Config round-trips through serialization
- add_to_queue() clears video when setting is off
- add_to_queue() keeps video when setting is on (default)
- the kept source and waiting queue jobs stay editable during an encode
"""

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tempfile

import pytest

from fastflix.models.config import Config


@pytest.fixture
def config():
    return Config(
        version="4.0.0",
        ffmpeg=Path("ffmpeg"),
        ffprobe=Path("ffprobe"),
        work_path=Path("work_path"),
    )


class TestConfigField:
    def test_default_is_true(self, config):
        assert config.keep_source_after_encode is True

    def test_can_set_false(self, config):
        config.keep_source_after_encode = False
        assert config.keep_source_after_encode is False

    def test_round_trip_model_dump(self, config):
        config.keep_source_after_encode = False
        data = config.model_dump()
        assert data["keep_source_after_encode"] is False

    def test_round_trip_model_dump_default(self, config):
        data = config.model_dump()
        assert data["keep_source_after_encode"] is True


class TestAddToQueueBehavior:
    """Test that add_to_queue respects the keep_source_after_encode setting."""

    @pytest.fixture
    def mock_main(self, config):
        """Create a mock Main widget with the encoding mixin behavior."""
        main = mock.MagicMock()
        main.app.fastflix.config = config
        main.app.fastflix.current_video = mock.MagicMock()
        main.video_options.queue.add_to_queue.return_value = None
        return main

    def test_clears_video_when_setting_off(self, mock_main):
        """Default behavior: clear source after adding to queue."""
        mock_main.app.fastflix.config.keep_source_after_encode = False

        from fastflix.widgets.main_encoding import EncodingMixin

        mixin = EncodingMixin()
        mixin.__dict__.update(
            {
                "app": mock_main.app,
                "video_options": mock_main.video_options,
                "clear_current_video": mock_main.clear_current_video,
            }
        )
        result = EncodingMixin.add_to_queue(mixin)

        assert result is True
        mock_main.clear_current_video.assert_called_once()

    def test_keeps_video_when_setting_on(self, mock_main):
        """With setting enabled: keep source after adding to queue."""
        mock_main.app.fastflix.config.keep_source_after_encode = True

        from fastflix.widgets.main_encoding import EncodingMixin

        mixin = EncodingMixin()
        mixin.__dict__.update(
            {
                "app": mock_main.app,
                "video_options": mock_main.video_options,
                "clear_current_video": mock_main.clear_current_video,
                "get_temp_work_path": lambda: Path(tempfile.mkdtemp()),
                "generate_output_filename": ("C:/videos/", "movie-fastflix-b2c3"),
                "output_video_path_widget": mock_main.output_video_path_widget,
                "_update_truncation_warning": mock_main._update_truncation_warning,
            }
        )
        old_uuid = mock_main.app.fastflix.current_video.uuid
        result = EncodingMixin.add_to_queue(mixin)

        assert result is True
        mock_main.clear_current_video.assert_not_called()
        # kept source gets a fresh identity so it can't collide with the queued copy
        assert mock_main.app.fastflix.current_video.uuid != old_uuid
        # and a new output name, so queueing it again is not "already in queue"
        mock_main.output_video_path_widget.setText.assert_called_once_with("movie-fastflix-b2c3")

    def test_no_clear_on_error(self, mock_main):
        """If add_to_queue raises, clear_current_video should not be called."""
        from fastflix.exceptions import FastFlixInternalException
        from fastflix.widgets.main_encoding import EncodingMixin

        mock_main.video_options.queue.add_to_queue.side_effect = FastFlixInternalException("test error")

        mixin = EncodingMixin()
        mixin.__dict__.update(
            {
                "app": mock_main.app,
                "video_options": mock_main.video_options,
                "clear_current_video": mock_main.clear_current_video,
            }
        )

        with mock.patch("fastflix.widgets.main_encoding.error_message"):
            result = EncodingMixin.add_to_queue(mixin)

        assert result is None
        mock_main.clear_current_video.assert_not_called()

    def test_no_clear_when_queue_returns_code(self, mock_main):
        """If queue.add_to_queue returns a non-None code, clear is not reached."""
        from fastflix.widgets.main_encoding import EncodingMixin

        mock_main.video_options.queue.add_to_queue.return_value = False

        mixin = EncodingMixin()
        mixin.__dict__.update(
            {
                "app": mock_main.app,
                "video_options": mock_main.video_options,
                "clear_current_video": mock_main.clear_current_video,
            }
        )
        result = EncodingMixin.add_to_queue(mixin)

        assert result is False
        mock_main.clear_current_video.assert_not_called()


class TestEditDuringEncode:
    """The kept source and the queued jobs stay editable while the queue runs."""

    def test_encode_start_leaves_kept_source_unlocked(self, config):
        from fastflix.widgets.main_encoding import EncodingMixin

        main = mock.MagicMock()
        main.app.fastflix.config = config
        main.app.fastflix.currently_encoding = False
        main.app.fastflix.conversion_paused = False
        queued = mock.MagicMock()
        queued.status.ready = True
        main.app.fastflix.conversion_list = [queued]

        mixin = EncodingMixin()
        mixin.__dict__.update(
            {
                name: getattr(main, name)
                for name in (
                    "app",
                    "video_options",
                    "disable_all",
                    "set_convert_button",
                    "send_video_request_to_worker_queue",
                    "encoding_status_signal",
                    "encoding_progress_signal",
                )
            }
        )
        # "Add current video to queue?" -> No: start the queue with the source still loaded
        with (
            mock.patch("fastflix.widgets.main_encoding.yes_no_message", return_value=False),
            mock.patch("fastflix.widgets.main_encoding.prevent_sleep_mode"),
        ):
            EncodingMixin.encode_video(mixin)

        main.send_video_request_to_worker_queue.assert_called_once_with(queued)
        main.disable_all.assert_not_called()

    @pytest.fixture
    def queue(self):
        return SimpleNamespace(app=mock.MagicMock(), main=mock.MagicMock(), remove_item=mock.MagicMock())

    @staticmethod
    def edit(queue, video, discard=True):
        from fastflix.widgets.panels.queue_panel import EncodingQueue

        with mock.patch("fastflix.widgets.panels.queue_panel.yes_no_message", return_value=discard) as asked:
            EncodingQueue.reload_from_queue(queue, video)
        return asked

    def test_edit_waiting_job_during_encode(self, queue):
        queue.app.fastflix.currently_encoding = True
        video = mock.MagicMock()
        video.status.running = False

        asked = self.edit(queue, video)

        asked.assert_called_once()  # the kept source is about to be replaced
        loaded = queue.main.reload_video_from_queue.call_args.args[0]
        assert loaded is not video  # the window gets a copy, never the queue's own object
        queue.remove_item.assert_called_once_with(video)

    def test_running_job_is_not_edited(self, queue):
        video = mock.MagicMock()
        video.status.running = True

        self.edit(queue, video)

        queue.main.reload_video_from_queue.assert_not_called()
        queue.remove_item.assert_not_called()

    def test_keeping_the_source_cancels_edit(self, queue):
        video = mock.MagicMock()
        video.status.running = False

        self.edit(queue, video, discard=False)

        queue.main.reload_video_from_queue.assert_not_called()
        queue.remove_item.assert_not_called()

    def test_refused_reload_keeps_job_queued(self, queue):
        from fastflix.exceptions import FastFlixInternalException

        video = mock.MagicMock()
        video.status.running = False
        queue.main.reload_video_from_queue.side_effect = FastFlixInternalException("encoder gone")

        self.edit(queue, video)

        queue.remove_item.assert_not_called()

    def test_failed_reload_keeps_job_queued(self, queue):
        video = mock.MagicMock()
        video.status.running = False
        queue.main.reload_video_from_queue.side_effect = ValueError("bad track")

        with mock.patch("fastflix.widgets.panels.queue_panel.error_message") as shown:
            self.edit(queue, video)

        queue.main.clear_current_video.assert_called_once()  # no half-loaded source left behind
        shown.assert_called_once()
        queue.remove_item.assert_not_called()

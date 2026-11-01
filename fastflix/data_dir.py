# -*- coding: utf-8 -*-
import os
from pathlib import Path

from platformdirs import user_data_dir


def fastflix_data_dir() -> Path:
    """Folder for FastFlix's config, queue, history and logs.

    FF_DATA_DIR overrides the user's app-data folder, so a second FastFlix (another build) can run with its own
    settings and queue instead of sharing them.
    """
    data_dir = os.getenv("FF_DATA_DIR")
    if data_dir:
        return Path(data_dir)
    return Path(user_data_dir("FastFlix", appauthor=False, roaming=True))

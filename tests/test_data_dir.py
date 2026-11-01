# -*- coding: utf-8 -*-
from pathlib import Path

from fastflix.data_dir import fastflix_data_dir
from fastflix.models.config import get_config


def test_ff_data_dir_moves_data_and_config(monkeypatch, tmp_path):
    """FF_DATA_DIR moves the data folder and the config file in it (the queue and log paths derive from the same
    helper when their modules are imported)."""
    monkeypatch.setenv("FF_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FF_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # no portable fastflix.yaml in the working directory

    assert fastflix_data_dir() == tmp_path
    assert get_config() == tmp_path / "fastflix.yaml"


def test_default_data_dir_is_app_data(monkeypatch):
    monkeypatch.delenv("FF_DATA_DIR", raising=False)

    assert fastflix_data_dir().name == "FastFlix"
    assert fastflix_data_dir() != Path.cwd()

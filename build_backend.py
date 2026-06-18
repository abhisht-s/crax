from __future__ import annotations

from setuptools import build_meta as _setuptools_build_meta


def __getattr__(name: str):
    return getattr(_setuptools_build_meta, name)


def get_requires_for_build_editable(config_settings=None):
    return _setuptools_build_meta.get_requires_for_build_wheel(config_settings)


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    return _setuptools_build_meta.prepare_metadata_for_build_wheel(
        metadata_directory,
        config_settings,
    )


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return _setuptools_build_meta.build_wheel(
        wheel_directory,
        config_settings,
        metadata_directory,
    )

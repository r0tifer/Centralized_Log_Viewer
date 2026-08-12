"""Centralized Log Viewer."""

#: Keep in step with [tool.poetry] version in pyproject.toml.
#: tests/test_version.py fails the build if the two disagree.
#: A literal rather than importlib.metadata, because the PyInstaller bundle has
#: no distribution metadata to read.
__version__ = "2.3.3"

__all__ = ["__version__"]

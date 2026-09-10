"""Only explicit path strings under the simulator's output roots are accepted."""

import os
import tempfile

import pytest

from api import _is_allowed_file_output_dir


@pytest.mark.parametrize("value", [None, True, 17, {}, [], "", "   ", "/tmp\0invalid", "/tmp-unrelated/file"])
def test_invalid_output_directory_is_rejected_without_raising(value):
    assert _is_allowed_file_output_dir(value) is False


def test_real_temporary_directory_is_allowed():
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        assert _is_allowed_file_output_dir(directory)
        assert _is_allowed_file_output_dir(os.path.realpath(directory))

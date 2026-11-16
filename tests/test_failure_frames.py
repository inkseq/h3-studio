"""Fatal service errors name where they were raised, never their message."""

import unittest

from studio_platform.failure_frames import frames


def _inner():
    raise RuntimeError("tenant-secret job-123 provider detail")


def _outer():
    _inner()


class FailureFramesTests(unittest.TestCase):
    def test_innermost_frame_first_without_the_message(self):
        try:
            _outer()
        except RuntimeError as error:
            where = frames(error)
        self.assertTrue(where.startswith("test_failure_frames.py:"), where)
        self.assertIn(":_inner < test_failure_frames.py:", where)
        self.assertIn(":_outer", where)
        self.assertNotIn("secret", where)
        self.assertNotIn("job-123", where)

    def test_an_error_never_raised_has_no_frames(self):
        self.assertEqual(frames(RuntimeError("x")), "unknown")

    def test_limit_keeps_the_innermost(self):
        try:
            _outer()
        except RuntimeError as error:
            self.assertEqual(frames(error, limit=1).count(" < "), 0)
            self.assertIn("_inner", frames(error, limit=1))


if __name__ == "__main__":
    unittest.main()

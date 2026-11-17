import logging
import unittest

from studio_platform import phase_timing


class PhaseTimingTests(unittest.TestCase):
    def test_inner_laps_join_the_outer_line_and_do_nothing_outside(self):
        phase_timing.lap("ignored")  # No timer: no error, no line.
        with self.assertLogs("studio_platform.timing", logging.INFO) as logs:
            with phase_timing.PhaseTimer("demo_timing") as timer:
                timer.lap("load")
                phase_timing.lap("inner")
                phase_timing.lap("inner")
        (line,) = logs.output
        self.assertIn("demo_timing total_ms=", line)
        self.assertIn("load_ms=", line)
        self.assertEqual(line.count("inner_ms="), 1)
        phase_timing.lap("after")  # The timer is no longer current.

    def test_the_line_is_written_when_the_block_raises(self):
        with self.assertLogs("studio_platform.timing", logging.INFO) as logs:
            with self.assertRaises(ValueError):
                with phase_timing.PhaseTimer("demo_timing"):
                    raise ValueError
        self.assertEqual(len(logs.output), 1)


if __name__ == "__main__":
    unittest.main()

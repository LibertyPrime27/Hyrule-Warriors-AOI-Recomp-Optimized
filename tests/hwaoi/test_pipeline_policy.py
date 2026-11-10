"""Interactive launches build pipelines in the background; evidence runs stay synchronous. No process execution."""
import argparse
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import hwaoi


class PipelinePolicyTests(unittest.TestCase):
    def test_normal_play_builds_pipelines_in_the_background(self):
        # A first-seen scene previously froze presentation for 10+ s while every pipeline compiled on the drawing
        # thread (HWAOI_PIPE_SYNC=1). Play now uses the async builder with boot prewarm, skipping only draws into
        # passes whose targets were all rendered the previous frame (one-shot targets still wait).
        self.assertEqual(hwaoi.pipeline_options(diagnostic=False, force_sync=False),
                         {"HWAOI_PIPE_SYNC": "0", "HWAOI_PIPE_SKIP": "strict", "HWAOI_PIPE_PREBUILD": "1"})

    def test_diagnostic_and_explicit_sync_runs_keep_deterministic_draws(self):
        for diagnostic, force in ((True, False), (False, True), (True, True)):
            with self.subTest(diagnostic=diagnostic, force=force):
                self.assertEqual(hwaoi.pipeline_options(diagnostic=diagnostic, force_sync=force), {"HWAOI_PIPE_SYNC": "1"})

    def test_launch_accepts_pipe_sync_flag(self):
        parser = hwaoi.build_parser()
        self.assertFalse(parser.parse_args(["launch"]).pipe_sync)
        self.assertTrue(parser.parse_args(["launch", "--pipe-sync"]).pipe_sync)


if __name__ == "__main__":
    unittest.main()

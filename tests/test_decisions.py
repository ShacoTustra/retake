"""python -m unittest discover tests"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retake"))
import runtime  # noqa: E402

runtime.setup_tools()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["RETAKE_HOME"] = self.tmp.name
        import projects
        self.projects = projects
        self.p = projects.Project("t")
        self.p.dir.mkdir(parents=True)
        self.p.save_meta(id="t", name="t", source="/nonexistent.mp4", created=1, duration=100)
        doc = {"source": "/nonexistent.mp4", "range": [0, 100], "words": [], "phrases": [], "edits": [
            {"id": 1, "kind": "retake", "start": 10.0, "end": 20.0, "on": True},
            {"id": 2, "kind": "pause", "start": 30.0, "end": 32.0, "on": True},
            {"id": 3, "kind": "stutter", "start": 40.0, "end": 41.0, "on": False},
        ]}
        (self.p.dir / "edits.json").write_text(json.dumps(doc))

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("RETAKE_HOME", None)

    def final(self, eid):
        return next(e for e in self.p.final_edits() if e["id"] == eid)


class Decisions(Base):
    def test_trim_toggle_and_reviewed_are_applied(self):
        self.p.save_decisions({"overrides": {
            "1": {"on": True, "start": 11.0, "end": 19.5, "orig_start": 10.0, "orig_end": 20.0, "reviewed": True},
            "3": {"on": True, "start": 40.0, "end": 41.0, "orig_start": 40.0, "orig_end": 41.0},
        }})
        e = self.final(1)
        self.assertEqual((e["start"], e["end"], e["reviewed"], e["orig_start"], e["orig_end"]), (11.0, 19.5, True, 10.0, 20.0))
        self.assertTrue(self.final(3)["on"])

    def test_file_from_before_trimming_still_works(self):
        (self.p.dir / "decisions.json").write_text(json.dumps(
            {"overrides": {"1": {"on": False, "start": 10.0, "end": 20.0}}, "manual": []}))
        e = self.final(1)
        self.assertFalse(e["on"])
        self.assertEqual((e["start"], e["end"]), (10.0, 20.0))

    def test_a_decision_does_not_follow_an_id_to_another_cut(self):
        self.p.save_decisions({"overrides": {
            "1": {"on": False, "start": 55.0, "end": 56.0, "orig_start": 50.0, "orig_end": 60.0}}})
        e = self.final(1)
        self.assertTrue(e["on"])
        self.assertEqual(e["start"], 10.0)

    def test_manual_cuts_and_splits(self):
        self.p.save_decisions({"manual": [{"id": 1000000, "start": 70, "end": 75, "on": True, "removed": "текст"},
                                          {"id": 1000001, "start": 80, "end": 80}],
                               "splits": [90.5, 85]})
        d = self.p.decisions()
        self.assertEqual(d["splits"], [85.0, 90.5])
        self.assertEqual([m["id"] for m in d["manual"]], [1000000])
        self.assertEqual(self.final(1000000)["removed"], "текст")

    def test_end_before_start_is_refused(self):
        with self.assertRaises(ValueError):
            self.p.save_decisions({"overrides": {"1": {"on": True, "start": 20.0, "end": 10.0}}})

    def test_the_render_cuts_what_the_editor_shows(self):
        import render
        self.p.save_decisions({"overrides": {
            "1": {"on": True, "start": 12.0, "end": 18.0, "orig_start": 10.0, "orig_end": 20.0},
            "2": {"on": False, "start": 30.0, "end": 32.0, "orig_start": 30.0, "orig_end": 32.0}},
            "manual": [{"id": 1000000, "start": 70, "end": 75, "on": True}]})
        keep = render.keep_segments(self.p.final_edits(), 0, 100, Fraction(25))
        self.assertEqual([(a / 25, b / 25) for a, b in keep], [(0, 12), (18, 70), (75, 100)])

    def test_summary_counts_the_trimmed_length(self):
        self.p.save_decisions({"overrides": {
            "1": {"on": True, "start": 12.0, "end": 18.0, "orig_start": 10.0, "orig_end": 20.0}}})
        self.assertAlmostEqual(self.p.summary()["result"]["cut"], 6 + 2)


class Timeline(Base):
    def test_peaks_scale(self):
        env = np.array([-180.0, -80.0, -40.0, 0.0, 5.0])
        np.savez(self.p.dir / "media.npz", env=env, frozen=np.zeros((0, 2)), t0=0.0, t1=100.0)
        self.assertEqual(list(self.p.peaks()), [0, 0, 127, 255, 255])

    def test_filmstrip(self):
        v = Path(self.tmp.name) / "v.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x180:r=10:d=12",
                        "-g", "20", str(v)], check=True)
        self.p.save_meta(source=str(v), duration=12.0)
        self.assertIsNone(self.p.thumbs())
        meta = self.p.make_thumbs()
        self.assertEqual(self.p.thumbs(), meta)
        self.assertEqual(meta["w"] / meta["h"], 320 / 180)
        self.assertEqual(meta["interval"], 5.0)
        size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                               "-of", "csv=p=0", str(self.p.dir / "thumbs.jpg")], capture_output=True, text=True, check=True).stdout
        self.assertEqual(size.strip(), f"{meta['w'] * meta['cols']},{meta['h'] * meta['rows']}")


if __name__ == "__main__":
    unittest.main()

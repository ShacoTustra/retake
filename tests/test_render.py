"""Rendering sources whose frames are not evenly spaced, like every MediaRecorder file."""
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retake"))
import render  # noqa: E402
import runtime  # noqa: E402

runtime.setup_tools()


def run(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def make_vfr(path: Path, opus: bool = False):
    """10 fps, then 30 fps, 12 s; timescale and fragments as MediaRecorder writes them.
    The picture changes with time, so a frame shows when it was taken."""
    audio = ["-c:a", "libopus"] if opus else ["-c:a", "aac"]
    run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=12", "-f", "lavfi", "-i", "sine=f=440:r=48000:d=12",
        "-vf", r"select='not(mod(n\,3))+gt(t\,6)'", "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "ultrafast",
        "-g", "60", "-video_track_timescale", "30000", *audio, "-movflags", "frag_keyframe+empty_moov", str(path))


def gray(path: Path, t: float) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                           "-vf", "scale=80:45,format=gray", "-f", "rawvideo", "-"],
                          capture_output=True, check=True).stdout


def diff(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))


class Rate(unittest.TestCase):
    def test_constant_sources_keep_their_rate(self):
        self.assertEqual(render.working_rate("25/1", "25/1"), (Fraction(25), False))
        self.assertEqual(render.working_rate("30000/1001", "30000/1001"), (Fraction(30000, 1001), False))
        self.assertEqual(render.working_rate("25/1", "2498/100"), (Fraction(25), False), "a dropped frame or two")
        self.assertEqual(render.working_rate("25/1", "0/0"), (Fraction(25), False))

    def test_a_timestamp_grid_is_not_a_frame_rate(self):
        self.assertEqual(render.working_rate("30000/1", "2216000/128917"), (Fraction(30), True))
        self.assertEqual(render.working_rate("60/1", "29/1"), (Fraction(30), True))
        self.assertEqual(render.working_rate("90000/1", "59/1"), (Fraction(60), True))

    def test_nothing_known(self):
        self.assertEqual(render.working_rate("0/0", "0/0"), (Fraction(30), True))
        self.assertEqual(render.working_rate(None, None), (Fraction(30), True))


class Variable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.src = cls.dir / "vfr.mp4"
        make_vfr(cls.src)
        cls.info = render.probe(cls.src)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def render(self, cuts, name):
        edits = [{"start": a, "end": b, "on": True} for a, b in cuts]
        keep = render.keep_segments(edits, 0, 12, self.info["fps"])
        out = self.dir / name
        render.render_mp4(self.src, keep, self.info["fps"], out, "x264", 12)
        return out

    def test_probe_sees_it(self):
        self.assertTrue(self.info["vfr"])
        self.assertEqual(self.info["fps"], Fraction(30))

    def test_picture_and_sound_have_the_expected_length(self):
        made = render.probe(self.render([(2, 4), (8, 9)], "len.mp4"))
        self.assertAlmostEqual(made["video_duration"], 9, delta=0.1)
        self.assertAlmostEqual(made["audio_duration"], 9, delta=0.1)
        self.assertFalse(made["vfr"], "the result has a constant rate")

    def test_frames_come_from_the_right_moment(self):
        out = self.render([(2, 4), (8, 9)], "frames.mp4")

        def closest(frame, t):
            # -ss gives the first frame at or after t, while the film shows the one before it: look back
            # by the longest frame of the source (0.1 s) to include what is on screen at t
            return min(diff(frame, gray(self.src, t + k)) for k in (-0.134, -0.034, 0.0))

        # result time -> source time, on the 10 fps part and on the 30 fps part
        for tau, t in [(1.0, 1.0), (3.0, 5.0), (5.5, 7.5), (6.5, 9.5), (8.0, 11.0)]:
            frame = gray(out, tau)
            right, wrong = closest(frame, t), closest(frame, t - 1.0)
            self.assertLess(right, 1.0, f"at {tau} s of the result, source {t} s")
            self.assertLess(right, wrong / 3, f"at {tau} s of the result, source {t} s: {right} vs {wrong}")

    def test_opus_in_fragments(self):
        src = self.dir / "opus.mp4"
        make_vfr(src, opus=True)
        info = render.probe(src)
        keep = render.keep_segments([{"start": 2, "end": 4, "on": True}], 0, 12, info["fps"])
        out = self.dir / "opus-out.mp4"
        render.render_mp4(src, keep, info["fps"], out, "x264", 12)
        self.assertAlmostEqual(render.probe(out)["video_duration"], 10, delta=0.1)

    def test_constant_rate_source_is_cut_to_the_frame(self):
        src = self.dir / "cfr.mp4"
        run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=6", "-f", "lavfi", "-i", "sine=f=440:r=48000:d=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src))
        info = render.probe(src)
        self.assertEqual((info["fps"], info["vfr"]), (Fraction(25), False))
        keep = render.keep_segments([{"start": 2, "end": 3, "on": True}], 0, 6, info["fps"])
        self.assertEqual(keep, [(0, 50), (75, 150)])
        out = self.dir / "cfr-out.mp4"
        render.render_mp4(src, keep, info["fps"], out, "x264", 6)
        made = render.probe(out)
        self.assertAlmostEqual(made["video_duration"], 5, delta=0.05)

    def test_a_broken_result_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "неполный"):
            render._check_lengths(self.src, self.src, 100.0)
        render._check_lengths(self.src, self.src, 12.0)

    def test_cutting_everything_says_so(self):
        with self.assertRaisesRegex(ValueError, "нечего"):
            render.render_mp4(self.src, [], self.info["fps"], self.dir / "none.mp4", "x264", 12)

    def test_fcpxml_names_a_real_format(self):
        out = self.dir / "t.fcpxml"
        render.write_fcpxml(self.src, [(0, 30), (60, 90)], self.info, out)
        text = out.read_text()
        self.assertIn('name="FFVideoFormat180p30"', text)
        self.assertEqual(text.count("<asset-clip"), 2)
        info = {**self.info, "fps": Fraction(30000, 1001)}
        render.write_fcpxml(self.src, [(0, 30)], info, out)
        self.assertIn('p2997"', out.read_text())


if __name__ == "__main__":
    unittest.main()

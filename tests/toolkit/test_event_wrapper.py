import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class EventWrapperTests(unittest.TestCase):
    def test_event_wrapper_keeps_schema_and_difference_semantics(self):
        with tempfile.TemporaryDirectory() as d:
            a=Path(d)/'a.jsonl';b=Path(d)/'b.jsonl'
            a.write_text('{"seq":1,"event":"start","state":{"n":1}}\n')
            b.write_text(a.read_text())
            cmd=[sys.executable,str(ROOT/'src/beaver/toolkit/compare_events.py'),str(a),str(b)]
            self.assertEqual(subprocess.run(cmd,capture_output=True).returncode,0)
            b.write_text('{"seq":1,"event":"start","state":{"n":2}}\n')
            self.assertEqual(subprocess.run(cmd,capture_output=True).returncode,1)
            b.write_text('{}\n');self.assertEqual(subprocess.run(cmd,capture_output=True).returncode,2)


if __name__ == '__main__':
    unittest.main()

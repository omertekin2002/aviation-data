import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_freshness import ERROR, FRESH, STALE, evaluate  # noqa: E402

NOW = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
EMPTY = {"total_count": 0, "workflow_runs": []}


def success_run(run_id, updated_at):
    return {
        "id": run_id,
        "status": "completed",
        "conclusion": "success",
        "head_branch": "main",
        "created_at": updated_at,
        "updated_at": updated_at,
    }


def payload(*runs):
    return {"total_count": len(runs), "workflow_runs": list(runs)}


class EvaluateTests(unittest.TestCase):
    def test_fresh_when_last_success_is_within_threshold(self):
        verdict, message = evaluate(
            payload(success_run(1, "2026-10-25T20:00:00Z")), [EMPTY], NOW, 12
        )
        self.assertEqual(verdict, FRESH)
        self.assertIn("2026-10-25 20:00 UTC", message)
        self.assertIn("4.0h ago", message)

    def test_stale_when_last_success_is_older_than_threshold(self):
        verdict, message = evaluate(
            payload(success_run(36619884051, "2026-09-29T19:34:09Z")),
            [EMPTY, EMPTY],
            NOW,
            12,
        )
        self.assertEqual(verdict, STALE)
        self.assertTrue(message.startswith("STALE:"))
        self.assertIn("36619884051", message)
        self.assertIn("2026-09-29 19:34 UTC", message)
        self.assertIn("No data-update runs are currently waiting", message)

    def test_uses_newest_success_when_several_are_returned(self):
        verdict, _ = evaluate(
            payload(
                success_run(1, "2026-09-29T19:34:09Z"),
                success_run(2, "2026-10-25T23:00:00Z"),
            ),
            [],
            NOW,
            12,
        )
        self.assertEqual(verdict, FRESH)

    def test_stale_when_there_are_no_successful_runs(self):
        verdict, message = evaluate(EMPTY, [EMPTY], NOW, 12)
        self.assertEqual(verdict, STALE)
        self.assertIn("no successful data-update run", message)

    def test_stale_message_names_waiting_and_pending_runs(self):
        waiting = {
            "id": 36644890155,
            "status": "waiting",
            "head_branch": "main",
            "created_at": "2026-09-29T23:22:54Z",
            "html_url": "https://github.com/o/r/actions/runs/36644890155",
        }
        pending = {
            "id": 40239259168,
            "status": "pending",
            "head_branch": "main",
            "created_at": "2026-10-25T22:14:32Z",
        }
        verdict, message = evaluate(
            payload(success_run(36619884051, "2026-09-29T19:34:09Z")),
            [payload(waiting), payload(pending), EMPTY, EMPTY],
            NOW,
            12,
        )
        self.assertEqual(verdict, STALE)
        self.assertIn("run 36644890155, status waiting", message)
        self.assertIn("run 40239259168, status pending", message)
        self.assertLess(message.index("36644890155"), message.index("40239259168"))

    def test_duplicate_active_runs_are_listed_once(self):
        waiting = {"id": 7, "status": "waiting", "created_at": "2026-10-01T00:00:00Z"}
        _, message = evaluate(EMPTY, [payload(waiting), payload(waiting)], NOW, 12)
        self.assertEqual(message.count("run 7,"), 1)

    def test_threshold_is_respected(self):
        success = payload(success_run(1, "2026-09-29T19:34:09Z"))
        verdict, _ = evaluate(success, [], NOW, 100000)
        self.assertEqual(verdict, FRESH)

    def test_malformed_success_payload_is_an_error(self):
        for bad in (None, [], {}, {"workflow_runs": "nope"}, "text"):
            with self.subTest(payload=bad):
                verdict, message = evaluate(bad, [], NOW, 12)
                self.assertEqual(verdict, ERROR)
                self.assertTrue(message.startswith("ERROR:"))

    def test_bad_timestamp_in_success_run_is_an_error(self):
        bad_run = {"id": 1, "updated_at": "not a date"}
        verdict, _ = evaluate(payload(bad_run), [], NOW, 12)
        self.assertEqual(verdict, ERROR)

    def test_malformed_active_payload_is_reported_but_not_fatal(self):
        verdict, message = evaluate(EMPTY, [None, {"oops": 1}], NOW, 12)
        self.assertEqual(verdict, STALE)
        self.assertIn("Could not read some unfinished-run lists", message)


if __name__ == "__main__":
    unittest.main()

"""Admin generation-stats read over a disposable ledger; no providers or GPU."""

import uuid

from sqlalchemy import insert, update

from studio_platform.generation_stats import _intent, content, generation_stats
from studio_platform.repository import attempts, jobs
from test_platform_repository import LedgerCase


def compiled(frames, fps, height, images, audios, mode="ref", native=False):
    return {
        "request": {
            "mode": mode,
            "inputs": {"images": [{}] * images, "audios": [{}] * audios},
            "prompt": "PRIVATE never returned",
        },
        "output_spec": {
            "frames": frames,
            "actual_duration": frames / fps,
            "height": height,
        }
        if native
        else {"frame_count": frames, "fps": fps, "height": height},
    }


class GenerationStatsTests(LedgerCase):
    def finished(
        self,
        request,
        *,
        started,
        finished,
        worker="dstack-" + "a" * 32,
        status="succeeded",
    ):
        job = self.job()
        with self.repo.engine.begin() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(request=request, created_at=started - 30)
            )
            connection.execute(
                insert(attempts).values(
                    id=str(uuid.uuid4()),
                    job_id=job["id"],
                    number=1,
                    status=status,
                    fence=1,
                    worker_id=worker,
                    created_at=started,
                    updated_at=finished,
                    submission_started_at=started,
                    actual_cost_microusd=750000,
                )
            )
        return job

    def test_groups_by_content_bands_and_hides_prompts(self):
        self.now = 1_800_000_000.0
        now = self.repo.clock()
        long = compiled(362, 24, 768, 3, 1)
        self.finished(long, started=now - 5000, finished=now - 1000)
        self.finished(long, started=now - 9000, finished=now - 4000)
        self.finished(
            compiled(97, 24, 480, 0, 0, "fl"), started=now - 800, finished=now - 500
        )
        self.finished(long, started=now - 900, finished=now - 100, status="failed")
        self.finished(long, started=now - 40 * 86400, finished=now - 40 * 86400 + 60)
        result = generation_stats(self.repo, self.scope.tenant_id, days=14, now=now)
        self.assertEqual(result["count"], 3)
        groups = {
            (g["mode"], g["length"], g["resolution"]): g for g in result["groups"]
        }
        ref = groups[("ref", "gt12s", "gt720")]
        self.assertEqual(ref["count"], 2)
        self.assertEqual(ref["image_refs_band"], "3-5")
        self.assertEqual(ref["audio_ref"], "yes")
        self.assertEqual(ref["generate_seconds"]["median"], 4500.0)
        self.assertEqual(ref["generate_seconds"]["max"], 5000.0)
        self.assertEqual(groups[("fl", "le5s", "le480")]["count"], 1)
        self.assertNotIn("PRIVATE", str(result))
        self.assertEqual(result["recent"][0]["wait_seconds"], 30.0)

    def test_content_and_worker_identity_are_closed(self):
        self.assertEqual(content({"request": {"mode": "x"}})["mode"], "unknown")
        self.assertEqual(content(None), {"mode": "unknown"})
        self.assertEqual(
            _intent("dstack-86dc8d5de1e148b493c886929e10e142"),
            "86dc8d5d-e1e1-48b4-93c8-86929e10e142",
        )
        self.assertIsNone(_intent("worker"))
        self.assertIsNone(_intent("dstack-" + "Z" * 32))


class NativeShapeTests(LedgerCase):
    def test_native_duration_and_artifact_finish_time(self):
        from studio_platform.repository import artifacts

        self.now = 1_800_000_000.0
        now = self.repo.clock()
        job = self.job()
        attempt_id = str(uuid.uuid4())
        with self.repo.engine.begin() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(
                    request=compiled(362, 24, 768, 3, 1, native=True),
                    created_at=now - 10000,
                )
            )
            connection.execute(
                insert(attempts).values(
                    id=attempt_id,
                    job_id=job["id"],
                    number=1,
                    status="succeeded",
                    fence=1,
                    worker_id="w",
                    created_at=now - 9800,
                    updated_at=now,  # settled much later
                    submission_started_at=now - 9700,
                )
            )
            connection.execute(
                insert(artifacts).values(
                    id=str(uuid.uuid4()),
                    job_id=job["id"],
                    attempt_id=attempt_id,
                    metadata={},
                    created_at=now - 100,
                )
            )
        row = generation_stats(self.repo, self.scope.tenant_id, days=14, now=now)[
            "recent"
        ][0]
        self.assertEqual(row["generate_seconds"], 9600.0)
        self.assertEqual((row["length"], row["seconds"]), ("gt12s", 15.08))

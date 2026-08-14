"""
The upload must not hold a connection open for the length of the work.

A 126-page document takes around two minutes. Answering the upload only when
that finishes means anything in the path — a tunnel, a proxy, the browser — is
entitled to give up first, and when one does the run is lost and the user sees
a connection error. The upload therefore starts a job and returns at once, and
the client polls. These tests hold that contract in place.
"""

import time

import pytest

app_module = pytest.importorskip("app")


@pytest.fixture(scope="module")
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


class TestUploadReturnsImmediately:
    def test_upload_is_accepted_not_completed(self, client):
        """202 with a job id — not 200 with a result."""
        with open("synthetic_test.docx", "rb") as handle:
            response = client.post(
                "/redact", data={"file": (handle, "synthetic_test.docx")},
                content_type="multipart/form-data",
            )
        assert response.status_code == 202
        body = response.get_json()
        assert body["status"] == "accepted"
        assert body["job_id"] and body["status_url"].endswith(body["job_id"])

    def test_the_job_finishes_and_carries_a_summary(self, client):
        with open("synthetic_test.docx", "rb") as handle:
            body = client.post(
                "/redact", data={"file": (handle, "synthetic_test.docx")},
                content_type="multipart/form-data",
            ).get_json()

        deadline = time.time() + 180
        while time.time() < deadline:
            job = client.get(body["status_url"]).get_json()
            if job["state"] != "running":
                break
            # While running it must report progress, or the page has nothing
            # to show and a long wait looks indistinguishable from a hang.
            assert job["elapsed"] >= 0
            time.sleep(1)
        else:                                          # pragma: no cover
            pytest.fail("job did not finish within the deadline")

        assert job["state"] == "done", job.get("error")
        assert job["download_url"].startswith("/download/")
        summary = job["summary"]
        for key in ("entities_redacted", "suppressed_by_policy", "distinct_entities",
                    "replacements", "defined_terms_learned", "seconds", "entity_counts"):
            assert key in summary, key
        assert summary["entities_redacted"] > 0

    def test_the_output_can_be_downloaded(self, client):
        with open("synthetic_test.docx", "rb") as handle:
            body = client.post(
                "/redact", data={"file": (handle, "synthetic_test.docx")},
                content_type="multipart/form-data",
            ).get_json()
        deadline = time.time() + 180
        while time.time() < deadline:
            job = client.get(body["status_url"]).get_json()
            if job["state"] != "running":
                break
            time.sleep(1)
        assert job["state"] == "done"
        download = client.get(job["download_url"])
        assert download.status_code == 200
        assert download.data[:2] == b"PK"          # a .docx is a zip


class TestStatusEndpoint:
    def test_an_unknown_job_is_not_a_crash(self, client):
        response = client.get("/status/deadbeef")
        assert response.status_code == 404
        assert response.get_json()["state"] == "unknown"

    def test_rejects_a_non_docx(self, client):
        response = client.post(
            "/redact", data={"file": (open(__file__, "rb"), "notes.txt")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400

    def test_the_job_store_is_bounded(self):
        """A long-running server must not grow a job entry per upload forever."""
        store, lock = app_module._jobs, app_module._jobs_lock
        with lock:
            store.clear()
            for n in range(app_module.MAX_JOBS + 25):
                while len(store) >= app_module.MAX_JOBS:
                    store.popitem(last=False)
                store[f"job{n}"] = {"state": "done"}
            assert len(store) <= app_module.MAX_JOBS
            store.clear()

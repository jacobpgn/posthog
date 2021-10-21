from typing import Tuple
from urllib.parse import parse_qs, urlencode, urlparse

from dateutil.relativedelta import relativedelta
from django.http import HttpRequest
from django.utils.timezone import now
from freezegun import freeze_time
from rest_framework.request import Request

from posthog.helpers.session_recording import compress_and_chunk_snapshots
from posthog.models import Filter, Person
from posthog.models.session_recording_event import SessionRecordingEvent
from posthog.queries.session_recordings.session_recording import SessionRecording
from posthog.test.base import BaseTest


def factory_session_recording_test(session_recording: SessionRecording, session_recording_event_factory):
    def create_recording_request_and_filter(session_recording_id, limit=None, offset=None) -> Tuple[Request, Filter]:
        params = {}
        if limit:
            params["limit"] = limit
        if offset:
            params["offset"] = offset
        build_req = HttpRequest()
        build_req.META = {"HTTP_HOST": "www.testserver"}

        req = Request(
            build_req, f"/api/event/session_recording?session_recording_id={session_recording_id}{urlencode(params)}"
        )
        return (req, Filter(request=req, data=params))

    class TestSessionRecording(BaseTest):
        maxDiff = None

        def test_get_snapshots(self):
            with freeze_time("2020-09-13T12:26:40.000Z"):
                Person.objects.create(team=self.team, distinct_ids=["user"], properties={"$some_prop": "something"})

                self.create_snapshot("user", "1", now())
                self.create_snapshot("user", "1", now() + relativedelta(seconds=10))
                self.create_snapshot("user2", "2", now() + relativedelta(seconds=20))
                self.create_snapshot("user", "1", now() + relativedelta(seconds=30))

                req, filt = create_recording_request_and_filter("1")
                session = session_recording(
                    team=self.team, session_recording_id="1", request=req, filter=filt
                ).get_snapshots()
                self.assertEqual(
                    session["snapshots"],
                    [
                        {"timestamp": 1_600_000_000, "type": 2},
                        {"timestamp": 1_600_000_010, "type": 2},
                        {"timestamp": 1_600_000_030, "type": 2},
                    ],
                )
                self.assertEqual(session["next"], None)

        def test_query_run_with_no_such_session(self):

            req, filt = create_recording_request_and_filter("xxx")
            session = session_recording(
                team=self.team, session_recording_id="xxx", request=req, filter=filt
            ).get_snapshots()
            self.assertEqual(session, {"snapshots": [], "next": None})

        def test_query_run_queries_with_specific_limit_and_offset(self):
            chunked_session_id = "7"
            chunk_limit = 10
            snapshots_per_chunk = 2
            base_time = now()

            Person.objects.create(team=self.team, distinct_ids=["user"], properties={"$some_prop": "something"})
            for _ in range(11):
                self.create_chunked_snapshots(2, "user", chunked_session_id, base_time)

            req, filt = create_recording_request_and_filter(chunked_session_id, chunk_limit)
            session = session_recording(
                team=self.team, session_recording_id=chunked_session_id, request=req, filter=filt
            ).get_snapshots()
            self.assertEqual(len(session["snapshots"]), chunk_limit * snapshots_per_chunk)
            self.assertIsNotNone(session["next"])
            parsed_params = parse_qs(urlparse(session["next"]).query)
            self.assertEqual(int(parsed_params["offset"][0]), chunk_limit)
            self.assertEqual(int(parsed_params["limit"][0]), chunk_limit)

        def create_snapshot(self, distinct_id, session_id, timestamp, type=2, team_id=None):
            if team_id == None:
                team_id = self.team.pk
            session_recording_event_factory(
                team_id=team_id,
                distinct_id=distinct_id,
                timestamp=timestamp,
                session_id=session_id,
                snapshot_data={"timestamp": timestamp.timestamp(), "type": type},
            )

        def create_chunked_snapshots(self, event_count, distinct_id, session_id, timestamp, has_full_snapshot=True):
            events = []
            for _ in range(event_count):
                events.append(
                    {
                        "event": "$snapshot",
                        "properties": {
                            "$snapshot_data": {
                                "type": 2 if has_full_snapshot else 3,
                                "data": {
                                    "source": 0,
                                    "texts": [],
                                    "attributes": [],
                                    "removes": [],
                                    "adds": [
                                        {
                                            "parentId": 4,
                                            "nextId": 386,
                                            "node": {
                                                "type": 2,
                                                "tagName": "style",
                                                "attributes": {"data-emotion": "css"},
                                                "childNodes": [],
                                                "id": 729,
                                            },
                                        },
                                    ],
                                },
                                "timestamp": str(timestamp),
                            },
                            "$session_id": session_id,
                            "distinct_id": distinct_id,
                        },
                        "offset": 1997,
                    }
                )
            chunked_snapshots = compress_and_chunk_snapshots(events, chunk_size=10)
            for snapshot in chunked_snapshots:
                session_recording_event_factory(
                    team_id=self.team.pk,
                    distinct_id=distinct_id,
                    timestamp=timestamp,
                    session_id=session_id,
                    snapshot_data=snapshot["properties"].get("$snapshot_data"),
                )

    return TestSessionRecording


class DjangoSessionRecordingTest(
    factory_session_recording_test(SessionRecording, SessionRecordingEvent.objects.create)  # type: ignore
):
    pass

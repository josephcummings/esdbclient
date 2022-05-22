# -*- coding: utf-8 -*-
from unittest import TestCase
from uuid import uuid4

from esdbclient.client import (
    EsdbClient,
    ExpectedPositionError,
    NewEvent,
    ServiceUnavailable,
    StreamNotFound,
)


class TestEsdbClient(TestCase):
    def test_service_unavailable_exception(self) -> None:
        esdb_client = EsdbClient("localhost:2222")
        with self.assertRaises(ServiceUnavailable) as cm:
            list(esdb_client.read_stream_events(str(uuid4())))
        self.assertEqual(
            cm.exception.args[0].details(), "failed to connect to all addresses"
        )

    def test_stream_not_found_exception(self) -> None:
        esdb_client = EsdbClient("localhost:2113")
        stream_name = str(uuid4())

        with self.assertRaises(StreamNotFound):
            list(esdb_client.read_stream_events(stream_name))

        with self.assertRaises(StreamNotFound):
            list(esdb_client.read_stream_events(stream_name, backwards=True))

        with self.assertRaises(StreamNotFound):
            list(esdb_client.read_stream_events(stream_name, position=1))

        with self.assertRaises(StreamNotFound):
            list(
                esdb_client.read_stream_events(stream_name, position=1, backwards=True)
            )

        with self.assertRaises(StreamNotFound):
            list(esdb_client.read_stream_events(stream_name, limit=10))

        with self.assertRaises(StreamNotFound):
            list(esdb_client.read_stream_events(stream_name, backwards=True, limit=10))

        with self.assertRaises(StreamNotFound):
            list(esdb_client.read_stream_events(stream_name, position=1, limit=10))

        with self.assertRaises(StreamNotFound):
            list(
                esdb_client.read_stream_events(
                    stream_name, position=1, backwards=True, limit=10
                )
            )

    def test_stream_append_and_read(self) -> None:
        client = EsdbClient("localhost:2113")
        stream_name = str(uuid4())

        # Check stream not found.
        with self.assertRaises(StreamNotFound):
            list(client.read_stream_events(stream_name))

        # Check stream position is None.
        self.assertEqual(client.get_stream_position(stream_name), None)

        # Check get error when attempting to append empty list to position 1.
        with self.assertRaises(ExpectedPositionError) as cm:
            client.append_events(stream_name, expected_position=1, events=[])
        self.assertEqual(cm.exception.args[0], f"Stream '{stream_name}' does not exist")

        # Append empty list of events.
        commit_position1 = client.append_events(
            stream_name, expected_position=None, events=[]
        )
        self.assertIsInstance(commit_position1, int)

        # Check stream still not found.
        with self.assertRaises(StreamNotFound):
            list(client.read_stream_events(stream_name))

        # Check stream position is None.
        self.assertEqual(client.get_stream_position(stream_name), None)

        # Check get error when attempting to append new event to position 1.
        event1 = NewEvent(type="OrderCreated", data=b"{}", metadata=b"{}")
        with self.assertRaises(ExpectedPositionError) as cm:
            client.append_events(stream_name, expected_position=1, events=[event1])
        self.assertEqual(cm.exception.args[0], f"Stream '{stream_name}' does not exist")

        # Append new event.
        commit_position2 = client.append_events(
            stream_name, expected_position=None, events=[event1]
        )

        # Todo: Why isn't this +1?
        # self.assertEqual(commit_position2 - commit_position1, 1)
        self.assertEqual(commit_position2 - commit_position1, 126)

        # Check stream position is 0.
        self.assertEqual(client.get_stream_position(stream_name), 0)

        # Read the stream forwards from the start (expect one event).
        events = list(client.read_stream_events(stream_name))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "OrderCreated")

        # Check we can't append another new event at initial position.
        event2 = NewEvent(type="OrderUpdated", data=b"{}", metadata=b"{}")
        with self.assertRaises(ExpectedPositionError) as cm:
            client.append_events(stream_name, expected_position=None, events=[event2])
        self.assertEqual(cm.exception.args[0], "Current position is 0")

        # Append another event.
        commit_position3 = client.append_events(
            stream_name, expected_position=0, events=[event2]
        )

        # Check stream position is 1.
        self.assertEqual(client.get_stream_position(stream_name), 1)

        # NB: Why isn't this +1? because it's "disk position" :-|
        # self.assertEqual(commit_position3 - commit_position2, 1)
        self.assertEqual(commit_position3 - commit_position2, 142)

        # Read the stream (expect two events in 'forwards' order).
        events = list(client.read_stream_events(stream_name))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].type, "OrderCreated")
        self.assertEqual(events[1].type, "OrderUpdated")

        # Read the stream backwards from the end.
        events = list(client.read_stream_events(stream_name, backwards=True))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1].type, "OrderCreated")
        self.assertEqual(events[0].type, "OrderUpdated")

        # Read the stream forwards from position 1.
        events = list(client.read_stream_events(stream_name, position=1))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "OrderUpdated")

        # Read the stream backwards from position 0.
        events = list(
            client.read_stream_events(stream_name, position=0, backwards=True)
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "OrderCreated")

        # Read the stream forwards from start with limit.
        events = list(client.read_stream_events(stream_name, limit=1))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "OrderCreated")

        # Read the stream backwards from end with limit.
        events = list(client.read_stream_events(stream_name, backwards=True, limit=1))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "OrderUpdated")

        # Check we can't append another new event at second position.
        event3 = NewEvent(type="OrderDeleted", data=b"{}", metadata=b"{}")
        with self.assertRaises(ExpectedPositionError) as cm:
            client.append_events(stream_name, expected_position=0, events=[event3])
        self.assertEqual(cm.exception.args[0], "Current position is 1")

        # Append another new event.
        commit_position4 = client.append_events(
            stream_name, expected_position=1, events=[event3]
        )

        # Check stream position is 2.
        self.assertEqual(client.get_stream_position(stream_name), 2)

        # NB: Why isn't this +1? because it's "disk position" :-|
        # self.assertEqual(commit_position4 - commit_position3, 1)
        self.assertEqual(commit_position4 - commit_position3, 142)

        # Read the stream forwards from start (expect three events).
        events = list(client.read_stream_events(stream_name))
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].type, "OrderCreated")
        self.assertEqual(events[1].type, "OrderUpdated")
        self.assertEqual(events[2].type, "OrderDeleted")

        # Read the stream backwards from end (expect three events).
        events = list(client.read_stream_events(stream_name, backwards=True))
        self.assertEqual(len(events), 3)
        self.assertEqual(events[2].type, "OrderCreated")
        self.assertEqual(events[1].type, "OrderUpdated")
        self.assertEqual(events[0].type, "OrderDeleted")

        # Read the stream forwards from position with limit.
        events = list(client.read_stream_events(stream_name, position=1, limit=1))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "OrderUpdated")

        # Read the stream backwards from position withm limit.
        events = list(
            client.read_stream_events(stream_name, position=1, backwards=True, limit=1)
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "OrderUpdated")

    def test_read_all_events(self) -> None:
        esdb_client = EsdbClient("localhost:2113")

        num_old_events = len(list(esdb_client.read_all_events()))

        event1 = NewEvent(type="OrderCreated", data=b"{}", metadata=b"{}")
        event2 = NewEvent(type="OrderUpdated", data=b"{}", metadata=b"{}")
        event3 = NewEvent(type="OrderDeleted", data=b"{}", metadata=b"{}")

        # Append new events.
        stream_name1 = str(uuid4())
        commit_position1 = esdb_client.append_events(
            stream_name1, expected_position=None, events=[event1, event2, event3]
        )

        stream_name2 = str(uuid4())
        commit_position2 = esdb_client.append_events(
            stream_name2, expected_position=None, events=[event1, event2, event3]
        )

        # Check we can read forwards from the start.
        events = list(esdb_client.read_all_events())
        self.assertEqual(len(events) - num_old_events, 6)
        self.assertEqual(events[-1].stream_name, stream_name2)
        self.assertEqual(events[-1].type, "OrderDeleted")
        self.assertEqual(events[-2].stream_name, stream_name2)
        self.assertEqual(events[-2].type, "OrderUpdated")
        self.assertEqual(events[-3].stream_name, stream_name2)
        self.assertEqual(events[-3].type, "OrderCreated")
        self.assertEqual(events[-4].stream_name, stream_name1)
        self.assertEqual(events[-4].type, "OrderDeleted")

        # Check we can read backwards from the end.
        events = list(esdb_client.read_all_events(backwards=True))
        self.assertEqual(len(events) - num_old_events, 6)
        self.assertEqual(events[0].stream_name, stream_name2)
        self.assertEqual(events[0].type, "OrderDeleted")
        self.assertEqual(events[1].stream_name, stream_name2)
        self.assertEqual(events[1].type, "OrderUpdated")
        self.assertEqual(events[2].stream_name, stream_name2)
        self.assertEqual(events[2].type, "OrderCreated")
        self.assertEqual(events[3].stream_name, stream_name1)
        self.assertEqual(events[3].type, "OrderDeleted")

        # Check we can read forwards from commit position 1.
        events = list(esdb_client.read_all_events(position=commit_position1))
        self.assertEqual(len(events), 4)
        self.assertEqual(events[0].stream_name, stream_name1)
        self.assertEqual(events[0].type, "OrderDeleted")
        self.assertEqual(events[1].stream_name, stream_name2)
        self.assertEqual(events[1].type, "OrderCreated")
        self.assertEqual(events[2].stream_name, stream_name2)
        self.assertEqual(events[2].type, "OrderUpdated")
        self.assertEqual(events[3].stream_name, stream_name2)
        self.assertEqual(events[3].type, "OrderDeleted")

        # Check we can read forwards from commit position 2.
        events = list(esdb_client.read_all_events(position=commit_position2))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].stream_name, stream_name2)
        self.assertEqual(events[0].type, "OrderDeleted")

        # Check we can read backwards from commit position 1.
        # NB backwards here doesn't include event at commit position.
        events = list(
            esdb_client.read_all_events(position=commit_position1, backwards=True)
        )
        self.assertEqual(len(events) - num_old_events, 2)
        self.assertEqual(events[0].stream_name, stream_name1)
        self.assertEqual(events[0].type, "OrderUpdated")
        self.assertEqual(events[1].stream_name, stream_name1)
        self.assertEqual(events[1].type, "OrderCreated")

        # Check we can read backwards from commit position 2.
        # NB backwards here doesn't include event at commit position.
        events = list(
            esdb_client.read_all_events(position=commit_position2, backwards=True)
        )
        self.assertEqual(len(events) - num_old_events, 5)
        self.assertEqual(events[0].stream_name, stream_name2)
        self.assertEqual(events[0].type, "OrderUpdated")
        self.assertEqual(events[1].stream_name, stream_name2)
        self.assertEqual(events[1].type, "OrderCreated")
        self.assertEqual(events[2].stream_name, stream_name1)
        self.assertEqual(events[2].type, "OrderDeleted")

        # Check we can read forwards from the start with limit.
        events = list(esdb_client.read_all_events(limit=3))
        self.assertEqual(len(events), 3)

        # Check we can read backwards from the end with limit.
        events = list(esdb_client.read_all_events(backwards=True, limit=3))
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].stream_name, stream_name2)
        self.assertEqual(events[0].type, "OrderDeleted")
        self.assertEqual(events[1].stream_name, stream_name2)
        self.assertEqual(events[1].type, "OrderUpdated")
        self.assertEqual(events[2].stream_name, stream_name2)
        self.assertEqual(events[2].type, "OrderCreated")

        # Check we can read forwards from commit position 1 with limit.
        events = list(esdb_client.read_all_events(position=commit_position1, limit=3))
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].stream_name, stream_name1)
        self.assertEqual(events[0].type, "OrderDeleted")
        self.assertEqual(events[1].stream_name, stream_name2)
        self.assertEqual(events[1].type, "OrderCreated")
        self.assertEqual(events[2].stream_name, stream_name2)
        self.assertEqual(events[2].type, "OrderUpdated")

        # Check we can read backwards from commit position 2 with limit.
        events = list(
            esdb_client.read_all_events(
                position=commit_position2, backwards=True, limit=3
            )
        )
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].stream_name, stream_name2)
        self.assertEqual(events[0].type, "OrderUpdated")
        self.assertEqual(events[1].stream_name, stream_name2)
        self.assertEqual(events[1].type, "OrderCreated")
        self.assertEqual(events[2].stream_name, stream_name1)
        self.assertEqual(events[2].type, "OrderDeleted")

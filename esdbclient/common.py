# -*- coding: utf-8 -*-
import datetime
from abc import ABC, abstractmethod
from base64 import b64encode
from threading import Lock
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Iterator,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)
from uuid import UUID
from weakref import WeakValueDictionary

import grpc
import grpc.aio

from esdbclient.connection_spec import ConnectionSpec
from esdbclient.events import RecordedEvent
from esdbclient.exceptions import (
    AbortedByServer,
    AlreadyExists,
    CancelledByClient,
    ConsumerTooSlow,
    EventStoreDBClientException,
    ExceptionThrownByHandler,
    FailedPrecondition,
    GrpcDeadlineExceeded,
    GrpcError,
    InternalError,
    MaximumSubscriptionsReached,
    NodeIsNotLeader,
    NotFound,
    ServiceUnavailable,
    SSLError,
)
from esdbclient.protos.Grpc import persistent_pb2, streams_pb2

if TYPE_CHECKING:  # pragma: no cover
    from grpc import Metadata

else:
    Metadata = Tuple[Tuple[str, str], ...]

__all__ = ["handle_rpc_error", "BasicAuthCallCredentials", "ESDBService", "Metadata"]


PROTOBUF_MAX_DEADLINE_SECONDS = 315576000000
DEFAULT_CHECKPOINT_INTERVAL_MULTIPLIER = 5
DEFAULT_WINDOW_SIZE = 30
DEFAULT_PERSISTENT_SUBSCRIPTION_MESSAGE_TIMEOUT = 30.0
DEFAULT_PERSISTENT_SUBSCRIPTION_MAX_RETRY_COUNT = 10
DEFAULT_PERSISTENT_SUBSCRIPTION_MIN_CHECKPOINT_COUNT = 10
DEFAULT_PERSISTENT_SUBSCRIPTION_MAX_CHECKPOINT_COUNT = 1000
DEFAULT_PERSISTENT_SUBSCRIPTION_CHECKPOINT_AFTER = 2.0
DEFAULT_PERSISTENT_SUBSCRIPTION_EVENT_BUFFER_SIZE = 150
DEFAULT_PERSISTENT_SUBSCRIPTION_MAX_ACK_BATCH_SIZE = 50
DEFAULT_PERSISTENT_SUBSCRIPTION_MAX_ACK_DELAY = 0.2
DEFAULT_PERSISTENT_SUBSCRIPTION_STOPPING_GRACE = 0.2
DEFAULT_PERSISTENT_SUBSCRIPTION_MAX_SUBSCRIBER_COUNT = 5
DEFAULT_PERSISTENT_SUBSCRIPTION_LIVE_BUFFER_SIZE = 500
DEFAULT_PERSISTENT_SUBSCRIPTION_READ_BATCH_SIZE = 200
DEFAULT_PERSISTENT_SUBSCRIPTION_HISTORY_BUFFER_SIZE = 500


GrpcOption = Tuple[str, Union[str, int]]
GrpcOptions = Tuple[GrpcOption, ...]


class GrpcStreamer(ABC):
    pass


class SyncGrpcStreamer(GrpcStreamer):
    @abstractmethod
    def stop(self) -> None:
        """
        Stops the iterator(s) of streaming call.
        """


class AsyncGrpcStreamer(GrpcStreamer):
    @abstractmethod
    async def stop(self) -> None:
        """
        Stops the iterator(s) of streaming call.
        """


TGrpcStreamer = TypeVar("TGrpcStreamer", bound=GrpcStreamer)


class GrpcStreamers(Generic[TGrpcStreamer]):
    def __init__(self) -> None:
        self.map: WeakValueDictionary[int, TGrpcStreamer] = WeakValueDictionary()
        self.lock = Lock()

    def __setitem__(self, key: int, value: TGrpcStreamer) -> None:
        with self.lock:
            self.map[key] = value

    def __iter__(self) -> Iterator[TGrpcStreamer]:
        with self.lock:
            return iter(tuple(self.map.values()))

    def pop(self, key: int) -> TGrpcStreamer:
        with self.lock:
            return self.map.pop(key)


class SyncGrpcStreamers(GrpcStreamers[SyncGrpcStreamer]):
    def close(self) -> None:
        for grpc_streamer in self:
            # print("closing streamer")
            grpc_streamer.stop()
            # print("closed streamer")


class AsyncGrpcStreamers(GrpcStreamers[AsyncGrpcStreamer]):
    async def close(self) -> None:
        for async_grpc_streamer in self:
            # print("closing streamer")
            await async_grpc_streamer.stop()
            # print("closed streamer")


TGrpcStreamers = TypeVar("TGrpcStreamers", bound=GrpcStreamers[Any])


class BasicAuthCallCredentials(grpc.AuthMetadataPlugin):
    def __init__(self, username: str, password: str):
        credentials = b64encode(f"{username}:{password}".encode())
        self._metadata = (("authorization", (b"Basic " + credentials)),)

    def __call__(
        self,
        context: grpc.AuthMetadataContext,
        callback: grpc.AuthMetadataPluginCallback,
    ) -> None:
        callback(self._metadata, None)


def handle_rpc_error(e: grpc.RpcError) -> EventStoreDBClientException:
    """
    Converts gRPC errors to client exceptions.
    """
    if isinstance(e, (grpc.Call, grpc.aio.AioRpcError)):
        if (
            e.code() == grpc.StatusCode.UNKNOWN
            and "Exception was thrown by handler" in str(e.details())
        ):
            return ExceptionThrownByHandler(e)
        elif e.code() == grpc.StatusCode.ABORTED:
            details = e.details()
            if isinstance(details, str) and "Consumer too slow" in details:
                return ConsumerTooSlow()
            else:
                return AbortedByServer()
        elif (
            e.code() == grpc.StatusCode.CANCELLED
            and e.details() == "Locally cancelled by application!"
        ):
            return CancelledByClient(e)
        elif e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
            return GrpcDeadlineExceeded(e)
        elif e.code() == grpc.StatusCode.UNAVAILABLE:
            details = e.details() or ""
            if "SSL_ERROR" in details:
                # root_certificates is None and CA cert not installed
                return SSLError(e)
            if "empty address list:" in details:
                # given root_certificates is invalid
                return SSLError(e)
            return ServiceUnavailable(details)
        elif e.code() == grpc.StatusCode.ALREADY_EXISTS:
            return AlreadyExists(e.details())
        elif e.code() == grpc.StatusCode.NOT_FOUND:
            if e.details() == "Leader info available":
                return NodeIsNotLeader(e)
            return NotFound()
        elif e.code() == grpc.StatusCode.FAILED_PRECONDITION:
            details = e.details()
            if details is not None and details.startswith(
                "Maximum subscriptions reached"
            ):
                return MaximumSubscriptionsReached(details)
            else:  # pragma: no cover
                return FailedPrecondition(details)
        elif e.code() == grpc.StatusCode.INTERNAL:  # pragma: no cover
            return InternalError(e.details())
    return GrpcError(e)


class ESDBService(Generic[TGrpcStreamers]):
    def __init__(
        self,
        connection_spec: ConnectionSpec,
        grpc_streamers: TGrpcStreamers,
    ):
        self._connection_spec = connection_spec
        self._grpc_streamers = grpc_streamers

    def _metadata(
        self, metadata: Optional[Metadata], requires_leader: bool = False
    ) -> Metadata:
        default = (
            "true"
            if self._connection_spec.options.NodePreference == "leader"
            else "false"
        )
        requires_leader_metadata: Metadata = (
            ("requires-leader", "true" if requires_leader else default),
        )
        metadata = tuple() if metadata is None else metadata
        return metadata + requires_leader_metadata


def construct_filter_include_regex(patterns: Sequence[str]) -> str:
    patterns = [patterns] if isinstance(patterns, str) else patterns
    return "^" + "|".join(patterns) + "$"


def construct_filter_exclude_regex(patterns: Sequence[str]) -> str:
    patterns = [patterns] if isinstance(patterns, str) else patterns
    return "^(?!(" + "|".join([s + "$" for s in patterns]) + "))"


def construct_recorded_event(
    read_event: Union[
        streams_pb2.ReadResp.ReadEvent, persistent_pb2.ReadResp.ReadEvent
    ],
) -> Optional[RecordedEvent]:
    assert isinstance(
        read_event, (streams_pb2.ReadResp.ReadEvent, persistent_pb2.ReadResp.ReadEvent)
    )
    event = read_event.event
    assert isinstance(
        event,
        (
            streams_pb2.ReadResp.ReadEvent.RecordedEvent,
            persistent_pb2.ReadResp.ReadEvent.RecordedEvent,
        ),
    )
    link = read_event.link
    assert isinstance(
        link,
        (
            streams_pb2.ReadResp.ReadEvent.RecordedEvent,
            persistent_pb2.ReadResp.ReadEvent.RecordedEvent,
        ),
    )

    if event.id.string == "":  # pragma: no cover
        # Sometimes get here when resolving links after deleting a stream.
        # Sometimes never, e.g. when the test suite runs, don't know why.
        return None

    position_oneof = read_event.WhichOneof("position")
    if position_oneof == "commit_position":
        ignore_commit_position = False
        # # Is this equality always true? Ans: Not when resolve_links=True.
        # assert read_event.commit_position == event.commit_position
    else:  # pragma: no cover
        # We get here with EventStoreDB < 22.10 when reading a stream.
        assert position_oneof == "no_position", position_oneof
        ignore_commit_position = True

    if isinstance(read_event, persistent_pb2.ReadResp.ReadEvent):
        retry_count: Optional[int] = read_event.retry_count
    else:
        retry_count = None

    if link.id.string == "":
        recorded_event_link: Optional[RecordedEvent] = None
    else:
        try:
            recorded_at = datetime.datetime.fromtimestamp(
                int(event.metadata.get("created", "")) / 10000000.0,
                tz=datetime.timezone.utc,
            )
        except (TypeError, ValueError):  # pragma: no cover
            recorded_at = None

        recorded_event_link = RecordedEvent(
            id=UUID(link.id.string),
            type=link.metadata.get("type", ""),
            data=link.data,
            metadata=link.custom_metadata,
            content_type=link.metadata.get("content-type", ""),
            stream_name=link.stream_identifier.stream_name.decode("utf8"),
            stream_position=link.stream_revision,
            commit_position=None if ignore_commit_position else link.commit_position,
            retry_count=retry_count,
            recorded_at=recorded_at,
        )

    try:
        recorded_at = datetime.datetime.fromtimestamp(
            int(event.metadata.get("created", "")) / 10000000.0,
            tz=datetime.timezone.utc,
        )
    except (TypeError, ValueError):  # pragma: no cover
        recorded_at = None

    recorded_event = RecordedEvent(
        id=UUID(event.id.string),
        type=event.metadata.get("type", ""),
        data=event.data,
        metadata=event.custom_metadata,
        content_type=event.metadata.get("content-type", ""),
        stream_name=event.stream_identifier.stream_name.decode("utf8"),
        stream_position=event.stream_revision,
        commit_position=None if ignore_commit_position else event.commit_position,
        retry_count=retry_count,
        link=recorded_event_link,
        recorded_at=recorded_at,
    )
    return recorded_event

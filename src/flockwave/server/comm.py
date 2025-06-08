"""Base classes for implementing communication managers that facilitate
communication between UAVs and a ground station via some communication
link (e.g., standard 802.11 wifi).
"""

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from errno import (
    EADDRNOTAVAIL,
    EHOSTDOWN,
    EHOSTUNREACH,
    ENETDOWN,
    ENETUNREACH,
    errorcode,
)
from functools import partial
from logging import Logger
from trio import (
    BrokenResourceError,
    ClosedResourceError,
    open_memory_channel,
    sleep,
    WouldBlock,
)
from trio.abc import ReceiveChannel, SendChannel
from trio_util import wait_all
from typing import (
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Generator,
    Generic,
    Iterable,
    Iterator,
    Optional,
    TypeVar,
    cast,
)

from flockwave.channels import BroadcastMessageChannel, MessageChannel
from flockwave.connections import Connection, get_connection_capabilities

from .types import Disposer


__all__ = ("BROADCAST", "CommunicationManager")


AddressType = TypeVar("AddressType", covariant=True)
"""Type variable representing the type of addresses used by a CommunicationManager"""

PacketType = TypeVar("PacketType")
"""Type variable representing the type of packets handled by a CommunicationManager"""

BROADCAST = object()
"""Marker object used to denote packets that should be broadcast over a
communication channel with no specific destination address.
"""

# TODO(ntamas): I think that the WSA* error codes do not need to be handled
# separately; the source code of errnomodule.c in Python suggests that these
# are transparently mapped to the appropriate errno codes.

#: Special Windows error codes for "network down or unreachable" condition
WSAENETDOWN = 10050
WSAENETUNREACH = 10051

#: Special Windows error codes for "host down or unreachable" condition
WSAEHOSTDOWN = 10064
WSAEHOSTUNREACH = 10065


class ErrorAction(Enum):
    SKIP_LOGGING = "skipLogging"
    LOG_AND_SUSPEND = "logAndSuspend"
    LOG = "log"


@dataclass
class CommunicationManagerEntry(Generic[PacketType, AddressType]):
    """A single entry in the communication manager that contains a connection
    managed by the manager, the associated message channel, and a few
    additional properties.

    Each entry is permanently assigned to a connection and has a name that
    uniquely identifies the connection. Besides that, it has an associated
    MessageChannel_ instance that is not `None` if and only if the connection
    is up and running.
    """

    connection: Connection
    """The connection associated to the entry."""

    name: str
    """The unique identifier of the connection."""

    can_broadcast: bool = False
    """Stores whether the connection can be used for broadcasting messages.
    This is essentially a cache for ``isinstance(channel, BroadcastMessageChannel)``.
    """

    can_send: bool = True
    """Stores whether the connection can be used for sending messages."""

    channel: Optional[MessageChannel[tuple[PacketType, AddressType], bytes]] = None
    """The channel that can be used to send messages on the connection.
    ``None`` if the connection is closed.
    """

    _error_count: int = 0
    """Number of consecutive errors that have occurred recently. Sending a
    successful message on this entry will clear the error counter.
    """

    def detach_channel(self) -> None:
        """Detaches the channel associated to this entry."""
        self.set_channel(None)

    def notify_error(self, limit: int) -> ErrorAction:
        """Increments the error count by one.

        Args:
            limit: error count limit from the communication manager

        Returns:
            an object describing whether the error should be logged
        """
        self._error_count += 1
        if self._error_count > limit:
            return ErrorAction.SKIP_LOGGING
        elif self._error_count == limit:
            return ErrorAction.LOG_AND_SUSPEND
        else:
            return ErrorAction.LOG

    def reset_error_count(self) -> bool:
        """Resets the error count.

        Returns:
            whether the error count was non-zero before resetting it
        """
        if self._error_count > 0:
            self._error_count = 0
            return True
        return False

    def set_channel(
        self, channel: Optional[MessageChannel[tuple[PacketType, AddressType], bytes]]
    ) -> None:
        """Sets the channel associated to this entry."""
        if self.channel is not channel:
            self.channel = channel
            self.can_broadcast = isinstance(channel, BroadcastMessageChannel)
            self._error_count = 0

    @property
    def is_open(self) -> bool:
        """Returns whether the communication channel represented by this
        entry is up and running.
        """
        return self.channel is not None


Consumer = Callable[[ReceiveChannel[tuple[str, tuple[PacketType, AddressType]]]], Any]
"""Type alias for a callable that consumes packets from a communication channel."""


class CommunicationManager(Generic[PacketType, AddressType]):
    """Reusable communication manager class for drone driver extensions, with
    multiple responsibilities:

    - watches a set of connections and uses the app supervisor to keep them
      open

    - parses the incoming messages from each of the connections in separate
      tasks, and forwards them to a central queue

    - provides a method that can be used to send a message on any of the
      currently open connections

    - provides facilities for adding aliases to connections and for mapping
      a single alias to multiple connections
    """

    channel_factory: Callable[[Connection, Logger], MessageChannel]
    """Callable that takes a Connection_ instance and a logger object, and that
    constructs a MessageChannel_ object that reads messages from and writes
    messages to the given connection, using the given logger for logging
    parsing errors.
    """

    format_address: Callable[[AddressType], str]
    """Callable that takes an address used by this communication manager and
    formats it into a string so it can be used in log messages. Defaults t
    `str()`.
    """

    broadcast_delay: float = 0
    """Number of seconds to wait after each successful broadcast. This is a
    hack that can be used to work around flow control problems when broadcasting
    RTK corrections. Typically you should leave this at zero.
    """

    error_limit: int = 5
    """Number of consecutive errors that can occur on a channel before the
    communication manager stops logging errors on that channel. This is
    useful for reducing log noise.
    """

    log: Logger

    BROADCAST: ClassVar[object]
    """Marker object that is used to indicate that a message is a broadcast
    message.
    """

    _aliases: dict[str, list[str]]
    """Mapping from channel aliases to the list of channel IDs that the alias
    refers to.
    """

    _entries_by_name: dict[
        str, list[CommunicationManagerEntry[PacketType, AddressType]]
    ]
    """Mapping from channel identifiers to the corresponding Entry_ objects."""

    _error_counters: dict[str, int]
    """Mapping from channel identifiers to the number of consecutive transmission
    errors on that channel.
    """

    def __init__(
        self,
        channel_factory: Callable[[Connection, Logger], MessageChannel],
        format_address: Callable[[AddressType], str] = str,
    ):
        """Constructor.

        Parameters:
            channel_factory: a callable that can be invoked with a connection
                object and a logger instance and that creates a new message
                channel instance that reads messages from and writes messages
                to the given connection
        """
        self.channel_factory = channel_factory
        self.format_address = format_address

        self._aliases = {}
        self._entries_by_name = defaultdict(list)
        self._error_counters = defaultdict(int)
        self._running = False
        self._outbound_tx_queue = None

    def add(self, connection, *, name: str, can_send: Optional[bool] = None):
        """Adds the given connection to the list of connections managed by
        the communication manager.

        Parameters:
            connection: the connection to add
            name: the name of the connection; passed back to consumers of the
                incoming packet queue along with the received packets so they
                know which connection the packet was received from
            can_send: whether the channel can be used for sending messages.
                `None` means to try figuring it out from the connection object
                itself by querying its `can_send` attribute. If the attribute
                is missing, we assume that the connection _can_ send messages.
        """
        assert connection is not None

        if self._running:
            raise RuntimeError("cannot add new connections when the manager is running")

        if can_send is None:
            cap = get_connection_capabilities(connection)
            can_send = cap["can_send"]

        entry = CommunicationManagerEntry(
            connection, name=name, can_send=bool(can_send)
        )
        self._entries_by_name[name].append(entry)

    def add_alias(self, alias: str, *, targets: Iterable[str]) -> Disposer:
        """Adds the given alias to the connection names recognized by the
        communication manager. Can be used to decide where certain messages
        should be routed to by dynamically assigning the alias to one of the
        "real" connection names.
        """
        if alias in self._aliases:
            raise RuntimeError(f"Alias already registered: {alias!r}")

        self._aliases[alias] = list(targets)
        return partial(self.remove_alias, alias)

    async def broadcast_packet(
        self,
        packet: PacketType,
        *,
        destination: Optional[str] = None,
        allow_failure: bool = False,
    ) -> None:
        """Requests the communication manager to broadcast the given message
        packet to all destinations, or to the broadcast address of a single
        destination.

        Blocks until the packet is enqueued in the outbound queue, allowing
        other tasks to run.

        Parameters:
            packet: the packet to send
        """
        queue = self._outbound_tx_queue
        if not queue:
            if not allow_failure:
                raise BrokenResourceError("Outbound message queue is closed")
            else:
                return

        address = BROADCAST if destination is None else (destination, BROADCAST)

        await queue.send((packet, address))

    def enqueue_broadcast_packet(
        self,
        packet: PacketType,
        *,
        destination: Optional[str] = None,
        allow_failure: bool = False,
    ) -> None:
        """Requests the communication manager to broadcast the given message
        packet to all destinations and return immediately.

        The packet may be dropped if the outbound queue is currently full.

        Parameters:
            packet: the packet to send
        """
        queue = self._outbound_tx_queue
        if not queue:
            if not allow_failure:
                raise BrokenResourceError("Outbound message queue is closed")
            else:
                return

        address = BROADCAST if destination is None else (destination, BROADCAST)

        try:
            queue.send_nowait((packet, address))
        except WouldBlock:
            if self.log:
                self.log.warning(
                    "Dropping outbound broadcast packet; outbound message queue is full"
                )

    def enqueue_packet(self, packet: PacketType, destination: tuple[str, AddressType]):
        """Requests the communication manager to send the given message packet
        to the given destination and return immediately.

        The packet may be dropped if the outbound queue is currently full.

        Parameters:
            packet: the packet to send
            destination: the name of the communication channel and the address
                on that communication channel to send the packet to.
        """
        queue = self._outbound_tx_queue
        if not queue:
            raise BrokenResourceError("Outbound message queue is closed")

        try:
            queue.send_nowait((packet, destination))
        except WouldBlock:
            if self.log:
                self.log.warning(
                    "Dropping outbound packet; outbound message queue is full"
                )

    def is_channel_open(self, name: str) -> bool:
        """Returns whether the channel with the given name is currently up and
        running.
        """
        entries = self._entries_by_name.get(name)
        return any(entry.is_open for entry in entries) if entries else False

    def open_channels(self) -> Iterator[MessageChannel]:
        """Returns an iterator that iterates over the list of open message
        channels corresponding to this network.
        """
        for entries in self._entries_by_name.values():
            for entry in entries:
                if entry.channel:
                    yield entry.channel

    async def run(self, *, consumer: Consumer, supervisor, log: Logger, tasks=None):
        """Runs the communication manager in a separate task, using the
        given supervisor function to ensure that the connections associated to
        the communication manager stay open.

        Parameters:
            consumer: a callable that will be called with a Trio ReceiveChannel_
                that will yield all the packets that are received on any of
                the managed connections. More precisely, the channel will yield
                pairs consisting of a connection name (used when they were
                registered) and another pair holding the received message and
                the address it was received from.
            supervisor: a callable that will be called with a connection
                instance and a `task` keyword argument that represents an
                async callable that will be called whenever the connection is
                opened. This signature matches the `supervise()` method of
                the application instance so you typically want to pass that
                in here.
            log: logger that will be used to log messages from the
                communication manager
            tasks: optional list of additional tasks that should be executed
                while the communication manager is managing the messages. Can
                be used to implement heartbeating on the connection channel.
        """
        try:
            self._running = True
            self.log = log
            await self._run(consumer=consumer, supervisor=supervisor, tasks=tasks)
        finally:
            self.log = None  # type: ignore
            self._running = False

    def remove_alias(self, alias: str) -> None:
        """Removes the given alias from the connection aliases recognized by
        the communication manager.
        """
        del self._aliases[alias]

    async def send_packet(
        self, packet: PacketType, destination: tuple[str, AddressType]
    ) -> None:
        """Requests the communication manager to send the given message packet
        to the given destination.

        Blocks until the packet is enqueued in the outbound queue, allowing
        other tasks to run.

        Parameters:
            packet: the packet to send
            destination: the name of the communication channel and the address
                on that communication channel to send the packet to.
        """
        queue = self._outbound_tx_queue
        if not queue:
            raise BrokenResourceError("Outbound message queue is closed")

        await queue.send((packet, destination))

    @contextmanager
    def with_alias(self, alias: str, *, targets: Iterable[str]):
        """Context manager that registers an alias when entering the context and
        unregisters it when exiting the context.
        """
        disposer = self.add_alias(alias, targets=targets)
        try:
            yield
        finally:
            disposer()

    def _iter_entries(
        self,
    ) -> Generator[CommunicationManagerEntry[PacketType, AddressType], None, None]:
        for _, entries in self._entries_by_name.items():
            yield from entries

    async def _run(
        self,
        *,
        consumer: Consumer,
        supervisor,
        tasks: Optional[list[Callable[..., Awaitable[Any]]]] = None,
    ) -> None:
        tx_queue, rx_queue = open_memory_channel[
            tuple[str, tuple[PacketType, AddressType]]
        ](256)

        tasks = [partial(task, self) for task in (tasks or [])]
        tasks.extend(
            partial(
                supervisor,
                entry.connection,
                task=partial(self._run_inbound_link, entry=entry, queue=tx_queue),
            )
            for entry in self._iter_entries()
        )
        tasks.append(partial(consumer, rx_queue))
        tasks.append(self._run_outbound_links)

        async with tx_queue, rx_queue:
            await wait_all(*tasks)

    async def _run_inbound_link(
        self,
        connection,
        *,
        entry: CommunicationManagerEntry[PacketType, AddressType],
        queue: SendChannel[tuple[str, tuple[PacketType, AddressType]]],
    ):
        has_error = False
        channel_created = False
        address = None

        log_extra = {"id": entry.name or ""}

        try:
            address = getattr(connection, "address", None)
            address = self.format_address(address) if address else None

            entry.set_channel(self.channel_factory(connection, self.log))
            assert entry.channel is not None

            channel_created = True
            if address:
                self.log.info(
                    f"Connection at {address} up and running", extra=log_extra
                )
            else:
                self.log.info("Connection up and running", extra=log_extra)

            async with entry.channel:
                async for message in entry.channel:
                    await queue.send((entry.name, message))

        except Exception as ex:
            has_error = True

            if not isinstance(ex, (BrokenResourceError, ClosedResourceError)):
                self.log.exception(ex)

            if channel_created:
                if address:
                    self.log.warning(
                        f"Connection at {address} down, trying to reopen...",
                        extra=log_extra,
                    )
                else:
                    self.log.warning(
                        "Connection down, trying to reopen...", extra=log_extra
                    )

        finally:
            entry.detach_channel()
            if channel_created and not has_error:
                if address:
                    self.log.info(f"Connection at {address} closed", extra=log_extra)
                else:
                    self.log.info("Connection closed", extra=log_extra)

    async def _run_outbound_links(self):
        # ephemeris RTK streams send messages in bursts so it's better to have
        # a relatively large queue here
        tx_queue, rx_queue = open_memory_channel(256)
        async with tx_queue, rx_queue:
            try:
                self._outbound_tx_queue = tx_queue
                await self._run_outbound_links_inner(rx_queue)
            finally:
                self._outbound_tx_queue = None

    async def _run_outbound_links_inner(self, queue):
        # TODO(ntamas): a slow outbound link may block sending messages on other
        # outbound links; revise if this causes a problem
        async for message, destination in queue:
            if destination is BROADCAST:
                await self._send_message_to_all_channels(message)
            else:
                await self._send_message_to_single_channel(message, destination)

    async def _send_message_to_all_channels(self, message: PacketType):
        for entries in self._entries_by_name.values():
            for _index, entry in enumerate(entries):
                channel = entry.channel
                if not entry.can_broadcast:
                    continue

                channel = cast(BroadcastMessageChannel, channel)
                try:
                    await channel.broadcast((message, BROADCAST))
                except Exception:
                    # we are going to try all channels so it does not matter
                    # if a few of them fail for whatever reason
                    pass

    async def _send_message_to_single_channel(
        self, message: PacketType, destination: tuple[str, AddressType]
    ):
        name, address = destination

        entries = self._entries_by_name.get(name)
        if not entries:
            # try with an alias
            targets = self._aliases.get(name)
            if not targets:
                return
            elif len(targets) == 1:
                name = targets[0]
                entries = self._entries_by_name.get(name)
            else:
                for target in targets:
                    if target in self._entries_by_name:
                        await self._send_message_to_single_channel(
                            message, (target, address)
                        )
                return

        sent = False
        is_broadcast = address is BROADCAST

        if entries:
            for index, entry in enumerate(entries):
                if entry.is_open and entry.can_send:
                    channel = entry.channel
                    try:
                        if is_broadcast:
                            # This message should be broadcast on this channel;
                            # let's check if the channel has a broadcast address
                            if entry.can_broadcast:
                                channel = cast(BroadcastMessageChannel, channel)
                                await channel.broadcast((message, BROADCAST))
                                if self.broadcast_delay > 0:
                                    await sleep(self.broadcast_delay)
                                sent = True
                        else:
                            assert channel is not None
                            await channel.send((message, address))
                            sent = True

                        if sent:
                            if entry.reset_error_count():
                                formatted_id = f"{name}[{index}]"
                                self.log.info(
                                    "Channel resumed normal operation",
                                    extra={"id": formatted_id},
                                )

                    except Exception as ex:
                        try:
                            self._handle_tx_error(ex, address, entry, index)
                        except Exception:
                            self.log.exception(
                                "Error while handling error during message transmission",
                                extra={"id": f"{name}[{index}]"},
                            )

        if sent:
            self._error_counters[name] = 0
        else:
            self._error_counters[name] += 1
            error_counter = self._error_counters[name]

            extra = {"id": name, "telemetry": "ignore"}

            if not is_broadcast:
                if error_counter <= self.error_limit:
                    if entries:
                        self.log.warning(
                            "Dropping outbound message, sending failed on all channels",
                            extra=extra,
                        )
                    else:
                        self.log.warning(
                            "Dropping outbound message, no suitable channel",
                            extra=extra,
                        )

    def _handle_tx_error(
        self,
        ex: Exception,
        address,
        entry: CommunicationManagerEntry,
        index: int,
    ) -> None:
        """Handles an error that occurred while sending a message on a channel."""
        action = entry.notify_error(self.error_limit)
        if action is ErrorAction.SKIP_LOGGING:
            return

        formatted_id = f"{entry.name}[{index}]"
        is_broadcast = address is BROADCAST

        try:
            if is_broadcast:
                formatted_address = ""
            else:
                formatted_address = self.format_address(address)
        except Exception:
            formatted_address = repr(address)

        if isinstance(ex, OSError):
            self._log_os_error_during_tx(ex, formatted_address, formatted_id)
        elif isinstance(ex, BrokenResourceError):
            self.log.error("Channel is broken", extra={"id": formatted_id})
        else:
            self.log.error("Error while sending message", extra={"id": formatted_id})

        if action is ErrorAction.LOG_AND_SUSPEND:
            self.log.warning(
                "Error reporting suspended until the connection recovers",
                extra={"id": formatted_id, "telemetry": "ignore"},
            )

        if not isinstance(ex, OSError):
            self.log.exception(
                "Error while sending message",
                extra={"id": f"{entry.name}[{index}]"},
            )
            return

    def _log_os_error_during_tx(
        self,
        ex: OSError,
        formatted_address: str,
        formatted_id: str,
    ) -> None:
        formatted_error_code = (
            str(errorcode.get(ex.errno, ex.errno))
            if ex.errno is not None
            else "code missing"
        )

        prefix = f"{formatted_address}: " if formatted_address else ""

        if ex.errno in (
            ENETDOWN,
            ENETUNREACH,
            EADDRNOTAVAIL,
            WSAENETDOWN,
            WSAENETUNREACH,
        ):
            # This is okay
            self.log.error(
                f"{prefix}Network is down or unreachable ({formatted_error_code})",
                extra={"id": formatted_id, "telemetry": "ignore"},
            )

        elif ex.errno in (
            EHOSTDOWN,
            EHOSTUNREACH,
            WSAEHOSTDOWN,
            WSAEHOSTUNREACH,
        ):
            self.log.error(
                f"{prefix}Host is down or unreachable ({formatted_error_code})",
                extra={"id": formatted_id, "telemetry": "ignore"},
            )

        else:
            self.log.exception(
                f"{prefix}Error while sending message ({formatted_error_code})",
                extra={"id": formatted_id},
            )


CommunicationManager.BROADCAST = BROADCAST

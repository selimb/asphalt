import logging
import weakref
from asyncio import get_event_loop, Queue, wait
from datetime import datetime, timezone, tzinfo
from inspect import isawaitable, getmembers
from time import time as stdlib_time
from weakref import WeakKeyDictionary
from typing import Callable, Any, Sequence, Awaitable, AsyncIterator

from async_generator import async_generator, aclosing, yield_
from typeguard import check_argument_types

from asphalt.core.utils import qualified_name

__all__ = ('Event', 'Signal', 'wait_event', 'stream_events')

logger = logging.getLogger(__name__)


class Event:
    """
    The base class for all events.

    :param source: the object where this event originated from
    :param topic: the event topic
    :param time: the time the event occurred

    :ivar source: the object where this event originated from
    :ivar str topic: the topic
    :ivar float time: event creation time as seconds from the UNIX epoch
    """

    __slots__ = 'source', 'topic', 'time'

    def __init__(self, source, topic: str, time: float = None):
        self.source = source
        self.topic = topic
        self.time = time or stdlib_time()

    @property
    def utc_timestamp(self) -> datetime:
        """
        Return a timezone aware :class:`~datetime.datetime` corresponding to the ``time`` variable,
        using the UTC timezone.

        """
        return datetime.fromtimestamp(self.time, timezone.utc)

    def __repr__(self):
        return '{self.__class__.__name__}(source={self.source!r}, topic={self.topic!r})'.\
            format(self=self)


class Signal:
    """
    Declaration of a signal that can be used to dispatch events.

    This is a descriptor that returns itself on class level attribute access and a bound version of
    itself on instance level access. Connecting listeners and dispatching events only works with
    these bound instances.

    Each signal must be assigned to a class attribute, but only once. The Signal will not function
    correctly if the same Signal instance is assigned to multiple attributes.

    :param event_class: an event class
    """

    __slots__ = 'event_class', 'source', 'topic', 'listeners', 'bound_signals'

    def __init__(self, event_class: type, *, source=None, topic: str = None):
        assert check_argument_types()
        assert issubclass(event_class, Event), 'event_class must be a subclass of Event'
        self.event_class = event_class
        self.topic = topic
        if source is not None:
            self.source = weakref.ref(source)
            self.listeners = []
        else:
            self.bound_signals = WeakKeyDictionary()

    def __get__(self, instance, owner) -> 'Signal':
        if instance is None:
            return self

        # Find the attribute this Signal was assigned to (needed only on Python 3.5)
        if self.topic is None:
            self.topic = next(
                attr for attr, value in getmembers(owner, lambda value: value is self))

        try:
            return self.bound_signals[instance]
        except KeyError:
            bound_signal = Signal(self.event_class, source=instance, topic=self.topic)
            self.bound_signals[instance] = bound_signal
            return bound_signal

    def __set_name__(self, owner, name) -> None:
        self.topic = name

    def connect(self, callback: Callable[[Event], Any]) -> Callable[[Event], Any]:
        """
        Connect a callback to this signal.

        Each callable can only be connected once. Duplicate registrations are ignored.

        If you need to pass extra arguments to the callback, you can use :func:`functools.partial`
        to wrap the callable.

        :param callback: a callable that will receive an event object as its only argument.
        :return: the value of ``callback`` argument

        """
        assert check_argument_types()
        if callback not in self.listeners:
            self.listeners.append(callback)

        return callback

    def disconnect(self, callback: Callable) -> None:
        """
        Disconnects the given callback.

        The callback will no longer receive events from this signal.

        No action is taken if the callback is not on the list of listener callbacks.

        :param callback: the callable to remove

        """
        assert check_argument_types()
        try:
            self.listeners.remove(callback)
        except ValueError:
            pass

    def dispatch_event(self, event: Event) -> Awaitable[bool]:
        """
        Dispatch the given event to all listeners.

        Creates a new task in which all listener callbacks are called with the given event as
        the only argument. Coroutine callbacks are converted to their own respective tasks and
        waited for concurrently.

        Before the dispatching is done, a snapshot of the listeners is taken and the event is only
        dispatched to those listeners, so adding a listener between the call to this method and the
        actual dispatching will only affect future calls to this method.

        :param event: the event object to dispatch
        :returns: an awaitable that completes when all the callbacks have been called (and any
            awaitables waited on) and resolves to ``True`` if there were no exceptions raised by
            the callbacks, ``False``otherwise

        """
        async def do_dispatch(listeners) -> bool:
            awaitables = []
            all_successful = True
            for callback in listeners:
                try:
                    retval = callback(event)
                except Exception:
                    logger.exception('uncaught exception in event listener')
                    all_successful = False
                else:
                    if isawaitable(retval):
                        awaitables.append(retval)

            # For any callbacks that returned awaitables, wait for their completion and log any
            # exceptions they raised
            if awaitables:
                done, _ = await wait(awaitables, loop=loop)
                for future in done:
                    exc = future.exception()
                    if exc is not None:
                        all_successful = False
                        logger.error('uncaught exception in event listener', exc_info=exc)

            return all_successful

        assert isinstance(event, self.event_class), \
            'event must be of type {}'.format(qualified_name(self.event_class))
        loop = get_event_loop()
        if self.listeners:
            return loop.create_task(do_dispatch(list(self.listeners)))
        else:
            f = loop.create_future()
            f.set_result(True)
            return f

    def dispatch(self, *args, **kwargs) -> Awaitable[bool]:
        """
        Create and dispatch an event.

        This method constructs an event object and then passes it to :meth:`dispatch_event` for
        the actual dispatching.

        :param args: positional arguments to the constructor of the associated event class
        :param kwargs: keyword arguments to the constructor of the associated event class
        :returns: an awaitable that completes when all the callbacks have been called (and any
            awaitables waited on) and resolves to ``True`` if there were no exceptions raised by
            the callbacks, ``False``otherwise

        """
        event = self.event_class(self.source(), self.topic, *args, **kwargs)
        return self.dispatch_event(event)

    def wait_event(self, filter: Callable[[Event], bool] = None) -> Awaitable[Event]:
        """Shortcut for calling :func:`wait_event` with this signal in the first argument."""
        return wait_event([self], filter)

    def stream_events(self, filter: Callable[[Event], bool] = None, *, max_queue_size: int = 0):
        """Shortcut for calling :func:`stream_events` with this signal in the first argument."""
        return stream_events([self], filter, max_queue_size=max_queue_size)


@async_generator
async def stream_events(signals: Sequence[Signal], filter: Callable[[Event], bool] = None, *,
                        max_queue_size: int = 0) -> AsyncIterator[Event]:
    """
    Return an async generator that yields events from the given signals.

    Only events that pass the filter callable (if one has been given) are returned.
    If no filter function was given, all events are yielded from the generator.

    :param signals: the signals to get events from
    :param filter: a callable that takes an event object as an argument and returns ``True`` if
        the event should pass, ``False`` if not
    :param max_queue_size: maximum size of the queue, after which it will start to drop events

    """
    assert check_argument_types()
    queue = Queue(max_queue_size)
    for signal in signals:
        signal.connect(queue.put_nowait)

    try:
        while True:
            event = await queue.get()
            if filter is None or filter(event):
                await yield_(event)
    finally:
        for signal in signals:
            signal.disconnect(queue.put_nowait)


async def wait_event(signals: Sequence[Signal], filter: Callable[[Event], bool] = None) -> Event:
    """
    Wait until any of the given signals dispatches an event that satisfies the filter (if any).

    If no filter has been given, the first event dispatched from the signal is returned.
    
    :param signals: the signals to get events from
    :param filter: a callable that takes an event object as an argument and returns ``True`` if
        the event should pass, ``False`` if not
    :return: the event that was dispatched

    """
    assert check_argument_types()
    async with aclosing(stream_events(signals, filter)) as events:
        return await events.asend(None)

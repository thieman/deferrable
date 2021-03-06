import sys
import time
import logging
from uuid import uuid1
import socket
from traceback import format_exc

from .pickling import loads, dumps, build_later_item, unpickle_method_call, pretty_unpickle
from .debounce import (get_debounce_strategy, set_debounce_keys_for_push_now,
                       set_debounce_keys_for_push_delayed, DebounceStrategy)
from .ttl import add_ttl_metadata_to_item, item_is_expired
from .backoff import apply_exponential_backoff_options, apply_exponential_backoff_delay
from .redis import initialize_redis_client
from .delay import MAXIMUM_DELAY_SECONDS

class Deferrable(object):
    """
    The Deferrable class provides an interface for deferred, distributed execution of
    module-level functions, using the provided backend for transport.

    Once instantiated, the Deferrable object is primarily used through two
    public methods:

    - @instance.deferrable: Decorator used to register a function for deferred execution.
    - instance.run_once(): Method to pop one deferred function off the backend queue and
                           execute it, subject to execution properties on the deferrable
                           instance and the specific deferred task itself (e.g. TTL)

    The following events are emitted by Deferrable and may be consumed by
    registering event handlers with the appropriate `on_{event}` methods,
    each of which takes the queue item as its sole argument. Event handlers
    regarding queue operations (e.g. pop) are called *after* the operation
    has taken place.

    - on_push           : item pushed to the non-error queue
    - on_pop            : pop was attempted and returned an item
    - on_empty          : pop was attempted but did not return an item
    - on_complete       : item completed in the non-error queue
    - on_expire         : TTL expiration
    - on_retry          : item execution errored but will be retried
    - on_error          : item execution errored and was pushed to the error queue
    - on_debounce_hit   : item was not queued subject to debounce constraints
    - on_debounce_miss  : item is configured for debounce but was queued
    - on_debounce_error : exception encountered while processing debounce logic (item will still be queued)
    """

    def __init__(self, backend, redis_client=None, default_error_classes=None, default_max_attempts=5):
        self.backend = backend
        self._redis_client = redis_client
        self.default_error_classes = default_error_classes
        self.default_max_attempts = default_max_attempts

        self._metadata_producer_consumers = []
        self._event_consumers = []

    @property
    def redis_client(self):
        if not hasattr(self, '_initialized_redis_client'):
            self._initialized_redis_client = initialize_redis_client(self._redis_client)
        return self._initialized_redis_client

    def deferrable(self, *args, **kwargs):
        """Decorator. Use this to register a function with this Deferrable
        instance. Example usage:

        @deferrable_instance.deferrable
        def some_function():
            pass

        Any arguments given to `deferrable` are passed as-is to `_deferrable`.

        @deferrable_instance.deferrable(error_classes=[ValueError])
        def some_function():
            pass
        """
        if len(args) == 1 and callable(args[0]) and not kwargs:
            method = args[0]
            return self._deferrable(method)
        return lambda method: self._deferrable(method, *args, **kwargs)

    def run_once(self):
        """Provided as a convenience function for consumers that are not
        concerned with envelope-level heartbeats (touch operations). If your
        consumer needs to implement touch, you should probably do these
        steps separately inside your consumer."""
        envelope, item = self.backend.queue.pop()
        return self.process(envelope, item)

    def process(self, envelope, item):
        if not envelope:
            self._emit('empty', item)
            return
        self._emit('pop', item)
        item_error_classes = loads(item['error_classes']) or tuple()

        for producer_consumer in self._metadata_producer_consumers:
            producer_consumer._consume_metadata_from_item(item)

        try:
            if item_is_expired(item):
                logging.warn("Deferrable job dropped with expired TTL: {}".format(pretty_unpickle(item)))
                self._emit('expire', item)
                self.backend.queue.complete(envelope)
                self._emit('complete', item)
                return
            method, args, kwargs = unpickle_method_call(item)
            method(*args, **kwargs)
        except tuple(item_error_classes):
            attempts, max_attempts = item['attempts'], item['max_attempts']
            if attempts >= max_attempts - 1:
                self._push_item_to_error_queue(item)
            else:
                item['attempts'] += 1
                apply_exponential_backoff_delay(item)
                self.backend.queue.push(item)
                self._emit('retry', item)
        except Exception:
            self._push_item_to_error_queue(item)

        self.backend.queue.complete(envelope)
        self._emit('complete', item)

    def register_metadata_producer_consumer(self, producer_consumer):
        for existing in self._metadata_producer_consumers:
            if existing.NAMESPACE == producer_consumer.NAMESPACE:
                raise ValueError('NAMESPACE {} is already in use'.format(producer_consumer.NAMESPACE))
        self._metadata_producer_consumers.append(producer_consumer)

    def clear_metadata_producer_consumers(self):
        self._metadata_producer_consumers = []

    def register_event_consumer(self, event_consumer):
        self._event_consumers.append(event_consumer)

    def clear_event_consumers(self):
        self._event_consumers = []

    def _emit(self, event, item):
        """Run any handler methods on registered event consumers for the given event,
        passing the item to the method. Processes the event consumers in the order
        they were registered."""
        handler_name = 'on_{}'.format(event)
        for event_consumer in self._event_consumers:
            if hasattr(event_consumer, handler_name):
                getattr(event_consumer, handler_name)(item)

    def _push_item_to_error_queue(self, item):
        """Put information about the current exception into the item's `error`
        key and push the transformed item to the error queue."""
        exc_info = sys.exc_info()
        assert exc_info[0], "_push_error_item must be called from inside an exception handler"
        error_info = {
            'error_type': str(exc_info[0].__name__),
            'error_text': str(exc_info[1]),
            'traceback': format_exc(),
            'hostname': socket.gethostname(),
            'ts': time.time(),
            'id': str(uuid1())
        }
        item['error'] = error_info
        item['last_push_time'] = time.time()
        if 'delay' in item:
            del item['delay']
        self.backend.error_queue.push(item)
        self._emit('error', item)

    def _validate_deferrable_args_compile_time(self, delay_seconds, debounce_seconds, debounce_always_delay, ttl_seconds):
        """Validation check which can be run at compile-time on decorated functions. This
        cannot do any bounds checking on the time arguments, which can be reified from
        callables at each individual .later() invocation."""
        if debounce_seconds and not self.redis_client:
            raise ValueError('redis_client is required for debounce')

        if delay_seconds and debounce_seconds:
            raise ValueError('You cannot delay and debounce at the same time (debounce uses delay internally).')

        if debounce_always_delay and not debounce_seconds:
            raise ValueError('debounce_always_delay is an option to debounce_seconds, which was not set. Probably a mistake.')

    def _validate_deferrable_args_run_time(self, delay_seconds, debounce_seconds, ttl_seconds):
        """Validation check run once all variables have been reified. This is where you
        can do bounds checking on time variables."""
        if delay_seconds > MAXIMUM_DELAY_SECONDS or debounce_seconds > MAXIMUM_DELAY_SECONDS:
            raise ValueError('Delay or debounce window cannot exceed {} seconds'.format(MAXIMUM_DELAY_SECONDS))

        if ttl_seconds:
            if delay_seconds > ttl_seconds or debounce_seconds > ttl_seconds:
                raise ValueError('delay_seconds or debounce_seconds must be less than ttl_seconds')

    def _apply_delay_and_skip_for_debounce(self, item, debounce_seconds, debounce_always_delay):
        """Modifies the item in place to meet the debouncing constraints set by `debounce_seconds`
        and `debounce_always_delay`. For more detail, see the `debouncing` module.

        - delay: Seconds by which to delay the item.
        - debounce_skip: If set to True, the item gets debounced and will not be queued.

        If an exception is encountered, we set `delay` to `None` so that the item is immediately
        queued for processing. We do not want a failure in debounce to stop the item from being
        processed."""
        try:
            debounce_strategy, seconds_to_delay = get_debounce_strategy(self.redis_client, item, debounce_seconds, debounce_always_delay)

            if debounce_strategy == DebounceStrategy.SKIP:
                item['debounce_skip'] = True
                self._emit('debounce_hit', item)
                return
            self._emit('debounce_miss', item)

            if debounce_strategy == DebounceStrategy.PUSH_NOW:
                set_debounce_keys_for_push_now(self.redis_client, item, debounce_seconds)
            elif debounce_strategy == DebounceStrategy.PUSH_DELAYED:
                set_debounce_keys_for_push_delayed(self.redis_client, item, seconds_to_delay, debounce_seconds)

            item['delay'] = seconds_to_delay
        except: # Skip debouncing if we hit an error, don't fail completely
            logging.exception("Encountered error while attempting to process debounce")
            item['delay'] = 0
            self._emit('debounce_error', item)

    def _deferrable(self, method, error_classes=None, max_attempts=None,
                    delay_seconds=0, debounce_seconds=0, debounce_always_delay=False, ttl_seconds=0,
                    use_exponential_backoff=True):
        self._validate_deferrable_args_compile_time(delay_seconds, debounce_seconds, debounce_always_delay, ttl_seconds)

        def later(*args, **kwargs):

            delay_actual = delay_seconds() if callable(delay_seconds) else delay_seconds
            debounce_actual = debounce_seconds() if callable(debounce_seconds) else debounce_seconds
            ttl_actual = ttl_seconds() if callable(ttl_seconds) else ttl_seconds

            self._validate_deferrable_args_run_time(delay_actual, debounce_actual, ttl_actual)

            item = build_later_item(method, *args, **kwargs)
            now = time.time()
            item_error_classes = error_classes if error_classes is not None else self.default_error_classes
            item_max_attempts = max_attempts if max_attempts is not None else self.default_max_attempts
            item.update({
                'group': self.backend.group,
                'error_classes': dumps(item_error_classes),
                'attempts': 0,
                'max_attempts': item_max_attempts,
                'first_push_time': now,
                'last_push_time': now,
                'original_delay_seconds': delay_actual,
                'original_debounce_seconds': debounce_actual,
                'original_debounce_always_delay': debounce_always_delay
            })
            apply_exponential_backoff_options(item, use_exponential_backoff)
            if ttl_actual:
                add_ttl_metadata_to_item(item, ttl_actual)

            if debounce_actual:
                self._apply_delay_and_skip_for_debounce(item, debounce_actual, debounce_always_delay)
                if item.get('debounce_skip'):
                    return
            else:
                item['delay'] = delay_actual

            # Final delay value calculated
            item['original_delay'] = item['delay']

            for producer_consumer in self._metadata_producer_consumers:
                producer_consumer._apply_metadata_to_item(item)

            self.backend.queue.push(item)
            self._emit('push', item)

        method.later = later
        return method

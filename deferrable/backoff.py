"""This module handles exponential backoff when retrying items
after a retriable exception is encountered."""

import time

from .delay import MAXIMUM_DELAY_SECONDS

BACKOFF_CONSTANT = 2
BACKOFF_BASE = 2

def apply_exponential_backoff_options(item, use_exponential_backoff):
    item['use_exponential_backoff'] = use_exponential_backoff

def apply_exponential_backoff_delay(item):
    if not item.get('use_exponential_backoff'):
        item['last_push_time'] = time.time()
        if 'delay' in item:
            del item['delay']
        return

    this_attempt_number = item['attempts'] # keep in mind this is 0-indexed
    delay_seconds = min(BACKOFF_CONSTANT + (BACKOFF_BASE ** this_attempt_number), MAXIMUM_DELAY_SECONDS)

    # We adjust the last push time by the delay here so that our response
    # time metrics are not skewed by the backoff delay
    item['last_push_time'] = time.time() + delay_seconds
    item['delay'] = delay_seconds

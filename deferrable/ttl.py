import time

def add_ttl_metadata_to_item(item, ttl_seconds):
    item['ttl_seconds'] = ttl_seconds
    item['item_queued_timestamp'] = time.time()

def item_is_expired(item):
    ttl_seconds = item.get('ttl_seconds')
    if not ttl_seconds:
        return False
    item_queued_time = item['item_queued_timestamp']
    item_consumed_time = time.time()
    elapsed_seconds = item_consumed_time - item_queued_time
    if elapsed_seconds > ttl_seconds:
        return True
    return False

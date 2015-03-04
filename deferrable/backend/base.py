class BackendFactory(object):
    def create_backend_for_group(self, group, *args, **kwargs):
        raise NotImplementedError()

    @staticmethod
    def _queue_name(group):
        base = 'deferrable'
        if group:
            return '{}:{}'.format(base, group)
        return base

class Backend(object):
    def __init__(self, group, queue, error_queue):
        self.group = group
        self.queue = queue
        self.error_queue = error_queue

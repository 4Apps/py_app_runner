"""Jobs on PostgreSQL, or on Redis streams behind the same interface.

The database driver is the one to reach for first: a push there is an INSERT, so it joins
the transaction that caused it. Redis cannot do that, whatever it is set to.
"""

from py_app_runner.queue.driver_pg import PgQueue
from py_app_runner.queue.driver_redis import RedisQueue
from py_app_runner.queue.handler import Handler, assert_resolvable, resolve
from py_app_runner.queue.interface import QueueDriver
from py_app_runner.queue.job import Job, QueueError
from py_app_runner.queue.worker import Worker, backoff

__all__ = [
    "Handler",
    "Job",
    "PgQueue",
    "QueueDriver",
    "QueueError",
    "RedisQueue",
    "Worker",
    "assert_resolvable",
    "backoff",
    "resolve",
]

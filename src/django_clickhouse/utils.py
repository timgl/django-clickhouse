import datetime
from queue import Queue, Empty
from threading import Thread, Lock

import os
from itertools import chain
from typing import Union, Any, Optional, TypeVar, Set, Dict, Iterable, Tuple, Iterator, Callable, List

import pytz
import six
from importlib import import_module
from importlib.util import find_spec
from django.db.models import Model as DjangoModel

from .database import connections


T = TypeVar('T')


def get_tz_offset(db_alias=None):  # type: (Optional[str]) -> int
    """
    Returns ClickHouse server timezone offset in minutes
    :param db_alias: The database alias used
    :return: Integer
    """
    db = connections[db_alias]
    return int(db.server_timezone.utcoffset(datetime.datetime.utcnow()).total_seconds() / 60)


def format_datetime(dt, timezone_offset=0, day_end=False, db_alias=None):
    # type: (Union[datetime.date, datetime.datetime], int, bool, Optional[str]) -> str
    """
    Formats datetime and date objects to format that can be used in WHERE conditions of query
    :param dt: datetime.datetime or datetime.date object
    :param timezone_offset: timezone offset (minutes)
    :param day_end: If datetime.date is given and flag is set, returns day end time, not day start.
    :param db_alias: The database alias used
    :return: A string representing datetime
    """
    assert isinstance(dt, (datetime.datetime, datetime.date)), "dt must be datetime.datetime instance"
    assert type(timezone_offset) is int, "timezone_offset must be integer"

    # datetime.datetime inherits datetime.date. So I can't just make isinstance(dt, datetime.date)
    if not isinstance(dt, datetime.datetime):
        t = datetime.time.max if day_end else datetime.time.min
        dt = datetime.datetime.combine(dt, t)

    # Convert datetime to UTC, if it has timezone
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        dt = pytz.utc.localize(dt)
    else:
        dt = dt.astimezone(pytz.utc)

    # Dates in ClickHouse are parsed in server local timezone. So I need to add server timezone
    server_dt = dt - datetime.timedelta(minutes=timezone_offset - get_tz_offset(db_alias))

    return server_dt.strftime("%Y-%m-%d %H:%M:%S")


def module_exists(module_name):  # type: (str) -> bool
    """
    Checks if moudle exists
    :param module_name: Dot-separated module name
    :return: Boolean
    """
    # Python 3.4+
    spam_spec = find_spec(module_name)
    return spam_spec is not None


def lazy_class_import(obj):  # type: (Union[str, Any]) -> Any
    """
    If string is given, imports object by given module path.
    Otherwise returns the object
    :param obj: A string class path or object to return
    :return: Imported object
    """
    if isinstance(obj, six.string_types):
        module_name, obj_name = obj.rsplit('.', 1)
        module = import_module(module_name)

        try:
            return getattr(module, obj_name)
        except AttributeError:
            raise ImportError('Invalid import path `%s`' % obj)
    else:
        return obj


def get_subclasses(cls, recursive=False):  # type: (T, bool) -> Set[T]
    """
    Gets all subclasses of given class
    Attention!!! Classes would be found only if they were imported before using this function
    :param cls: Class to get subcalsses
    :param recursive: If flag is set, returns subclasses of subclasses and so on too
    :return: A list of subclasses
    """
    subclasses = set(cls.__subclasses__())

    if recursive:
        for subcls in subclasses.copy():
            subclasses.update(get_subclasses(subcls, recursive=True))

    return subclasses


def model_to_dict(instance, fields=None, exclude_fields=None):
    # type: (DjangoModel, Optional[Iterable[str]], Optional[Iterable[str]]) -> Dict[str, Any]
    """
    Standard model_to_dict ignores some fields if they have invalid naming
    :param instance: Object to convert to dictionary
    :param fields: Field list to extract from instance
    :param exclude_fields: Filed list to exclude from extraction
    :return: Serialized dictionary
    """
    data = {}

    opts = instance._meta
    fields = fields or {f.name for f in chain(opts.concrete_fields, opts.private_fields, opts.many_to_many)}

    for name in set(fields) - set(exclude_fields or set()):
        val = getattr(instance, name, None)
        if val is not None:
            data[name] = val

    return data


def check_pid(pid):
    """
    Check For the existence of a unix pid.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def int_ranges(items: Iterable[int]) -> Iterator[Tuple[int, int]]:
    """
    Finds continuous intervals in integer iterable.
    :param items: Items to search in
    :return: Iterator over Tuple[start, end]
    """
    interval_start = None
    prev_item = None
    for item in sorted(items):
        if prev_item is None:
            interval_start = prev_item = item
        elif prev_item + 1 == item:
            prev_item = item
        else:
            interval = interval_start, prev_item
            interval_start = prev_item = item
            yield interval

    if interval_start is None:
        raise StopIteration()
    else:
        yield interval_start, prev_item


class ExceptionThread(Thread):
    """
    Thread objects, which catches thread exceptions and raises them in main thread
    """
    def __init__(self, *args, **kwargs):
        super(ExceptionThread, self).__init__(*args, **kwargs)
        self.exc = None

    def run(self):
        try:
            return super(ExceptionThread, self).run()
        except Exception as e:
            self.exc = e

    def join(self, timeout=None):
        super(ExceptionThread, self).join(timeout=timeout)
        if self.exc:
            raise self.exc


def exec_in_parallel(func: Callable, args_queue: Queue, threads_count: Optional[int] = None) -> List[Any]:
    """
    Executes func in multiple threads in parallel
    Functions are expected to be thread safe. If it needs some locks, func must provide them.
    :param func: Function to execute in thread
    :param args_queue: A queue with arguments for separate function call. Each element is tuple of (args, kwargs)
    :param threads_count: Maximum number of parallel threads tho run
    :return: A list of results. Order of results is not guaranteed. Element types depends func return type.
    """
    results = []
    results_lock = Lock()

    # If thread_count is not given, we execute all tasks in parallel.
    # If queue has less elements than threads_count, take queue size.
    threads_count = min(args_queue.qsize(), threads_count) if threads_count else args_queue.qsize()

    def _worker():
        """
        Thread worker, gets next arguments from queue and processes them.
        Results are put into results array using thread safe lock
        :return: None
        """
        finished = False
        while not finished:
            try:
                # Get arguments
                args, kwargs = args_queue.get_nowait()

                # Execute function
                local_res = func(*args, **kwargs)

                # Write result in lock
                with results_lock:
                    results.append(local_res)

                args_queue.task_done()

            except Empty:
                # No data in queue, finish worker thread
                finished = True

    # Run threads
    threads = []
    for index in range(threads_count):
        t = ExceptionThread(target=_worker)
        threads.append(t)
        t.start()

    # Wait for threads to finish
    for t in threads:
        t.join()

    return results


def exec_multi_db_func(func: Callable, using: Iterable[str], *args, threads_count: Optional[int] = None,
                       **kwargs) -> List[Any]:
    """
    Executes multiple databases function in parallel threads. Thread functions (func) receive db alias as first argument
    Another arguments passed to functions - args and kwargs
    If function uses single shard, separate threads are not run, main thread is used.
    :param func: Function to execute on single database
    :param using: A list of database aliases to use.
    :param threads_count: Maximum number of threads to run in parallel
    :return: A list of execution results. Order of execution is not guaranteed.
    """
    using = list(using)
    if len(using) == 0:
        return []
    elif len(using) == 1:
        return [func(using[0], *args, **kwargs)]
    else:
        q = Queue()
        for s in using:
            q.put(([s] + list(args), kwargs))

        return exec_in_parallel(func, q, threads_count=threads_count)

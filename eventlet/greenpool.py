import itertools
import traceback

from eventlet import event
from eventlet import greenthread
from eventlet import queue
from eventlet import semaphore

__all__ = ['GreenPool', 'GreenPile']


class GreenPool(object):
    """The GreenPool class is a pool of green threads.
    """
    def __init__(self, size=1000):
        self.size = size
        self.coroutines_running = set()
        self.sem = semaphore.Semaphore(size)
        self.no_coros_running = event.Event()

    def resize(self, new_size):
        """ Change the max number of greenthreads doing work at any given time.

        If resize is called when there are more than *new_size* greenthreads
        already working on tasks, they will be allowed to complete but no new
        tasks will be allowed to get launched until enough greenthreads finish
        their tasks to drop the overall quantity below *new_size*.  Until
        then, the return value of free() will be negative.
        """
        size_delta = new_size - self.size
        self.sem.counter += size_delta
        self.size = new_size

    def running(self):
        """ Returns the number of greenthreads that are currently executing
        functions in the GreenPool."""
        return len(self.coroutines_running)

    def free(self):
        """ Returns the number of greenthreads available for use.

        If zero or less, the next call to :meth:`spawn` will
        block the calling greenthread until a slot becomes available."""
        return self.sem.counter

    def spawn(self, function, *args, **kwargs):
        """Run the *function* with its arguments in its own green thread.
        Returns the :class:`GreenThread <eventlet.greenthread.GreenThread>`
        object that is running the function, which can be used to retrieve the
        results.

        If the pool is currently at capacity, ``spawn`` will block until one of
        the running greenthreads completes its task and frees up a slot.

        This function is reentrant; *function* can call ``spawn`` on the same
        pool without risk of deadlocking the whole thing.
        """
        # if reentering an empty pool, don't try to wait on a coroutine freeing
        # itself -- instead, just execute in the current coroutine
        current = greenthread.get_current()
        if self.sem.locked() and current in self.coroutines_running:
            # a bit hacky to use the GT without switching to it
            gt = greenthread.GreenThread(current)
            gt.main(function, args, kwargs)
            return gt
        else:
            self.sem.acquire()
            gt = greenthread.spawn(function, *args, **kwargs)
            if not self.coroutines_running:
                self.no_coros_running = event.Event()
            self.coroutines_running.add(gt)
            gt.link(self._spawn_done)
        return gt

    def waitall(self):
        """Waits until all greenthreads in the pool are finished working."""
        assert greenthread.get_current() not in self.coroutines_running, \
                          "Calling waitall() from within one of the "\
                          "GreenPool's greenthreads will never terminate."
        if self.running():
            self.no_coros_running.wait()

    def _spawn_done(self, coro):
        self.sem.release()
        if coro is not None:
            self.coroutines_running.remove(coro)
        # if done processing (no more work is waiting for processing),
        # we can finish off any waitall() calls that might be pending
        if self.sem.balance == self.size:
            self.no_coros_running.send(None)

    def waiting(self):
        """Return the number of greenthreads waiting to spawn.
        """
        if self.sem.balance < 0:
            return -self.sem.balance
        else:
            return 0

    def _do_map(self, func, it, gi):
        for args in it:
            gi.spawn(func, *args)
        gi.spawn(return_stop_iteration)

    def starmap(self, function, iterable):
        """This is the same as :func:`itertools.starmap`, except that *func* is
        executed in a separate green thread for each item, with the concurrency
        limited by the pool's size. In operation, starmap consumes a constant
        amount of memory, proportional to the size of the pool, and is thus
        suited for iterating over extremely long input lists.
        """
        if function is None:
            function = lambda *a: a
        gi = GreenMap(self.size)
        greenthread.spawn(self._do_map, function, iterable, gi)
        return gi

    def imap(self, function, *iterables):
        """This is the same as :func:`itertools.imap`, and has the same
        concurrency and memory behavior as :meth:`starmap`.

        It's quite convenient for, e.g., farming out jobs from a file::

           def worker(line):
               return do_something(line)
           pool = GreenPool()
           for result in pool.imap(worker, open("filename", 'r')):
               print result
        """
        return self.starmap(function, itertools.izip(*iterables))


def return_stop_iteration():
    return StopIteration()


class GreenPile(object):
    """GreenPile is an abstraction representing a bunch of I/O-related tasks.

    Construct a GreenPile with an existing GreenPool object.  The GreenPile will
    then use that pool's concurrency as it processes its jobs.  There can be
    many GreenPiles associated with a single GreenPool.

    A GreenPile can also be constructed standalone, not associated with any
    GreenPool.  To do this, construct it with an integer size parameter instead
    of a GreenPool.

    It is not advisable to iterate over a GreenPile in a different greenthread
    than the one which is calling spawn.  The iterator will exit early in that
    situation.
    """
    def __init__(self, size_or_pool=1000):
        if isinstance(size_or_pool, GreenPool):
            self.pool = size_or_pool
        else:
            self.pool = GreenPool(size_or_pool)
        self.waiters = queue.LightQueue()
        self.used = False
        self.counter = 0

    def spawn(self, func, *args, **kw):
        """Runs *func* in its own green thread, with the result available by
        iterating over the GreenPile object."""
        self.used =  True
        self.counter += 1
        try:
            gt = self.pool.spawn(func, *args, **kw)
            self.waiters.put(gt)
        except:
            self.counter -= 1
            raise

    def __iter__(self):
        return self

    def next(self):
        """Wait for the next result, suspending the current greenthread until it
        is available.  Raises StopIteration when there are no more results."""
        if self.counter == 0 and self.used:
            raise StopIteration()
        try:
            return self.waiters.get().wait()
        finally:
            self.counter -= 1


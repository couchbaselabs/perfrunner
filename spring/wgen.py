import os
import signal
import time
from multiprocessing import Event, Lock, Process, Value
from threading import Timer
from typing import Callable, List, Tuple, Union

import twisted
from decorator import decorator
from numpy import random
from psutil import cpu_count
from twisted.internet import reactor

from logger import logger
from perfrunner.helpers.sync import SyncHotWorkload
from spring.cbgen import CBAsyncGen, CBGen, SubDocGen
from spring.docgen import (
    ArrayIndexingDocument,
    BigFunDocument,
    Document,
    EventingSmallDocument,
    ExtReverseLookupDocument,
    GSIMultiIndexDocument,
    HashJoinDocument,
    HotKey,
    ImportExportDocument,
    ImportExportDocumentArray,
    ImportExportDocumentNested,
    IncompressibleString,
    JoinedDocument,
    KeyForCASUpdate,
    KeyForRemoval,
    LargeDocument,
    LargeItemPlasmaDocument,
    MovingWorkingSetKey,
    NestedDocument,
    NewOrderedKey,
    PackageDocument,
    PowerKey,
    ProfileDocument,
    RefDocument,
    ReverseLookupDocument,
    ReverseRangeLookupDocument,
    SequentialKey,
    SequentialPlasmaDocument,
    SmallPlasmaDocument,
    String,
    TpcDsDocument,
    UniformKey,
    VaryingItemSizePlasmaDocument,
    WorkingSetKey,
    ZipfKey,
)
from spring.querygen import N1QLQueryGen, ViewQueryGen, ViewQueryGenByType
from spring.reservoir import Reservoir


def err(*args, **kwargs):
    pass


twisted.python.log.err = err


@decorator
def with_sleep(method, *args):
    self = args[0]
    if self.target_time is None:
        return method(self)
    else:
        t0 = time.time()
        method(self)
        actual_time = time.time() - t0
        delta = self.target_time - actual_time
        if delta > 0:
            time.sleep(self.CORRECTION_FACTOR * delta)


def set_cpu_afinity(sid):
    os.system('taskset -p -c {} {}'.format(sid % cpu_count(), os.getpid()))


class Worker:

    CORRECTION_FACTOR = 0.975  # empiric!

    BATCH_SIZE = 100

    NAME = 'worker'

    def __init__(self, workload_settings, target_settings, shutdown_event=None):
        self.ws = workload_settings
        self.ts = target_settings
        self.shutdown_event = shutdown_event
        self.sid = 0

        self.next_report = 0.05  # report after every 5% of completion

        self.init_keys()
        self.init_docs()
        self.init_db()
        self.init_creds()

    def init_keys(self):
        self.new_keys = NewOrderedKey(prefix=self.ts.prefix,
                                      fmtr=self.ws.key_fmtr)

        if self.ws.working_set_move_time:
            self.existing_keys = MovingWorkingSetKey(self.ws,
                                                     self.ts.prefix)
        elif self.ws.working_set < 100:
            self.existing_keys = WorkingSetKey(self.ws,
                                               self.ts.prefix)
        elif self.ws.power_alpha:
            self.existing_keys = PowerKey(self.ts.prefix,
                                          self.ws.key_fmtr,
                                          self.ws.power_alpha)
        elif self.ws.zipf_alpha:
            self.existing_keys = ZipfKey(self.ts.prefix,
                                         self.ws.key_fmtr,
                                         self.ws.zipf_alpha)
        else:
            self.existing_keys = UniformKey(self.ts.prefix,
                                            self.ws.key_fmtr)

        self.keys_for_removal = KeyForRemoval(self.ts.prefix,
                                              self.ws.key_fmtr)

        self.keys_for_cas_update = KeyForCASUpdate(self.ws.n1ql_workers,
                                                   self.ts.prefix,
                                                   self.ws.key_fmtr)

    def init_docs(self):
        if not hasattr(self.ws, 'doc_gen') or self.ws.doc_gen == 'basic':
            self.docs = Document(self.ws.size)
        elif self.ws.doc_gen == 'string':
            self.docs = String(self.ws.size)
        elif self.ws.doc_gen == 'nested':
            self.docs = NestedDocument(self.ws.size)
        elif self.ws.doc_gen == 'reverse_lookup':
            self.docs = ReverseLookupDocument(self.ws.size,
                                              self.ts.prefix)
        elif self.ws.doc_gen == 'reverse_range_lookup':
            self.docs = ReverseRangeLookupDocument(self.ws.size,
                                                   self.ts.prefix,
                                                   self.ws.range_distance)
        elif self.ws.doc_gen == 'ext_reverse_lookup':
            self.docs = ExtReverseLookupDocument(self.ws.size,
                                                 self.ts.prefix,
                                                 self.ws.items)
        elif self.ws.doc_gen == 'hash_join':
            self.docs = HashJoinDocument(self.ws.size,
                                         self.ts.prefix,
                                         self.ws.range_distance)
        elif self.ws.doc_gen == 'join':
            self.docs = JoinedDocument(self.ws.size,
                                       self.ts.prefix,
                                       self.ws.items,
                                       self.ws.num_categories,
                                       self.ws.num_replies)
        elif self.ws.doc_gen == 'ref':
            self.docs = RefDocument(self.ws.size,
                                    self.ts.prefix)
        elif self.ws.doc_gen == 'array_indexing':
            self.docs = ArrayIndexingDocument(self.ws.size,
                                              self.ts.prefix,
                                              self.ws.array_size,
                                              self.ws.items)
        elif self.ws.doc_gen == 'profile':
            self.docs = ProfileDocument(self.ws.size,
                                        self.ts.prefix)
        elif self.ws.doc_gen == 'import_export_simple':
            self.docs = ImportExportDocument(self.ws.size,
                                             self.ts.prefix)
        elif self.ws.doc_gen == 'import_export_array':
            self.docs = ImportExportDocumentArray(self.ws.size,
                                                  self.ts.prefix)
        elif self.ws.doc_gen == 'import_export_nested':
            self.docs = ImportExportDocumentNested(self.ws.size,
                                                   self.ts.prefix)
        elif self.ws.doc_gen == 'large':
            self.docs = LargeDocument(self.ws.size)
        elif self.ws.doc_gen == 'gsi_multiindex':
            self.docs = GSIMultiIndexDocument(self.ws.size)
        elif self.ws.doc_gen == 'small_plasma':
            self.docs = SmallPlasmaDocument(self.ws.size)
        elif self.ws.doc_gen == 'sequential_plasma':
            self.docs = SequentialPlasmaDocument(self.ws.size)
        elif self.ws.doc_gen == 'large_item_plasma':
            self.docs = LargeItemPlasmaDocument(self.ws.size,
                                                self.ws.item_size)
        elif self.ws.doc_gen == 'varying_item_plasma':
            self.docs = VaryingItemSizePlasmaDocument(self.ws.size,
                                                      self.ws.size_variation_min,
                                                      self.ws.size_variation_max)
        elif self.ws.doc_gen == 'eventing_small':
            self.docs = EventingSmallDocument(self.ws.size)
        elif self.ws.doc_gen == 'tpc_ds':
            self.docs = TpcDsDocument()
        elif self.ws.doc_gen == 'package':
            self.docs = PackageDocument(self.ws.size)
        elif self.ws.doc_gen == 'incompressible':
            self.docs = IncompressibleString(self.ws.size)
        elif self.ws.doc_gen == 'big_fun':
            self.docs = BigFunDocument()

    def init_db(self):
        params = {
            'bucket': self.ts.bucket,
            'host': self.ts.node,
            'port': 8091,
            'username': self.ts.bucket,
            'password': self.ts.password,
            'ssl_mode': self.ws.ssl_mode,
            'n1ql_timeout': self.ws.n1ql_timeout,
        }

        try:
            self.cb = CBGen(**params)
        except Exception as e:
            raise SystemExit(e)

    def init_creds(self):
        for bucket in getattr(self.ws, 'buckets', []):
            self.cb.client.add_bucket_creds(bucket, self.ts.password)

    def report_progress(self, curr_ops):  # only first worker
        if not self.sid and self.ws.ops < float('inf') and \
                curr_ops > self.next_report * self.ws.ops:
            progress = 100.0 * curr_ops / self.ws.ops
            self.next_report += 0.05
            logger.info('Current progress: {:.2f} %'.format(progress))

    def time_to_stop(self):
        return (self.shutdown_event is not None and
                self.shutdown_event.is_set())

    def seed(self):
        random.seed(seed=self.sid * 9901)

    def dump_stats(self):
        self.reservoir.dump(filename='{}-{}'.format(self.NAME, self.sid))


Client = Union[CBAsyncGen, CBGen, SubDocGen]

Sequence = List[Tuple[str, Callable, Tuple]]


class KVWorker(Worker):

    NAME = 'kv-worker'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.reservoir = Reservoir(num_workers=self.ws.workers)

    @property
    def random_ops(self) -> List[str]:
        ops = \
            ['c'] * self.ws.creates + \
            ['r'] * self.ws.reads + \
            ['u'] * self.ws.updates + \
            ['d'] * self.ws.deletes + \
            ['m'] * (self.ws.reads_and_updates // 2)
        random.shuffle(ops)
        return ops

    def create_args(self, cb: Client,
                    curr_items: int) -> Sequence:
        key = self.new_keys.next(curr_items)
        doc = self.docs.next(key)
        args = key.string, doc, self.ws.persist_to, self.ws.replicate_to

        return [('set', cb.create, args)]

    def read_args(self, cb: Client,
                  curr_items: int, deleted_items: int) -> Sequence:
        key = self.existing_keys.next(curr_items, deleted_items)
        args = key.string,

        return [('get', cb.read, args)]

    def update_args(self, cb: Client,
                    curr_items: int, deleted_items: int) -> Sequence:
        key = self.existing_keys.next(curr_items,
                                      deleted_items,
                                      self.current_hot_load_start,
                                      self.timer_elapse)
        doc = self.docs.next(key)
        args = key.string, doc, self.ws.persist_to, self.ws.replicate_to

        return [('set', cb.update, args)]

    def delete_args(self, cb: Client,
                    deleted_items: int) -> Sequence:
        key = self.keys_for_removal.next(deleted_items)
        args = key.string,

        return [('delete', cb.delete, args)]

    def modify_args(self, cb: Client,
                    curr_items: int, deleted_items: int) -> Sequence:
        key = self.existing_keys.next(curr_items, deleted_items)
        doc = self.docs.next(key)
        read_args = key.string,
        update_args = key.string, doc, self.ws.persist_to, self.ws.replicate_to

        return [('get', cb.read, read_args), ('set', cb.update, update_args)]

    def gen_cmd_sequence(self, cb: Client = None) -> Sequence:
        if not cb:
            cb = self.cb

        curr_items = self.ws.items
        if self.ws.creates:
            with self.lock:
                curr_items = self.curr_items.value
                self.curr_items.value += self.ws.creates

        deleted_items = 0
        if self.ws.deletes:
            with self.lock:
                deleted_items = self.deleted_items.value + \
                    self.ws.deletes * self.ws.workers
                self.deleted_items.value += self.ws.deletes

        cmds = []
        for op in self.random_ops:
            if op == 'c':
                cmds += self.create_args(cb, curr_items)
                curr_items += 1
            elif op == 'r':
                cmds += self.read_args(cb, curr_items, deleted_items)
            elif op == 'u':
                cmds += self.update_args(cb, curr_items, deleted_items)
            elif op == 'd':
                cmds += self.delete_args(cb, deleted_items)
                deleted_items += 1
            elif op == 'm':
                cmds += self.modify_args(cb, curr_items, deleted_items)
        return cmds

    @with_sleep
    def do_batch(self, *args, **kwargs):
        for cmd, func, args in self.gen_cmd_sequence():
            latency = func(*args)
            if latency is not None:
                self.reservoir.update(operation=cmd, value=latency)

    def run_condition(self, curr_ops):
        return curr_ops.value < self.ws.ops and not self.time_to_stop()

    def run(self, sid, lock, curr_ops, curr_items, deleted_items,
            current_hot_load_start=None, timer_elapse=None):

        if self.ws.throughput < float('inf'):
            self.target_time = float(self.BATCH_SIZE) * self.ws.workers / \
                self.ws.throughput
        else:
            self.target_time = None
        self.sid = sid
        self.lock = lock
        self.curr_items = curr_items
        self.deleted_items = deleted_items
        self.current_hot_load_start = current_hot_load_start
        self.timer_elapse = timer_elapse

        self.seed()

        logger.info('Started: {}-{}'.format(self.NAME, self.sid))
        try:
            while self.run_condition(curr_ops):
                with lock:
                    curr_ops.value += self.BATCH_SIZE
                self.do_batch()
                self.report_progress(curr_ops.value)
        except KeyboardInterrupt:
            logger.info('Interrupted: {}-{}'.format(self.NAME, self.sid))
        else:
            logger.info('Finished: {}-{}'.format(self.NAME, self.sid))

        self.dump_stats()


class SubDocWorker(KVWorker):

    NAME = 'sub-doc-worker'

    def init_db(self):
        params = {'bucket': self.ts.bucket, 'host': self.ts.node, 'port': 8091,
                  'username': self.ts.bucket, 'password': self.ts.password}
        self.cb = SubDocGen(**params)

    def read_args(self, cb: Client,
                  curr_items: int, deleted_items: int) -> Sequence:
        key = self.existing_keys.next(curr_items, deleted_items)
        read_args = key.string, self.ws.subdoc_field

        return [('get', cb.read, read_args)]

    def update_args(self, cb: Client,
                    curr_items: int, deleted_items: int) -> Sequence:
        key = self.existing_keys.next(curr_items,
                                      deleted_items,
                                      self.current_hot_load_start,
                                      self.timer_elapse)
        doc = self.docs.next(key)
        update_args = key.string, self.ws.subdoc_field, doc

        return [('set', cb.update, update_args)]


class XATTRWorker(SubDocWorker):

    NAME = 'xattr-worker'

    def read_args(self, cb: Client,
                  curr_items: int, deleted_items: int) -> Sequence:
        key = self.existing_keys.next(curr_items, deleted_items)

        return [('get', cb.read_xattr, (key.string, self.ws.xattr_field))]

    def update_args(self, cb: Client,
                    curr_items: int, deleted_items: int) -> Sequence:
        key = self.existing_keys.next(curr_items,
                                      deleted_items,
                                      self.current_hot_load_start,
                                      self.timer_elapse)
        doc = self.docs.next(key)
        update_args = key.string, self.ws.xattr_field, doc

        return [('set', cb.update_xattr, update_args)]


class AsyncKVWorker(KVWorker):

    NAME = 'async-kv-worker'

    NUM_CONNECTIONS = 8

    def init_db(self):
        params = {'bucket': self.ts.bucket, 'host': self.ts.node, 'port': 8091,
                  'username': self.ts.bucket, 'password': self.ts.password}

        self.cbs = [CBAsyncGen(**params) for _ in range(self.NUM_CONNECTIONS)]
        self.counter = list(range(self.NUM_CONNECTIONS))

    def restart(self, _, cb, i):
        self.counter[i] += 1
        if self.counter[i] == self.BATCH_SIZE:
            actual_time = time.time() - self.time_started
            if self.target_time is not None:
                delta = self.target_time - actual_time
                if delta > 0:
                    time.sleep(self.CORRECTION_FACTOR * delta)

            self.report_progress(self.curr_ops.value)
            if not self.done and (
                    self.curr_ops.value >= self.ws.ops or self.time_to_stop()):
                with self.lock:
                    self.done = True
                logger.info('Finished: {}-{}'.format(self.NAME, self.sid))
                reactor.stop()
            else:
                self.do_batch(_, cb, i)

    def do_batch(self, _, cb, i):
        self.counter[i] = 0
        self.time_started = time.time()

        with self.lock:
            self.curr_ops.value += self.BATCH_SIZE

        for _, func, args in self.gen_cmd_sequence(cb):
            d = func(*args)
            d.addCallback(self.restart, cb, i)
            d.addErrback(self.log_and_restart, cb, i)

    def log_and_restart(self, err, cb, i):
        logger.warn('Request problem with worker-{} thread-{}: {}'.format(
            self.sid, i, err.value)
        )
        self.restart(None, cb, i)

    def error(self, err, cb, i):
        logger.warn('Connection problem with worker-{} thread-{}: {}'.format(
            self.sid, i, err)
        )

        cb.client._close()
        time.sleep(15)
        d = cb.client.connect()
        d.addCallback(self.do_batch, cb, i)
        d.addErrback(self.error, cb, i)

    def run(self, sid, lock, curr_ops, curr_items, deleted_items,
            current_hot_load_start=None, timer_elapse=None):
        set_cpu_afinity(sid)

        if self.ws.throughput < float('inf'):
            self.target_time = (self.BATCH_SIZE * self.ws.workers /
                                float(self.ws.throughput))
        else:
            self.target_time = None

        self.sid = sid
        self.lock = lock
        self.curr_items = curr_items
        self.deleted_items = deleted_items
        self.curr_ops = curr_ops
        self.current_hot_load_start = current_hot_load_start
        self.timer_elapse = timer_elapse

        self.seed()

        self.done = False
        for i, cb in enumerate(self.cbs):
            d = cb.client.connect()
            d.addCallback(self.do_batch, cb, i)
            d.addErrback(self.error, cb, i)
        logger.info('Started: {}-{}'.format(self.NAME, self.sid))
        reactor.run()


class HotReadsWorker(Worker):

    def run(self, sid, *args):
        set_cpu_afinity(sid)

        for key in HotKey(sid, self.ws, self.ts.prefix):
            self.cb.read(key.string)


class SeqUpsertsWorker(Worker):

    def run(self, sid, *args):
        for key in SequentialKey(sid, self.ws, self.ts.prefix):
            doc = self.docs.next(key)
            self.cb.update(key.string, doc)


class SeqXATTRUpdatesWorker(XATTRWorker):

    def run(self, sid, *args):
        for key in SequentialKey(sid, self.ws, self.ts.prefix):
            doc = self.docs.next(key)
            self.cb.update_xattr(key.string, self.ws.xattr_field, doc)


class WorkerFactory:

    def __new__(cls, settings):
        if getattr(settings, 'async', None):
            worker = AsyncKVWorker
        elif getattr(settings, 'seq_upserts') and \
                getattr(settings, 'xattr_field', None):
            worker = SeqXATTRUpdatesWorker
        elif getattr(settings, 'seq_upserts', None):
            worker = SeqUpsertsWorker
        elif getattr(settings, 'hot_reads', None):
            worker = HotReadsWorker
        elif getattr(settings, 'subdoc_field', None):
            worker = SubDocWorker
        elif getattr(settings, 'xattr_field', None):
            worker = XATTRWorker
        else:
            worker = KVWorker
        return worker, settings.workers


class ViewWorkerFactory:

    def __new__(cls, workload_settings):
        return ViewWorker, workload_settings.query_workers


class ViewWorker(Worker):

    NAME = 'query-worker'

    def __init__(self, workload_settings, target_settings, shutdown_event):
        super().__init__(workload_settings, target_settings, shutdown_event)

        self.reservoir = Reservoir(num_workers=self.ws.query_workers)

        if workload_settings.index_type is None:
            self.new_queries = ViewQueryGen(workload_settings.ddocs,
                                            workload_settings.query_params)
        else:
            self.new_queries = ViewQueryGenByType(workload_settings.index_type,
                                                  workload_settings.query_params)

    @with_sleep
    def do_batch(self):
        curr_items_spot = \
            self.curr_items.value - self.ws.creates * self.ws.workers
        deleted_spot = \
            self.deleted_items.value + self.ws.deletes * self.ws.workers

        for _ in range(self.BATCH_SIZE):
            key = self.existing_keys.next(curr_items_spot, deleted_spot)
            doc = self.docs.next(key)
            ddoc_name, view_name, query = self.new_queries.next(doc)

            latency = self.cb.view_query(ddoc_name, view_name, query=query)

            self.reservoir.update(operation='query', value=latency)

    def run(self, sid, lock, curr_ops, curr_items, deleted_items, *args):
        if self.ws.query_throughput < float('inf'):
            self.target_time = float(self.BATCH_SIZE) * self.ws.query_workers / \
                self.ws.query_throughput
        else:
            self.target_time = None
        self.sid = sid
        self.curr_items = curr_items
        self.deleted_items = deleted_items

        try:
            logger.info('Started: {}-{}'.format(self.NAME, self.sid))
            while not self.time_to_stop():
                self.do_batch()
        except KeyboardInterrupt:
            logger.info('Interrupted: {}-{}'.format(self.NAME, self.sid))
        else:
            logger.info('Finished: {}-{}'.format(self.NAME, self.sid))

        self.dump_stats()


class N1QLWorkerFactory:

    def __new__(cls, workload_settings):
        return N1QLWorker, workload_settings.n1ql_workers


class N1QLWorker(Worker):

    NAME = 'query-worker'

    def __init__(self, workload_settings, target_settings, shutdown_event=None):
        super().__init__(workload_settings, target_settings, shutdown_event)

        self.new_queries = N1QLQueryGen(workload_settings.n1ql_queries)

        self.reservoir = Reservoir(num_workers=self.ws.n1ql_workers)

    def read(self):
        curr_items = self.curr_items.value
        if self.ws.doc_gen == 'ext_reverse_lookup':
            curr_items //= 4

        for _ in range(self.ws.n1ql_batch_size):
            key = self.existing_keys.next(curr_items=curr_items,
                                          curr_deletes=0)
            doc = self.docs.next(key)
            query = self.new_queries.next(key.string, doc)

            latency = self.cb.n1ql_query(query)
            self.reservoir.update(operation='query', value=latency)

    def create(self):
        with self.lock:
            curr_items = self.curr_items.value
            self.curr_items.value += self.ws.n1ql_batch_size

        for _ in range(self.ws.n1ql_batch_size):
            curr_items += 1
            key = self.new_keys.next(curr_items=curr_items)
            doc = self.docs.next(key)
            query = self.new_queries.next(key.string, doc)

            latency = self.cb.n1ql_query(query)
            self.reservoir.update(operation='query', value=latency)

    def update(self):
        with self.lock:
            curr_items = self.curr_items.value

        for _ in range(self.ws.n1ql_batch_size):
            key = self.keys_for_cas_update.next(sid=self.sid,
                                                curr_items=curr_items)
            doc = self.docs.next(key)
            query = self.new_queries.next(key.string, doc)

            latency = self.cb.n1ql_query(query)
            self.reservoir.update(operation='query', value=latency)

    @with_sleep
    def do_batch(self):
        if self.ws.n1ql_op == 'read':
            self.read()
        elif self.ws.n1ql_op == 'create':
            self.create()
        elif self.ws.n1ql_op == 'update':
            self.update()

    def run(self, sid, lock, curr_ops, curr_items, *args):
        if self.ws.n1ql_throughput < float('inf'):
            self.target_time = self.ws.n1ql_batch_size * self.ws.n1ql_workers / \
                float(self.ws.n1ql_throughput)
        else:
            self.target_time = None
        self.lock = lock
        self.sid = sid
        self.curr_items = curr_items

        try:
            logger.info('Started: {}-{}'.format(self.NAME, self.sid))
            while not self.time_to_stop():
                self.do_batch()
        except KeyboardInterrupt:
            logger.info('Interrupted: {}-{}'.format(self.NAME, self.sid))
        else:
            logger.info('Finished: {}-{}'.format(self.NAME, self.sid))

        self.dump_stats()


class WorkloadGen:

    def __init__(self, workload_settings, target_settings, timer=None, *args):
        self.ws = workload_settings
        self.ts = target_settings
        self.timer = timer and Timer(timer, self.abort) or None
        self.shutdown_event = timer and Event() or None
        self.worker_processes = []

    def start_workers(self,
                      worker_factory,
                      curr_items=None,
                      deleted_items=None,
                      current_hot_load_start=None,
                      timer_elapse=None):
        curr_ops = Value('L', 0)
        lock = Lock()
        worker_type, total_workers = worker_factory(self.ws)

        for sid in range(total_workers):
            args = (sid, lock, curr_ops, curr_items, deleted_items,
                    current_hot_load_start, timer_elapse)

            worker = worker_type(self.ws, self.ts, self.shutdown_event)

            worker_process = Process(target=worker.run, args=args)
            worker_process.daemon = True
            worker_process.start()
            self.worker_processes.append(worker_process)

            if getattr(self.ws, 'async', False):
                time.sleep(2)

    def set_signal_handler(self):
        """Abort the execution upon receiving a signal from perfrunner."""
        signal.signal(signal.SIGPWR, self.abort)

    def abort(self, *args):
        """Triggers the shutdown event."""
        self.shutdown_event.set()

    @staticmethod
    def store_pid():
        """Store PID of the current Celery worker."""
        pid = os.getpid()
        with open('worker.pid', 'w') as f:
            f.write(str(pid))

    def start_timers(self):
        """Start the optional timers."""
        if self.timer is not None and self.ws.ops == float('inf'):
            self.timer.start()

        if self.ws.working_set_move_time:
            self.sync.start_timer(self.ws)

    def stop_timers(self):
        """Cancel all the active timers."""
        if self.timer is not None:
            self.timer.cancel()

        if self.ws.working_set_move_time:
            self.sync.stop_timer()

    def wait_for_completion(self):
        """Wait until the sub-processes terminate."""
        for process in self.worker_processes:
            process.join()

    def start_all_workers(self):
        """Start all the workers groups."""
        logger.info('Starting all workers')

        curr_items = Value('L', self.ws.items)
        deleted_items = Value('L', 0)
        current_hot_load_start = Value('L', 0)
        timer_elapse = Value('I', 0)

        if self.ws.working_set_move_time:
            current_hot_load_start.value = int(self.ws.items * self.ws.working_set / 100)
            self.sync = SyncHotWorkload(current_hot_load_start, timer_elapse)

        self.start_workers(WorkerFactory,
                           curr_items, deleted_items,
                           current_hot_load_start, timer_elapse)
        self.start_workers(ViewWorkerFactory,
                           curr_items, deleted_items)
        self.start_workers(N1QLWorkerFactory,
                           curr_items, deleted_items)

    def run(self):
        self.start_all_workers()

        self.start_timers()

        self.store_pid()

        self.set_signal_handler()

        self.wait_for_completion()

        self.stop_timers()

import time

from logger import logger
from perfrunner.helpers import misc
from perfrunner.helpers.remote import RemoteHelper
from perfrunner.helpers.rest import RestHelper


class Monitor(RestHelper):

    MAX_RETRY = 60
    MAX_RETRY_RECOVERY = 1200
    MAX_RETRY_TIMER_EVENT = 18000
    MAX_RETRY_BOOTSTRAP = 1200

    MONITORING_DELAY = 5

    POLLING_INTERVAL = 2
    POLLING_INTERVAL_INDEXING = 1
    POLLING_INTERVAL_MACHINE_UP = 10
    POLLING_INTERVAL_ANALYTICS = 15
    POLLING_INTERVAL_EVENTING = 1

    REBALANCE_TIMEOUT = 3600 * 6
    TIMEOUT = 3600 * 12

    DISK_QUEUES = (
        'ep_queue_size',
        'ep_flusher_todo',
        'ep_diskqueue_items',
        'vb_active_queue_size',
        'vb_replica_queue_size',
    )

    DCP_QUEUES = (
        'ep_dcp_replica_items_remaining',
        'ep_dcp_other_items_remaining',
    )

    XDCR_QUEUES = (
        'replication_changes_left',
    )

    def __init__(self, cluster_spec, test_config, verbose):
        super().__init__(cluster_spec=cluster_spec)
        self.cluster_spec = cluster_spec
        self.test_config = test_config
        self.remote = RemoteHelper(cluster_spec, verbose)

    def monitor_rebalance(self, host):
        logger.info('Monitoring rebalance status')

        is_running = True
        last_progress = 0
        last_progress_time = time.time()
        while is_running:
            time.sleep(self.POLLING_INTERVAL)

            is_running, progress = self.get_task_status(host,
                                                        task_type='rebalance')
            if progress == last_progress:
                if time.time() - last_progress_time > self.REBALANCE_TIMEOUT:
                    logger.error('Rebalance hung')
                    break
            else:
                last_progress = progress
                last_progress_time = time.time()

            if progress is not None:
                logger.info('Rebalance progress: {} %'.format(progress))

        logger.info('Rebalance completed')

    def _wait_for_empty_queues(self, host, bucket, queues, stats_function):
        metrics = list(queues)

        start_time = time.time()
        while metrics:
            bucket_stats = stats_function(host, bucket)
            # As we are changing metrics in the loop; take a copy of it to
            # iterate over.
            for metric in list(metrics):
                stats = bucket_stats['op']['samples'].get(metric)
                if stats:
                    last_value = stats[-1]
                    if last_value:
                        logger.info('{} = {:,}'.format(metric, last_value))
                        continue
                    else:
                        logger.info('{} reached 0'.format(metric))
                    metrics.remove(metric)
            if metrics:
                time.sleep(self.POLLING_INTERVAL)
            if time.time() - start_time > self.TIMEOUT:
                raise Exception('Monitoring got stuck')

    def monitor_disk_queues(self, host, bucket):
        logger.info('Monitoring disk queues: {}'.format(bucket))
        self._wait_for_empty_queues(host, bucket, self.DISK_QUEUES,
                                    self.get_bucket_stats)

    def monitor_dcp_queues(self, host, bucket):
        logger.info('Monitoring DCP queues: {}'.format(bucket))
        self._wait_for_empty_queues(host, bucket, self.DCP_QUEUES,
                                    self.get_bucket_stats)

    def _wait_for_xdcr_to_start(self, host: str):
        is_running = False
        while not is_running:
            time.sleep(self.POLLING_INTERVAL)
            is_running, _ = self.get_task_status(host, task_type='xdcr')

    def monitor_xdcr_queues(self, host: str, bucket: str):
        logger.info('Monitoring XDCR queues: {}'.format(bucket))
        self._wait_for_xdcr_to_start(host)
        self._wait_for_empty_queues(host, bucket, self.XDCR_QUEUES,
                                    self.get_xdcr_stats)

    def _get_num_items(self, host: str, bucket: str, total: bool = False) -> int:
        stats = self.get_bucket_stats(host=host, bucket=bucket)
        if total:
            curr_items = stats['op']['samples'].get('curr_items_tot')
        else:
            curr_items = stats['op']['samples'].get('curr_items')
        if curr_items:
            return curr_items[-1]
        return 0

    def monitor_num_items(self, host: str, bucket: str, num_items: int):
        logger.info('Checking the number of items in {}'.format(bucket))
        retries = 0
        while retries < self.MAX_RETRY:
            if self._get_num_items(host, bucket, total=True) == num_items:
                break
            time.sleep(self.POLLING_INTERVAL)
            retries += 1
        else:
            actual_items = self._get_num_items(host, bucket, total=True)
            raise Exception('Mismatch in the number of items: {}'
                            .format(actual_items))

    def monitor_task(self, host, task_type):
        logger.info('Monitoring task: {}'.format(task_type))
        time.sleep(self.MONITORING_DELAY)

        while True:
            time.sleep(self.POLLING_INTERVAL)

            tasks = [task for task in self.get_tasks(host)
                     if task.get('type') == task_type]
            if tasks:
                for task in tasks:
                    logger.info('{}: {}%, bucket: {}, ddoc: {}'.format(
                        task_type, task.get('progress'),
                        task.get('bucket'), task.get('designDocument')
                    ))
            else:
                break
        logger.info('Task {} successfully completed'.format(task_type))

    def monitor_warmup(self, memcached, host, bucket):
        logger.info('Monitoring warmup status: {}@{}'.format(bucket,
                                                             host))

        memcached_port = self.get_memcached_port(host)

        while True:
            stats = memcached.get_stats(host, memcached_port, bucket, 'warmup')
            if b'ep_warmup_state' in stats:
                state = stats[b'ep_warmup_state']
                if state == b'done':
                    return float(stats.get(b'ep_warmup_time', 0))
                else:
                    logger.info('Warmpup status: {}'.format(state))
                    time.sleep(self.POLLING_INTERVAL)
            else:
                    logger.info('No warmup stats are available, continue polling')
                    time.sleep(self.POLLING_INTERVAL)

    def monitor_compression(self, memcached, host, bucket):
        logger.info('Monitoring active compression status')

        memcached_port = self.get_memcached_port(host)

        json_docs = -1
        while json_docs:
            stats = memcached.get_stats(host, memcached_port, bucket)
            json_docs = int(stats[b'ep_active_datatype_json'])
            if json_docs:
                logger.info('Still uncompressed: {:,} items'.format(json_docs))
                time.sleep(self.POLLING_INTERVAL)
        logger.info('All items are compressed')

    def monitor_node_health(self, host):
        logger.info('Monitoring node health')

        for retry in range(self.MAX_RETRY):
            unhealthy_nodes = {
                n for n, status in self.node_statuses(host).items()
                if status != 'healthy'
            } | {
                n for n, status in self.node_statuses_v2(host).items()
                if status != 'healthy'
            }
            if unhealthy_nodes:
                time.sleep(self.POLLING_INTERVAL)
            else:
                break
        else:
            logger.interrupt('Some nodes are not healthy: {}'.format(
                unhealthy_nodes
            ))

    def monitor_analytics_node_active(self, host):
        logger.info('Monitoring analytics node health')

        for retry in range(self.MAX_RETRY):
            active = self.analytics_node_active(host)
            if active:
                break
            else:
                time.sleep(self.POLLING_INTERVAL)
        else:
            logger.interrupt('Analytics node still not healthy: {}'.format(
                host
            ))

    def is_index_ready(self, host: str) -> bool:
        for status in self.get_index_status(host)['status']:
            if status['status'] != 'Ready':
                return False
        return True

    def estimate_pending_docs(self, host: str) -> int:
        stats = self.get_gsi_stats(host)
        pending_docs = 0
        for metric, value in stats.items():
            if 'num_docs_queued' in metric or 'num_docs_pending' in metric:
                pending_docs += value
        return pending_docs

    def monitor_indexing(self, host):
        logger.info('Monitoring indexing progress')

        while not self.is_index_ready(host):
            time.sleep(self.POLLING_INTERVAL_INDEXING * 5)
            pending_docs = self.estimate_pending_docs(host)
            logger.info('Pending docs: {:,}'.format(pending_docs))

        logger.info('Indexing completed')

    def wait_for_secindex_init_build(self, host, indexes):
        # POLL until initial index build is complete
        logger.info(
            "Waiting for the following indexes to be ready: {}".format(indexes))

        indexes_ready = [0 for _ in indexes]

        def get_index_status(json2i, index):
            """Return the index status."""
            for d in json2i["status"]:
                if d["name"] == index:
                    return d["status"]
            return None

        @misc.retry(catch=(KeyError,), iterations=10, wait=30)
        def update_indexes_ready():
            json2i = self.get_index_status(host)
            for i, index in enumerate(indexes):
                status = get_index_status(json2i, index)
                if status == 'Ready':
                    indexes_ready[i] = 1

        init_ts = time.time()
        while sum(indexes_ready) != len(indexes):
            time.sleep(self.POLLING_INTERVAL_INDEXING)
            update_indexes_ready()
        finish_ts = time.time()
        logger.info('secondary index build time: {}'.format(finish_ts - init_ts))
        time_elapsed = round(finish_ts - init_ts)
        return time_elapsed

    def wait_for_secindex_incr_build(self, index_nodes, bucket, indexes, numitems):
        # POLL until incremenal index build is complete
        logger.info('expecting {} num_docs_indexed for indexes {}'.format(numitems, indexes))

        # collect num_docs_indexed information globally from all index nodes
        def get_num_docs_indexed():
            data = self.get_index_stats(index_nodes)
            num_indexed = []
            for index in indexes:
                key = "" + bucket + ":" + index + ":num_docs_indexed"
                val = data[key]
                num_indexed.append(val)
            return num_indexed

        def get_num_docs_index_pending():
            data = self.get_index_stats(index_nodes)
            num_pending = []
            for index in indexes:
                key = "" + bucket + ":" + index + ":num_docs_pending"
                val1 = data[key]
                key = "" + bucket + ":" + index + ":num_docs_queued"
                val2 = data[key]
                val = int(val1) + int(val2)
                num_pending.append(val)
            return num_pending

        expected_num_pending = [0] * len(indexes)
        while True:
            time.sleep(self.POLLING_INTERVAL_INDEXING)
            curr_num_pending = get_num_docs_index_pending()
            if curr_num_pending == expected_num_pending:
                break
        curr_num_indexed = get_num_docs_indexed()
        logger.info("Number of Items indexed {}".format(curr_num_indexed))

    def wait_for_num_connections(self, index_node, expected_connections):
        curr_connections = self.get_index_num_connections(index_node)
        retry = 1
        while curr_connections < expected_connections and retry < self.MAX_RETRY:
            time.sleep(self.POLLING_INTERVAL_INDEXING)
            curr_connections = self.get_index_num_connections(index_node)
            logger.info("Got current connections {}".format(curr_connections))
            retry += 1
        if retry == self.MAX_RETRY:
            return False
        return True

    def wait_for_recovery(self, index_nodes, bucket, index):
        time.sleep(self.MONITORING_DELAY)
        for retry in range(self.MAX_RETRY_RECOVERY):
            response = self.get_index_stats(index_nodes)
            item = "{}:{}:disk_load_duration".format(bucket, index)
            if item in response:
                return response[item]
            else:
                time.sleep(self.POLLING_INTERVAL)
        return -1

    def wait_for_servers(self):
        for retry in range(self.MAX_RETRY):
            logger.info('Waiting for all servers to be available')
            time.sleep(self.POLLING_INTERVAL_MACHINE_UP)

            for server in self.cluster_spec.servers:
                if not self.remote.is_up(server):
                    break
            else:
                logger.info('All nodes are up')
                return

        logger.interrupt('Some nodes are still down')

    def monitor_fts_indexing_queue(self, host: str, index: str, items: int):
        logger.info('Waiting for indexing to finish')
        count = 0
        while count < items:
            count = self.get_fts_doc_count(host, index)
            logger.info('FTS indexed documents: {:,}'.format(count))
            time.sleep(self.POLLING_INTERVAL)

    def monitor_fts_index_persistence(self, hosts: list, index: str):
        logger.info('Waiting for index to be persisted')
        pending_items = 1
        while pending_items:
            persist = 0
            compact = 0
            for host in hosts:
                stats = self.get_fts_stats(host)

                metric = '{}:{}:{}'.format(self.test_config.buckets[0],
                                           index, 'num_recs_to_persist')
                persist += stats[metric]

                metric = '{}:{}:{}'.format(self.test_config.buckets[0],
                                           index, 'total_compactions')
                compact += stats[metric]

            pending_items = persist or compact
            logger.info('Records to persist: {:,}'.format(persist))
            logger.info('Ongoing compactions: {:,}'.format(compact))
            time.sleep(self.POLLING_INTERVAL)

    def monitor_elastic_indexing_queue(self, host: str, index: str):
        logger.info(' Waiting for indexing to finish')
        items = int(self.test_config.fts_settings.test_total_docs)
        count = 0
        while count < items:
            count = self.get_elastic_doc_count(host, index)
            logger.info('Elasticsearch indexed documents: {:,}'.format(count))
            time.sleep(self.POLLING_INTERVAL)

    def monitor_elastic_index_persistence(self, host: str, index: str):
        logger.info('Waiting for index to be persisted')

        pending_items = -1
        while pending_items:
            stats = self.get_elastic_stats(host)
            pending_items = stats['indices'][index]['total']['translog']['operations']
            logger.info('Records to persist: {:,}'.format(pending_items))
            time.sleep(self.POLLING_INTERVAL)

    def wait_for_bootstrap(self, nodes: list, function: str):
        logger.info('Waiting for bootstrap of eventing function: {} '.format(function))
        for node in nodes:
            retry = 1
            while retry < self.MAX_RETRY_BOOTSTRAP:
                if function in self.get_deployed_apps(node):
                    break
                time.sleep(self.POLLING_INTERVAL)
                retry += 1
            if retry == self.MAX_RETRY_BOOTSTRAP:
                logger.info('Failed to bootstrap function: {}, node: {}'.
                            format(function, node))

    def get_num_analytics_items(self, data_node: str, bucket: str) -> int:
        stats_key = '{}:all:incoming_records_count_total'.format(bucket)
        num_items = 0
        for node in self.get_active_nodes_by_role(data_node, 'cbas'):
            stats = self.get_analytics_stats(node)
            num_items += stats[stats_key]
        return num_items

    def monitor_data_synced(self, data_node: str, bucket: str) -> int:
        logger.info('Waiting for data to be synced from {}'.format(data_node))

        num_items = self._get_num_items(data_node, bucket)

        while True:
            num_analytics_items = self.get_num_analytics_items(data_node,
                                                               bucket)
            if num_analytics_items == num_items:
                break
            logger.info('Analytics has {:,} docs (target is {:,})'.format(
                num_analytics_items, num_items))
            time.sleep(self.POLLING_INTERVAL_ANALYTICS)

        return num_items

    def wait_for_timer_event(self, node: str, function: str, event="DOC_TIMER_EVENTS"):
        logger.info('Waiting for timer events to start processing: {} '.format(function))
        retry = 1
        while retry < self.MAX_RETRY_TIMER_EVENT:
            if 0 < self.get_num_events_processed(
                    event=event, node=node, name=function):
                break
            time.sleep(self.POLLING_INTERVAL_EVENTING)
            retry += 1
        if retry == self.MAX_RETRY_TIMER_EVENT:
            logger.info('Failed to get timer event for function: {}'.format(function))

    def wait_for_all_mutations_processed(self, host: str, bucket1: str, bucket2: str):
        logger.info('Waiting for mutations to be processed of eventing function')
        retry = 1
        while retry < self.MAX_RETRY_BOOTSTRAP:
            if self._get_num_items(host=host, bucket=bucket1) == \
                    self._get_num_items(host=host, bucket=bucket2):
                break
            retry += 1
            time.sleep(self.POLLING_INTERVAL_EVENTING)
        if retry == self.MAX_RETRY_BOOTSTRAP:
            logger.info('Failed to process all mutations... TIMEOUT')

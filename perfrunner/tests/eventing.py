import calendar
import json
import time

from logger import logger
from perfrunner.helpers.cbmonitor import timeit, with_stats
from perfrunner.helpers.misc import pretty_dict
from perfrunner.tests import PerfTest, TargetIterator


class EventingTest(PerfTest):

    """Eventing test base class.

    This is base class for eventing related operations required
    to measure eventing performance parameters.
    """

    FUNCTION_SAMPLE_FILE = "tests/eventing/config/function_sample.json"

    COLLECTORS = {'eventing_stats': True, 'ns_server_system': True}

    STAT_REQ_FIELDS = ["event_processing_stats", "events_remaining", "execution_stats",
                       "failure_stats", "function_name", "latency_stats", "lcb_exception_stats",
                       "planner_stats", "plasma_stats"]

    def __init__(self, *args):
        super().__init__(*args)

        self.functions = self.test_config.eventing_settings.functions
        self.worker_count = self.test_config.eventing_settings.worker_count
        self.cpp_worker_thread_count = self.test_config.eventing_settings.cpp_worker_thread_count
        self.timer_worker_pool_size = self.test_config.eventing_settings.timer_worker_pool_size
        self.worker_queue_cap = self.test_config.eventing_settings.worker_queue_cap
        self.timer_timeout = self.test_config.eventing_settings.timer_timeout
        self.timer_fuzz = self.test_config.eventing_settings.timer_fuzz
        self.time = self.test_config.access_settings.time
        self.rebalance_settings = self.test_config.rebalance_settings

        for master in self.cluster_spec.masters:
            self.rest.add_rbac_user(
                host=master,
                user="eventing",
                password="password",
                roles=['admin'],
            )

        self.target_iterator = TargetIterator(self.cluster_spec, self.test_config, "eventing")

    @timeit
    def deploy_and_bootstrap(self, func, name):
        self.rest.deploy_function(node=self.eventing_nodes[0],
                                  func=func, name=name)

        self.monitor.wait_for_bootstrap(nodes=self.eventing_nodes,
                                        function=name)

    def set_functions(self) -> float:
        with open(self.FUNCTION_SAMPLE_FILE) as f:
            func = json.load(f)

        func["settings"]["worker_count"] = self.worker_count
        func["settings"]["cpp_worker_thread_count"] = self.cpp_worker_thread_count
        func["settings"]["timer_worker_pool_size"] = self.timer_worker_pool_size
        func["settings"]["worker_queue_cap"] = self.worker_queue_cap
        time_to_deploy = 0
        for name, filename in self.functions.items():
            with open(filename, 'r') as myfile:
                code = myfile.read()
                if self.timer_timeout:
                    expiry = calendar.timegm(time.gmtime()) + self.timer_timeout
                    code = code.replace("fixed_expiry", str(expiry))
                    code = code.replace("fuzz_factor", str(self.timer_fuzz))
                func["appname"] = name
                func["appcode"] = code
            self.rest.create_function(node=self.eventing_nodes[0],
                                      func=func, name=name)
            time_to_deploy += self.deploy_and_bootstrap(func, name)
        return time_to_deploy

    def process_latency_stats(self):
        ret_val = {}
        all_stats = self.rest.get_eventing_stats(node=self.eventing_nodes[0], full_stats=True)
        for stat in all_stats:
            latency_stats = stat["latency_stats"]
            ret_val[stat["function_name"]] = sorted(latency_stats.items(), key=lambda x: int(x[0]))

        return ret_val

    def get_on_update_success(self):
        on_update_success = 0
        for node in self.eventing_nodes:
            stats = self.rest.get_eventing_stats(node=node)
            for stat in stats:
                logger.info("Execution stats for {node}: {stats}"
                            .format(node=node,
                                    stats=pretty_dict(stat["execution_stats"])))
                on_update_success += stat["execution_stats"]["on_update_success"]
        return on_update_success

    def get_doc_timer_responses(self):
        doc_timer_responses = 0
        for node in self.eventing_nodes:
            stats = self.rest.get_eventing_stats(node=node)
            for stat in stats:
                logger.info("Event processing stats for {node}: {stats}"
                            .format(node=node,
                                    stats=pretty_dict(stat["event_processing_stats"])))
                doc_timer_responses += \
                    stat["event_processing_stats"]["DOC_TIMER_RESPONSES_RECEIVED"]
        return doc_timer_responses

    def get_timer_events(self, event_name: str, function_name: str):
        timer_events = 0
        for node in self.eventing_nodes:
            events = self.rest.get_num_events_processed(
                event=event_name, node=node, name=function_name)
            timer_events += events
        return timer_events

    @with_stats
    @timeit
    def load_access_and_wait(self):
        self.load()
        self.access_bg()
        self.sleep()

    def validate_failures(self):
        for node in self.eventing_nodes:
            all_stats = self.rest.get_eventing_stats(node=node, full_stats=True)

            req_stats = [{key: fun_stat[key]
                          for key in self.STAT_REQ_FIELDS} for fun_stat in all_stats]

            logger.info("Required stats for {node} : {stats}"
                        .format(node=node,
                                stats=pretty_dict(req_stats)))
            for function_stats in all_stats:
                execution_stats = function_stats["execution_stats"]
                failure_stats = function_stats["failure_stats"]

                # Validate Execution stats
                for stat, value in execution_stats.items():
                    if "failure" in stat and value != 0:
                        raise Exception(
                            '{function}: {node}: {stat} is not zero'.format(
                                function=function_stats["function_name"], node=node, stat=stat))

                # Validate Failure stats
                for stat, value in failure_stats.items():
                    if value != 0 and stat != "timestamp":
                        raise Exception(
                            '{function}: {node}: {stat} is not zero'.format(
                                function=function_stats["function_name"], node=node, stat=stat))

    def print_max_rss_values(self):
        for node in self.eventing_nodes:
            for name, filename in self.functions.items():
                max_consumer_rss, max_producer_rss = \
                    self.metrics.get_max_rss_values(function_name=name, server=node)
                logger.info("Max Consumer rss is {}MB on {} for function {}".
                            format(max_consumer_rss, node, name))
                logger.info("Max Producer rss is {}MB on {} for function {}".
                            format(max_producer_rss, node, name))

    def post_test(self):
        self.validate_failures()
        self.print_max_rss_values()

    def run(self):
        self.set_functions()

        time_elapsed = self.load_access_and_wait()

        self.report_kpi(time_elapsed)

        self.post_test()


class FunctionsTimeTest(EventingTest):
    @timeit
    def process_all_docs(self):
        self.monitor.wait_for_all_mutations_processed(host=self.master_node,
                                                      bucket1=self.test_config.buckets[0],
                                                      bucket2=self.test_config.eventing_buckets[0]
                                                      )

    @with_stats
    def apply_function(self):
        time_to_deploy = self.set_functions()
        time_to_process = self.process_all_docs()
        return time_to_deploy, time_to_deploy + time_to_process

    def run(self):
        self.load()

        time_to_deploy, time_to_process = self.apply_function()

        self.report_kpi(time_to_deploy, time_to_process)

        self.post_test()

    def _report_kpi(self, time_to_deploy, time_to_process):
        self.reporter.post(
            *self.metrics.function_time(time=time_to_deploy,
                                        time_type="bootstrap",
                                        initials="Bootstrap time(min)")
        )
        self.reporter.post(
            *self.metrics.function_time(time=time_to_process,
                                        time_type="processing",
                                        initials="Processing time(min)")
        )


class FunctionsThroughputTest(EventingTest):
    def _report_kpi(self, time_elapsed):
        events_successfully_processed = self.get_on_update_success()
        self.reporter.post(
            *self.metrics.function_throughput(time=time_elapsed,
                                              event_name=None,
                                              events_processed=events_successfully_processed)
        )


class CreateDocTimerThroughputTest(EventingTest):
    def _report_kpi(self, time_elapsed):
        events_successfully_processed = self.get_doc_timer_responses()
        self.reporter.post(
            *self.metrics.function_throughput(time=time_elapsed,
                                              event_name=None,
                                              events_processed=events_successfully_processed)
        )


class FunctionsIndexThroughputTest(EventingTest):
    def __init__(self, *args):
        super().__init__(*args)
        self.on_update_success = 0

    @with_stats
    @timeit
    def access_and_wait(self):
        self.create_index()
        self.access_bg()
        self.sleep()

    def create_index(self):
        storage = self.test_config.gsi_settings.storage
        indexes = self.test_config.gsi_settings.indexes

        for server in self.index_nodes:
            for bucket in self.test_config.buckets:
                for name, field in indexes.items():
                    self.rest.create_index(host=server,
                                           bucket=bucket,
                                           name=name,
                                           field=field,
                                           storage=storage)

    def run(self):
        self.set_functions()

        self.load()

        on_update_success = self.get_on_update_success()
        time_elapsed = self.access_and_wait()
        self.on_update_success = \
            self.get_on_update_success() - on_update_success

        self.report_kpi(time_elapsed)

        self.post_test()

    def _report_kpi(self, time_elapsed):
        self.reporter.post(
            *self.metrics.function_throughput(time=time_elapsed,
                                              event_name=None,
                                              events_processed=self.on_update_success)
        )


class FunctionsThroughputIndexN1QLTest(FunctionsIndexThroughputTest):
    def run(self):
        self.set_functions()

        self.create_index()

        time_elapsed = self.load_access_and_wait()

        self.report_kpi(time_elapsed)

        self.post_test()

    def _report_kpi(self, time_elapsed):
        events_successfully_processed = self.get_on_update_success()
        self.reporter.post(
            *self.metrics.function_throughput(time=time_elapsed,
                                              event_name=None,
                                              events_processed=events_successfully_processed)
        )


class EventingRebalance(EventingTest):

    def pre_rebalance(self):
        """Execute additional steps before rebalance."""
        logger.info('Sleeping for {} seconds before taking actions'
                    .format(self.rebalance_settings.start_after))
        time.sleep(self.rebalance_settings.start_after)

    def post_rebalance(self):
        """Execute additional steps after rebalance."""
        logger.info('Sleeping for {} seconds before finishing'
                    .format(self.rebalance_settings.stop_after))
        time.sleep(self.rebalance_settings.stop_after)

    def rebalance(self, initial_nodes, nodes_after):
        for _, servers in self.cluster_spec.clusters:
            master = servers[0]
            ejected_nodes = []
            new_nodes = enumerate(
                servers[initial_nodes:nodes_after],
                start=initial_nodes
            )
            known_nodes = servers[:nodes_after]
            for i, node in new_nodes:
                roles = self.cluster_spec.roles[node]
                self.rest.add_node(master, node, roles)
                if "eventing" in roles:
                    self.eventing_nodes.append(node)

            self.rest.rebalance(master, known_nodes, ejected_nodes)

    @timeit
    def rebalance_time(self):
        initial_nodes = self.test_config.cluster.initial_nodes
        self.rebalance(initial_nodes[0], self.rebalance_settings.nodes_after[0])
        self.monitor.monitor_rebalance(self.master_node)


class FunctionsRebalanceThroughputTest(EventingRebalance):

    def __init__(self, *args):
        super().__init__(*args)
        self.on_update_success = 0

    @with_stats
    def execute_handler(self):
        self.pre_rebalance()

        on_update_success = self.get_on_update_success()
        time_taken = self.rebalance_time()
        self.on_update_success = \
            self.get_on_update_success() - on_update_success

        self.post_rebalance()
        time_taken = round(time_taken, 2)
        logger.info("Time taken for rebalance: {}sec".format(time_taken))
        return time_taken

    def run(self):
        self.set_functions()
        self.load()
        self.access_bg()

        time_taken = self.execute_handler()

        self.report_kpi(time_taken)
        self.post_test()

    def _report_kpi(self, time_elapsed):
        self.reporter.post(
            *self.metrics.function_throughput(time=time_elapsed,
                                              event_name=None,
                                              events_processed=self.on_update_success)
        )


class FunctionsRebalanceTimeThroughputTest(FunctionsRebalanceThroughputTest):
    def _report_kpi(self, time_elapsed):
        super()._report_kpi(time_elapsed)
        self.reporter.post(
            *self.metrics.eventing_rebalance_time(time=time_elapsed)
        )


class FunctionsScalingThroughputTest(EventingTest):

    def __init__(self, *args):
        super().__init__(*args)
        self.on_update_success = 0

    @with_stats
    def execute_handler(self):
        on_update_success = self.get_on_update_success()
        self.sleep()
        self.on_update_success = \
            self.get_on_update_success() - on_update_success

    def run(self):
        self.load()
        self.set_functions()

        self.execute_handler()

        self.report_kpi(self.test_config.access_settings.time)
        self.post_test()

    def _report_kpi(self, time_elapsed):
        self.reporter.post(
            *self.metrics.function_throughput(time=time_elapsed,
                                              event_name=None,
                                              events_processed=self.on_update_success)
        )


class TimerTest(EventingTest):

    @with_stats
    def process_timer_events(self):
        for name, filename in self.functions.items():
            self.monitor.wait_for_timer_event(node=self.eventing_nodes[0],
                                              function=name)
            break
        self.sleep()

    def run(self):
        self.set_functions()

        self.load()

        self.process_timer_events()

        self.report_kpi(self.time)

        self.post_test()


class TimerThroughputTest(TimerTest):

    def _report_kpi(self, time_elapsed):
        self.reporter.post(
            *self.metrics.function_throughput(time=time_elapsed,
                                              event_name="DOC_TIMER_EVENTS",
                                              events_processed=0)
        )


class CronTimerThroughputTest(TimerTest):
    EVENT_NAME = "CRON_TIMER_EVENTS"

    @with_stats
    def process_timer_events(self):
        for name, filename in self.functions.items():
            self.monitor.wait_for_timer_event(node=self.eventing_nodes[0],
                                              function=name,
                                              event=self.EVENT_NAME)
            break
        self.sleep()

    def _report_kpi(self, time_elapsed):
        self.reporter.post(
            *self.metrics.function_throughput(time=time_elapsed,
                                              event_name=self.EVENT_NAME,
                                              events_processed=0)
        )


class TimerRebalanceThroughputTest(EventingRebalance):
    EVENT_NAME = "DOC_TIMER_EVENTS"

    def __init__(self, *args):
        super().__init__(*args)
        self.timer_events = 0
        self.function_names = []
        for name, filename in self.functions.items():
            self.function_names.append(name)

    @with_stats
    def execute_handler(self):
        self.pre_rebalance()

        timer_events = self.get_timer_events(self.EVENT_NAME, self.function_names[0])
        time_taken = self.rebalance_time()
        self.timer_events = \
            self.get_timer_events(self.EVENT_NAME, self.function_names[0]) - timer_events

        self.post_rebalance()
        time_taken = round(time_taken, 2)
        logger.info("Time taken for rebalance: {}sec".format(time_taken))
        return time_taken

    def wait_for_timer_event(self):
        for name, filename in self.functions.items():
            self.monitor.wait_for_timer_event(node=self.eventing_nodes[0],
                                              function=name)
            break

    def run(self):
        self.set_functions()
        self.load()
        self.wait_for_timer_event()

        time_taken = self.execute_handler()

        self.report_kpi(time_taken)
        self.post_test()

    def _report_kpi(self, time_elapsed):
        self.reporter.post(
            *self.metrics.function_throughput(time=time_elapsed,
                                              event_name=None,
                                              events_processed=self.timer_events)
        )


class TimerRebalanceTimeThroughputTest(TimerRebalanceThroughputTest):
    def _report_kpi(self, time_elapsed):
        super()._report_kpi(time_elapsed)
        self.reporter.post(
            *self.metrics.eventing_rebalance_time(time=time_elapsed)
        )


class FunctionsLatencyTest(EventingTest):
    def _report_kpi(self, time_elapsed):
        latency_stats = self.process_latency_stats()
        self.reporter.post(
            *self.metrics.function_latency(percentile=80.0, latency_stats=latency_stats)
        )

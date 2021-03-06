# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2020 ScyllaDB

import contextlib

from performance_regression_test import PerformanceRegressionTest
from sdcm.utils.alternator import create_table as alternator_create_table, set_table_write_isolation, \
    ignore_alternator_client_errors, WriteIsolation
from sdcm.utils.common import normalize_ipv6_url
from sdcm.sct_events import Severity, EventsSeverityChangerFilter, DatabaseLogEvent


class PerformanceRegressionAlternatorTest(PerformanceRegressionTest):
    def __init__(self, *args):
        super().__init__(*args)

        # suppress YCSB client error and timeout to warnings for all the test in this class
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(ignore_alternator_client_errors())
        self.stack.enter_context(EventsSeverityChangerFilter(event_class=DatabaseLogEvent, regex=r".*Operation timed out.*",
                                                             severity=Severity.WARNING, extra_time_to_expiration=30))
        self.stack.enter_context(EventsSeverityChangerFilter(event_class=DatabaseLogEvent, regex=r'.*Operation failed for system.paxos.*',
                                                             severity=Severity.WARNING, extra_time_to_expiration=30))

    def _workload(self, stress_cmd, stress_num, test_name=None, sub_type=None, keyspace_num=1, prefix='', debug_message='',  # pylint: disable=too-many-arguments,arguments-differ
                  save_stats=True, alternator=True):
        if not alternator:
            stress_cmd = stress_cmd.replace('dynamodb', 'cassandra-cql')

        if debug_message:
            self.log.debug(debug_message)

        if save_stats:
            self.create_test_stats(test_name=test_name, sub_type=sub_type,
                                   doc_id_with_timestamp=True, append_sub_test_to_name=False)
        stress_queue = self.run_stress_thread(stress_cmd=stress_cmd, stress_num=stress_num, keyspace_num=keyspace_num,
                                              prefix=prefix, stats_aggregate_cmds=False)
        self.get_stress_results(queue=stress_queue, store_results=True)
        if save_stats:
            self.update_test_details(scylla_conf=True, alternator=alternator)

    @property
    def alternator_endpoint_url(self):
        alternator_port = self.params.get('alternator_port')
        endpoint_url = f'http://{normalize_ipv6_url(self.db_cluster.nodes[0].external_address)}:{alternator_port}'
        return endpoint_url

    def create_alternator_table(self, table_params):
        # drop tables
        table = 'alternator_usertable'
        self.log.info('Drop keyspace/table {}'.format(table))
        with self.cql_connection_patient(self.db_cluster.nodes[0]) as session:
            session.execute('DROP KEYSPACE IF EXISTS {};'.format(table))

        # create new tables
        self.log.info("Going to create alternator tables")
        alternator_create_table(self.alternator_endpoint_url, table_params)

        self.run_fstrim_on_all_db_nodes()
        self.wait_no_compactions_running()

    def create_cql_ks_and_table(self, field_number):
        node = self.db_cluster.nodes[0]
        with self.cql_connection_patient(node) as session:
            session.execute(
                """create keyspace ycsb WITH REPLICATION = {'class' : 'SimpleStrategy', 'replication_factor': 3 };""")
            fields = ', '.join([f"field{i} varchar" for i in range(field_number)])
            session.execute(f"""CREATE TABLE ycsb.usertable (
                                y_id varchar primary key,
                                {fields});""")

    def preload_data(self):
        # if test require a pre-population of data
        prepare_write_cmd = self.params.get('prepare_write_cmd')
        if prepare_write_cmd:
            # create new document in ES with doc_id = test_id + timestamp
            # allow to correctly save results for future compare
            self.create_test_stats(sub_type='write-prepare', doc_id_with_timestamp=True)
            stress_queue = list()
            params = {'prefix': 'preload-'}
            for stress_type in ['dynamodb', 'cassandra-cql']:
                # Check if the prepare_cmd is a list of commands
                if not isinstance(prepare_write_cmd, str) and len(prepare_write_cmd) > 1:
                    # Check if it should be round_robin across loaders
                    if self.params.get('round_robin'):
                        self.log.debug('Populating data using round_robin')
                        params.update({'stress_num': 1, 'round_robin': True})

                    for stress_cmd in prepare_write_cmd:
                        params.update({'stress_cmd': stress_cmd.replace('dynamodb', stress_type)})

                        # Run all stress commands
                        params.update(dict(stats_aggregate_cmds=False))
                        self.log.debug('RUNNING stress cmd: {}'.format(stress_cmd.replace('dynamodb', stress_type)))
                        stress_queue.append(self.run_stress_thread(**params))

                # One stress cmd command
                else:
                    stress_queue.append(self.run_stress_thread(stress_cmd=prepare_write_cmd.replace('dynamodb', stress_type), stress_num=1,
                                                               prefix='preload-', stats_aggregate_cmds=False))

            for stress in stress_queue:
                self.get_stress_results(queue=stress, store_results=False)

            self.update_test_details()
        else:
            self.log.warning("No prepare command defined in YAML!")

    def test_write(self):
        """
        Test steps:

        1. Run a write workload with cql
        2. Run a write workload without lwt
        3. Run a write workload with lwt enabled
        """
        # run a write workload
        base_cmd_w = self.params.get('stress_cmd_w')
        stress_multiplier = self.params.get('stress_multiplier', default=1)

        self.create_cql_ks_and_table(field_number=10)

        self._workload(sub_type='cql', stress_cmd=base_cmd_w,
                       stress_num=stress_multiplier, keyspace_num=1, alternator=False)

        # run a workload without lwt as baseline
        table_params = dict(dynamodb_primarykey_type=self.params.get(
            'dynamodb_primarykey_type'), alternator_write_isolation=WriteIsolation.FORBID_RMW)
        self.create_alternator_table(table_params)

        self._workload(sub_type='without-lwt', stress_cmd=base_cmd_w, stress_num=stress_multiplier, keyspace_num=1)

        # run a workload with lwt
        table_params = dict(dynamodb_primarykey_type=self.params.get(
            'dynamodb_primarykey_type'), alternator_write_isolation=WriteIsolation.ALWAYS_USE_LWT)
        self.create_alternator_table(table_params)
        self._workload(sub_type='with-lwt', stress_cmd=base_cmd_w, stress_num=stress_multiplier, keyspace_num=1)

        self.check_regression_with_baseline('cql')

    def test_read(self):
        """
        Test steps:

        1. Run a write workload as a preparation
        2. Run a read workload with cql
        3. Run a read workload without lwt
        4. Run a read workload with lwt enabled
        """

        base_cmd_r = self.params.get('stress_cmd_r')
        stress_multiplier = self.params.get('stress_multiplier', default=1)
        self.run_fstrim_on_all_db_nodes()
        # run a prepare write workload
        self.create_cql_ks_and_table(field_number=10)
        table_params = dict(dynamodb_primarykey_type=self.params.get(
            'dynamodb_primarykey_type'), alternator_write_isolation=WriteIsolation.FORBID_RMW)
        self.create_alternator_table(table_params)

        self.preload_data()

        self._workload(sub_type='cql', stress_cmd=base_cmd_r,
                       stress_num=stress_multiplier, keyspace_num=1, alternator=False)

        # run a workload without lwt as baseline
        set_table_write_isolation(isolation=WriteIsolation.FORBID_RMW, endpoint_url=self.alternator_endpoint_url)
        self._workload(sub_type='without-lwt', stress_cmd=base_cmd_r, stress_num=stress_multiplier, keyspace_num=1)

        # run a workload with lwt
        set_table_write_isolation(isolation=WriteIsolation.ALWAYS_USE_LWT, endpoint_url=self.alternator_endpoint_url)
        self._workload(sub_type='with-lwt', stress_cmd=base_cmd_r, stress_num=stress_multiplier, keyspace_num=1)

        self.check_regression_with_baseline('cql')

    def test_mixed(self):
        """
        Test steps:

        1. Run a write workload as a preparation
        2. Run a mixed workload with cql
        3. Run a mixed workload without lwt
        4. Run a mixed workload with lwt
        """

        base_cmd_m = self.params.get('stress_cmd_m')
        stress_multiplier = self.params.get('stress_multiplier', default=1)
        self.run_fstrim_on_all_db_nodes()

        self.create_cql_ks_and_table(field_number=10)
        table_params = dict(dynamodb_primarykey_type=self.params.get(
            'dynamodb_primarykey_type'), alternator_write_isolation=WriteIsolation.FORBID_RMW)
        self.create_alternator_table(table_params)

        # run a write workload as a preparation
        self.preload_data()

        self._workload(sub_type='cql', stress_cmd=base_cmd_m,
                       stress_num=stress_multiplier, keyspace_num=1, alternator=False)

        # run a mixed workload
        # run a workload without lwt as baseline
        set_table_write_isolation(isolation=WriteIsolation.FORBID_RMW, endpoint_url=self.alternator_endpoint_url)
        self._workload(sub_type='without-lwt', stress_cmd=base_cmd_m, stress_num=stress_multiplier, keyspace_num=1)

        # run a workload with lwt
        set_table_write_isolation(isolation=WriteIsolation.ALWAYS_USE_LWT, endpoint_url=self.alternator_endpoint_url)
        self._workload(sub_type='with-lwt', stress_cmd=base_cmd_m, stress_num=stress_multiplier, keyspace_num=1)

        self.check_regression_with_baseline('cql')

    def test_latency(self):
        """
        Test steps:

        1. Prepare cluster with data (reach steady_stet of compactions and ~x10 capacity than RAM.
        with round_robin and list of stress_cmd - the data will load several times faster.
        2. Run READ workload with cql.
        3. Run READ workload without lwt.
        4. Run READ workload with lwt.
        5. Run WRITE workload with cql.
        6. Run WRITE workload without lwt.
        7. Run WRITE workload with lwt.
        8. Run MIXED workload with cql.
        9. Run MIXED workload without lwt.
        10. Run MIXED workload with lwt.
        """

        table_params = dict(dynamodb_primarykey_type=self.params.get(
            'dynamodb_primarykey_type'), alternator_write_isolation=WriteIsolation.FORBID_RMW)
        self.create_alternator_table(table_params)
        set_table_write_isolation(isolation=WriteIsolation.FORBID_RMW, endpoint_url=self.alternator_endpoint_url)

        self.create_cql_ks_and_table(field_number=10)
        self.run_fstrim_on_all_db_nodes()
        self.preload_data()

        base_cmd_w = self.params.get('stress_cmd_w')
        base_cmd_r = self.params.get('stress_cmd_r')
        base_cmd_m = self.params.get('stress_cmd_m')

        stress_multiplier = 2
        self.wait_no_compactions_running(n=120)

        self.run_fstrim_on_all_db_nodes()
        self._workload(
            test_name=self.id() + '_read', sub_type='cql', stress_cmd=base_cmd_r, stress_num=stress_multiplier,
            keyspace_num=1, alternator=False)

        # run a workload without lwt as baseline
        set_table_write_isolation(isolation=WriteIsolation.FORBID_RMW, endpoint_url=self.alternator_endpoint_url)
        self._workload(
            test_name=self.id() + '_read', sub_type='without-lwt', stress_cmd=base_cmd_r, stress_num=stress_multiplier,
            keyspace_num=1)

        self.wait_no_compactions_running()
        # run a workload with lwt
        set_table_write_isolation(isolation=WriteIsolation.ALWAYS_USE_LWT, endpoint_url=self.alternator_endpoint_url)
        self._workload(
            test_name=self.id() + '_read', sub_type='with-lwt', stress_cmd=base_cmd_r, stress_num=stress_multiplier,
            keyspace_num=1)
        self.check_regression_with_baseline('cql')

        stress_multiplier = 1
        self.run_fstrim_on_all_db_nodes()

        self.wait_no_compactions_running()
        self._workload(
            test_name=self.id() + '_write', sub_type='cql', stress_cmd=base_cmd_w + " -target 10000", stress_num=stress_multiplier,
            keyspace_num=1, alternator=False)

        self.wait_no_compactions_running()
        # run a workload without lwt as baseline
        set_table_write_isolation(isolation=WriteIsolation.FORBID_RMW, endpoint_url=self.alternator_endpoint_url)
        self._workload(
            test_name=self.id() + '_write', sub_type='without-lwt', stress_cmd=base_cmd_w + " -target 10000", stress_num=stress_multiplier,
            keyspace_num=1)

        self.wait_no_compactions_running(n=120)
        # run a workload with lwt
        set_table_write_isolation(isolation=WriteIsolation.ALWAYS_USE_LWT, endpoint_url=self.alternator_endpoint_url)
        self._workload(
            test_name=self.id() + '_write', sub_type='with-lwt', stress_cmd=base_cmd_w + " -target 3000", stress_num=stress_multiplier,
            keyspace_num=1)
        self.check_regression_with_baseline('cql')

        stress_multiplier = 1
        self.wait_no_compactions_running(n=120)
        self.run_fstrim_on_all_db_nodes()

        self._workload(test_name=self.id() + '_mixed', sub_type='cql',
                       stress_cmd=base_cmd_m + " -target 10000", stress_num=stress_multiplier, keyspace_num=1, alternator=False)

        self.wait_no_compactions_running()
        # run a workload without lwt as baseline
        set_table_write_isolation(isolation=WriteIsolation.FORBID_RMW, endpoint_url=self.alternator_endpoint_url)
        self._workload(test_name=self.id() + '_mixed', sub_type='without-lwt',
                       stress_cmd=base_cmd_m + " -target 10000", stress_num=stress_multiplier, keyspace_num=1)

        self.wait_no_compactions_running()
        # run a workload with lwt
        set_table_write_isolation(isolation=WriteIsolation.ALWAYS_USE_LWT, endpoint_url=self.alternator_endpoint_url)
        self._workload(test_name=self.id() + '_mixed', sub_type='with-lwt',
                       stress_cmd=base_cmd_m + " -target 5000", stress_num=stress_multiplier, keyspace_num=1)

        self.check_regression_with_baseline('cql')

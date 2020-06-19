#!/usr/bin/env python

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

import shutil
import sys
import os
import time
from tempfile import TemporaryDirectory
from textwrap import dedent

from cassandra import ConsistencyLevel
from cassandra.query import SimpleStatement

from sdcm.tester import ClusterTester

def print_file_to_stdout(path: str) -> None:
    with open(path, "r") as f:
       shutil.copyfileobj(f, sys.stdout)

# Write a CQL select result to a file.
def write_cql_result(res, path: str):
    with open(path, 'w') as f:
        for r in res:
            f.write(str(r) + '\n')
        f.flush()
        os.fsync(f.fileno())

SCYLLA_MIGRATE_URL = "https://kbr-scylla.s3-eu-west-1.amazonaws.com/scylla-migrate"
REPLICATOR_URL = "https://kbr-scylla.s3-eu-west-1.amazonaws.com/scylla-cdc-replicator-0.0.1-SNAPSHOT-jar-with-dependencies.jar"

def keyspace_def(rf: int) -> str:
    return f"create keyspace if not exists ks with replication = {{'class': 'SimpleStrategy', 'replication_factor': {rf}}}"

TABLE_DEF_BASE = """create table if not exists ks.tb (
    pk int,
    ck int,
    v text,
    primary key (pk, ck))
"""
TABLE_DEF_MASTER = TABLE_DEF_BASE + " with cdc = {'enabled': true}"

class CDCReplicationTest(ClusterTester):
    def test_replication(self):
        self.log.info('Adding node to db_cluster... ')
        node2 = self.db_cluster.add_nodes(count=1, enable_auto_bootstrap=True)[0]
        self.log.info('Waiting for added node to initialize...')
        self.db_cluster.wait_for_init(node_list=[node2])

        self.log.info('Adding node to db_cluster... ')
        node3 = self.db_cluster.add_nodes(count=1, enable_auto_bootstrap=True)[0]
        self.log.info('Waiting for added node to initialize...')
        self.db_cluster.wait_for_init(node_list=[node3])

        master_node = self.db_cluster.nodes[0]
        replica_node = self.cs_db_cluster.nodes[0]

        self.log.info('Creating table on master cluster.')
        with self.cql_connection_patient(node=master_node) as sess:
            sess.execute(keyspace_def(3))
            sess.execute(TABLE_DEF_MASTER)

        self.log.info('Creating table on replica cluster.')
        with self.cql_connection_patient(node=replica_node) as sess:
            sess.execute(keyspace_def(1))
            sess.execute(TABLE_DEF_BASE)

        self.log.info('Waiting for the latest CDC generation to start...')
        # 2 * ring_delay (ring_delay = 30s)
        time.sleep(60)

        self.log.info('Starting cassandra-stress')
        stress_thread = self.run_stress_cassandra_thread(stress_cmd=self.params.get('stress_cmd'))

        self.log.info('Let C-S run for a while...')
        time.sleep(10)

        self.log.info('Starting nemesis.')
        self.db_cluster.add_nemesis(nemesis=self.get_nemesis_class(), tester_obj=self)
        self.db_cluster.start_nemesis()

        loader_node : BaseNode = self.loaders.nodes[0]

        self.log.info('Installing tmux on loader node.')
        res = loader_node.remoter.run(cmd='sudo yum install -y tmux')
        if res.exit_status != 0:
            self.fail('Could not install tmux.')

        self.log.info('Getting scylla-migrate on loader node.')
        res = loader_node.remoter.run(cmd=f'wget {SCYLLA_MIGRATE_URL} -O scylla-migrate && chmod +x scylla-migrate')
        if res.exit_status != 0:
            self.fail('Could not obtain scylla-migrate.')

        self.log.info('Getting replicator on loader node.')
        res = loader_node.remoter.run(cmd=f'wget {REPLICATOR_URL} -O replicator.jar')
        if res.exit_status != 0:
            self.fail('Could not obtain CDC replicator.')

        # We run the replicator in a tmux session so that remoter.run returns immediately
        # (the replicator will run in the background). We redirect the output to a log file for later extraction.
        replicator_script = dedent("""
            (cat >runreplicator.sh && chmod +x runreplicator.sh && tmux new-session -d -s 'replicator' ./runreplicator.sh) <<'EOF'
            #!/bin/bash

            java -cp replicator.jar com.scylladb.scylla.cdc.replicator.Main -k ks -t tb -s {} -d {} -cl {} 2>&1 | tee replicatorlog
            EOF
        """.format(master_node.external_address, replica_node.external_address, 'one'))

        self.log.info('Replicator script:\n{}'.format(replicator_script))

        self.log.info('Starting replicator.')
        res = loader_node.remoter.run(cmd=replicator_script)
        if res.exit_status != 0:
            self.fail('Could not start CDC replicator.')

        self.log.info('Let C-S run for a while...')
        time.sleep(30)

        self.log.info('Stopping nemesis before bootstrapping a new node.')
        self.db_cluster.stop_nemesis(timeout=60)

        self.log.info('Bootstrapping a new node...')
        new_node = self.db_cluster.add_nodes(count=1, enable_auto_bootstrap=True)[0]
        self.log.info('Waiting for new node to finish initializing...')
        self.db_cluster.wait_for_init(node_list=[new_node])

        self.log.info('Bootstrapped, restarting nemesis.')
        self.db_cluster.start_nemesis()

        self.log.info('Waiting for C-S to finish...')
        stress_results = stress_thread.get_results()

        self.log.info('Stress results: {}'.format([r for r in stress_results]))

        self.log.info('Waiting for replicator to finish (sleeping 60s)...')
        time.sleep(60)

        self.log.info('Stopping nemesis.')
        self.db_cluster.stop_nemesis(timeout=60)

        self.log.info('Fetching replicator logs.')
        replicator_log_path = os.path.join(self.logdir, 'replicator.log')
        loader_node.remoter.receive_files(src='replicatorlog', dst=replicator_log_path)

        self.log.info('Comparing table contents using scylla-migrate...')
        res = loader_node.remoter.run(cmd=
            './scylla-migrate check --master-address {} --replica-address {}'
            ' --ignore-schema-difference ks.tb 2>&1 | tee migratelog'.format(
                    master_node.external_address, replica_node.external_address))

        migrate_log_path = os.path.join(self.logdir, 'scylla-migrate.log')
        loader_node.remoter.receive_files(src='migratelog', dst=migrate_log_path)
        with open(migrate_log_path) as f:
            consistency_ok = 'Consistency check OK.\n' in (line for line in f)

        if not consistency_ok:
            self.log.error('Inconsistency detected.')
        if res.exit_status != 0:
            self.log.error('scylla-migrate command returned status', res.exit_status)

        if consistency_ok and res.exit_status == 0:
            self.log.info('Consistency check successful.')
            return

        # Collect more data for analysis

        self.log.info('scylla-migrate log:')
        print_file_to_stdout(migrate_log_path)
        self.log.info('Replicator log:')
        print_file_to_stdout(replicator_log_path)

        with self.cql_connection_patient(node=master_node) as sess:
            self.log.info('Fetching master table...')
            res = sess.execute(SimpleStatement('select * from ks.tb',
                consistency_level=ConsistencyLevel.QUORUM, fetch_size=1000))
            write_cql_result(res, os.path.join(self.logdir, 'master-table'))

        with self.cql_connection_patient(node=replica_node) as sess:
            self.log.info('Fetching replica table...')
            res = sess.execute(SimpleStatement('select * from ks.tb',
                consistency_level=ConsistencyLevel.QUORUM, fetch_size=1000))
            write_cql_result(res, os.path.join(self.logdir, 'replica-table'))

        self.fail('Consistency check failed.')

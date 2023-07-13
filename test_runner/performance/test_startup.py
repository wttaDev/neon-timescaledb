from contextlib import closing

import pytest
import requests
from fixtures.benchmark_fixture import MetricReport, NeonBenchmarker
from fixtures.neon_fixtures import NeonEnvBuilder


# Just start and measure duration.
#
# This test runs pretty quickly and can be informative when used in combination
# with emulated network delay. Some useful delay commands:
#
# 1. Add 2msec delay to all localhost traffic
# `sudo tc qdisc add dev lo root handle 1:0 netem delay 2msec`
#
# 2. Test that it works (you should see 4ms ping)
# `ping localhost`
#
# 3. Revert back to normal
# `sudo tc qdisc del dev lo root netem`
#
# NOTE this test might not represent the real startup time because the basebackup
#      for a large database might be larger if there's a lof of transaction metadata,
#      or safekeepers might need more syncing, or there might be more operations to
#      apply during config step, like more users, databases, or extensions. By default
#      we load extensions 'neon,pg_stat_statements,timescaledb,pg_cron', but in this
#      test we only load neon.
def test_startup_simple(neon_env_builder: NeonEnvBuilder, zenbenchmark: NeonBenchmarker):
    neon_env_builder.num_safekeepers = 3
    env = neon_env_builder.init_start()

    env.neon_cli.create_branch("test_startup")

    endpoint = None

    # We do two iterations so we can see if the second startup is faster. It should
    # be because the compute node should already be configured with roles, databases,
    # extensions, etc from the first run.
    for i in range(2):
        # Start
        with zenbenchmark.record_duration(f"{i}_start_and_select"):
            if endpoint:
                endpoint.start()
            else:
                endpoint = env.endpoints.create_start("test_startup")
            endpoint.safe_psql("select 1;")

        # Get metrics
        metrics = requests.get(f"http://localhost:{endpoint.http_port}/metrics.json").json()
        durations = {
            "wait_for_spec_ms": f"{i}_wait_for_spec",
            "sync_safekeepers_ms": f"{i}_sync_safekeepers",
            "basebackup_ms": f"{i}_basebackup",
            "start_postgres_ms": f"{i}_start_postgres",
            "config_ms": f"{i}_config",
            "total_startup_ms": f"{i}_total_startup",
        }
        for key, name in durations.items():
            value = metrics[key]
            zenbenchmark.record(name, value, "ms", report=MetricReport.LOWER_IS_BETTER)

        # Check basebackup size makes sense
        basebackup_bytes = metrics["basebackup_bytes"]
        if i > 0:
            assert basebackup_bytes < 100 * 1024

        # Stop so we can restart
        endpoint.stop()

        # Imitate optimizations that console would do for the second start
        endpoint.respec(skip_pg_catalog_updates=True)


# This test sometimes runs for longer than the global 5 minute timeout.
@pytest.mark.timeout(600)
def test_startup(neon_env_builder: NeonEnvBuilder, zenbenchmark: NeonBenchmarker):
    neon_env_builder.num_safekeepers = 3
    env = neon_env_builder.init_start()

    # Start
    env.neon_cli.create_branch("test_startup")
    with zenbenchmark.record_duration("startup_time"):
        endpoint = env.endpoints.create_start("test_startup")
        endpoint.safe_psql("select 1;")

    # Restart
    endpoint.stop_and_destroy()
    with zenbenchmark.record_duration("restart_time"):
        endpoint.create_start("test_startup")
        endpoint.safe_psql("select 1;")

    # Fill up
    num_rows = 1000000  # 30 MB
    num_tables = 100
    with closing(endpoint.connect()) as conn:
        with conn.cursor() as cur:
            for i in range(num_tables):
                cur.execute(f"create table t_{i} (i integer);")
                cur.execute(f"insert into t_{i} values (generate_series(1,{num_rows}));")

    # Read
    with zenbenchmark.record_duration("read_time"):
        endpoint.safe_psql("select * from t_0;")

    # Read again
    with zenbenchmark.record_duration("second_read_time"):
        endpoint.safe_psql("select * from t_0;")

    # Restart
    endpoint.stop_and_destroy()
    with zenbenchmark.record_duration("restart_with_data"):
        endpoint.create_start("test_startup")
        endpoint.safe_psql("select 1;")

    # Read
    with zenbenchmark.record_duration("read_after_restart"):
        endpoint.safe_psql("select * from t_0;")

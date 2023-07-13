from __future__ import annotations

import abc
import asyncio
import enum
import filecmp
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import textwrap
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Flag, auto
from functools import cached_property
from itertools import chain, product
from pathlib import Path
from types import TracebackType
from typing import Any, Dict, Iterator, List, Optional, Tuple, Type, Union, cast
from urllib.parse import urlparse

import asyncpg
import backoff
import boto3
import jwt
import psycopg2
import pytest
import requests
from _pytest.config import Config
from _pytest.config.argparsing import Parser
from _pytest.fixtures import FixtureRequest

# Type-related stuff
from psycopg2.extensions import connection as PgConnection
from psycopg2.extensions import cursor as PgCursor
from psycopg2.extensions import make_dsn, parse_dsn
from typing_extensions import Literal

from fixtures.log_helper import log
from fixtures.pageserver.http import PageserverHttpClient
from fixtures.pageserver.utils import wait_for_last_record_lsn, wait_for_upload
from fixtures.pg_version import PgVersion
from fixtures.types import Lsn, TenantId, TimelineId
from fixtures.utils import (
    ATTACHMENT_NAME_REGEX,
    allure_add_grafana_links,
    allure_attach_from_dir,
    get_self_dir,
    subprocess_capture,
)

"""
This file contains pytest fixtures. A fixture is a test resource that can be
summoned by placing its name in the test's arguments.

A fixture is created with the decorator @pytest.fixture decorator.
See docs: https://docs.pytest.org/en/6.2.x/fixture.html

There are several environment variables that can control the running of tests:
NEON_BIN, POSTGRES_DISTRIB_DIR, etc. See README.md for more information.

There's no need to import this file to use it. It should be declared as a plugin
inside conftest.py, and that makes it available to all tests.

Don't import functions from this file, or pytest will emit warnings. Instead
put directly-importable functions into utils.py or another separate file.
"""

Env = Dict[str, str]

DEFAULT_OUTPUT_DIR: str = "test_output"
DEFAULT_BRANCH_NAME: str = "main"

BASE_PORT: int = 15000
WORKER_PORT_NUM: int = 1000


def pytest_configure(config: Config):
    """
    Check that we do not overflow available ports range.
    """

    numprocesses = config.getoption("numprocesses")
    if (
        numprocesses is not None and BASE_PORT + numprocesses * WORKER_PORT_NUM > 32768
    ):  # do not use ephemeral ports
        raise Exception("Too many workers configured. Cannot distribute ports for services.")


@pytest.fixture(scope="session")
def base_dir() -> Iterator[Path]:
    # find the base directory (currently this is the git root)
    base_dir = get_self_dir().parent.parent
    log.info(f"base_dir is {base_dir}")

    yield base_dir


@pytest.fixture(scope="function")
def neon_binpath(base_dir: Path, build_type: str) -> Iterator[Path]:
    if os.getenv("REMOTE_ENV"):
        # we are in remote env and do not have neon binaries locally
        # this is the case for benchmarks run on self-hosted runner
        return

    # Find the neon binaries.
    if env_neon_bin := os.environ.get("NEON_BIN"):
        binpath = Path(env_neon_bin)
    else:
        binpath = base_dir / "target" / build_type
    log.info(f"neon_binpath is {binpath}")

    if not (binpath / "pageserver").exists():
        raise Exception(f"neon binaries not found at '{binpath}'")

    yield binpath


@pytest.fixture(scope="function")
def pg_distrib_dir(base_dir: Path) -> Iterator[Path]:
    if env_postgres_bin := os.environ.get("POSTGRES_DISTRIB_DIR"):
        distrib_dir = Path(env_postgres_bin).resolve()
    else:
        distrib_dir = base_dir / "pg_install"

    log.info(f"pg_distrib_dir is {distrib_dir}")
    yield distrib_dir


@pytest.fixture(scope="session")
def top_output_dir(base_dir: Path) -> Iterator[Path]:
    # Compute the top-level directory for all tests.
    if env_test_output := os.environ.get("TEST_OUTPUT"):
        output_dir = Path(env_test_output).resolve()
    else:
        output_dir = base_dir / DEFAULT_OUTPUT_DIR
    output_dir.mkdir(exist_ok=True)

    log.info(f"top_output_dir is {output_dir}")
    yield output_dir


@pytest.fixture(scope="function")
def versioned_pg_distrib_dir(pg_distrib_dir: Path, pg_version: PgVersion) -> Iterator[Path]:
    versioned_dir = pg_distrib_dir / pg_version.v_prefixed

    psql_bin_path = versioned_dir / "bin/psql"
    postgres_bin_path = versioned_dir / "bin/postgres"

    if os.getenv("REMOTE_ENV"):
        # When testing against a remote server, we only need the client binary.
        if not psql_bin_path.exists():
            raise Exception(f"psql not found at '{psql_bin_path}'")
    else:
        if not postgres_bin_path.exists():
            raise Exception(f"postgres not found at '{postgres_bin_path}'")

    log.info(f"versioned_pg_distrib_dir is {versioned_dir}")
    yield versioned_dir


def shareable_scope(fixture_name: str, config: Config) -> Literal["session", "function"]:
    """Return either session of function scope, depending on TEST_SHARED_FIXTURES envvar.

    This function can be used as a scope like this:
    @pytest.fixture(scope=shareable_scope)
    def myfixture(...)
       ...
    """
    scope: Literal["session", "function"]

    if os.environ.get("TEST_SHARED_FIXTURES") is None:
        # Create the environment in the per-test output directory
        scope = "function"
    elif (
        os.environ.get("BUILD_TYPE") is not None
        and os.environ.get("DEFAULT_PG_VERSION") is not None
    ):
        scope = "session"
    else:
        pytest.fail(
            "Shared environment(TEST_SHARED_FIXTURES) requires BUILD_TYPE and DEFAULT_PG_VERSION to be set",
            pytrace=False,
        )

    return scope


@pytest.fixture(scope="session")
def worker_seq_no(worker_id: str) -> int:
    # worker_id is a pytest-xdist fixture
    # it can be master or gw<number>
    # parse it to always get a number
    if worker_id == "master":
        return 0
    assert worker_id.startswith("gw")
    return int(worker_id[2:])


@pytest.fixture(scope="session")
def worker_base_port(worker_seq_no: int) -> int:
    # so we divide ports in ranges of 100 ports
    # so workers have disjoint set of ports for services
    return BASE_PORT + worker_seq_no * WORKER_PORT_NUM


def get_dir_size(path: str) -> int:
    """Return size in bytes."""
    totalbytes = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            totalbytes += os.path.getsize(os.path.join(root, name))

    return totalbytes


def can_bind(host: str, port: int) -> bool:
    """
    Check whether a host:port is available to bind for listening

    Inspired by the can_bind() perl function used in Postgres tests, in
    vendor/postgres-v14/src/test/perl/PostgresNode.pm
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        # TODO: The pageserver and safekeepers don't use SO_REUSEADDR at the
        # moment. If that changes, we should use start using SO_REUSEADDR here
        # too, to allow reusing ports more quickly.
        # See https://github.com/neondatabase/neon/issues/801
        # sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            sock.bind((host, port))
            sock.listen()
            return True
        except socket.error:
            log.info(f"Port {port} is in use, skipping")
            return False
        finally:
            sock.close()


class PortDistributor:
    def __init__(self, base_port: int, port_number: int):
        self.iterator = iter(range(base_port, base_port + port_number))
        self.port_map: Dict[int, int] = {}

    def get_port(self) -> int:
        for port in self.iterator:
            if can_bind("localhost", port):
                return port
        raise RuntimeError(
            "port range configured for test is exhausted, consider enlarging the range"
        )

    def replace_with_new_port(self, value: Union[int, str]) -> Union[int, str]:
        """
        Returns a new port for a port number in a string (like "localhost:1234") or int.
        Replacements are memorised, so a substitution for the same port is always the same.
        """

        # TODO: replace with structural pattern matching for Python >= 3.10
        if isinstance(value, int):
            return self._replace_port_int(value)

        if isinstance(value, str):
            return self._replace_port_str(value)

        raise TypeError(f"unsupported type {type(value)} of {value=}")

    def _replace_port_int(self, value: int) -> int:
        known_port = self.port_map.get(value)
        if known_port is None:
            known_port = self.port_map[value] = self.get_port()

        return known_port

    def _replace_port_str(self, value: str) -> str:
        # Use regex to find port in a string
        # urllib.parse.urlparse produces inconvenient results for cases without scheme like "localhost:5432"
        # See https://bugs.python.org/issue27657
        ports = re.findall(r":(\d+)(?:/|$)", value)
        assert len(ports) == 1, f"can't find port in {value}"
        port_int = int(ports[0])

        return value.replace(f":{port_int}", f":{self._replace_port_int(port_int)}")


@pytest.fixture(scope="session")
def port_distributor(worker_base_port: int) -> PortDistributor:
    return PortDistributor(base_port=worker_base_port, port_number=WORKER_PORT_NUM)


@pytest.fixture(scope="session")
def httpserver_listen_address(port_distributor: PortDistributor):
    port = port_distributor.get_port()
    return ("localhost", port)


@pytest.fixture(scope="function")
def default_broker(
    port_distributor: PortDistributor,
    test_output_dir: Path,
    neon_binpath: Path,
) -> Iterator[NeonBroker]:
    # multiple pytest sessions could get launched in parallel, get them different ports/datadirs
    client_port = port_distributor.get_port()
    broker_logfile = test_output_dir / "repo" / "storage_broker.log"

    broker = NeonBroker(logfile=broker_logfile, port=client_port, neon_binpath=neon_binpath)
    yield broker
    broker.stop()


@pytest.fixture(scope="session")
def run_id() -> Iterator[uuid.UUID]:
    yield uuid.uuid4()


@pytest.fixture(scope="session")
def mock_s3_server(port_distributor: PortDistributor) -> Iterator[MockS3Server]:
    mock_s3_server = MockS3Server(port_distributor.get_port())
    yield mock_s3_server
    mock_s3_server.kill()


class PgProtocol:
    """Reusable connection logic"""

    def __init__(self, **kwargs: Any):
        self.default_options = kwargs

    def connstr(self, **kwargs: Any) -> str:
        """
        Build a libpq connection string for the Postgres instance.
        """
        return str(make_dsn(**self.conn_options(**kwargs)))

    def conn_options(self, **kwargs: Any) -> Dict[str, Any]:
        """
        Construct a dictionary of connection options from default values and extra parameters.
        An option can be dropped from the returning dictionary by None-valued extra parameter.
        """
        result = self.default_options.copy()
        if "dsn" in kwargs:
            result.update(parse_dsn(kwargs["dsn"]))
        result.update(kwargs)
        result = {k: v for k, v in result.items() if v is not None}

        # Individual statement timeout in seconds. 2 minutes should be
        # enough for our tests, but if you need a longer, you can
        # change it by calling "SET statement_timeout" after
        # connecting.
        options = result.get("options", "")
        if "statement_timeout" not in options:
            options = f"-cstatement_timeout=120s {options}"
        result["options"] = options
        return result

    # autocommit=True here by default because that's what we need most of the time
    def connect(self, autocommit: bool = True, **kwargs: Any) -> PgConnection:
        """
        Connect to the node.
        Returns psycopg2's connection object.
        This method passes all extra params to connstr.
        """
        conn: PgConnection = psycopg2.connect(**self.conn_options(**kwargs))

        # WARNING: this setting affects *all* tests!
        conn.autocommit = autocommit
        return conn

    @contextmanager
    def cursor(self, autocommit: bool = True, **kwargs: Any) -> Iterator[PgCursor]:
        """
        Shorthand for pg.connect().cursor().
        The cursor and connection are closed when the context is exited.
        """
        with closing(self.connect(autocommit=autocommit, **kwargs)) as conn:
            yield conn.cursor()

    async def connect_async(self, **kwargs: Any) -> asyncpg.Connection:
        """
        Connect to the node from async python.
        Returns asyncpg's connection object.
        """

        # asyncpg takes slightly different options than psycopg2. Try
        # to convert the defaults from the psycopg2 format.

        # The psycopg2 option 'dbname' is called 'database' is asyncpg
        conn_options = self.conn_options(**kwargs)
        if "dbname" in conn_options:
            conn_options["database"] = conn_options.pop("dbname")

        # Convert options='-c<key>=<val>' to server_settings
        if "options" in conn_options:
            options = conn_options.pop("options")
            for match in re.finditer(r"-c(\w*)=(\w*)", options):
                key = match.group(1)
                val = match.group(2)
                if "server_options" in conn_options:
                    conn_options["server_settings"].update({key: val})
                else:
                    conn_options["server_settings"] = {key: val}
        return await asyncpg.connect(**conn_options)

    def safe_psql(self, query: str, **kwargs: Any) -> List[Tuple[Any, ...]]:
        """
        Execute query against the node and return all rows.
        This method passes all extra params to connstr.
        """
        return self.safe_psql_many([query], **kwargs)[0]

    def safe_psql_many(self, queries: List[str], **kwargs: Any) -> List[List[Tuple[Any, ...]]]:
        """
        Execute queries against the node and return all rows.
        This method passes all extra params to connstr.
        """
        result: List[List[Any]] = []
        with closing(self.connect(**kwargs)) as conn:
            with conn.cursor() as cur:
                for query in queries:
                    log.info(f"Executing query: {query}")
                    cur.execute(query)

                    if cur.description is None:
                        result.append([])  # query didn't return data
                    else:
                        result.append(cur.fetchall())
        return result


@dataclass
class AuthKeys:
    pub: str
    priv: str

    def generate_token(self, *, scope: str, **token_data: str) -> str:
        token = jwt.encode({"scope": scope, **token_data}, self.priv, algorithm="EdDSA")
        # cast(Any, self.priv)

        # jwt.encode can return 'bytes' or 'str', depending on Python version or type
        # hinting or something (not sure what). If it returned 'bytes', convert it to 'str'
        # explicitly.
        if isinstance(token, bytes):
            token = token.decode()

        return token

    def generate_pageserver_token(self) -> str:
        return self.generate_token(scope="pageserverapi")

    def generate_safekeeper_token(self) -> str:
        return self.generate_token(scope="safekeeperdata")

    def generate_tenant_token(self, tenant_id: TenantId) -> str:
        return self.generate_token(scope="tenant", tenant_id=str(tenant_id))


class MockS3Server:
    """
    Starts a mock S3 server for testing on a port given, errors if the server fails to start or exits prematurely.
    Relies that `poetry` and `moto` server are installed, since it's the way the tests are run.

    Also provides a set of methods to derive the connection properties from and the method to kill the underlying server.
    """

    def __init__(
        self,
        port: int,
    ):
        self.port = port

        # XXX: do not use `shell=True` or add `exec ` to the command here otherwise.
        # We use `self.subprocess.kill()` to shut down the server, which would not "just" work in Linux
        # if a process is started from the shell process.
        self.subprocess = subprocess.Popen(["poetry", "run", "moto_server", "s3", f"-p{port}"])
        error = None
        try:
            return_code = self.subprocess.poll()
            if return_code is not None:
                error = f"expected mock s3 server to run but it exited with code {return_code}. stdout: '{self.subprocess.stdout}', stderr: '{self.subprocess.stderr}'"
        except Exception as e:
            error = f"expected mock s3 server to start but it failed with exception: {e}. stdout: '{self.subprocess.stdout}', stderr: '{self.subprocess.stderr}'"
        if error is not None:
            log.error(error)
            self.kill()
            raise RuntimeError("failed to start s3 mock server")

    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def region(self) -> str:
        return "us-east-1"

    def access_key(self) -> str:
        return "test"

    def secret_key(self) -> str:
        return "test"

    def kill(self):
        self.subprocess.kill()


@enum.unique
class RemoteStorageKind(str, enum.Enum):
    LOCAL_FS = "local_fs"
    MOCK_S3 = "mock_s3"
    REAL_S3 = "real_s3"
    # Pass to tests that are generic to remote storage
    # to ensure the test pass with or without the remote storage
    NOOP = "noop"


def available_remote_storages() -> List[RemoteStorageKind]:
    remote_storages = [RemoteStorageKind.LOCAL_FS, RemoteStorageKind.MOCK_S3]
    if os.getenv("ENABLE_REAL_S3_REMOTE_STORAGE") is not None:
        remote_storages.append(RemoteStorageKind.REAL_S3)
        log.info("Enabling real s3 storage for tests")
    else:
        log.info("Using mock implementations to test remote storage")
    return remote_storages


@dataclass
class LocalFsStorage:
    root: Path


@dataclass
class S3Storage:
    bucket_name: str
    bucket_region: str
    access_key: str
    secret_key: str
    endpoint: Optional[str] = None
    prefix_in_bucket: Optional[str] = None

    def access_env_vars(self) -> Dict[str, str]:
        return {
            "AWS_ACCESS_KEY_ID": self.access_key,
            "AWS_SECRET_ACCESS_KEY": self.secret_key,
        }


RemoteStorage = Union[LocalFsStorage, S3Storage]


# serialize as toml inline table
def remote_storage_to_toml_inline_table(remote_storage: RemoteStorage) -> str:
    if isinstance(remote_storage, LocalFsStorage):
        remote_storage_config = f"local_path='{remote_storage.root}'"
    elif isinstance(remote_storage, S3Storage):
        remote_storage_config = f"bucket_name='{remote_storage.bucket_name}',\
            bucket_region='{remote_storage.bucket_region}'"

        if remote_storage.prefix_in_bucket is not None:
            remote_storage_config += f",prefix_in_bucket='{remote_storage.prefix_in_bucket}'"

        if remote_storage.endpoint is not None:
            remote_storage_config += f",endpoint='{remote_storage.endpoint}'"
    else:
        raise Exception("invalid remote storage type")

    return f"{{{remote_storage_config}}}"


class RemoteStorageUsers(Flag):
    PAGESERVER = auto()
    SAFEKEEPER = auto()


class NeonEnvBuilder:
    """
    Builder object to create a Neon runtime environment

    You should use the `neon_env_builder` or `neon_simple_env` pytest
    fixture to create the NeonEnv object. That way, the repository is
    created in the right directory, based on the test name, and it's properly
    cleaned up after the test has finished.
    """

    def __init__(
        self,
        repo_dir: Path,
        port_distributor: PortDistributor,
        broker: NeonBroker,
        run_id: uuid.UUID,
        mock_s3_server: MockS3Server,
        neon_binpath: Path,
        pg_distrib_dir: Path,
        pg_version: PgVersion,
        remote_storage: Optional[RemoteStorage] = None,
        remote_storage_users: RemoteStorageUsers = RemoteStorageUsers.PAGESERVER,
        pageserver_config_override: Optional[str] = None,
        num_safekeepers: int = 1,
        # Use non-standard SK ids to check for various parsing bugs
        safekeepers_id_start: int = 0,
        # fsync is disabled by default to make the tests go faster
        safekeepers_enable_fsync: bool = False,
        auth_enabled: bool = False,
        rust_log_override: Optional[str] = None,
        default_branch_name: str = DEFAULT_BRANCH_NAME,
        preserve_database_files: bool = False,
        initial_tenant: Optional[TenantId] = None,
    ):
        self.repo_dir = repo_dir
        self.rust_log_override = rust_log_override
        self.port_distributor = port_distributor
        self.remote_storage = remote_storage
        self.remote_storage_users = remote_storage_users
        self.broker = broker
        self.run_id = run_id
        self.mock_s3_server = mock_s3_server
        self.pageserver_config_override = pageserver_config_override
        self.num_safekeepers = num_safekeepers
        self.safekeepers_id_start = safekeepers_id_start
        self.safekeepers_enable_fsync = safekeepers_enable_fsync
        self.auth_enabled = auth_enabled
        self.default_branch_name = default_branch_name
        self.env: Optional[NeonEnv] = None
        self.remote_storage_prefix: Optional[str] = None
        self.keep_remote_storage_contents: bool = True
        self.neon_binpath = neon_binpath
        self.pg_distrib_dir = pg_distrib_dir
        self.pg_version = pg_version
        self.preserve_database_files = preserve_database_files
        self.initial_tenant = initial_tenant or TenantId.generate()

    def init_configs(self) -> NeonEnv:
        # Cannot create more than one environment from one builder
        assert self.env is None, "environment already initialized"
        self.env = NeonEnv(self)
        return self.env

    def start(self):
        assert self.env is not None, "environment is not already initialized, call init() first"
        self.env.start()

    def init_start(self, initial_tenant_conf: Optional[Dict[str, str]] = None) -> NeonEnv:
        env = self.init_configs()
        self.start()

        # Prepare the default branch to start the postgres on later.
        # Pageserver itself does not create tenants and timelines, until started first and asked via HTTP API.
        log.info(
            f"Services started, creating initial tenant {env.initial_tenant} and its initial timeline"
        )
        initial_tenant, initial_timeline = env.neon_cli.create_tenant(
            tenant_id=env.initial_tenant, conf=initial_tenant_conf
        )
        env.initial_timeline = initial_timeline
        log.info(f"Initial timeline {initial_tenant}/{initial_timeline} created successfully")

        return env

    def enable_remote_storage(
        self,
        remote_storage_kind: RemoteStorageKind,
        test_name: str,
        force_enable: bool = True,
    ):
        if remote_storage_kind == RemoteStorageKind.NOOP:
            return
        elif remote_storage_kind == RemoteStorageKind.LOCAL_FS:
            self.enable_local_fs_remote_storage(force_enable=force_enable)
        elif remote_storage_kind == RemoteStorageKind.MOCK_S3:
            self.enable_mock_s3_remote_storage(bucket_name=test_name, force_enable=force_enable)
        elif remote_storage_kind == RemoteStorageKind.REAL_S3:
            self.enable_real_s3_remote_storage(test_name=test_name, force_enable=force_enable)
        else:
            raise RuntimeError(f"Unknown storage type: {remote_storage_kind}")

        self.remote_storage_kind = remote_storage_kind

    def enable_local_fs_remote_storage(self, force_enable: bool = True):
        """
        Sets up the pageserver to use the local fs at the `test_dir/local_fs_remote_storage` path.
        Errors, if the pageserver has some remote storage configuration already, unless `force_enable` is not set to `True`.
        """
        assert force_enable or self.remote_storage is None, "remote storage is enabled already"
        self.remote_storage = LocalFsStorage(Path(self.repo_dir / "local_fs_remote_storage"))

    def enable_mock_s3_remote_storage(self, bucket_name: str, force_enable: bool = True):
        """
        Sets up the pageserver to use the S3 mock server, creates the bucket, if it's not present already.
        Starts up the mock server, if that does not run yet.
        Errors, if the pageserver has some remote storage configuration already, unless `force_enable` is not set to `True`.
        """
        assert force_enable or self.remote_storage is None, "remote storage is enabled already"
        mock_endpoint = self.mock_s3_server.endpoint()
        mock_region = self.mock_s3_server.region()

        self.remote_storage_client = boto3.client(
            "s3",
            endpoint_url=mock_endpoint,
            region_name=mock_region,
            aws_access_key_id=self.mock_s3_server.access_key(),
            aws_secret_access_key=self.mock_s3_server.secret_key(),
        )
        self.remote_storage_client.create_bucket(Bucket=bucket_name)

        self.remote_storage = S3Storage(
            bucket_name=bucket_name,
            endpoint=mock_endpoint,
            bucket_region=mock_region,
            access_key=self.mock_s3_server.access_key(),
            secret_key=self.mock_s3_server.secret_key(),
        )

    def enable_real_s3_remote_storage(self, test_name: str, force_enable: bool = True):
        """
        Sets up configuration to use real s3 endpoint without mock server
        """
        assert force_enable or self.remote_storage is None, "remote storage is enabled already"

        access_key = os.getenv("AWS_ACCESS_KEY_ID")
        assert access_key, "no aws access key provided"
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        assert secret_key, "no aws access key provided"

        # session token is needed for local runs with sso auth
        session_token = os.getenv("AWS_SESSION_TOKEN")

        bucket_name = os.getenv("REMOTE_STORAGE_S3_BUCKET")
        assert bucket_name, "no remote storage bucket name provided"
        region = os.getenv("REMOTE_STORAGE_S3_REGION")
        assert region, "no remote storage region provided"

        # do not leave data in real s3
        self.keep_remote_storage_contents = False

        # construct a prefix inside bucket for the particular test case and test run
        self.remote_storage_prefix = f"{self.run_id}/{test_name}"

        self.remote_storage_client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )
        self.remote_storage = S3Storage(
            bucket_name=bucket_name,
            bucket_region=region,
            access_key=access_key,
            secret_key=secret_key,
            prefix_in_bucket=self.remote_storage_prefix,
        )

    def cleanup_local_storage(self):
        if self.preserve_database_files:
            return

        directories_to_clean: List[Path] = []
        for test_entry in Path(self.repo_dir).glob("**/*"):
            if test_entry.is_file():
                test_file = test_entry
                if ATTACHMENT_NAME_REGEX.fullmatch(test_file.name):
                    continue
                if SMALL_DB_FILE_NAME_REGEX.fullmatch(test_file.name):
                    continue
                log.debug(f"Removing large database {test_file} file")
                test_file.unlink()
            elif test_entry.is_dir():
                directories_to_clean.append(test_entry)

        for directory_to_clean in reversed(directories_to_clean):
            if not os.listdir(directory_to_clean):
                log.debug(f"Removing empty directory {directory_to_clean}")
                directory_to_clean.rmdir()

    def cleanup_remote_storage(self):
        # here wee check for true remote storage, no the local one
        # local cleanup is not needed after test because in ci all env will be destroyed anyway
        if self.remote_storage_prefix is None:
            log.info("no remote storage was set up, skipping cleanup")
            return

        # Making mypy happy with allowing only `S3Storage` further.
        # `self.remote_storage_prefix` is coupled with `S3Storage` storage type,
        # so this line effectively a no-op
        assert isinstance(self.remote_storage, S3Storage)

        if self.keep_remote_storage_contents:
            log.info("keep_remote_storage_contents skipping remote storage cleanup")
            return

        log.info(
            "removing data from test s3 bucket %s by prefix %s",
            self.remote_storage.bucket_name,
            self.remote_storage_prefix,
        )
        paginator = self.remote_storage_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=self.remote_storage.bucket_name,
            Prefix=self.remote_storage_prefix,
        )

        # Using Any because DeleteTypeDef (from boto3-stubs) doesn't fit our case
        objects_to_delete: Any = {"Objects": []}
        cnt = 0
        for item in pages.search("Contents"):
            # weirdly when nothing is found it returns [None]
            if item is None:
                break

            objects_to_delete["Objects"].append({"Key": item["Key"]})

            # flush once aws limit reached
            if len(objects_to_delete["Objects"]) >= 1000:
                self.remote_storage_client.delete_objects(
                    Bucket=self.remote_storage.bucket_name,
                    Delete=objects_to_delete,
                )
                objects_to_delete = {"Objects": []}
                cnt += 1

        # flush rest
        if len(objects_to_delete["Objects"]):
            self.remote_storage_client.delete_objects(
                Bucket=self.remote_storage.bucket_name,
                Delete=objects_to_delete,
            )

        log.info(f"deleted {cnt} objects from remote storage")

    def __enter__(self) -> "NeonEnvBuilder":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ):
        # Stop all the nodes.
        if self.env:
            log.info("Cleaning up all storage and compute nodes")
            self.env.endpoints.stop_all()
            for sk in self.env.safekeepers:
                sk.stop(immediate=True)
            self.env.pageserver.stop(immediate=True)

            cleanup_error = None
            try:
                self.cleanup_remote_storage()
            except Exception as e:
                log.error(f"Error during remote storage cleanup: {e}")
                cleanup_error = e

            try:
                self.cleanup_local_storage()
            except Exception as e:
                log.error(f"Error during local storage cleanup: {e}")
                if cleanup_error is not None:
                    cleanup_error = e

            if cleanup_error is not None:
                raise cleanup_error

            self.env.pageserver.assert_no_errors()


class NeonEnv:
    """
    An object representing the Neon runtime environment. It consists of
    the page server, 0-N safekeepers, and the compute nodes.

    NeonEnv contains functions for stopping/starting nodes in the
    environment, checking their status, creating tenants, connecting to the
    nodes, creating and destroying compute nodes, etc. The page server and
    the safekeepers are considered fixed in the environment, you cannot
    create or destroy them after the environment is initialized. (That will
    likely change in the future, as we start supporting multiple page
    servers and adding/removing safekeepers on the fly).

    Some notable functions and fields in NeonEnv:

    postgres - A factory object for creating postgres compute nodes.

    pageserver - An object that contains functions for manipulating and
        connecting to the pageserver

    safekeepers - An array containing objects representing the safekeepers

    pg_bin - pg_bin.run() can be used to execute Postgres client binaries,
        like psql or pg_dump

    initial_tenant - tenant ID of the initial tenant created in the repository

    neon_cli - can be used to run the 'neon' CLI tool

    create_tenant() - initializes a new tenant in the page server, returns
        the tenant id
    """

    def __init__(self, config: NeonEnvBuilder):
        self.repo_dir = config.repo_dir
        self.rust_log_override = config.rust_log_override
        self.port_distributor = config.port_distributor
        self.s3_mock_server = config.mock_s3_server
        self.neon_cli = NeonCli(env=self)
        self.endpoints = EndpointFactory(self)
        self.safekeepers: List[Safekeeper] = []
        self.broker = config.broker
        self.remote_storage = config.remote_storage
        self.remote_storage_users = config.remote_storage_users
        self.pg_version = config.pg_version
        self.neon_binpath = config.neon_binpath
        self.pg_distrib_dir = config.pg_distrib_dir
        self.endpoint_counter = 0

        # generate initial tenant ID here instead of letting 'neon init' generate it,
        # so that we don't need to dig it out of the config file afterwards.
        self.initial_tenant = config.initial_tenant
        self.initial_timeline: Optional[TimelineId] = None

        # Create a config file corresponding to the options
        toml = textwrap.dedent(
            f"""
            default_tenant_id = '{config.initial_tenant}'
        """
        )

        toml += textwrap.dedent(
            f"""
            [broker]
            listen_addr = '{self.broker.listen_addr()}'
        """
        )

        # Create config for pageserver
        pageserver_port = PageserverPort(
            pg=self.port_distributor.get_port(),
            http=self.port_distributor.get_port(),
        )
        http_auth_type = "NeonJWT" if config.auth_enabled else "Trust"
        pg_auth_type = "NeonJWT" if config.auth_enabled else "Trust"

        toml += textwrap.dedent(
            f"""
            [pageserver]
            id=1
            listen_pg_addr = 'localhost:{pageserver_port.pg}'
            listen_http_addr = 'localhost:{pageserver_port.http}'
            pg_auth_type = '{pg_auth_type}'
            http_auth_type = '{http_auth_type}'
        """
        )

        # Create a corresponding NeonPageserver object
        self.pageserver = NeonPageserver(
            self, port=pageserver_port, config_override=config.pageserver_config_override
        )

        # Create config and a Safekeeper object for each safekeeper
        for i in range(1, config.num_safekeepers + 1):
            port = SafekeeperPort(
                pg=self.port_distributor.get_port(),
                http=self.port_distributor.get_port(),
            )
            id = config.safekeepers_id_start + i  # assign ids sequentially
            toml += textwrap.dedent(
                f"""
                [[safekeepers]]
                id = {id}
                pg_port = {port.pg}
                http_port = {port.http}
                sync = {'true' if config.safekeepers_enable_fsync else 'false'}"""
            )
            if config.auth_enabled:
                toml += textwrap.dedent(
                    """
                auth_enabled = true
                """
                )
            if (
                bool(self.remote_storage_users & RemoteStorageUsers.SAFEKEEPER)
                and self.remote_storage is not None
            ):
                toml += textwrap.dedent(
                    f"""
                remote_storage = "{remote_storage_to_toml_inline_table(self.remote_storage)}"
                """
                )
            safekeeper = Safekeeper(env=self, id=id, port=port)
            self.safekeepers.append(safekeeper)

        log.info(f"Config: {toml}")
        self.neon_cli.init(toml)

    def start(self):
        # Start up broker, pageserver and all safekeepers
        self.broker.try_start()
        self.pageserver.start()

        for safekeeper in self.safekeepers:
            safekeeper.start()

    def get_safekeeper_connstrs(self) -> str:
        """Get list of safekeeper endpoints suitable for safekeepers GUC"""
        return ",".join(f"localhost:{wa.port.pg}" for wa in self.safekeepers)

    def timeline_dir(self, tenant_id: TenantId, timeline_id: TimelineId) -> Path:
        """Get a timeline directory's path based on the repo directory of the test environment"""
        return self.repo_dir / "tenants" / str(tenant_id) / "timelines" / str(timeline_id)

    def get_pageserver_version(self) -> str:
        bin_pageserver = str(self.neon_binpath / "pageserver")
        res = subprocess.run(
            [bin_pageserver, "--version"],
            check=True,
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return res.stdout

    @cached_property
    def auth_keys(self) -> AuthKeys:
        pub = (Path(self.repo_dir) / "auth_public_key.pem").read_text()
        priv = (Path(self.repo_dir) / "auth_private_key.pem").read_text()
        return AuthKeys(pub=pub, priv=priv)

    def generate_endpoint_id(self) -> str:
        """
        Generate a unique endpoint ID
        """
        self.endpoint_counter += 1
        return "ep-" + str(self.endpoint_counter)


@pytest.fixture(scope=shareable_scope)
def _shared_simple_env(
    request: FixtureRequest,
    pytestconfig: Config,
    port_distributor: PortDistributor,
    mock_s3_server: MockS3Server,
    default_broker: NeonBroker,
    run_id: uuid.UUID,
    top_output_dir: Path,
    neon_binpath: Path,
    pg_distrib_dir: Path,
    pg_version: PgVersion,
) -> Iterator[NeonEnv]:
    """
    # Internal fixture backing the `neon_simple_env` fixture. If TEST_SHARED_FIXTURES
     is set, this is shared by all tests using `neon_simple_env`.
    """

    if os.environ.get("TEST_SHARED_FIXTURES") is None:
        # Create the environment in the per-test output directory
        repo_dir = get_test_repo_dir(request, top_output_dir)
    else:
        # We're running shared fixtures. Share a single directory.
        repo_dir = top_output_dir / "shared_repo"
        shutil.rmtree(repo_dir, ignore_errors=True)

    with NeonEnvBuilder(
        repo_dir=repo_dir,
        port_distributor=port_distributor,
        broker=default_broker,
        mock_s3_server=mock_s3_server,
        neon_binpath=neon_binpath,
        pg_distrib_dir=pg_distrib_dir,
        pg_version=pg_version,
        run_id=run_id,
        preserve_database_files=pytestconfig.getoption("--preserve-database-files"),
    ) as builder:
        env = builder.init_start()

        # For convenience in tests, create a branch from the freshly-initialized cluster.
        env.neon_cli.create_branch("empty", ancestor_branch_name=DEFAULT_BRANCH_NAME)

        yield env


@pytest.fixture(scope="function")
def neon_simple_env(_shared_simple_env: NeonEnv) -> Iterator[NeonEnv]:
    """
    Simple Neon environment, with no authentication and no safekeepers.

    If TEST_SHARED_FIXTURES environment variable is set, we reuse the same
    environment for all tests that use 'neon_simple_env', keeping the
    page server and safekeepers running. Any compute nodes are stopped after
    each the test, however.
    """
    yield _shared_simple_env

    _shared_simple_env.endpoints.stop_all()


@pytest.fixture(scope="function")
def neon_env_builder(
    pytestconfig: Config,
    test_output_dir: str,
    port_distributor: PortDistributor,
    mock_s3_server: MockS3Server,
    neon_binpath: Path,
    pg_distrib_dir: Path,
    pg_version: PgVersion,
    default_broker: NeonBroker,
    run_id: uuid.UUID,
) -> Iterator[NeonEnvBuilder]:
    """
    Fixture to create a Neon environment for test.

    To use, define 'neon_env_builder' fixture in your test to get access to the
    builder object. Set properties on it to describe the environment.
    Finally, initialize and start up the environment by calling
    neon_env_builder.init_start().

    After the initialization, you can launch compute nodes by calling
    the functions in the 'env.endpoints' factory object, stop/start the
    nodes, etc.
    """

    # Create the environment in the test-specific output dir
    repo_dir = os.path.join(test_output_dir, "repo")

    # Return the builder to the caller
    with NeonEnvBuilder(
        repo_dir=Path(repo_dir),
        port_distributor=port_distributor,
        mock_s3_server=mock_s3_server,
        neon_binpath=neon_binpath,
        pg_distrib_dir=pg_distrib_dir,
        pg_version=pg_version,
        broker=default_broker,
        run_id=run_id,
        preserve_database_files=pytestconfig.getoption("--preserve-database-files"),
    ) as builder:
        yield builder


@dataclass
class PageserverPort:
    pg: int
    http: int


CREATE_TIMELINE_ID_EXTRACTOR: re.Pattern = re.compile(  # type: ignore[type-arg]
    r"^Created timeline '(?P<timeline_id>[^']+)'", re.MULTILINE
)
TIMELINE_DATA_EXTRACTOR: re.Pattern = re.compile(  # type: ignore[type-arg]
    r"\s?(?P<branch_name>[^\s]+)\s\[(?P<timeline_id>[^\]]+)\]", re.MULTILINE
)


class AbstractNeonCli(abc.ABC):
    """
    A typed wrapper around an arbitrary Neon CLI tool.
    Supports a way to run arbitrary command directly via CLI.
    Do not use directly, use specific subclasses instead.
    """

    def __init__(self, env: NeonEnv):
        self.env = env

    COMMAND: str = cast(str, None)  # To be overwritten by the derived class.

    def raw_cli(
        self,
        arguments: List[str],
        extra_env_vars: Optional[Dict[str, str]] = None,
        check_return_code=True,
        timeout=None,
    ) -> "subprocess.CompletedProcess[str]":
        """
        Run the command with the specified arguments.

        Arguments must be in list form, e.g. ['pg', 'create']

        Return both stdout and stderr, which can be accessed as

        >>> result = env.neon_cli.raw_cli(...)
        >>> assert result.stderr == ""
        >>> log.info(result.stdout)

        If `check_return_code`, on non-zero exit code logs failure and raises.
        """

        assert type(arguments) == list
        assert type(self.COMMAND) == str

        bin_neon = str(self.env.neon_binpath / self.COMMAND)

        args = [bin_neon] + arguments
        log.info('Running command "{}"'.format(" ".join(args)))
        log.info(f'Running in "{self.env.repo_dir}"')

        env_vars = os.environ.copy()
        env_vars["NEON_REPO_DIR"] = str(self.env.repo_dir)
        env_vars["POSTGRES_DISTRIB_DIR"] = str(self.env.pg_distrib_dir)
        if self.env.rust_log_override is not None:
            env_vars["RUST_LOG"] = self.env.rust_log_override
        for extra_env_key, extra_env_value in (extra_env_vars or {}).items():
            env_vars[extra_env_key] = extra_env_value

        # Pass coverage settings
        var = "LLVM_PROFILE_FILE"
        val = os.environ.get(var)
        if val:
            env_vars[var] = val

        # Intercept CalledProcessError and print more info
        res = subprocess.run(
            args,
            env=env_vars,
            check=False,
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if not res.returncode:
            log.info(f"Run {res.args} success: {res.stdout}")
        elif check_return_code:
            # this way command output will be in recorded and shown in CI in failure message
            msg = f"""\
            Run {res.args} failed:
              stdout: {res.stdout}
              stderr: {res.stderr}
            """
            log.info(msg)
            raise Exception(msg) from subprocess.CalledProcessError(
                res.returncode, res.args, res.stdout, res.stderr
            )
        return res


class NeonCli(AbstractNeonCli):
    """
    A typed wrapper around the `neon` CLI tool.
    Supports main commands via typed methods and a way to run arbitrary command directly via CLI.
    """

    COMMAND = "neon_local"

    def create_tenant(
        self,
        tenant_id: Optional[TenantId] = None,
        timeline_id: Optional[TimelineId] = None,
        conf: Optional[Dict[str, str]] = None,
        set_default: bool = False,
    ) -> Tuple[TenantId, TimelineId]:
        """
        Creates a new tenant, returns its id and its initial timeline's id.
        """
        if tenant_id is None:
            tenant_id = TenantId.generate()
        if timeline_id is None:
            timeline_id = TimelineId.generate()

        args = [
            "tenant",
            "create",
            "--tenant-id",
            str(tenant_id),
            "--timeline-id",
            str(timeline_id),
            "--pg-version",
            self.env.pg_version,
        ]
        if conf is not None:
            args.extend(
                chain.from_iterable(
                    product(["-c"], (f"{key}:{value}" for key, value in conf.items()))
                )
            )
        if set_default:
            args.append("--set-default")

        res = self.raw_cli(args)
        res.check_returncode()
        return tenant_id, timeline_id

    def set_default(self, tenant_id: TenantId):
        """
        Update default tenant for future operations that require tenant_id.
        """
        res = self.raw_cli(["tenant", "set-default", "--tenant-id", str(tenant_id)])
        res.check_returncode()

    def config_tenant(self, tenant_id: TenantId, conf: Dict[str, str]):
        """
        Update tenant config.
        """

        args = ["tenant", "config", "--tenant-id", str(tenant_id)]
        if conf is not None:
            args.extend(
                chain.from_iterable(
                    product(["-c"], (f"{key}:{value}" for key, value in conf.items()))
                )
            )

        res = self.raw_cli(args)
        res.check_returncode()

    def list_tenants(self) -> "subprocess.CompletedProcess[str]":
        res = self.raw_cli(["tenant", "list"])
        res.check_returncode()
        return res

    def create_timeline(
        self,
        new_branch_name: str,
        tenant_id: Optional[TenantId] = None,
    ) -> TimelineId:
        cmd = [
            "timeline",
            "create",
            "--branch-name",
            new_branch_name,
            "--tenant-id",
            str(tenant_id or self.env.initial_tenant),
            "--pg-version",
            self.env.pg_version,
        ]

        res = self.raw_cli(cmd)
        res.check_returncode()

        matches = CREATE_TIMELINE_ID_EXTRACTOR.search(res.stdout)

        created_timeline_id = None
        if matches is not None:
            created_timeline_id = matches.group("timeline_id")

        return TimelineId(str(created_timeline_id))

    def create_branch(
        self,
        new_branch_name: str = DEFAULT_BRANCH_NAME,
        ancestor_branch_name: Optional[str] = None,
        tenant_id: Optional[TenantId] = None,
        ancestor_start_lsn: Optional[Lsn] = None,
    ) -> TimelineId:
        cmd = [
            "timeline",
            "branch",
            "--branch-name",
            new_branch_name,
            "--tenant-id",
            str(tenant_id or self.env.initial_tenant),
        ]
        if ancestor_branch_name is not None:
            cmd.extend(["--ancestor-branch-name", ancestor_branch_name])
        if ancestor_start_lsn is not None:
            cmd.extend(["--ancestor-start-lsn", str(ancestor_start_lsn)])

        res = self.raw_cli(cmd)
        res.check_returncode()

        matches = CREATE_TIMELINE_ID_EXTRACTOR.search(res.stdout)

        created_timeline_id = None
        if matches is not None:
            created_timeline_id = matches.group("timeline_id")

        if created_timeline_id is None:
            raise Exception("could not find timeline id after `neon timeline create` invocation")
        else:
            return TimelineId(str(created_timeline_id))

    def list_timelines(self, tenant_id: Optional[TenantId] = None) -> List[Tuple[str, TimelineId]]:
        """
        Returns a list of (branch_name, timeline_id) tuples out of parsed `neon timeline list` CLI output.
        """

        # main [b49f7954224a0ad25cc0013ea107b54b]
        # ┣━ @0/16B5A50: test_cli_branch_list_main [20f98c79111b9015d84452258b7d5540]
        res = self.raw_cli(
            ["timeline", "list", "--tenant-id", str(tenant_id or self.env.initial_tenant)]
        )
        timelines_cli = sorted(
            map(
                lambda branch_and_id: (branch_and_id[0], TimelineId(branch_and_id[1])),
                TIMELINE_DATA_EXTRACTOR.findall(res.stdout),
            )
        )
        return timelines_cli

    def init(
        self,
        config_toml: str,
    ) -> "subprocess.CompletedProcess[str]":
        with tempfile.NamedTemporaryFile(mode="w+") as tmp:
            tmp.write(config_toml)
            tmp.flush()

            cmd = ["init", f"--config={tmp.name}", "--pg-version", self.env.pg_version]

            append_pageserver_param_overrides(
                params_to_update=cmd,
                remote_storage=self.env.remote_storage,
                remote_storage_users=self.env.remote_storage_users,
                pageserver_config_override=self.env.pageserver.config_override,
            )

            s3_env_vars = None
            if self.env.remote_storage is not None and isinstance(
                self.env.remote_storage, S3Storage
            ):
                s3_env_vars = self.env.remote_storage.access_env_vars()
            res = self.raw_cli(cmd, extra_env_vars=s3_env_vars)
            res.check_returncode()
            return res

    def pageserver_start(
        self,
        overrides: Tuple[str, ...] = (),
        extra_env_vars: Optional[Dict[str, str]] = None,
    ) -> "subprocess.CompletedProcess[str]":
        start_args = ["pageserver", "start", *overrides]
        append_pageserver_param_overrides(
            params_to_update=start_args,
            remote_storage=self.env.remote_storage,
            remote_storage_users=self.env.remote_storage_users,
            pageserver_config_override=self.env.pageserver.config_override,
        )

        if self.env.remote_storage is not None and isinstance(self.env.remote_storage, S3Storage):
            s3_env_vars = self.env.remote_storage.access_env_vars()
            extra_env_vars = (extra_env_vars or {}) | s3_env_vars

        return self.raw_cli(start_args, extra_env_vars=extra_env_vars)

    def pageserver_stop(self, immediate=False) -> "subprocess.CompletedProcess[str]":
        cmd = ["pageserver", "stop"]
        if immediate:
            cmd.extend(["-m", "immediate"])

        log.info(f"Stopping pageserver with {cmd}")
        return self.raw_cli(cmd)

    def safekeeper_start(self, id: int) -> "subprocess.CompletedProcess[str]":
        s3_env_vars = None
        if self.env.remote_storage is not None and isinstance(self.env.remote_storage, S3Storage):
            s3_env_vars = self.env.remote_storage.access_env_vars()

        return self.raw_cli(["safekeeper", "start", str(id)], extra_env_vars=s3_env_vars)

    def safekeeper_stop(
        self, id: Optional[int] = None, immediate=False
    ) -> "subprocess.CompletedProcess[str]":
        args = ["safekeeper", "stop"]
        if id is not None:
            args.append(str(id))
        if immediate:
            args.extend(["-m", "immediate"])
        return self.raw_cli(args)

    def endpoint_create(
        self,
        branch_name: str,
        pg_port: int,
        http_port: int,
        endpoint_id: Optional[str] = None,
        tenant_id: Optional[TenantId] = None,
        hot_standby: bool = False,
        lsn: Optional[Lsn] = None,
    ) -> "subprocess.CompletedProcess[str]":
        args = [
            "endpoint",
            "create",
            "--tenant-id",
            str(tenant_id or self.env.initial_tenant),
            "--branch-name",
            branch_name,
            "--pg-version",
            self.env.pg_version,
        ]
        if lsn is not None:
            args.extend(["--lsn", str(lsn)])
        if pg_port is not None:
            args.extend(["--pg-port", str(pg_port)])
        if http_port is not None:
            args.extend(["--http-port", str(http_port)])
        if endpoint_id is not None:
            args.append(endpoint_id)
        if hot_standby:
            args.extend(["--hot-standby", "true"])

        res = self.raw_cli(args)
        res.check_returncode()
        return res

    def endpoint_start(
        self,
        endpoint_id: str,
        pg_port: int,
        http_port: int,
        safekeepers: Optional[List[int]] = None,
        tenant_id: Optional[TenantId] = None,
        lsn: Optional[Lsn] = None,
    ) -> "subprocess.CompletedProcess[str]":
        args = [
            "endpoint",
            "start",
            "--tenant-id",
            str(tenant_id or self.env.initial_tenant),
            "--pg-version",
            self.env.pg_version,
        ]
        if lsn is not None:
            args.append(f"--lsn={lsn}")
        args.extend(["--pg-port", str(pg_port)])
        args.extend(["--http-port", str(http_port)])
        if safekeepers is not None:
            args.extend(["--safekeepers", (",".join(map(str, safekeepers)))])
        if endpoint_id is not None:
            args.append(endpoint_id)

        res = self.raw_cli(args)
        res.check_returncode()
        return res

    def endpoint_stop(
        self,
        endpoint_id: str,
        tenant_id: Optional[TenantId] = None,
        destroy=False,
        check_return_code=True,
    ) -> "subprocess.CompletedProcess[str]":
        args = [
            "endpoint",
            "stop",
            "--tenant-id",
            str(tenant_id or self.env.initial_tenant),
        ]
        if destroy:
            args.append("--destroy")
        if endpoint_id is not None:
            args.append(endpoint_id)

        return self.raw_cli(args, check_return_code=check_return_code)

    def start(self, check_return_code=True) -> "subprocess.CompletedProcess[str]":
        return self.raw_cli(["start"], check_return_code=check_return_code)

    def stop(self, check_return_code=True) -> "subprocess.CompletedProcess[str]":
        return self.raw_cli(["stop"], check_return_code=check_return_code)


class WalCraft(AbstractNeonCli):
    """
    A typed wrapper around the `wal_craft` CLI tool.
    Supports main commands via typed methods and a way to run arbitrary command directly via CLI.
    """

    COMMAND = "wal_craft"

    def postgres_config(self) -> List[str]:
        res = self.raw_cli(["print-postgres-config"])
        res.check_returncode()
        return res.stdout.split("\n")

    def in_existing(self, type: str, connection: str) -> None:
        res = self.raw_cli(["in-existing", type, connection])
        res.check_returncode()


class ComputeCtl(AbstractNeonCli):
    """
    A typed wrapper around the `compute_ctl` CLI tool.
    """

    COMMAND = "compute_ctl"


class NeonPageserver(PgProtocol):
    """
    An object representing a running pageserver.
    """

    TEMP_FILE_SUFFIX = "___temp"

    def __init__(self, env: NeonEnv, port: PageserverPort, config_override: Optional[str] = None):
        super().__init__(host="localhost", port=port.pg, user="cloud_admin")
        self.env = env
        self.running = False
        self.service_port = port
        self.config_override = config_override
        self.version = env.get_pageserver_version()

        # After a test finishes, we will scrape the log to see if there are any
        # unexpected error messages. If your test expects an error, add it to
        # 'allowed_errors' in the test with something like:
        #
        # env.pageserver.allowed_errors.append(".*could not open garage door.*")
        #
        # The entries in the list are regular experessions.
        self.allowed_errors = [
            # All tests print these, when starting up or shutting down
            ".*wal receiver task finished with an error: walreceiver connection handling failure.*",
            ".*Shutdown task error: walreceiver connection handling failure.*",
            ".*wal_connection_manager.*tcp connect error: Connection refused.*",
            ".*query handler for .* failed: Socket IO error: Connection reset by peer.*",
            ".*serving compute connection task.*exited with error: Postgres connection error.*",
            ".*serving compute connection task.*exited with error: Connection reset by peer.*",
            ".*serving compute connection task.*exited with error: Postgres query error.*",
            ".*Connection aborted: error communicating with the server: Transport endpoint is not connected.*",
            # FIXME: replication patch for tokio_postgres regards  any but CopyDone/CopyData message in CopyBoth stream as unexpected
            ".*Connection aborted: unexpected message from server*",
            ".*kill_and_wait_impl.*: wait successful.*",
            ".*: db error:.*ending streaming to Some.*",
            ".*query handler for 'pagestream.*failed: Broken pipe.*",  # pageserver notices compute shut down
            ".*query handler for 'pagestream.*failed: Connection reset by peer.*",  # pageserver notices compute shut down
            # safekeeper connection can fail with this, in the window between timeline creation
            # and streaming start
            ".*Failed to process query for timeline .*: state uninitialized, no data to read.*",
            # Tests related to authentication and authorization print these
            ".*Error processing HTTP request: Forbidden",
            # intentional failpoints
            ".*failpoint ",
            # FIXME: there is a race condition between GC and detach, see
            # https://github.com/neondatabase/neon/issues/2442
            ".*could not remove ephemeral file.*No such file or directory.*",
            # FIXME: These need investigation
            ".*manual_gc.*is_shutdown_requested\\(\\) called in an unexpected task or thread.*",
            ".*tenant_list: timeline is not found in remote index while it is present in the tenants registry.*",
            ".*Removing intermediate uninit mark file.*",
            # Tenant::delete_timeline() can cause any of the four following errors.
            # FIXME: we shouldn't be considering it an error: https://github.com/neondatabase/neon/issues/2946
            ".*could not flush frozen layer.*queue is in state Stopped",  # when schedule layer upload fails because queued got closed before compaction got killed
            ".*wait for layer upload ops to complete.*",  # .*Caused by:.*wait_completion aborted because upload queue was stopped
            ".*gc_loop.*Gc failed, retrying in.*timeline is Stopping",  # When gc checks timeline state after acquiring layer_removal_cs
            ".*gc_loop.*Gc failed, retrying in.*: Cannot run GC iteration on inactive tenant",  # Tenant::gc precondition
            ".*compaction_loop.*Compaction failed, retrying in.*timeline is Stopping",  # When compaction checks timeline state after acquiring layer_removal_cs
            ".*query handler for 'pagestream.*failed: Timeline .* was not found",  # postgres reconnects while timeline_delete doesn't hold the tenant's timelines.lock()
            ".*query handler for 'pagestream.*failed: Timeline .* is not active",  # timeline delete in progress
            ".*task iteration took longer than the configured period.*",
            # this is until #3501
            ".*Compaction failed, retrying in [^:]+: Cannot run compaction iteration on inactive tenant",
            # these can happen anytime we do compactions from background task and shutdown pageserver
            r".*ERROR.*ancestor timeline \S+ is being stopped",
            # this is expected given our collaborative shutdown approach for the UploadQueue
            ".*Compaction failed, retrying in .*: queue is in state Stopped.*",
            # Pageserver timeline deletion should be polled until it gets 404, so ignore it globally
            ".*Error processing HTTP request: NotFound: Timeline .* was not found",
        ]

    def start(
        self,
        overrides: Tuple[str, ...] = (),
        extra_env_vars: Optional[Dict[str, str]] = None,
    ) -> "NeonPageserver":
        """
        Start the page server.
        `overrides` allows to add some config to this pageserver start.
        Returns self.
        """
        assert self.running is False

        self.env.neon_cli.pageserver_start(overrides=overrides, extra_env_vars=extra_env_vars)
        self.running = True
        return self

    def stop(self, immediate: bool = False) -> "NeonPageserver":
        """
        Stop the page server.
        Returns self.
        """
        if self.running:
            self.env.neon_cli.pageserver_stop(immediate)
            self.running = False
        return self

    def __enter__(self) -> "NeonPageserver":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ):
        self.stop(immediate=True)

    def is_testing_enabled_or_skip(self):
        if '"testing"' not in self.version:
            pytest.skip("pageserver was built without 'testing' feature")

    def http_client(self, auth_token: Optional[str] = None) -> PageserverHttpClient:
        return PageserverHttpClient(
            port=self.service_port.http,
            auth_token=auth_token,
            is_testing_enabled_or_skip=self.is_testing_enabled_or_skip,
        )

    def assert_no_errors(self):
        logfile = open(os.path.join(self.env.repo_dir, "pageserver.log"), "r")
        error_or_warn = re.compile(r"\s(ERROR|WARN)")
        errors = []
        while True:
            line = logfile.readline()
            if not line:
                break

            if error_or_warn.search(line):
                # It's an ERROR or WARN. Is it in the allow-list?
                for a in self.allowed_errors:
                    if re.match(a, line):
                        break
                else:
                    errors.append(line)

        for error in errors:
            log.info(f"not allowed error: {error.strip()}")

        assert not errors

    def log_contains(self, pattern: str) -> Optional[str]:
        """Check that the pageserver log contains a line that matches the given regex"""
        logfile = open(os.path.join(self.env.repo_dir, "pageserver.log"), "r")

        contains_re = re.compile(pattern)

        # XXX: Our rust logging machinery buffers the messages, so if you
        # call this function immediately after it's been logged, there is
        # no guarantee it is already present in the log file. This hasn't
        # been a problem in practice, our python tests are not fast enough
        # to hit that race condition.
        while True:
            line = logfile.readline()
            if not line:
                break

            if contains_re.search(line):
                # found it!
                return line

        return None


def append_pageserver_param_overrides(
    params_to_update: List[str],
    remote_storage: Optional[RemoteStorage],
    remote_storage_users: RemoteStorageUsers,
    pageserver_config_override: Optional[str] = None,
):
    if bool(remote_storage_users & RemoteStorageUsers.PAGESERVER) and remote_storage is not None:
        remote_storage_toml_table = remote_storage_to_toml_inline_table(remote_storage)

        params_to_update.append(
            f"--pageserver-config-override=remote_storage={remote_storage_toml_table}"
        )

    env_overrides = os.getenv("NEON_PAGESERVER_OVERRIDES")
    if env_overrides is not None:
        params_to_update += [
            f"--pageserver-config-override={o.strip()}" for o in env_overrides.split(";")
        ]

    if pageserver_config_override is not None:
        params_to_update += [
            f"--pageserver-config-override={o.strip()}"
            for o in pageserver_config_override.split(";")
        ]


class PgBin:
    """A helper class for executing postgres binaries"""

    def __init__(self, log_dir: Path, pg_distrib_dir: Path, pg_version: PgVersion):
        self.log_dir = log_dir
        self.pg_version = pg_version
        self.pg_bin_path = pg_distrib_dir / pg_version.v_prefixed / "bin"
        self.pg_lib_dir = pg_distrib_dir / pg_version.v_prefixed / "lib"
        self.env = os.environ.copy()
        self.env["LD_LIBRARY_PATH"] = str(self.pg_lib_dir)

    def _fixpath(self, command: List[str]):
        if "/" not in str(command[0]):
            command[0] = str(self.pg_bin_path / command[0])

    def _build_env(self, env_add: Optional[Env]) -> Env:
        if env_add is None:
            return self.env
        env = self.env.copy()
        env.update(env_add)
        return env

    def run(self, command: List[str], env: Optional[Env] = None, cwd: Optional[str] = None):
        """
        Run one of the postgres binaries.

        The command should be in list form, e.g. ['pgbench', '-p', '55432']

        All the necessary environment variables will be set.

        If the first argument (the command name) doesn't include a path (no '/'
        characters present), then it will be edited to include the correct path.

        If you want stdout/stderr captured to files, use `run_capture` instead.
        """

        self._fixpath(command)
        log.info(f"Running command '{' '.join(command)}'")
        env = self._build_env(env)
        subprocess.run(command, env=env, cwd=cwd, check=True)

    def run_capture(
        self,
        command: List[str],
        env: Optional[Env] = None,
        cwd: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """
        Run one of the postgres binaries, with stderr and stdout redirected to a file.

        This is just like `run`, but for chatty programs. Returns basepath for files
        with captured output.
        """

        self._fixpath(command)
        log.info(f"Running command '{' '.join(command)}'")
        env = self._build_env(env)
        return subprocess_capture(self.log_dir, command, env=env, cwd=cwd, check=True, **kwargs)


@pytest.fixture(scope="function")
def pg_bin(test_output_dir: Path, pg_distrib_dir: Path, pg_version: PgVersion) -> PgBin:
    return PgBin(test_output_dir, pg_distrib_dir, pg_version)


class VanillaPostgres(PgProtocol):
    def __init__(self, pgdatadir: Path, pg_bin: PgBin, port: int, init: bool = True):
        super().__init__(host="localhost", port=port, dbname="postgres")
        self.pgdatadir = pgdatadir
        self.pg_bin = pg_bin
        self.running = False
        if init:
            self.pg_bin.run_capture(["initdb", "-D", str(pgdatadir)])
        self.configure([f"port = {port}\n"])

    def enable_tls(self):
        assert not self.running
        # generate self-signed certificate
        subprocess.run(
            [
                "openssl",
                "req",
                "-new",
                "-x509",
                "-days",
                "365",
                "-nodes",
                "-text",
                "-out",
                self.pgdatadir / "server.crt",
                "-keyout",
                self.pgdatadir / "server.key",
                "-subj",
                "/CN=localhost",
            ]
        )
        # configure postgresql.conf
        self.configure(
            [
                "ssl = on",
                "ssl_cert_file = 'server.crt'",
                "ssl_key_file = 'server.key'",
            ]
        )

    def configure(self, options: List[str]):
        """Append lines into postgresql.conf file."""
        assert not self.running
        with open(os.path.join(self.pgdatadir, "postgresql.conf"), "a") as conf_file:
            conf_file.write("\n".join(options))

    def start(self, log_path: Optional[str] = None):
        assert not self.running
        self.running = True

        if log_path is None:
            log_path = os.path.join(self.pgdatadir, "pg.log")

        self.pg_bin.run_capture(
            ["pg_ctl", "-w", "-D", str(self.pgdatadir), "-l", log_path, "start"]
        )

    def stop(self):
        assert self.running
        self.running = False
        self.pg_bin.run_capture(["pg_ctl", "-w", "-D", str(self.pgdatadir), "stop"])

    def get_subdir_size(self, subdir) -> int:
        """Return size of pgdatadir subdirectory in bytes."""
        return get_dir_size(os.path.join(self.pgdatadir, subdir))

    def __enter__(self) -> "VanillaPostgres":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ):
        if self.running:
            self.stop()


@pytest.fixture(scope="function")
def vanilla_pg(
    test_output_dir: Path,
    port_distributor: PortDistributor,
    pg_distrib_dir: Path,
    pg_version: PgVersion,
) -> Iterator[VanillaPostgres]:
    pgdatadir = test_output_dir / "pgdata-vanilla"
    pg_bin = PgBin(test_output_dir, pg_distrib_dir, pg_version)
    port = port_distributor.get_port()
    with VanillaPostgres(pgdatadir, pg_bin, port) as vanilla_pg:
        yield vanilla_pg


class RemotePostgres(PgProtocol):
    def __init__(self, pg_bin: PgBin, remote_connstr: str):
        super().__init__(**parse_dsn(remote_connstr))
        self.pg_bin = pg_bin
        # The remote server is assumed to be running already
        self.running = True

    def configure(self, options: List[str]):
        raise Exception("cannot change configuration of remote Posgres instance")

    def start(self):
        raise Exception("cannot start a remote Postgres instance")

    def stop(self):
        raise Exception("cannot stop a remote Postgres instance")

    def get_subdir_size(self, subdir) -> int:
        # TODO: Could use the server's Generic File Access functions if superuser.
        # See https://www.postgresql.org/docs/14/functions-admin.html#FUNCTIONS-ADMIN-GENFILE
        raise Exception("cannot get size of a Postgres instance")

    def __enter__(self) -> "RemotePostgres":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ):
        # do nothing
        pass


@pytest.fixture(scope="function")
def remote_pg(
    test_output_dir: Path, pg_distrib_dir: Path, pg_version: PgVersion
) -> Iterator[RemotePostgres]:
    pg_bin = PgBin(test_output_dir, pg_distrib_dir, pg_version)

    connstr = os.getenv("BENCHMARK_CONNSTR")
    if connstr is None:
        raise ValueError("no connstr provided, use BENCHMARK_CONNSTR environment variable")

    host = parse_dsn(connstr).get("host", "")
    is_neon = host.endswith(".neon.build")

    start_ms = int(datetime.utcnow().timestamp() * 1000)
    with RemotePostgres(pg_bin, connstr) as remote_pg:
        if is_neon:
            timeline_id = TimelineId(remote_pg.safe_psql("SHOW neon.timeline_id")[0][0])

        yield remote_pg

    end_ms = int(datetime.utcnow().timestamp() * 1000)
    if is_neon:
        # Add 10s margin to the start and end times
        allure_add_grafana_links(
            host,
            timeline_id,
            start_ms - 10_000,
            end_ms + 10_000,
        )


class PSQL:
    """
    Helper class to make it easier to run psql in the proxy tests.
    Copied and modified from PSQL from cloud/tests_e2e/common/psql.py
    """

    path: str
    database_url: str

    def __init__(
        self,
        path: str = "psql",
        host: str = "127.0.0.1",
        port: int = 5432,
    ):
        assert shutil.which(path)

        self.path = path
        self.database_url = f"postgres://{host}:{port}/main?options=project%3Dgeneric-project-name"

    async def run(self, query: Optional[str] = None) -> asyncio.subprocess.Process:
        run_args = [self.path, "--no-psqlrc", "--quiet", "--tuples-only", self.database_url]
        if query is not None:
            run_args += ["--command", query]

        log.info(f"Run psql: {subprocess.list2cmdline(run_args)}")
        return await asyncio.create_subprocess_exec(
            *run_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"LC_ALL": "C", **os.environ},  # one locale to rule them all
        )


class NeonProxy(PgProtocol):
    link_auth_uri: str = "http://dummy-uri"

    class AuthBackend(abc.ABC):
        """All auth backends must inherit from this class"""

        @property
        def default_conn_url(self) -> Optional[str]:
            return None

        @abc.abstractmethod
        def extra_args(self) -> list[str]:
            pass

    class Link(AuthBackend):
        def extra_args(self) -> list[str]:
            return [
                # Link auth backend params
                *["--auth-backend", "link"],
                *["--uri", NeonProxy.link_auth_uri],
                *["--allow-self-signed-compute", "true"],
            ]

    @dataclass(frozen=True)
    class Postgres(AuthBackend):
        pg_conn_url: str

        @property
        def default_conn_url(self) -> Optional[str]:
            return self.pg_conn_url

        def extra_args(self) -> list[str]:
            return [
                # Postgres auth backend params
                *["--auth-backend", "postgres"],
                *["--auth-endpoint", self.pg_conn_url],
            ]

    def __init__(
        self,
        neon_binpath: Path,
        test_output_dir: Path,
        proxy_port: int,
        http_port: int,
        mgmt_port: int,
        external_http_port: int,
        auth_backend: NeonProxy.AuthBackend,
        metric_collection_endpoint: Optional[str] = None,
        metric_collection_interval: Optional[str] = None,
    ):
        host = "127.0.0.1"
        domain = "proxy.localtest.me"  # resolves to 127.0.0.1
        super().__init__(dsn=auth_backend.default_conn_url, host=domain, port=proxy_port)

        self.domain = domain
        self.host = host
        self.http_port = http_port
        self.external_http_port = external_http_port
        self.neon_binpath = neon_binpath
        self.test_output_dir = test_output_dir
        self.proxy_port = proxy_port
        self.mgmt_port = mgmt_port
        self.auth_backend = auth_backend
        self.metric_collection_endpoint = metric_collection_endpoint
        self.metric_collection_interval = metric_collection_interval
        self._popen: Optional[subprocess.Popen[bytes]] = None

    def start(self) -> NeonProxy:
        assert self._popen is None

        # generate key of it doesn't exist
        crt_path = self.test_output_dir / "proxy.crt"
        key_path = self.test_output_dir / "proxy.key"

        if not key_path.exists():
            r = subprocess.run(
                [
                    "openssl",
                    "req",
                    "-new",
                    "-x509",
                    "-days",
                    "365",
                    "-nodes",
                    "-text",
                    "-out",
                    str(crt_path),
                    "-keyout",
                    str(key_path),
                    "-subj",
                    "/CN=*.localtest.me",
                    "-addext",
                    "subjectAltName = DNS:*.localtest.me",
                ]
            )
            assert r.returncode == 0

        args = [
            str(self.neon_binpath / "proxy"),
            *["--http", f"{self.host}:{self.http_port}"],
            *["--proxy", f"{self.host}:{self.proxy_port}"],
            *["--mgmt", f"{self.host}:{self.mgmt_port}"],
            *["--wss", f"{self.host}:{self.external_http_port}"],
            *["-c", str(crt_path)],
            *["-k", str(key_path)],
            *self.auth_backend.extra_args(),
        ]

        if (
            self.metric_collection_endpoint is not None
            and self.metric_collection_interval is not None
        ):
            args += [
                *["--metric-collection-endpoint", self.metric_collection_endpoint],
                *["--metric-collection-interval", self.metric_collection_interval],
            ]

        logfile = open(self.test_output_dir / "proxy.log", "w")
        self._popen = subprocess.Popen(args, stdout=logfile, stderr=logfile)
        self._wait_until_ready()
        return self

    # Sends SIGTERM to the proxy if it has been started
    def terminate(self):
        if self._popen:
            self._popen.terminate()

    # Waits for proxy to exit if it has been opened with a default timeout of
    # two seconds. Raises subprocess.TimeoutExpired if the proxy does not exit in time.
    def wait_for_exit(self, timeout=2):
        if self._popen:
            self._popen.wait(timeout=2)

    @backoff.on_exception(backoff.expo, requests.exceptions.RequestException, max_time=10)
    def _wait_until_ready(self):
        requests.get(f"http://{self.host}:{self.http_port}/v1/status")

    def get_metrics(self) -> str:
        request_result = requests.get(f"http://{self.host}:{self.http_port}/metrics")
        request_result.raise_for_status()
        return request_result.text

    @staticmethod
    def get_session_id(uri_prefix, uri_line):
        assert uri_prefix in uri_line

        url_parts = urlparse(uri_line)
        psql_session_id = url_parts.path[1:]
        assert psql_session_id.isalnum(), "session_id should only contain alphanumeric chars"

        return psql_session_id

    @staticmethod
    async def find_auth_link(link_auth_uri, proc):
        for _ in range(100):
            line = (await proc.stderr.readline()).decode("utf-8").strip()
            log.info(f"psql line: {line}")
            if link_auth_uri in line:
                log.info(f"SUCCESS, found auth url: {line}")
                return line

    def __enter__(self) -> NeonProxy:
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ):
        if self._popen is not None:
            self._popen.terminate()
            try:
                self._popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("failed to gracefully terminate proxy; killing")
                self._popen.kill()

    @staticmethod
    async def activate_link_auth(
        local_vanilla_pg, proxy_with_metric_collector, psql_session_id, create_user=True
    ):
        pg_user = "proxy"

        if create_user:
            log.info("creating a new user for link auth test")
            local_vanilla_pg.enable_tls()
            local_vanilla_pg.start()
            local_vanilla_pg.safe_psql(f"create user {pg_user} with login superuser")

        db_info = json.dumps(
            {
                "session_id": psql_session_id,
                "result": {
                    "Success": {
                        "host": local_vanilla_pg.default_options["host"],
                        "port": local_vanilla_pg.default_options["port"],
                        "dbname": local_vanilla_pg.default_options["dbname"],
                        "user": pg_user,
                        "aux": {
                            "project_id": "test_project_id",
                            "endpoint_id": "test_endpoint_id",
                            "branch_id": "test_branch_id",
                        },
                    }
                },
            }
        )

        log.info("sending session activation message")
        psql = await PSQL(
            host=proxy_with_metric_collector.host,
            port=proxy_with_metric_collector.mgmt_port,
        ).run(db_info)
        assert psql.stdout is not None
        out = (await psql.stdout.read()).decode("utf-8").strip()
        assert out == "ok"


@pytest.fixture(scope="function")
def link_proxy(
    port_distributor: PortDistributor, neon_binpath: Path, test_output_dir: Path
) -> Iterator[NeonProxy]:
    """Neon proxy that routes through link auth."""

    http_port = port_distributor.get_port()
    proxy_port = port_distributor.get_port()
    mgmt_port = port_distributor.get_port()
    external_http_port = port_distributor.get_port()

    with NeonProxy(
        neon_binpath=neon_binpath,
        test_output_dir=test_output_dir,
        proxy_port=proxy_port,
        http_port=http_port,
        mgmt_port=mgmt_port,
        external_http_port=external_http_port,
        auth_backend=NeonProxy.Link(),
    ) as proxy:
        proxy.start()
        yield proxy


@pytest.fixture(scope="function")
def static_proxy(
    vanilla_pg: VanillaPostgres,
    port_distributor: PortDistributor,
    neon_binpath: Path,
    test_output_dir: Path,
) -> Iterator[NeonProxy]:
    """Neon proxy that routes directly to vanilla postgres."""

    # For simplicity, we use the same user for both `--auth-endpoint` and `safe_psql`
    vanilla_pg.start()
    vanilla_pg.safe_psql("create user proxy with login superuser password 'password'")

    port = vanilla_pg.default_options["port"]
    host = vanilla_pg.default_options["host"]
    dbname = vanilla_pg.default_options["dbname"]
    auth_endpoint = f"postgres://proxy:password@{host}:{port}/{dbname}"

    proxy_port = port_distributor.get_port()
    mgmt_port = port_distributor.get_port()
    http_port = port_distributor.get_port()
    external_http_port = port_distributor.get_port()

    with NeonProxy(
        neon_binpath=neon_binpath,
        test_output_dir=test_output_dir,
        proxy_port=proxy_port,
        http_port=http_port,
        mgmt_port=mgmt_port,
        external_http_port=external_http_port,
        auth_backend=NeonProxy.Postgres(auth_endpoint),
    ) as proxy:
        proxy.start()
        yield proxy


class Endpoint(PgProtocol):
    """An object representing a Postgres compute endpoint managed by the control plane."""

    def __init__(
        self,
        env: NeonEnv,
        tenant_id: TenantId,
        pg_port: int,
        http_port: int,
        check_stop_result: bool = True,
    ):
        super().__init__(host="localhost", port=pg_port, user="cloud_admin", dbname="postgres")
        self.env = env
        self.running = False
        self.branch_name: Optional[str] = None  # dubious
        self.endpoint_id: Optional[str] = None  # dubious, see asserts below
        self.pgdata_dir: Optional[str] = None  # Path to computenode PGDATA
        self.tenant_id = tenant_id
        self.pg_port = pg_port
        self.http_port = http_port
        self.check_stop_result = check_stop_result
        self.active_safekeepers: List[int] = list(map(lambda sk: sk.id, env.safekeepers))
        # path to conf is <repo_dir>/endpoints/<endpoint_id>/pgdata/postgresql.conf

    def create(
        self,
        branch_name: str,
        endpoint_id: Optional[str] = None,
        hot_standby: bool = False,
        lsn: Optional[Lsn] = None,
        config_lines: Optional[List[str]] = None,
    ) -> "Endpoint":
        """
        Create a new Postgres endpoint.
        Returns self.
        """

        if not config_lines:
            config_lines = []

        if endpoint_id is None:
            endpoint_id = self.env.generate_endpoint_id()
        self.endpoint_id = endpoint_id
        self.branch_name = branch_name

        self.env.neon_cli.endpoint_create(
            branch_name,
            endpoint_id=self.endpoint_id,
            tenant_id=self.tenant_id,
            lsn=lsn,
            hot_standby=hot_standby,
            pg_port=self.pg_port,
            http_port=self.http_port,
        )
        path = Path("endpoints") / self.endpoint_id / "pgdata"
        self.pgdata_dir = os.path.join(self.env.repo_dir, path)

        if config_lines is None:
            config_lines = []

        # set small 'max_replication_write_lag' to enable backpressure
        # and make tests more stable.
        config_lines = ["max_replication_write_lag=15MB"] + config_lines
        self.config(config_lines)

        return self

    def start(self) -> "Endpoint":
        """
        Start the Postgres instance.
        Returns self.
        """

        assert self.endpoint_id is not None

        log.info(f"Starting postgres endpoint {self.endpoint_id}")

        self.env.neon_cli.endpoint_start(
            self.endpoint_id,
            pg_port=self.pg_port,
            http_port=self.http_port,
            tenant_id=self.tenant_id,
            safekeepers=self.active_safekeepers,
        )
        self.running = True

        return self

    def endpoint_path(self) -> Path:
        """Path to endpoint directory"""
        assert self.endpoint_id
        path = Path("endpoints") / self.endpoint_id
        return self.env.repo_dir / path

    def pg_data_dir_path(self) -> str:
        """Path to Postgres data directory"""
        return os.path.join(self.endpoint_path(), "pgdata")

    def pg_xact_dir_path(self) -> str:
        """Path to pg_xact dir"""
        return os.path.join(self.pg_data_dir_path(), "pg_xact")

    def pg_twophase_dir_path(self) -> str:
        """Path to pg_twophase dir"""
        return os.path.join(self.pg_data_dir_path(), "pg_twophase")

    def config_file_path(self) -> str:
        """Path to the postgresql.conf in the endpoint directory (not the one in pgdata)"""
        return os.path.join(self.endpoint_path(), "postgresql.conf")

    def config(self, lines: List[str]) -> "Endpoint":
        """
        Add lines to postgresql.conf.
        Lines should be an array of valid postgresql.conf rows.
        Returns self.
        """

        with open(self.config_file_path(), "a") as conf:
            for line in lines:
                conf.write(line)
                conf.write("\n")

        return self

    def respec(self, **kwargs):
        """Update the endpoint.json file used by control_plane."""
        # Read config
        config_path = os.path.join(self.endpoint_path(), "endpoint.json")
        with open(config_path, "r") as f:
            data_dict = json.load(f)

        # Write it back updated
        with open(config_path, "w") as file:
            json.dump(dict(data_dict, **kwargs), file, indent=4)

    def stop(self) -> "Endpoint":
        """
        Stop the Postgres instance if it's running.
        Returns self.
        """

        if self.running:
            assert self.endpoint_id is not None
            self.env.neon_cli.endpoint_stop(
                self.endpoint_id, self.tenant_id, check_return_code=self.check_stop_result
            )
            self.running = False

        return self

    def stop_and_destroy(self) -> "Endpoint":
        """
        Stop the Postgres instance, then destroy the endpoint.
        Returns self.
        """

        assert self.endpoint_id is not None
        self.env.neon_cli.endpoint_stop(
            self.endpoint_id, self.tenant_id, True, check_return_code=self.check_stop_result
        )
        self.endpoint_id = None
        self.running = False

        return self

    def create_start(
        self,
        branch_name: str,
        endpoint_id: Optional[str] = None,
        hot_standby: bool = False,
        lsn: Optional[Lsn] = None,
        config_lines: Optional[List[str]] = None,
    ) -> "Endpoint":
        """
        Create an endpoint, apply config, and start Postgres.
        Returns self.
        """

        started_at = time.time()

        self.create(
            branch_name=branch_name,
            endpoint_id=endpoint_id,
            config_lines=config_lines,
            hot_standby=hot_standby,
            lsn=lsn,
        ).start()

        log.info(f"Postgres startup took {time.time() - started_at} seconds")

        return self

    def __enter__(self) -> "Endpoint":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ):
        self.stop()


class EndpointFactory:
    """An object representing multiple compute endpoints."""

    def __init__(self, env: NeonEnv):
        self.env = env
        self.num_instances: int = 0
        self.endpoints: List[Endpoint] = []

    def create_start(
        self,
        branch_name: str,
        endpoint_id: Optional[str] = None,
        tenant_id: Optional[TenantId] = None,
        lsn: Optional[Lsn] = None,
        hot_standby: bool = False,
        config_lines: Optional[List[str]] = None,
    ) -> Endpoint:
        ep = Endpoint(
            self.env,
            tenant_id=tenant_id or self.env.initial_tenant,
            pg_port=self.env.port_distributor.get_port(),
            http_port=self.env.port_distributor.get_port(),
        )
        self.num_instances += 1
        self.endpoints.append(ep)

        return ep.create_start(
            branch_name=branch_name,
            endpoint_id=endpoint_id,
            hot_standby=hot_standby,
            config_lines=config_lines,
            lsn=lsn,
        )

    def create(
        self,
        branch_name: str,
        endpoint_id: Optional[str] = None,
        tenant_id: Optional[TenantId] = None,
        lsn: Optional[Lsn] = None,
        hot_standby: bool = False,
        config_lines: Optional[List[str]] = None,
    ) -> Endpoint:
        ep = Endpoint(
            self.env,
            tenant_id=tenant_id or self.env.initial_tenant,
            pg_port=self.env.port_distributor.get_port(),
            http_port=self.env.port_distributor.get_port(),
        )

        if endpoint_id is None:
            endpoint_id = self.env.generate_endpoint_id()

        self.num_instances += 1
        self.endpoints.append(ep)

        return ep.create(
            branch_name=branch_name,
            endpoint_id=endpoint_id,
            lsn=lsn,
            hot_standby=hot_standby,
            config_lines=config_lines,
        )

    def stop_all(self) -> "EndpointFactory":
        for ep in self.endpoints:
            ep.stop()

        return self

    def new_replica(self, origin: Endpoint, endpoint_id: str, config_lines: Optional[List[str]]):
        branch_name = origin.branch_name
        assert origin in self.endpoints
        assert branch_name is not None

        return self.create(
            branch_name=branch_name,
            endpoint_id=endpoint_id,
            tenant_id=origin.tenant_id,
            lsn=None,
            hot_standby=True,
            config_lines=config_lines,
        )

    def new_replica_start(
        self, origin: Endpoint, endpoint_id: str, config_lines: Optional[List[str]] = None
    ):
        branch_name = origin.branch_name
        assert origin in self.endpoints
        assert branch_name is not None

        return self.create_start(
            branch_name=branch_name,
            endpoint_id=endpoint_id,
            tenant_id=origin.tenant_id,
            lsn=None,
            hot_standby=True,
            config_lines=config_lines,
        )


@dataclass
class SafekeeperPort:
    pg: int
    http: int


@dataclass
class Safekeeper:
    """An object representing a running safekeeper daemon."""

    env: NeonEnv
    port: SafekeeperPort
    id: int
    running: bool = False

    def start(self) -> "Safekeeper":
        assert self.running is False
        self.env.neon_cli.safekeeper_start(self.id)
        self.running = True
        # wait for wal acceptor start by checking its status
        started_at = time.time()
        while True:
            try:
                with self.http_client() as http_cli:
                    http_cli.check_status()
            except Exception as e:
                elapsed = time.time() - started_at
                if elapsed > 3:
                    raise RuntimeError(
                        f"timed out waiting {elapsed:.0f}s for wal acceptor start: {e}"
                    )
                time.sleep(0.5)
            else:
                break  # success
        return self

    def stop(self, immediate: bool = False) -> "Safekeeper":
        log.info("Stopping safekeeper {}".format(self.id))
        self.env.neon_cli.safekeeper_stop(self.id, immediate)
        self.running = False
        return self

    def append_logical_message(
        self, tenant_id: TenantId, timeline_id: TimelineId, request: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Send JSON_CTRL query to append LogicalMessage to WAL and modify
        safekeeper state. It will construct LogicalMessage from provided
        prefix and message, and then will write it to WAL.
        """

        # "replication=0" hacks psycopg not to send additional queries
        # on startup, see https://github.com/psycopg/psycopg2/pull/482
        token = self.env.auth_keys.generate_tenant_token(tenant_id)
        connstr = f"host=localhost port={self.port.pg} password={token} replication=0 options='-c timeline_id={timeline_id} tenant_id={tenant_id}'"

        with closing(psycopg2.connect(connstr)) as conn:
            # server doesn't support transactions
            conn.autocommit = True
            with conn.cursor() as cur:
                request_json = json.dumps(request)
                log.info(f"JSON_CTRL request on port {self.port.pg}: {request_json}")
                cur.execute("JSON_CTRL " + request_json)
                all = cur.fetchall()
                log.info(f"JSON_CTRL response: {all[0][0]}")
                res = json.loads(all[0][0])
                assert isinstance(res, dict)
                return res

    def http_client(self, auth_token: Optional[str] = None) -> SafekeeperHttpClient:
        return SafekeeperHttpClient(port=self.port.http, auth_token=auth_token)

    def data_dir(self) -> str:
        return os.path.join(self.env.repo_dir, "safekeepers", f"sk{self.id}")


@dataclass
class SafekeeperTimelineStatus:
    acceptor_epoch: int
    pg_version: int  # Not exactly a PgVersion, safekeeper returns version as int, for example 150002 for 15.2
    flush_lsn: Lsn
    commit_lsn: Lsn
    timeline_start_lsn: Lsn
    backup_lsn: Lsn
    peer_horizon_lsn: Lsn
    remote_consistent_lsn: Lsn


@dataclass
class SafekeeperMetrics:
    # These are metrics from Prometheus which uses float64 internally.
    # As a consequence, values may differ from real original int64s.
    flush_lsn_inexact: Dict[Tuple[TenantId, TimelineId], int] = field(default_factory=dict)
    commit_lsn_inexact: Dict[Tuple[TenantId, TimelineId], int] = field(default_factory=dict)


class SafekeeperHttpClient(requests.Session):
    HTTPError = requests.HTTPError

    def __init__(self, port: int, auth_token: Optional[str] = None):
        super().__init__()
        self.port = port
        self.auth_token = auth_token

        if auth_token is not None:
            self.headers["Authorization"] = f"Bearer {auth_token}"

    def check_status(self):
        self.get(f"http://localhost:{self.port}/v1/status").raise_for_status()

    def debug_dump(self, params: Dict[str, str] = {}) -> Dict[str, Any]:
        res = self.get(f"http://localhost:{self.port}/v1/debug_dump", params=params)
        res.raise_for_status()
        res_json = res.json()
        assert isinstance(res_json, dict)
        return res_json

    def pull_timeline(self, body: Dict[str, Any]) -> Dict[str, Any]:
        res = self.post(f"http://localhost:{self.port}/v1/pull_timeline", json=body)
        res.raise_for_status()
        res_json = res.json()
        assert isinstance(res_json, dict)
        return res_json

    def timeline_create(
        self,
        tenant_id: TenantId,
        timeline_id: TimelineId,
        pg_version: int,  # Not exactly a PgVersion, safekeeper returns version as int, for example 150002 for 15.2
        commit_lsn: Lsn,
    ):
        body = {
            "tenant_id": str(tenant_id),
            "timeline_id": str(timeline_id),
            "pg_version": pg_version,
            "commit_lsn": str(commit_lsn),
        }
        res = self.post(f"http://localhost:{self.port}/v1/tenant/timeline", json=body)
        res.raise_for_status()

    def timeline_status(
        self, tenant_id: TenantId, timeline_id: TimelineId
    ) -> SafekeeperTimelineStatus:
        res = self.get(f"http://localhost:{self.port}/v1/tenant/{tenant_id}/timeline/{timeline_id}")
        res.raise_for_status()
        resj = res.json()
        return SafekeeperTimelineStatus(
            acceptor_epoch=resj["acceptor_state"]["epoch"],
            pg_version=resj["pg_info"]["pg_version"],
            flush_lsn=Lsn(resj["flush_lsn"]),
            commit_lsn=Lsn(resj["commit_lsn"]),
            timeline_start_lsn=Lsn(resj["timeline_start_lsn"]),
            backup_lsn=Lsn(resj["backup_lsn"]),
            peer_horizon_lsn=Lsn(resj["peer_horizon_lsn"]),
            remote_consistent_lsn=Lsn(resj["remote_consistent_lsn"]),
        )

    def record_safekeeper_info(self, tenant_id: TenantId, timeline_id: TimelineId, body):
        res = self.post(
            f"http://localhost:{self.port}/v1/record_safekeeper_info/{tenant_id}/{timeline_id}",
            json=body,
        )
        res.raise_for_status()

    def timeline_delete_force(self, tenant_id: TenantId, timeline_id: TimelineId) -> Dict[Any, Any]:
        res = self.delete(
            f"http://localhost:{self.port}/v1/tenant/{tenant_id}/timeline/{timeline_id}"
        )
        res.raise_for_status()
        res_json = res.json()
        assert isinstance(res_json, dict)
        return res_json

    def tenant_delete_force(self, tenant_id: TenantId) -> Dict[Any, Any]:
        res = self.delete(f"http://localhost:{self.port}/v1/tenant/{tenant_id}")
        res.raise_for_status()
        res_json = res.json()
        assert isinstance(res_json, dict)
        return res_json

    def get_metrics_str(self) -> str:
        request_result = self.get(f"http://localhost:{self.port}/metrics")
        request_result.raise_for_status()
        return request_result.text

    def get_metrics(self) -> SafekeeperMetrics:
        all_metrics_text = self.get_metrics_str()

        metrics = SafekeeperMetrics()
        for match in re.finditer(
            r'^safekeeper_flush_lsn{tenant_id="([0-9a-f]+)",timeline_id="([0-9a-f]+)"} (\S+)$',
            all_metrics_text,
            re.MULTILINE,
        ):
            metrics.flush_lsn_inexact[(TenantId(match.group(1)), TimelineId(match.group(2)))] = int(
                match.group(3)
            )
        for match in re.finditer(
            r'^safekeeper_commit_lsn{tenant_id="([0-9a-f]+)",timeline_id="([0-9a-f]+)"} (\S+)$',
            all_metrics_text,
            re.MULTILINE,
        ):
            metrics.commit_lsn_inexact[
                (TenantId(match.group(1)), TimelineId(match.group(2)))
            ] = int(match.group(3))
        return metrics


@dataclass
class NeonBroker:
    """An object managing storage_broker instance"""

    logfile: Path
    port: int
    neon_binpath: Path
    handle: Optional[subprocess.Popen[Any]] = None  # handle of running daemon

    def listen_addr(self):
        return f"127.0.0.1:{self.port}"

    def client_url(self):
        return f"http://{self.listen_addr()}"

    def check_status(self):
        return True  # TODO

    def try_start(self):
        if self.handle is not None:
            log.debug(f"storage_broker is already running on port {self.port}")
            return

        listen_addr = self.listen_addr()
        log.info(f'starting storage_broker to listen incoming connections at "{listen_addr}"')
        with open(self.logfile, "wb") as logfile:
            args = [
                str(self.neon_binpath / "storage_broker"),
                f"--listen-addr={listen_addr}",
            ]
            self.handle = subprocess.Popen(args, stdout=logfile, stderr=logfile)

        # wait for start
        started_at = time.time()
        while True:
            try:
                self.check_status()
            except Exception as e:
                elapsed = time.time() - started_at
                if elapsed > 5:
                    raise RuntimeError(
                        f"timed out waiting {elapsed:.0f}s for storage_broker start: {e}"
                    )
                time.sleep(0.5)
            else:
                break  # success

    def stop(self):
        if self.handle is not None:
            self.handle.terminate()
            self.handle.wait()


def get_test_output_dir(request: FixtureRequest, top_output_dir: Path) -> Path:
    """Compute the working directory for an individual test."""
    test_name = request.node.name
    test_dir = top_output_dir / test_name.replace("/", "-")
    log.info(f"get_test_output_dir is {test_dir}")
    # make mypy happy
    assert isinstance(test_dir, Path)
    return test_dir


def get_test_repo_dir(request: FixtureRequest, top_output_dir: Path) -> Path:
    return get_test_output_dir(request, top_output_dir) / "repo"


def pytest_addoption(parser: Parser):
    parser.addoption(
        "--preserve-database-files",
        action="store_true",
        default=False,
        help="Preserve timeline files after the test suite is over",
    )


SMALL_DB_FILE_NAME_REGEX: re.Pattern = re.compile(  # type: ignore[type-arg]
    r"config|metadata|.+\.(?:toml|pid|json|sql)"
)


# This is autouse, so the test output directory always gets created, even
# if a test doesn't put anything there. It also solves a problem with the
# neon_simple_env fixture: if TEST_SHARED_FIXTURES is not set, it
# creates the repo in the test output directory. But it cannot depend on
# 'test_output_dir' fixture, because when TEST_SHARED_FIXTURES is not set,
# it has 'session' scope and cannot access fixtures with 'function'
# scope. So it uses the get_test_output_dir() function to get the path, and
# this fixture ensures that the directory exists.  That works because
# 'autouse' fixtures are run before other fixtures.
@pytest.fixture(scope="function", autouse=True)
def test_output_dir(request: FixtureRequest, top_output_dir: Path) -> Iterator[Path]:
    """Create the working directory for an individual test."""

    # one directory per test
    test_dir = get_test_output_dir(request, top_output_dir)
    log.info(f"test_output_dir is {test_dir}")
    shutil.rmtree(test_dir, ignore_errors=True)
    test_dir.mkdir()

    yield test_dir

    allure_attach_from_dir(test_dir)


SKIP_DIRS = frozenset(
    (
        "pg_wal",
        "pg_stat",
        "pg_stat_tmp",
        "pg_subtrans",
        "pg_logical",
        "pg_replslot/wal_proposer_slot",
    )
)

SKIP_FILES = frozenset(
    (
        "pg_internal.init",
        "pg.log",
        "zenith.signal",
        "pg_hba.conf",
        "postgresql.conf",
        "postmaster.opts",
        "postmaster.pid",
        "pg_control",
    )
)


def should_skip_dir(dirname: str) -> bool:
    return dirname in SKIP_DIRS


def should_skip_file(filename: str) -> bool:
    if filename in SKIP_FILES:
        return True
    # check for temp table files according to https://www.postgresql.org/docs/current/storage-file-layout.html
    # i e "tBBB_FFF"
    if not filename.startswith("t"):
        return False

    tmp_name = filename[1:].split("_")
    if len(tmp_name) != 2:
        return False

    try:
        list(map(int, tmp_name))
    except:  # noqa: E722
        return False
    return True


#
# Test helpers
#
def list_files_to_compare(pgdata_dir: Path) -> List[str]:
    pgdata_files = []
    for root, _file, filenames in os.walk(pgdata_dir):
        for filename in filenames:
            rel_dir = os.path.relpath(root, pgdata_dir)
            # Skip some dirs and files we don't want to compare
            if should_skip_dir(rel_dir) or should_skip_file(filename):
                continue
            rel_file = os.path.join(rel_dir, filename)
            pgdata_files.append(rel_file)

    pgdata_files.sort()
    log.info(pgdata_files)
    return pgdata_files


# pg is the existing and running compute node, that we want to compare with a basebackup
def check_restored_datadir_content(
    test_output_dir: Path,
    env: NeonEnv,
    endpoint: Endpoint,
):
    # Get the timeline ID. We need it for the 'basebackup' command
    timeline = TimelineId(endpoint.safe_psql("SHOW neon.timeline_id")[0][0])

    # stop postgres to ensure that files won't change
    endpoint.stop()

    # Take a basebackup from pageserver
    restored_dir_path = env.repo_dir / f"{endpoint.endpoint_id}_restored_datadir"
    restored_dir_path.mkdir(exist_ok=True)

    pg_bin = PgBin(test_output_dir, env.pg_distrib_dir, env.pg_version)
    psql_path = os.path.join(pg_bin.pg_bin_path, "psql")

    cmd = rf"""
        {psql_path}                                    \
            --no-psqlrc                                \
            postgres://localhost:{env.pageserver.service_port.pg}  \
            -c 'basebackup {endpoint.tenant_id} {timeline}'  \
         | tar -x -C {restored_dir_path}
    """

    # Set LD_LIBRARY_PATH in the env properly, otherwise we may use the wrong libpq.
    # PgBin sets it automatically, but here we need to pipe psql output to the tar command.
    psql_env = {"LD_LIBRARY_PATH": pg_bin.pg_lib_dir}
    result = subprocess.run(cmd, env=psql_env, capture_output=True, text=True, shell=True)

    # Print captured stdout/stderr if basebackup cmd failed.
    if result.returncode != 0:
        log.error("Basebackup shell command failed with:")
        log.error(result.stdout)
        log.error(result.stderr)
    assert result.returncode == 0

    # list files we're going to compare
    assert endpoint.pgdata_dir
    pgdata_files = list_files_to_compare(Path(endpoint.pgdata_dir))
    restored_files = list_files_to_compare(restored_dir_path)

    # check that file sets are equal
    assert pgdata_files == restored_files

    # compare content of the files
    # filecmp returns (match, mismatch, error) lists
    # We've already filtered all mismatching files in list_files_to_compare(),
    # so here expect that the content is identical
    (match, mismatch, error) = filecmp.cmpfiles(
        endpoint.pgdata_dir, restored_dir_path, pgdata_files, shallow=False
    )
    log.info(f"filecmp result mismatch and error lists:\n\t mismatch={mismatch}\n\t error={error}")

    for f in mismatch:
        f1 = os.path.join(endpoint.pgdata_dir, f)
        f2 = os.path.join(restored_dir_path, f)
        stdout_filename = "{}.filediff".format(f2)

        with open(stdout_filename, "w") as stdout_f:
            subprocess.run("xxd -b {} > {}.hex ".format(f1, f1), shell=True)
            subprocess.run("xxd -b {} > {}.hex ".format(f2, f2), shell=True)

            cmd = "diff {}.hex {}.hex".format(f1, f2)
            subprocess.run([cmd], stdout=stdout_f, shell=True)

    assert (mismatch, error) == ([], [])


def wait_for_last_flush_lsn(
    env: NeonEnv, endpoint: Endpoint, tenant: TenantId, timeline: TimelineId
) -> Lsn:
    """Wait for pageserver to catch up the latest flush LSN, returns the last observed lsn."""
    last_flush_lsn = Lsn(endpoint.safe_psql("SELECT pg_current_wal_flush_lsn()")[0][0])
    return wait_for_last_record_lsn(env.pageserver.http_client(), tenant, timeline, last_flush_lsn)


def wait_for_wal_insert_lsn(
    env: NeonEnv, endpoint: Endpoint, tenant: TenantId, timeline: TimelineId
) -> Lsn:
    """Wait for pageserver to catch up the latest flush LSN, returns the last observed lsn."""
    last_flush_lsn = Lsn(endpoint.safe_psql("SELECT pg_current_wal_insert_lsn()")[0][0])
    return wait_for_last_record_lsn(env.pageserver.http_client(), tenant, timeline, last_flush_lsn)


def fork_at_current_lsn(
    env: NeonEnv,
    endpoint: Endpoint,
    new_branch_name: str,
    ancestor_branch_name: str,
    tenant_id: Optional[TenantId] = None,
) -> TimelineId:
    """
    Create new branch at the last LSN of an existing branch.
    The "last LSN" is taken from the given Postgres instance. The pageserver will wait for all the
    the WAL up to that LSN to arrive in the pageserver before creating the branch.
    """
    current_lsn = endpoint.safe_psql("SELECT pg_current_wal_lsn()")[0][0]
    return env.neon_cli.create_branch(new_branch_name, ancestor_branch_name, tenant_id, current_lsn)


def last_flush_lsn_upload(
    env: NeonEnv, endpoint: Endpoint, tenant_id: TenantId, timeline_id: TimelineId
) -> Lsn:
    """
    Wait for pageserver to catch to the latest flush LSN of given endpoint,
    checkpoint pageserver, and wait for it to be uploaded (remote_consistent_lsn
    reaching flush LSN).
    """
    last_flush_lsn = wait_for_last_flush_lsn(env, endpoint, tenant_id, timeline_id)
    ps_http = env.pageserver.http_client()
    wait_for_last_record_lsn(ps_http, tenant_id, timeline_id, last_flush_lsn)
    # force a checkpoint to trigger upload
    ps_http.timeline_checkpoint(tenant_id, timeline_id)
    wait_for_upload(ps_http, tenant_id, timeline_id, last_flush_lsn)
    return last_flush_lsn


def parse_project_git_version_output(s: str) -> str:
    """
    Parses the git commit hash out of the --version output supported at least by neon_local.

    The information is generated by utils::project_git_version!
    """
    import re

    res = re.search(r"git(-env)?:([0-9a-fA-F]{8,40})(-\S+)?", s)
    if res and (commit := res.group(2)):
        return commit

    raise ValueError(f"unable to parse --version output: '{s}'")

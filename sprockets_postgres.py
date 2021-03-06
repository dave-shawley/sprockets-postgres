import asyncio
import contextlib
import dataclasses
import logging
import os
import time
import typing
from distutils import util

import aiopg
import psycopg2
from aiopg import pool
from psycopg2 import errors, extras
from tornado import ioloop, web

LOGGER = logging.getLogger('sprockets-postgres')

DEFAULT_POSTGRES_CONNECTION_TIMEOUT = 10
DEFAULT_POSTGRES_CONNECTION_TTL = 300
DEFAULT_POSTGRES_HSTORE = 'FALSE'
DEFAULT_POSTGRES_JSON = 'FALSE'
DEFAULT_POSTGRES_MAX_POOL_SIZE = '10'
DEFAULT_POSTGRES_MIN_POOL_SIZE = '1'
DEFAULT_POSTGRES_QUERY_TIMEOUT = 120
DEFAULT_POSTGRES_UUID = 'TRUE'

QueryParameters = typing.Union[dict, list, tuple, None]
"""Type annotation for query parameters"""

Timeout = typing.Union[int, float, None]
"""Type annotation for timeout values"""


@dataclasses.dataclass
class QueryResult:
    """A :func:`Data Class <dataclasses.dataclass>` that is generated as a
    result of each query that is executed.

    :param row_count: The quantity of rows impacted by the query
    :param row: If a single row is returned, the data for that row
    :param rows: If more than one row is returned, this attribute is set as the
        list of rows, in order.

    """
    row_count: int
    row: typing.Optional[dict]
    rows: typing.Optional[typing.List[dict]]


class PostgresConnector:
    """Wraps a :class:`aiopg.Cursor` instance for creating explicit
    transactions, calling stored procedures, and executing queries.

    Unless the :meth:`~sprockets_postgres.PostgresConnector.transaction`
    asynchronous :ref:`context-manager <python:typecontextmanager>` is used,
    each call to :meth:`~sprockets_postgres.PostgresConnector.callproc` and
    :meth:`~sprockets_postgres.PostgresConnector.execute` is an explicit
    transaction.

    .. note:: :class:`PostgresConnector` instances are created by
        :meth:`ApplicationMixin.postgres_connector
        <sprockets_postgres.ApplicationMixin.postgres_connector>` and should
        not be created directly.

    :param cursor: The cursor to use in the connector
    :type cursor: aiopg.Cursor
    :param on_error: The callback to invoke when an exception is caught
    :param on_duration: The callback to invoke when a query is complete and all
        of the data has been returned.
    :param timeout: A timeout value in seconds for executing queries. If
        unspecified, defaults to the ``POSTGRES_QUERY_TIMEOUT`` environment
        variable and if that is not specified, to the
        :const:`DEFAULT_POSTGRES_QUERY_TIMEOUT` value of ``120``
    :type timeout: :data:`~sprockets_postgres.Timeout`

    """
    def __init__(self,
                 cursor: aiopg.Cursor,
                 on_error: typing.Callable,
                 on_duration: typing.Optional[typing.Callable] = None,
                 timeout: Timeout = None):
        self.cursor = cursor
        self._on_error = on_error
        self._on_duration = on_duration
        self._timeout = timeout or int(
            os.environ.get(
                'POSTGRES_QUERY_TIMEOUT',
                DEFAULT_POSTGRES_QUERY_TIMEOUT))

    async def callproc(self,
                       name: str,
                       parameters: QueryParameters = None,
                       metric_name: str = '',
                       *,
                       timeout: Timeout = None) -> QueryResult:
        """Execute a stored procedure / function

        :param name: The stored procedure / function name to call
        :param parameters: Query parameters to pass when calling
        :type parameters: :data:`~sprockets_postgres.QueryParameters`
        :param metric_name: The metric name for duration recording and logging
        :param timeout: Timeout value to override the default or the value
            specified when creating the
            :class:`~sprockets_postgres.PostgresConnector`.
        :type timeout: :data:`~sprockets_postgres.Timeout`

        :raises asyncio.TimeoutError: when there is a query or network timeout
        :raises psycopg2.Error: when there is an exception raised by Postgres

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        :rtype: :class:`~sprockets_postgres.QueryResult`

        """
        return await self._query(
            self.cursor.callproc,
            metric_name,
            procname=name,
            parameters=parameters,
            timeout=timeout)

    async def execute(self,
                      sql: str,
                      parameters: QueryParameters = None,
                      metric_name: str = '',
                      *,
                      timeout: Timeout = None) -> QueryResult:
        """Execute a query, specifying a name for the query, the SQL statement,
        and optional positional arguments to pass in with the query.

        Parameters may be provided as sequence or mapping and will be
        bound to variables in the operation.  Variables are specified
        either with positional ``%s`` or named ``%({name})s`` placeholders.

        :param sql: The SQL statement to execute
        :param parameters: Query parameters to pass as part of the execution
        :type parameters: :data:`~sprockets_postgres.QueryParameters`
        :param metric_name: The metric name for duration recording and logging
        :param timeout: Timeout value to override the default or the value
            specified when creating the
            :class:`~sprockets_postgres.PostgresConnector`.
        :type timeout: :data:`~sprockets_postgres.Timeout`

        :raises asyncio.TimeoutError: when there is a query or network timeout
        :raises psycopg2.Error: when there is an exception raised by Postgres

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        :rtype: :class:`~sprockets_postgres.QueryResult`

        """
        return await self._query(
            self.cursor.execute,
            metric_name,
            operation=sql,
            parameters=parameters,
            timeout=timeout)

    @contextlib.asynccontextmanager
    async def transaction(self) \
            -> typing.AsyncContextManager['PostgresConnector']:
        """asynchronous :ref:`context-manager <python:typecontextmanager>`
        function that implements full ``BEGIN``, ``COMMIT``, and ``ROLLBACK``
        semantics. If there is a :exc:`psycopg2.Error` raised during the
        transaction, the entire transaction will be rolled back.

        If no exception is raised, the transaction will be committed when
        exiting the context manager.

        .. note:: This method is provided for edge case usage. As a
            generalization
            :meth:`sprockets_postgres.RequestHandlerMixin.postgres_transaction`
            should be used instead.

        *Usage Example*

        .. code-block::

            class RequestHandler(sprockets_postgres.RequestHandlerMixin,
                                 web.RequestHandler):

                async def post(self):
                    async with self.postgres_transaction() as transaction:
                        result1 = await transaction.execute(QUERY_ONE)
                        result2 = await transaction.execute(QUERY_TWO)
                        result3 = await transaction.execute(QUERY_THREE)

        :raises asyncio.TimeoutError: when there is a query or network timeout
            when starting the transaction
        :raises psycopg2.Error: when there is an exception raised by Postgres
            when starting the transaction

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        """
        async with self.cursor.begin():
            yield self

    async def _query(self,
                     method: typing.Callable,
                     metric_name: str,
                     **kwargs):
        if kwargs['timeout'] is None:
            kwargs['timeout'] = self._timeout
        start_time = time.monotonic()
        try:
            await method(**kwargs)
        except (asyncio.TimeoutError, psycopg2.Error) as err:
            exc = self._on_error(metric_name, err)
            if exc:
                raise exc
        else:
            results = await self._query_results()
            if self._on_duration:
                self._on_duration(
                    metric_name, time.monotonic() - start_time)
            return results

    async def _query_results(self) -> QueryResult:
        count, row, rows = self.cursor.rowcount, None, None
        if self.cursor.rowcount == 1:
            try:
                row = dict(await self.cursor.fetchone())
            except psycopg2.ProgrammingError:
                pass
        elif self.cursor.rowcount > 1:
            try:
                rows = [dict(row) for row in await self.cursor.fetchall()]
            except psycopg2.ProgrammingError:
                pass
        return QueryResult(count, row, rows)


class ConnectionException(Exception):
    """Raised when the connection to Postgres can not be established"""


class ApplicationMixin:
    """
    :class:`sprockets.http.app.Application` / :class:`tornado.web.Application`
    mixin for handling the connection to Postgres and exporting functions for
    querying the database, getting the status, and proving a cursor.

    Automatically creates and shuts down :class:`aiopg.Pool` on startup
    and shutdown by installing `on_start` and `shutdown` callbacks into the
    :class:`~sprockets.http.app.Application` instance.

    """
    POSTGRES_STATUS_TIMEOUT = 3

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._postgres_pool: typing.Optional[pool.Pool] = None
        self.runner_callbacks['on_start'].append(self._postgres_setup)
        self.runner_callbacks['shutdown'].append(self._postgres_shutdown)

    @contextlib.asynccontextmanager
    async def postgres_connector(self,
                                 on_error: typing.Callable,
                                 on_duration: typing.Optional[
                                     typing.Callable] = None,
                                 timeout: Timeout = None) \
            -> typing.AsyncContextManager[PostgresConnector]:
        """Asynchronous :ref:`context-manager <python:typecontextmanager>`
        that returns a :class:`~sprockets_postgres.PostgresConnector` instance
        from the connection pool with a cursor.

        .. note:: This function is designed to work in conjunction with the
            :class:`~sprockets_postgres.RequestHandlerMixin` and is generally
            not invoked directly.

        :param on_error: A callback function that is invoked on exception. If
            an exception is returned from that function, it will raise it.
        :param on_duration: An optional callback function that is invoked after
            a query has completed to record the duration that encompasses
            both executing the query and retrieving the returned records, if
            any.
        :param timeout: Used to override the default query timeout.
        :type timeout: :data:`~sprockets_postgres.Timeout`

        :raises asyncio.TimeoutError: when the request to retrieve a connection
            from the pool times out.
        :raises sprockets_postgres.ConnectionException: when the application
            can not connect to the configured Postgres instance.
        :raises psycopg2.Error: when Postgres raises an exception during the
            creation of the cursor.

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        """
        try:
            async with self._postgres_pool.acquire() as conn:
                async with conn.cursor(
                        cursor_factory=extras.RealDictCursor,
                        timeout=timeout) as cursor:
                    yield PostgresConnector(
                        cursor, on_error, on_duration, timeout)
        except (asyncio.TimeoutError, psycopg2.Error) as err:
            exc = on_error('postgres_connector', ConnectionException(str(err)))
            if exc:
                raise exc
            else:   # postgres_status.on_error does not return an exception
                yield None

    async def postgres_status(self) -> dict:
        """Invoke from the ``/status`` RequestHandler to check that there is
        a Postgres connection handler available and return info about the
        pool.

        The ``available`` item in the dictionary indicates that the
        application was able to perform a ``SELECT 1`` against the database
        using a :class:`~sprockets_postgres.PostgresConnector` instance.

        The ``pool_size`` item indicates the current quantity of open
        connections to Postgres.

        The ``pool_free`` item indicates the current number of idle
        connections available to process queries.

        *Example return value*

        .. code-block:: python

            {
                'available': True,
                'pool_size': 10,
                'pool_free': 8
            }

        """
        query_error = asyncio.Event()

        def on_error(_metric_name, _exc) -> None:
            query_error.set()
            return None

        async with self.postgres_connector(
                on_error,
                timeout=self.POSTGRES_STATUS_TIMEOUT) as connector:
            if connector:
                await connector.execute('SELECT 1')

        return {
            'available': not query_error.is_set(),
            'pool_size': self._postgres_pool.size,
            'pool_free': self._postgres_pool.freesize
        }

    async def _postgres_setup(self,
                              _app: web.Application,
                              loop: ioloop.IOLoop) -> None:
        """Setup the Postgres pool of connections and log if there is an error.

        This is invoked by the :class:`sprockets.http.app.Application` on start
        callback mechanism.

        """
        if 'POSTGRES_URL' not in os.environ:
            LOGGER.critical('Missing POSTGRES_URL environment variable')
            return self.stop(loop)
        self._postgres_pool = pool.Pool(
            os.environ['POSTGRES_URL'],
            maxsize=int(
                os.environ.get(
                    'POSTGRES_MAX_POOL_SIZE',
                    DEFAULT_POSTGRES_MAX_POOL_SIZE)),
            minsize=int(
                os.environ.get(
                    'POSTGRES_MIN_POOL_SIZE',
                    DEFAULT_POSTGRES_MIN_POOL_SIZE)),
            timeout=int(
                os.environ.get(
                    'POSTGRES_CONNECT_TIMEOUT',
                    DEFAULT_POSTGRES_CONNECTION_TIMEOUT)),
            enable_hstore=util.strtobool(
                os.environ.get(
                    'POSTGRES_HSTORE', DEFAULT_POSTGRES_HSTORE)),
            enable_json=util.strtobool(
                os.environ.get('POSTGRES_JSON', DEFAULT_POSTGRES_JSON)),
            enable_uuid=util.strtobool(
                os.environ.get('POSTGRES_UUID', DEFAULT_POSTGRES_UUID)),
            echo=False,
            on_connect=None,
            pool_recycle=int(
                os.environ.get(
                    'POSTGRES_CONNECTION_TTL',
                    DEFAULT_POSTGRES_CONNECTION_TTL)))
        try:
            async with self._postgres_pool._cond:
                await self._postgres_pool._fill_free_pool(False)
        except (psycopg2.OperationalError,
                psycopg2.Error) as error:  # pragma: nocover
            LOGGER.warning('Error connecting to PostgreSQL on startup: %s',
                           error)

    async def _postgres_shutdown(self, _ioloop: ioloop.IOLoop) -> None:
        """Shutdown the Postgres connections and wait for them to close.

        This is invoked by the :class:`sprockets.http.app.Application` shutdown
        callback mechanism.

        """
        if self._postgres_pool is not None:
            self._postgres_pool.close()
            await self._postgres_pool.wait_closed()


class RequestHandlerMixin:
    """
    A RequestHandler mixin class exposing functions for querying the database,
    recording the duration to either :mod:`sprockets-influxdb
    <sprockets_influxdb>` or :mod:`sprockets.mixins.metrics`, and
    handling exceptions.

    """
    async def postgres_callproc(self,
                                name: str,
                                parameters: QueryParameters = None,
                                metric_name: str = '',
                                *,
                                timeout: Timeout = None) -> QueryResult:
        """Execute a stored procedure / function

        :param name: The stored procedure / function name to call
        :param parameters: Query parameters to pass when calling
        :type parameters: :data:`~sprockets_postgres.QueryParameters`
        :param metric_name: The metric name for duration recording and logging
        :param timeout: Timeout value to override the default or the value
            specified when creating the
            :class:`~sprockets_postgres.PostgresConnector`.
        :type timeout: :data:`~sprockets_postgres.Timeout`

        :raises asyncio.TimeoutError: when there is a query or network timeout
        :raises psycopg2.Error: when there is an exception raised by Postgres

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        :rtype: :class:`~sprockets_postgres.QueryResult`

        """
        async with self.application.postgres_connector(
                self._on_postgres_error,
                self._on_postgres_timing,
                timeout) as connector:
            return await connector.callproc(
                name, parameters, metric_name, timeout=timeout)

    async def postgres_execute(self,
                               sql: str,
                               parameters: QueryParameters = None,
                               metric_name: str = '',
                               *,
                               timeout: Timeout = None) -> QueryResult:
        """Execute a query, specifying a name for the query, the SQL statement,
        and optional positional arguments to pass in with the query.

        Parameters may be provided as sequence or mapping and will be
        bound to variables in the operation.  Variables are specified
        either with positional ``%s`` or named ``%({name})s`` placeholders.

        :param sql: The SQL statement to execute
        :param parameters: Query parameters to pass as part of the execution
        :type parameters: :data:`~sprockets_postgres.QueryParameters`
        :param metric_name: The metric name for duration recording and logging
        :param timeout: Timeout value to override the default or the value
            specified when creating the
            :class:`~sprockets_postgres.PostgresConnector`.
        :type timeout: :data:`~sprockets_postgres.Timeout`

        :raises asyncio.TimeoutError: when there is a query or network timeout
        :raises psycopg2.Error: when there is an exception raised by Postgres

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        :rtype: :class:`~sprockets_postgres.QueryResult`

        """
        async with self.application.postgres_connector(
                self._on_postgres_error,
                self._on_postgres_timing,
                timeout) as connector:
            return await connector.execute(
                sql, parameters, metric_name, timeout=timeout)

    @contextlib.asynccontextmanager
    async def postgres_transaction(self, timeout: Timeout = None) \
            -> typing.AsyncContextManager[PostgresConnector]:
        """asynchronous :ref:`context-manager <python:typecontextmanager>`
        function that implements full ``BEGIN``, ``COMMIT``, and ``ROLLBACK``
        semantics. If there is a :exc:`psycopg2.Error` raised during the
        transaction, the entire transaction will be rolled back.

        If no exception is raised, the transaction will be committed when
        exiting the context manager.

        *Usage Example*

        .. code-block:: python

           class RequestHandler(sprockets_postgres.RequestHandlerMixin,
                                web.RequestHandler):

           async def post(self):
               async with self.postgres_transaction() as transaction:
                   result1 = await transaction.execute(QUERY_ONE)
                   result2 = await transaction.execute(QUERY_TWO)
                   result3 = await transaction.execute(QUERY_THREE)


        :param timeout: Timeout value to override the default or the value
            specified when creating the
            :class:`~sprockets_postgres.PostgresConnector`.
        :type timeout: :data:`~sprockets_postgres.Timeout`

        :raises asyncio.TimeoutError: when there is a query or network timeout
            when starting the transaction
        :raises psycopg2.Error: when there is an exception raised by Postgres
            when starting the transaction

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        """
        async with self.application.postgres_connector(
                self._on_postgres_error,
                self._on_postgres_timing,
                timeout) as connector:
            async with connector.transaction():
                yield connector

    def _on_postgres_error(self,
                           metric_name: str,
                           exc: Exception) -> typing.Optional[Exception]:
        """Override for different error handling behaviors

        Return an exception if you would like for it to be raised, or swallow
        it here.

        """
        LOGGER.error('%s in %s for %s (%s)',
                     exc.__class__.__name__, self.__class__.__name__,
                     metric_name, str(exc).split('\n')[0])
        if isinstance(exc, ConnectionException):
            raise web.HTTPError(503, reason='Database Connection Error')
        elif isinstance(exc, asyncio.TimeoutError):
            raise web.HTTPError(500, reason='Query Timeout')
        elif isinstance(exc, errors.UniqueViolation):
            raise web.HTTPError(409, reason='Unique Violation')
        elif isinstance(exc, psycopg2.Error):
            raise web.HTTPError(500, reason='Database Error')
        return exc

    def _on_postgres_timing(self,
                            metric_name: str,
                            duration: float) -> None:
        """Override for custom metric recording. As a default behavior it will
        attempt to detect `sprockets-influxdb
        <https://sprockets-influxdb.readthedocs.io/>`_ and
        `sprockets.mixins.metrics
        <https://sprocketsmixinsmetrics.readthedocs.io/en/latest/>`_ and
        record the metrics using them if they are available. If they are not
        available, it will record the query duration to the `DEBUG` log.

        :param metric_name: The name of the metric to record
        :param duration: The duration to record for the metric

        """
        if hasattr(self, 'influxdb'):  # sprockets-influxdb
            self.influxdb.set_field(metric_name, duration)
        elif hasattr(self, 'record_timing'):  # sprockets.mixins.metrics
            self.record_timing(metric_name, duration)
        else:
            LOGGER.debug('Postgres query %s duration: %s',
                         metric_name, duration)

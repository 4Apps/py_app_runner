"""
Bridge service should be treated as a transport layer for the other services.
"""

import asyncio
import logging
import signal
import socket
import threading
from argparse import Namespace
from collections.abc import Awaitable, Callable

import uvloop
from database_wrapper_pgsql import PgsqlWithPoolingAsync
from database_wrapper_redis import RedisDbAsync, RedisDbWithPoolAsync
from tornado import escape, httpserver, netutil, process
from tornado.platform.asyncio import AsyncIOMainLoop

from py_app_runner.config import is_env_dev
from py_app_runner.db_pools import DbPools
from py_app_runner.pybridge import PyBridge
from py_app_runner.registry import AppRegistry
from py_app_runner.request_handler.handlers import WebApplicationBase
from py_app_runner.utils import json_decode, json_encode, workers_auto
from py_app_runner.wbcm.wb_connection_manager import WbConnectionManager

ServerStarter = Callable[
    [WebApplicationBase, Namespace, logging.Logger, list[socket.socket] | None],
    Awaitable[None],
]


async def start_server_dev(
    app: WebApplicationBase,
    args: Namespace,
    log: logging.Logger,
    sockets: list[socket.socket] | None = None,  # unused
) -> None:
    # Enable autoreload
    from tornado import autoreload

    autoreload.start()

    # Start the server
    server = httpserver.HTTPServer(app, xheaders=True)
    server.listen(args.port, address=args.address)  # no fork, no reuse_port

    # Graceful shutdown on SIGINT/SIGTERM
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    log.info("Web server started in DEV mode (autoreload ON)")
    try:
        await shutdown.wait()

    finally:
        # Shutdown the server
        server.stop()
        await asyncio.sleep(0.1)
        await server.close_all_connections()


async def start_server_prod(
    app: WebApplicationBase,
    args: Namespace,
    log: logging.Logger,
    sockets: list[socket.socket] | None,
) -> None:
    assert sockets, "Prod starter requires pre-bound sockets"

    # Set asyncio event loop for tornado
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    AsyncIOMainLoop().install()

    # Disable debug stuff
    app.settings.update(autoreload=False)

    # Start the server
    server = httpserver.HTTPServer(app, xheaders=True)
    server.add_sockets(sockets)

    # Graceful shutdown on SIGINT/SIGTERM
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    log.info("Web server started in PROD mode")
    try:
        await shutdown.wait()
    finally:
        server.stop()
        await asyncio.sleep(0.1)
        await server.close_all_connections()
        await asyncio.sleep(0)  # let callbacks run once


async def child_process_initializer(
    application: WebApplicationBase,
    args: Namespace,
    base_logger: logging.Logger,
    sockets: list[socket.socket] | None,
    start_server: ServerStarter,
) -> None:
    config = AppRegistry.config()

    base_logger.info(f"Child starting, task_id={process.task_id()}")

    # Init redis
    redisPool = RedisDbWithPoolAsync(config["db"]["redis"])

    # Init postgres
    pgPool = PgsqlWithPoolingAsync(
        db_config=config["db"]["main"],
        instance_name=f"services_bridge_{config['environment']}",
    )
    await pgPool.open_pool()

    # Init database pools
    dbPools = DbPools(cache_db_pool=redisPool, main_db_pool=pgPool)
    application.set_db_pools(dbPools)

    # Init connection manager
    wb_redis = RedisDbAsync(config["db"]["redis"])
    cm = WbConnectionManager(cache_db=wb_redis, debug=args.debug_wbcm)
    bg_thread = threading.Thread(target=cm.start_in_new_loop)
    bg_thread.start()
    application.set_wb_connection_manager(cm)

    # Start the server
    try:
        base_logger.info(f"Web service child process started (task_id={process.task_id()})")
        await start_server(application, args, base_logger, sockets)

    finally:
        # Stop processing messages, thus stopping the background thread
        await cm.stop()
        bg_thread.join()

        # Close the database pools
        await redisPool.close()
        await pgPool.close_pool()

        # Remove references
        application.set_db_pools(None)  # type: ignore
        application.set_pybridge(None)  # type: ignore
        application.set_wb_connection_manager(None)  # type: ignore

        base_logger.info("Web service child process stopped")


####################
### Init Service ###
####################
async def init_service(
    args: Namespace,
    pybridge: PyBridge,
    base_logger: logging.Logger,
) -> None:
    """Init service"""
    config = AppRegistry.config()

    # Replace tornado's default json encoder/decoder
    escape.json_encode = json_encode
    escape.json_decode = json_decode

    # Load all the endpoint services
    for service_name in config["services"]:
        service = pybridge.load_service_pybridge(service_name)
        if service:
            base_logger.info(f"Loaded service: {service_name}")
            if hasattr(service, "service_name"):
                service_name = service.service_name
            pybridge.cache_service(service_name, service)

            # Load additional endpoints / services
            number_of_routes = 0
            if hasattr(service, "bridge_routes"):
                routes = service.bridge_routes()
                number_of_routes = len(routes)
                for route_name in routes:
                    route_handler = routes[route_name]
                    pybridge.cache_service(route_name, route_handler)

            base_logger.info(f"Loaded service: {service_name}; Additional routes: {number_of_routes}")
        else:
            base_logger.info(f"No endpoint for the service: {service_name}")

    # Determine which WebApplication class to use
    web_app_cls = AppRegistry.web_app_class()
    if web_app_cls is None:
        from py_app_runner.bridge.web_app import WebApplication

        web_app_cls = WebApplication

    # Initialize our application
    application = web_app_cls(
        debug=config["debug"],
        autoreload=False,
        xheaders=True,
    )
    application.set_pybridge(pybridge)

    # Dev mode: run single process without forking
    # This is needed to have autoreload working properly
    if is_env_dev(config):
        await child_process_initializer(application, args, base_logger, None, start_server_dev)
        return

    # Bind sockets and start processes
    sockets = netutil.bind_sockets(args.port, address=args.address, reuse_port=True)
    process.fork_processes(workers_auto() if not config["debug"] else 1)

    # Parent exits quickly; children continue
    if process.task_id() is None:
        base_logger.info(f"Web service listening on {args.address}:{args.port}")
        return

    asyncio.run(child_process_initializer(application, args, base_logger, sockets, start_server_prod))

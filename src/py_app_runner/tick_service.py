import asyncio
import inspect
import logging
import random
from collections.abc import Awaitable, Callable
from signal import SIGHUP, SIGINT, SIGQUIT, SIGTERM, Signals

from .timer import Timer

CALLBACK_TYPE = Callable[[int, float], None] | Callable[[int, float], Awaitable[None]]
SHUTDOWN_CALLBACK_TYPE = Callable[[], None] | Callable[[], Awaitable[None]] | None


class ShutdownRequestedException(Exception):
    """
    This class can be used to signal parts of the service/app that a shutdown was requested.
    When needToShutdown is called with withException=True, this exception will be raised.
    This is useful for example when you want to stop a long running task, but don't want
    shutdown exception to be logged.
    """

    pass


class TickService:
    # Intervals
    default_tick_interval: float
    tick_interval: float
    tick_count: int
    tick_time: float

    # Callbacks
    callbacks: list[CALLBACK_TYPE]
    shut_down_callback: SHUTDOWN_CALLBACK_TYPE
    back_off_callback: CALLBACK_TYPE | None

    # Various
    shutdown_event: asyncio.Event
    current_loop: asyncio.AbstractEventLoop
    back_off_mode: bool
    back_off_count: int
    logger_name: str
    logger: logging.Logger
    timer: Timer

    skip_signal_handling: bool = False
    """Skip signal handling. Be careful with this, as it can lead to unstoppable processes"""

    #######################
    ### Class Lifecycle ###
    #######################

    def __del__(self) -> None:
        self.logger.debug("Dealloc")

    def __init__(
        self,
        tick_interval: float,
        callback: CALLBACK_TYPE | None = None,
        shut_down_callback: SHUTDOWN_CALLBACK_TYPE = None,
        back_off_callback: CALLBACK_TYPE | None = None,
        instance_name: str | None = None,
    ):
        # Intervals
        self.default_tick_interval = tick_interval
        self.tick_interval = tick_interval
        self.tick_count = 0
        self.tick_time = 0

        # Callbacks
        self.callbacks = [] if callback is None else [callback]
        self.shut_down_callback = shut_down_callback
        self.back_off_callback = back_off_callback

        # Various
        self.shutdown_event = asyncio.Event()
        self.back_off_mode = False
        self.back_off_count = 0

        instance_name = f".{instance_name}" if instance_name is not None else ""
        self.logger_name = f"{self.__class__.__name__}{instance_name}"
        self.logger = logging.getLogger(self.logger_name)
        self.timer = Timer("TickService")

    ###############
    ### Helpers ###
    ###############

    def add_callback(self, callback: CALLBACK_TYPE) -> None:
        self.callbacks.append(callback)

    def set_interval(self, interval: float) -> None:
        self.tick_interval = interval
        self.logger.debug(f"Tick interval set to {self.tick_interval} seconds")

    def increase_interval_by(self, amount: float) -> None:
        """
        Increases the tick interval by a specified amount.

        :param amount: The amount in seconds to increase the tick interval by.
        """
        self.tick_interval += amount
        self.logger.debug(f"Tick interval increased by {amount} seconds, now at {self.tick_interval} seconds")

    def increase_interval_exp(self, factor: float = 1.1, max_interval: float = 300) -> None:
        """
        Increases the tick interval exponentially for the next tick.

        :param factor: The exponential factor to multiply the current interval by. Default is 1.1.
        :param max_interval: The maximum interval in seconds. Default is 300.
        """
        self.tick_interval = min(self.tick_interval * factor, max_interval)
        self.tick_interval *= random.uniform(0.9, 1.1)  # Little jitter
        self.logger.debug(f"Tick interval increased to {self.tick_interval} seconds")

    def reset_interval(self) -> None:
        """
        Resets the tick interval to the default value.
        """
        self.tick_interval = self.default_tick_interval
        self.logger.debug(f"Tick interval reset to {self.tick_interval} seconds")

    def set_backoff(self, start: bool = True) -> None:
        self.back_off_mode = start
        self.back_off_count = 0

    def format_time(self, seconds: float) -> str:
        """
        Formats seconds to human readable time.

        :param seconds: The seconds to format.
        :return: The formatted time.
        """
        if seconds < 60:
            return f"{seconds:.2f}s"

        if seconds < 3600:
            m, s = divmod(seconds, 60)
            return f"{int(m)}m {int(s)}s"

        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{int(h)}h {int(m)}m {int(s)}s"

    async def need_to_shutdown(self, with_exception: bool = False) -> bool:
        """
        Waits for 0.001 seconds and checks if the shutdown event is set.

        Returns:
            bool: True if the shutdown event is set, False otherwise.
        """
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            self.logger.debug("needToShutdown: Sleep was cancelled")

        if with_exception:
            if self.shutdown_event.is_set():
                raise ShutdownRequestedException("Shutdown requested")

        return self.shutdown_event.is_set()

    #######################
    ### Loop management ###
    #######################

    async def shutdown(self, signal: Signals) -> None:
        self.logger.debug(f"Shutdown signal received: {signal.name} ({signal.value})")

        # Lets first shutdown our event loop
        self.shutdown_event.set()

        # Then call external shutdown callbacks
        if self.shut_down_callback is not None:
            # Need to catch exceptions here, otherwise the loop will be stopped with a interval delay
            try:
                if inspect.iscoroutinefunction(self.shut_down_callback):
                    await self.shut_down_callback()
                else:
                    self.shut_down_callback()
            except Exception as e:
                self.logger.exception(e)

        # And finally we cancel all tasks so for example asyncio.sleep is terminated
        tasks = [
            t
            for t in asyncio.all_tasks(loop=self.current_loop)
            if t is not asyncio.current_task(loop=self.current_loop)
        ]
        [task.cancel() for task in tasks]

        logging.debug(f"Cancelling {len(tasks)} outstanding tasks")
        await asyncio.gather(*tasks)
        logging.debug("All outstanding tasks are cancelled")

        # Remove callback handlers to avoid circular references
        self.callbacks = []
        self.shut_down_callback = None

    def shutdown_handler(self, signal: Signals) -> None:
        asyncio.create_task(self.shutdown(signal))

    def init_signals(self) -> None:
        if self.skip_signal_handling:
            self.logger.debug("Skipping signal handlers")
            return

        self.logger.debug("Initializing signal handlers")

        # Attach signal handlers
        signals = (SIGHUP, SIGTERM, SIGINT, SIGQUIT)
        for s in signals:
            self.current_loop.add_signal_handler(s, lambda s=s: self.shutdown_handler(s))

    async def loop_wait(self, timeout: int | float | None = None) -> None:
        if timeout is None:
            timeout = self.tick_interval

        self.logger.debug(f"loop_wait: Sleeping for {self.format_time(timeout)}")

        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            self.logger.debug("loop_wait: Sleep was cancelled")

    async def run_loop(self) -> None:
        self.current_loop = asyncio.get_running_loop()
        self.init_signals()

        # Start infinite loop
        self.logger.debug("Starting process loop")
        while not self.shutdown_event.is_set():
            # Tick
            self.logger.debug(f"Tick #{self.tick_count + 1} started")
            async with self.timer.aenter("TickService.tick"):
                await self.tick()

            # Timer stats
            self.timer.print_timer_stats()
            tick_total = self.timer.total_times.get("TickService.tick", {}).get("total", 0)
            self.logger.debug(f"Tick #{self.tick_count} finished in {self.format_time(tick_total)}")

            # Per-run timings key on run_index; without a reset the runtimes dicts
            # grow unbounded on long-running services.
            self.timer.reset_timers()
            self.timer.run_index += 1

            # Sleep
            if not self.shutdown_event.is_set():
                await self.loop_wait()

        self.logger.debug("Process loop is stopped")

    async def stop_loop(self) -> None:
        self.logger.debug("Stopping process loop")
        asyncio.run_coroutine_threadsafe(self.shutdown(SIGTERM), self.current_loop)

    ####################
    ### Tick methods ###
    ####################

    async def tick(self) -> None:
        self.tick_count += 1
        self.tick_time += self.tick_interval

        # Backoff mode
        if self.back_off_mode:
            self.back_off_count += 1
            if self.back_off_callback is not None:
                if inspect.iscoroutinefunction(self.back_off_callback):
                    await self.back_off_callback(self.back_off_count, self.tick_time)
                else:
                    self.back_off_callback(self.back_off_count, self.tick_time)

        # Regular tick
        else:
            for callback in self.callbacks:
                try:
                    if inspect.iscoroutinefunction(callback):
                        await callback(self.tick_count, self.tick_time)
                    else:
                        callback(self.tick_count, self.tick_time)
                except asyncio.CancelledError:
                    self.logger.debug("Tick callback cancelled")
                    raise

                if await self.need_to_shutdown():
                    break

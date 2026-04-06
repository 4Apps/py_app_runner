import logging
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import Any, TypedDict


class TimerProperties(TypedDict):
    level: int
    total: float
    runtimes: dict[int, float]


class Timer:
    name: str
    logger: logging.Logger

    start_times: dict[str, float]
    total_times: dict[str, TimerProperties]

    timers: list[tuple[int, str]]
    level: int
    run_index: int

    def __init__(self, name: str):
        self.name = name
        self.logger = logging.getLogger(f"{self.__class__.__name__}")

        self.start_times = {}
        self.total_times = {}
        self.timers = []
        self.level = 0
        self.run_index = 0

    # * Class generator
    @contextmanager
    def enter(self, name: str) -> Generator["Timer", None, None]:
        self.level += 1
        self.start(name=name)

        try:
            yield self
        finally:
            self.stop(name=name)
            self.level -= 1

    @asynccontextmanager
    async def aenter(self, name: str) -> AsyncGenerator["Timer", None]:
        self.level += 1
        self.start(name=name)

        try:
            yield self
        finally:
            self.stop(name=name)
            self.level -= 1

    # * Class context manager
    def __enter__(self) -> "Timer":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.stop()

    async def __aenter__(self) -> "Timer":
        self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.stop()

    # * Start / stop
    def start(self, name: str | None = None, skip_append: bool = False) -> None:
        if name is None:
            name = self.name

        if len(self.timers) > 0:
            (
                level,
                prev_timer_name,
            ) = self.timers[-1]
            if prev_timer_name != name and level != self.level:
                self.stop(name=prev_timer_name, skip_remove=True)

        self.start_times[name] = time.perf_counter()

        if not skip_append:
            self.timers.append(
                (
                    self.level,
                    name,
                )
            )

    def stop(
        self,
        name: str | None = None,
        skip_remove: bool = False,
    ) -> None:
        if name is None:
            name = self.name
        end_time = time.perf_counter()

        if name not in self.start_times:
            self.logger.warning(f"Timer {name} stopped before it was started")
            return None

        elapsed = end_time - self.start_times[name]
        if name not in self.total_times:
            self.total_times[name] = TimerProperties(level=self.level, total=0, runtimes={})

        self.total_times[name]["runtimes"][self.run_index] = elapsed
        self.total_times[name]["total"] += elapsed

        if not skip_remove:
            # Remove current timer
            self.timers.pop()

            # Continue previous timer
            if len(self.timers) > 0:
                (
                    level,
                    prev_timer_name,
                ) = self.timers[-1]
                if prev_timer_name != name and level != self.level:
                    self.start(name=prev_timer_name, skip_append=True)

    # * Stats
    def reset_timers(self) -> None:
        """Resets the current timer. Call this at the start of an iteration."""
        self.start_times = {}
        self.total_times = {}

    # * Output
    def print_timer_stats_table(self, return_data: bool = False) -> str | None:
        """Prints the current timing information into a table."""
        timer_stats = "\n\n"

        all_fn_names = [k for k in self.total_times.keys()]
        if len(all_fn_names) == 0:
            return None

        # Max width of level column
        max_level_width = max([self.total_times[k].get("level", 0) for k in all_fn_names])
        if max_level_width % 2 == 1:
            max_level_width += 1
        max_level_width = max(max_level_width, 6)

        # Max width of name column
        max_name_width = max([len(k) for k in all_fn_names] + [4])
        if max_name_width % 2 == 1:
            max_name_width += 1

        # Format string
        format_str = (
            f"{{:>{max_level_width}}} | {{:>{max_name_width}}} | "
            f"{{:>10.4f}} | {{:>10.4f}} | {{:>10.4f}} | {{:>10.4f}} | {{:>5}}"
        )

        header = (
            f"{{:>{max_level_width}}} | {{:>{max_name_width}}} | {{:^10}} | {{:^10}} | {{:^10}} | {{:^10}} | {{:^5}}"
        ).format("Level", "Name", "Total (ms)", "Min (ms)", "Max (ms)", "Avg (ms)", "Count")
        timer_stats += f"{header}\n"

        sep_idx = header.find("|")
        sep_text = ("-" * sep_idx) + "+" + ("-" * (len(header) - sep_idx - 1))
        timer_stats += f"{sep_text}\n"

        for name in all_fn_names:
            runtimes = self.total_times[name].get("runtimes", {}).values()
            total_time = self.total_times[name].get("total", 0) * 1000
            min_time = min(runtimes) * 1000 if runtimes else 0
            max_time = max(runtimes) * 1000 if runtimes else 0
            avg_time = (sum(runtimes) / len(runtimes)) * 1000 if runtimes else 0
            run_count = len(runtimes)

            timer_stats += format_str.format(
                self.total_times[name].get("level"),
                name,
                total_time,
                min_time,
                max_time,
                avg_time,
                run_count,
            )
            timer_stats += "\n"

        totals_format_str = (
            f"{{:>{max_level_width}}} | {{:>{max_name_width}}} | "
            f"{{:>10.4f}} | {{:>10.4}} | {{:>10.4}} | {{:>10.4}} | {{:>10.4}}"
        )
        timer_stats += f"{sep_text}\n"
        timer_stats += totals_format_str.format(
            "",
            "Total",
            self.total_time(self.total_times) * 1000,
            "",
            "",
            "",
            "",
        )
        timer_stats += "\n\n"

        if return_data:
            return timer_stats

        self.logger.debug(timer_stats)
        return None

    def print_timer_stats_json(self, return_data: bool = False) -> str | None:
        """Prints the current timing information into a JSON string."""
        pass

    def print_timer_stats_csv(self, return_data: bool = False) -> str | None:
        """Prints the current timing information into a CSV string."""
        all_fn_names = list(self.total_times.keys())
        if not all_fn_names:
            return None

        # Prepare header
        header = all_fn_names + ["Total"]

        # Get all runtime indices and find the maximum index
        all_indices: set[int] = set()
        for name in all_fn_names:
            all_indices.update(self.total_times[name].get("runtimes", {}).keys())
        max_index = max(all_indices) if all_indices else 0

        # Prepare data rows
        data_rows: list[list[str]] = []
        for i in range(max_index + 1):
            row: list[str] = []
            row_total = 0.0
            for name in all_fn_names:
                runtime = self.total_times[name].get("runtimes", {}).get(i)
                if runtime is not None:
                    value = runtime * 1000
                    row.append(f"{value:.4f}")
                    row_total += value
                else:
                    row.append("")
            row.append(f"{row_total:.4f}")
            data_rows.append(row)

        # Prepare total row
        total_row: list[str] = []
        grand_total = 0.0
        for name in all_fn_names:
            total = sum(self.total_times[name].get("runtimes", {}).values()) * 1000
            total_row.append(f"{total:.4f}")
            grand_total += total
        total_row.append(f"{grand_total:.4f}")

        # Combine all rows
        all_rows = [header] + data_rows + [total_row]

        # Convert to CSV string
        output = "\n".join([",".join(str(cell) for cell in row) for row in all_rows])

        if return_data:
            return output

        self.logger.info(f"\n{output}")
        return None

    def print_timer_stats(
        self,
        output_format: str = "table",
        return_data: bool = False,
    ) -> str | None:
        """
        Prints the current timing information into a table.

        output_format: str - choices: ["table", "json", "csv"]
        """

        if output_format == "table":
            return self.print_timer_stats_table(return_data=return_data)
        elif output_format == "json":
            return self.print_timer_stats_json(return_data=return_data)
        elif output_format == "csv":
            return self.print_timer_stats_csv(return_data=return_data)

        return None

    def total_time(self, total_times: dict[str, TimerProperties]) -> float:
        """Returns the total amount accumulated across all functions in seconds."""
        return sum([timer.get("total", 0) for _name, timer in total_times.items()])

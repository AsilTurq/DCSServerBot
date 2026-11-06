from __future__ import annotations

import asyncio
import logging
import os
import re

from core import Service, ServiceRegistry, Status, NodeImpl, get_translation
from datetime import datetime
from logging.handlers import QueueHandler, RotatingFileHandler
from queue import Queue
from rich import box
from rich.console import Console, ConsoleOptions, RenderResult, ConsoleRenderable, Group
from rich.layout import Layout
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from typing import TYPE_CHECKING

from ..servicebus import ServiceBus

if TYPE_CHECKING:
    from core import Node

__all__ = [
    "Dashboard"
]

_ = get_translation("dashboard")

WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

STATUS_STYLES = {
    "RUNNING": ("🟢", "bold bright_green", "Running"),
    "LOADING": ("🟡", "bold bright_yellow", "Loading"),
    "PAUSED": ("🟡", "bold gold1", "Paused"),
    "STOPPED": ("🟠", "bold orange3", "Stopped"),
    "SHUTDOWN": ("🔴", "bold bright_red", "Shutdown"),
    "SHUTTING_DOWN": ("🟡", "bold yellow", "Shutting Down"),
    "UNREGISTERED": ("⚪", "dim grey62", "Unregistered"),
}

TRANSLATABLE_LOG_PHRASES = [
    "DCSServerBot MASTER starting up ...",
    "DCSServerBot AGENT starting up ...",
    "DCSServerBot MASTER started.",
    "DCSServerBot stopped.",
    "Connection to PostgreSQL",
    "established.",
    "Loading Plugins ...",
    "Plugins loaded.",
    "Unloading Plugins ...",
    "Plugins unloaded.",
    "Starting Services ...",
    "Services started.",
    "Services stopped.",
    "Starting Service ",
    "Service Dashboard stopped.",
    "Service Cleanup stopped.",
    "Service WebService stopped.",
    "Service Bot stopped.",
    "Service ServiceBus stopped.",
    "Service Monitoring stopped.",
    "Preparing Discord Bot",
    "Registering Discord Commands (this might take a bit) ...",
    "Discord Commands registered.",
    "Discord Bot started, accepting commands.",
    "Searching for local DCS servers (this might take a bit) ...",
    "local DCS servers registered.",
    "Local DCS-Server",
    "registered as UP.",
    "registered as DOWN (no process).",
    "DCS server starting up with PID",
    "starting up ...",
    "DCS server",
    "started by Scheduler.",
    "LogAnalyser launched for",
    "LogAnalyser shut down for",
    "SRS autoconnect is enabled for this server.",
    "SRS launched for",
    "SRS shut down for",
    "Could not find a running SRS server process.",
    "Please move your SRS configuration to",
    "Aborting the main loop ...",
    "- Stopping Services...",
    "NOT launched for",
    "launched for",
    "shut down for",
    "Preset image answered 404:",
    "is not in this server's DCS installation",
    "looked for",
    "Web UI: http://localhost:9876",
    "WebService: render warm-up done",
    "schemas parsed,",
    "notices checked,",
    "templates compiled in",
    "signed in with the local backend",
    "signed in via the local backend",
    "signed out",
    "refusing a local login for",
    "bad password",
    "login refused for",
    "- Master node.",
]


def _format_status(status_name: str) -> str:
    if status_name in STATUS_STYLES:
        icon, style, label = STATUS_STYLES[status_name]
        return f"{icon} [{style}]{_(label)}[/]"
    return f"⚪ {_(status_name.title())}"


def _clean_and_translate_log(msg: str) -> str:
    msg = re.sub(r"^\s*(?:=>|->|-)\s*", "", msg.strip())
    for emoji in ("🟢", "🟡", "🔴", "🟠", "⚪"):
        msg = msg.replace(emoji, "")
    msg = re.sub(r"\s{2,}", " ", msg).strip()
    for phrase in TRANSLATABLE_LOG_PHRASES:
        if phrase in msg:
            msg = msg.replace(phrase, _(phrase))
    msg = re.sub(r"^\s*(?:=>|->|-)\s*", "", msg.strip())
    return msg


def _log_badge_and_module(record: logging.LogRecord, raw_msg: str, clean_msg: str) -> tuple[str, str, str]:
    """Return (status_badge, module_label, styled_message) for symmetrical 4-column log rows."""
    fn = (record.filename or "").lower()
    low = raw_msg.lower()

    if "webservice" in fn or "local.py" in fn or "routes.py" in fn or "web " in low or "signed in" in low or "signed out" in low:
        mod = _("WEB UI")
    elif "dcsserverbot" in fn or "discord" in low:
        mod = _("DISCORD")
    elif "commands.py" in fn or "scheduler" in fn:
        mod = _("SCHEDULER")
    elif "srs" in low or "srs" in fn:
        mod = _("SRS RADIO")
    elif "extension" in fn or "loganalyser" in low:
        mod = _("EXTENSION")
    elif "server" in fn or "service.py" in fn:
        mod = _("SERVER")
    else:
        mod = _("SYSTEM")

    if record.levelno >= logging.ERROR:
        badge = f"🔴 [bold bright_red]{_('ERROR'):<6}[/]"
        msg_styled = f"[bright_red]{clean_msg}[/]"
    elif record.levelno >= logging.WARNING:
        badge = f"🟡 [bold bright_yellow]{_('WARN'):<6}[/]"
        msg_styled = f"[bright_yellow]{clean_msg}[/]"
    elif "registered as down" in low or "shut down" in low or "stopped" in low:
        badge = f"🔴 [bold bright_red]{_('DOWN'):<6}[/]"
        msg_styled = f"[white]{clean_msg}[/]"
    elif "starting up" in low or "searching for" in low or "registering" in low or "preparing" in low or "loading" in low:
        badge = f"🟡 [bold gold1]{_('INIT'):<6}[/]"
        msg_styled = f"[bright_white]{clean_msg}[/]"
    elif "registered as up" in low or "started" in low or "signed in" in low or "loaded" in low:
        badge = f"🟢 [bold bright_green]{_('ACTIVE'):<6}[/]"
        msg_styled = f"[bold bright_white]{clean_msg}[/]"
    else:
        badge = f"🟢 [bold bright_cyan]{_('INFO'):<6}[/]"
        msg_styled = f"[white]{clean_msg}[/]"

    msg_styled = re.sub(r'"([^"]+)"', r'[bold gold1]"\1"[/]', msg_styled)
    return badge, mod, msg_styled


class HeaderWidget:
    """Display symmetrical single-line header with clock."""

    def __init__(self, service: Dashboard):
        self.service = service
        self.node = service.node
        self.log = service.log

    def __rich__(self) -> Panel:
        config = self.service.get_config().get('header', {})
        grid = Table.grid(expand=True)
        grid.add_column(justify="left", width=34, no_wrap=True)
        grid.add_column(justify="center", ratio=1, no_wrap=True)
        grid.add_column(justify="right", width=30, no_wrap=True)

        prefix = ""
        if self.service.is_multinode():
            prefix = f"{_('Cluster Master')} | " if self.node.master else f"{_('Cluster Agent')} | "
        left_msg = f"🟢 [bold bright_white]{prefix}DCSServerBot v{self.node.bot_version}.{self.node.sub_version}[/]"

        center_msg = f"[bold bright_cyan]{_('Web')}:[/] [underline bright_white]localhost:9876[/]"
        if self.node.dcs_version:
            center_msg = f"[bold bright_cyan]DCS:[/] [bright_white]{self.node.dcs_version}[/]  [dim cyan]│[/]  " + center_msg

        now = datetime.now()
        time_str = now.strftime("%H:%M:%S").replace(":", "[blink]:[/]")
        day_name = _(WEEKDAYS[now.weekday()])
        right_msg = f"[bold bright_white]{now.strftime('%d.%m.%Y')} {day_name} • {time_str}[/]"

        grid.add_row(left_msg, center_msg, right_msg)
        return Panel(
            grid,
            style=config.get("background", "white on deep_sky_blue4"),
            border_style=config.get("border", "bright_cyan"),
            box=box.ROUNDED,
            padding=(0, 1),
        )


class ServersWidget:
    """Displaying Symmetrical List of Servers"""

    def __init__(self, service: Dashboard):
        self.service = service
        self.node = service.node
        self.bus = service.bus

    def __rich__(self) -> Panel:
        config = self.service.get_config().get("servers", {})
        table = Table(
            expand=True,
            show_edge=False,
            pad_edge=False,
            header_style="bold gold1",
            border_style="bright_blue",
        )
        table.add_column(_("Status"), justify="left", width=14, no_wrap=True)
        table.add_column(_("Server Name"), justify="left", width=29, no_wrap=True)
        table.add_column(_("Mission Name"), justify="left", ratio=1, no_wrap=True)
        table.add_column(_("Players"), justify="center", width=10, no_wrap=True)
        table.add_column(_("Port / SRS"), justify="center", width=18, no_wrap=True)
        if self.service.node.master and self.service.is_multinode():
            table.add_column(_("Node"), justify="center", width=12, no_wrap=True)

        for server_name, server in self.bus.servers.items():
            if (not self.node.master or config.get('hide_remote_servers', False)) and server.is_remote:
                continue
            name = re.sub(self.bus.filter['server_name'], '', server.name).strip()
            status_label = _format_status(server.status.name)

            if server.current_mission:
                mission_raw = re.sub(self.bus.filter['mission_name'], '', server.current_mission.name).strip()
                mission_name = f"[bold bright_white]{mission_raw}[/]"
            else:
                _m = server.settings.get('lastSelectedMission') or (
                    server.settings.get('missionList', [''])[0] if server.settings.get('missionList') else ''
                )
                mission_raw = os.path.splitext(os.path.basename(_m))[0] if _m else '—'
                mission_name = f"[dim white]{mission_raw}[/]"

            if server.status in (Status.RUNNING, Status.PAUSED, Status.LOADING):
                n = server.num_players if server.num_players is not None else len(server.get_active_players())
                max_p = max(int(server.settings.get('maxPlayers', 60)) - 1, 1) if int(server.settings.get('maxPlayers', 60)) == 61 else int(server.settings.get('maxPlayers', 60))
                if n > 0:
                    num_players = f"[bold bright_green]{n:2d} / {max_p}[/]"
                else:
                    num_players = f"[bright_white]{n:2d} / {max_p}[/]"
            else:
                num_players = "[dim]  —   [/]"

            dcs_port = getattr(getattr(server.instance, 'dcs_port', None), 'port', None) or server.instance.locals.get('dcs_port', '-')
            srs_port = server.instance.locals.get('extensions', {}).get('SRS', {}).get('port', '-')
            ports_str = f"[bright_cyan]{dcs_port}[/] [dim]│[/] [gold1]SRS:{srs_port}[/]"

            name_styled = f"[bold bright_white]{name}[/]" if server.status == Status.RUNNING else f"[white]{name}[/]"

            if self.service.node.master and self.service.is_multinode():
                table.add_row(status_label, name_styled, mission_name, num_players, ports_str, server.node.name)
            else:
                table.add_row(status_label, name_styled, mission_name, num_players, ports_str)

        panel_title = f"[bold bright_cyan] 🖥️  {_('DCS SERVER STATUS')} [/]"
        panel_subtitle = f"[dim white]🟢 {_('Running')}   🟡 {_('Loading')}   🔴 {_('Shutdown')}[/]"
        return Panel(
            table,
            title=panel_title,
            title_align="left",
            subtitle=panel_subtitle,
            subtitle_align="right",
            padding=(0, 1),
            style=config.get("background", "white on navy_blue"),
            border_style=config.get("border", "bright_cyan"),
            box=box.ROUNDED,
        )


class NodeWidget:
    """Displaying Bot Info"""

    def __init__(self, service: Dashboard):
        self.service = service
        self.node = service.node
        self.bus = service.bus
        self.pool = service.pool
        self.log = service.log

    def __rich__(self) -> Panel:
        config = self.service.get_config().get("nodes", {})
        table = Table(expand=True, show_edge=False)
        table.add_column(_("Node ([green]Master[/])"), justify="left")
        table.add_column(_("Servers"), justify="center")
        nodes: dict[str, Node | None] = {name: None for name in self.node.all_nodes.keys()}
        servers: dict[str, int] = dict()
        for server in self.bus.servers.values():
            nodes[server.node.name] = server.node
            if server.node.name not in servers:
                servers[server.node.name] = 0
            if server.status not in [Status.SHUTDOWN, Status.UNREGISTERED]:
                servers[server.node.name] += 1
        for name, node in nodes.items():  # type: Node
            if not node:
                table.add_row(f"🔴 [grey54]{name}[/]", f"[grey54]{_('inactive')}[/]")
            elif node.master:
                table.add_row(f"🟢 [green]{node.name}[/]", f"{servers[node.name]}/{len(node.instances)}")
            else:
                table.add_row(f"🟢 {node.name}", f"{servers[node.name]}/{len(node.instances)}")
        return Panel(
            table,
            title=f"[bold bright_cyan] {_('Nodes')} [/]",
            padding=1,
            style=config.get("background", "white on navy_blue"),
            border_style=config.get("border", "bright_cyan"),
            box=box.ROUNDED,
        )


class RichQueueHandler(QueueHandler):
    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        record = logging.makeLogRecord(record.__dict__.copy())
        return record


class LogWidget:
    """Display symmetrical 4-column log table"""

    def __init__(self, service: Dashboard):
        self.service = service
        self.queue = service.queue
        self.buffer: list[tuple[int, ConsoleRenderable]] = []
        self.handler = service.old_handler
        self.console = Console(record=True)
        config = self.service.get_config().get("log", {})
        self.panel = Panel(
            "",
            title=f"[bold bright_cyan] 📋  {_('LIVE SYSTEM LOG')} [/]",
            title_align="left",
            subtitle=f"[dim white]{_('Time   │   Status   │   Module   │   Event Detail')}[/]",
            subtitle_align="right",
            padding=(0, 1),
            style=config.get("background", "white on grey11"),
            border_style=config.get("border", "bright_cyan"),
            box=box.ROUNDED,
            expand=True,
        )
        self.previous_size = self.console.size
        self.cwd = os.getcwd()

    def _measure_renderable_lines(self, renderable: ConsoleRenderable, width: int) -> int:
        with self.console.capture() as capture:
            self.console.print(renderable, width=width)
        return len(capture.get().splitlines()) or 1

    def _check_size_change(self) -> bool:
        current_size = self.console.size
        if current_size != self.previous_size:
            self.previous_size = current_size
            return True
        return False

    def _emit(self, record: logging.LogRecord) -> ConsoleRenderable | None:
        raw_msg = record.getMessage()
        if any(s in raw_msg for s in (
            "signed in via the local backend",
            "Registrar: '",
            "Admin web shell installed:",
            "Admin web UI auth:",
            "PluginManager:",
            "Python version ",
            "Web UI session: session.https_only",
        )):
            return None

        clean_msg = _clean_and_translate_log(raw_msg)
        badge, mod, msg_styled = _log_badge_and_module(record, raw_msg, clean_msg)
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")

        row = Table.grid(expand=True)
        row.add_column(width=8, no_wrap=True)
        row.add_column(width=3, justify="center", no_wrap=True)
        row.add_column(width=9, no_wrap=True)
        row.add_column(width=3, justify="center", no_wrap=True)
        row.add_column(width=11, no_wrap=True)
        row.add_column(width=3, justify="center", no_wrap=True)
        row.add_column(ratio=1, no_wrap=True)

        row.add_row(
            f"[dim bright_cyan]{ts}[/]",
            "[dim blue]│[/]",
            badge,
            "[dim blue]│[/]",
            f"[bold cyan]{mod:<11}[/]",
            "[dim blue]│[/]",
            msg_styled,
        )
        return row

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        try:
            if not self.queue.empty() or self._check_size_change():
                config = self.service.get_config()
                max_displayable_height = max(1, options.max_height - 2)

                while not self.queue.empty():
                    record: logging.LogRecord = self.queue.get_nowait()
                    try:
                        log_renderable = self._emit(record)
                        if log_renderable is None:
                            continue
                        self.buffer.append((1, log_renderable))
                    except Exception as ex:
                        fallback = self.console.render_str(f"[red]Log render failed[/red]: {ex!r}")
                        self.buffer.append((1, fallback))

                total_height_used = sum(lines for lines, _r in self.buffer)
                while total_height_used > max_displayable_height and self.buffer:
                    removed_lines, _r = self.buffer.pop(0)
                    total_height_used -= removed_lines

                content = [renderable for _lines, renderable in self.buffer]
                content.extend(self.console.render_str("") for _i in range(max_displayable_height - total_height_used))

                self.panel = Panel(
                    Group(*content),
                    title=f"[bold bright_cyan] 📋  {_('LIVE SYSTEM LOG')} [/]",
                    title_align="left",
                    subtitle=f"[dim white]{_('Time   │   Status   │   Module   │   Event Detail')}[/]",
                    subtitle_align="right",
                    padding=(0, 1),
                    style=config.get("log", {}).get("background", "white on grey11"),
                    border_style=config.get("border", "bright_cyan"),
                    box=box.ROUNDED,
                    expand=True,
                )

            yield self.panel
        except Exception as ex:
            yield Panel(
                f"[red]Dashboard log widget error:[/red] {ex!r}",
                style="white on dark_red",
                border_style="red",
                expand=True,
            )


@ServiceRegistry.register(depends_on=[ServiceBus])
class Dashboard(Service):

    def __init__(self, node: NodeImpl):
        super().__init__(node)
        self.console = Console()
        self.layout = None
        self.bus = None
        self.queue = None
        self.log_handler = None
        self.old_handler = None
        self.update_task = None
        self.header_widget = None
        self.servers_widget = None
        self.log_widget = None
        self.stop_event = asyncio.Event()

    def is_multinode(self):
        return len(self.node.all_nodes) > 1

    def create_widgets(self):
        self.header_widget = HeaderWidget(self)
        self.servers_widget = ServersWidget(self)
        self.log_widget = LogWidget(self)

    def create_layout(self):
        layout = Layout()
        layout.split(
            Layout(self.header_widget, name="header", size=3),
            Layout(self.servers_widget, name="main", size=len(self.bus.servers) + 5),
            Layout(self.log_widget, name="log", ratio=2, minimum_size=5)
        )
        if self.node.master and self.is_multinode():
            servers = ServersWidget(self)
            nodes = NodeWidget(self)
            layout['main'].split_row(Layout(servers, name="servers", ratio=2), Layout(nodes, name="nodes"))
        return layout

    def hook_logging(self):
        self.queue = Queue()
        self.log_handler = RichQueueHandler(self.queue)
        self.log_handler.setLevel(logging.INFO)
        for handler in self.log.root.handlers:
            if isinstance(handler, RichHandler) and not isinstance(handler, RotatingFileHandler):
                self.old_handler = handler
                for rec in getattr(handler, "early_records", ()):
                    self.queue.put_nowait(rec)
                if hasattr(handler, "early_records"):
                    handler.early_records.clear()
                self.log_handler.setFormatter(handler.formatter)
                self.log.root.removeHandler(handler)
                self.log.root.addHandler(self.log_handler)

    def unhook_logging(self):
        self.log.root.removeHandler(self.log_handler)
        self.log.root.addHandler(self.old_handler)

    async def start(self):
        if not self.node.config.get('use_dashboard', True):
            return
        await super().start()
        self.bus = ServiceRegistry.get(ServiceBus)
        self.hook_logging()
        self.create_widgets()
        self.layout = self.create_layout()
        self.stop_event.clear()
        self.update_task = asyncio.create_task(self.update())
        await asyncio.sleep(0.05)

    async def stop(self):
        if not self.node.config.get('use_dashboard', True):
            return
        self.stop_event.set()
        if self.update_task and self.update_task is not asyncio.current_task():
            await self.update_task
        self.unhook_logging()
        self.console.clear()
        await super().stop()

    async def switch(self, master: bool):
        await super().switch(master)
        await self.stop()
        await self.start()

    async def update(self):
        try:
            previous_server_count = len(self.bus.servers)
            with Live(self.layout, console=self.console, refresh_per_second=2, screen=True) as live:
                while not self.stop_event.is_set():
                    current_server_count = len(self.bus.servers)
                    if current_server_count != previous_server_count:
                        self.layout = self.create_layout()
                        live.update(self.layout)
                        previous_server_count = current_server_count
                    await asyncio.sleep(0.5)
        except Exception:
            self.log.exception("Dashboard update loop crashed")
            self.stop_event.set()

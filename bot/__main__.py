# ruff: noqa: E402

from .core.config_manager import BinConfig, Config

Config.load()

from datetime import datetime
from logging import Formatter
from time import localtime

from pytz import timezone

from . import LOGGER, bot_loop
from .core.tg_client import TgClient


def _prepare_optional_services():
    """Start optional qBittorrent only after persisted config is loaded."""
    from os import environ, getcwd
    from shutil import which
    from subprocess import DEVNULL, run as srun

    if Config.DISABLE_TORRENTS or Config.DISABLE_QBITTORRENT:
        environ["DISABLE_QBITTORRENT"] = "true"
        LOGGER.info("qBittorrent daemon startup skipped by configuration.")
    else:
        qbit_binary = BinConfig.QBIT_NAME
        if not which(qbit_binary):
            environ["DISABLE_QBITTORRENT"] = "true"
            LOGGER.warning(
                f"qBittorrent binary '{qbit_binary}' was not found. "
                "qBittorrent-specific tasks are disabled; Aria2 torrent support remains enabled. "
                "Install the binary or set QBITTORRENT_BIN to its executable name/path to enable qBittorrent."
            )
        else:
            try:
                qbit_result = srun(
                    [qbit_binary, "-d", f"--profile={getcwd()}"],
                    check=False,
                    stdout=DEVNULL,
                    stderr=DEVNULL,
                    timeout=15,
                )
                if qbit_result.returncode != 0:
                    environ["DISABLE_QBITTORRENT"] = "true"
                    LOGGER.warning(
                        f"qBittorrent daemon exited with code {qbit_result.returncode}; "
                        "qBittorrent-specific tasks are disabled. Aria2 torrent support remains enabled."
                    )
                else:
                    LOGGER.info("qBittorrent daemon started successfully.")
            except Exception as exc:
                environ["DISABLE_QBITTORRENT"] = "true"
                LOGGER.warning(
                    f"Unable to start qBittorrent daemon ({type(exc).__name__}); "
                    "qBittorrent-specific tasks are disabled. Aria2 torrent support remains enabled."
                )

    # The public FastAPI process is a child process. It must receive the
    # effective Config/MongoDB values through the inherited environment.
    for key in (
        "MONITOR_API_ENABLED",
        "MONITOR_API_TOKEN",
        "MONITOR_INTERNAL_HOST",
        "MONITOR_INTERNAL_PORT",
        "MONITOR_SNAPSHOT_MAX_AGE",
        "MONITOR_HISTORY_ENABLED",
        "MONITOR_HISTORY_RETENTION_DAYS",
    ):
        value = getattr(Config, key, None)
        if value is not None:
            environ[key] = str(value)

    if Config.MONITOR_API_ENABLED:
        if len(str(Config.MONITOR_API_TOKEN or "").strip()) < 32:
            LOGGER.error(
                "MONITOR_API_ENABLED is true but MONITOR_API_TOKEN is missing/too short; "
                "the monitoring API will not start."
            )
        elif not str(Config.MONITOR_INTERNAL_SECRET or "").strip():
            from secrets import token_urlsafe

            Config.MONITOR_INTERNAL_SECRET = token_urlsafe(48)
            LOGGER.info("Generated a separate ephemeral secret for the private monitoring bridge.")
        if Config.MONITOR_INTERNAL_SECRET:
            environ["MONITOR_INTERNAL_SECRET"] = str(Config.MONITOR_INTERNAL_SECRET)


async def main():
    from asyncio import gather

    from .core.startup import (
        load_configurations,
        load_settings,
        save_settings,
        update_aria2_options,
        update_nzb_options,
        update_qb_options,
        update_variables,
    )

    await load_settings()
    _prepare_optional_services()

    try:
        tz = timezone(Config.TIMEZONE)
    except Exception:
        from pytz import utc

        tz = utc

    def changetz(*args):
        try:
            return datetime.now(tz).timetuple()
        except Exception:
            return localtime()

    Formatter.converter = changetz

    await gather(
        TgClient.start_bot(), TgClient.start_user(), TgClient.start_helper_bots()
    )
    await gather(load_configurations(), update_variables())

    from .core.torrent_manager import TorrentManager

    await TorrentManager.initiate()
    await gather(
        update_qb_options(),
        update_aria2_options(),
        update_nzb_options(),
    )
    from .core.jdownloader_booter import jdownloader
    from .helper.ext_utils.files_utils import clean_all
    from .helper.ext_utils.telegraph_helper import telegraph
    from .helper.mirror_leech_utils.rclone_utils.serve import rclone_serve_booter
    from .modules import (
        get_packages_version,
        initiate_search_tools,
    )

    await gather(
        save_settings(),
        jdownloader.boot(),
        clean_all(),
        initiate_search_tools(),
        get_packages_version(),
        telegraph.create_account(),
        rclone_serve_booter(),
    )


bot_loop.run_until_complete(main())

from .core.handlers import add_handlers
from .helper.ext_utils.bot_utils import create_help_buttons
from .helper.listeners.aria2_listener import add_aria2_callbacks

add_aria2_callbacks()
create_help_buttons()
add_handlers()

if Config.MONITOR_API_ENABLED:
    async def _start_monitoring_api():
        from .monitoring.config import settings
        from .monitoring.internal_server import private_server
        from .monitoring.service import start_monitoring, stop_monitoring

        monitor_settings = settings()
        if len(monitor_settings["public_token"]) < 32:
            LOGGER.error(
                "Monitoring API is enabled but MONITOR_API_TOKEN must be at least 32 characters. API was not started."
            )
            return
        if monitor_settings["internal_host"] not in ("127.0.0.1", "localhost", "::1"):
            LOGGER.error("Monitoring bridge must bind to loopback only. API was not started.")
            return
        # The public token is shared with Graberr. The private bridge secret
        # was configured/generated before the public Gunicorn child started.
        if len(monitor_settings["internal_token"]) < 32:
            LOGGER.error("Monitoring private bridge secret must be at least 32 characters. API was not started.")
            return
        try:
            await start_monitoring(monitor_settings)
            await private_server.start(
                monitor_settings["internal_host"], monitor_settings["internal_port"]
            )
            LOGGER.info("WZML read-only Monitoring API private bridge started.")
        except Exception:
            await stop_monitoring()
            LOGGER.exception("Failed to start WZML Monitoring API")

    bot_loop.create_task(_start_monitoring_api(), name="wzml-monitor-startup")

from .modules import restart_notification

bot_loop.create_task(restart_notification())

if not Config.DISABLE_PLUGINS:
    from .core.plugin_manager import get_plugin_manager
    from .modules.plugin_manager import register_plugin_commands

    plugin_manager = get_plugin_manager()
    plugin_manager.bot = TgClient.bot
    register_plugin_commands()
    bot_loop.create_task(plugin_manager.load_saved_plugins())

from pyrogram.filters import regex
from pyrogram.handlers import CallbackQueryHandler

from .core.handlers import add_handlers
from .helper.ext_utils.bot_utils import new_task
from .helper.telegram_helper.filters import CustomFilters
from .helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)


@new_task
async def restart_sessions_confirm(_, query):
    data = query.data.split()
    message = query.message
    if data[1] == "confirm":
        reply_to = message.reply_to_message
        restart_message = await send_message(reply_to, "Restarting Session(s)...")
        await delete_message(message)
        await TgClient.reload()
        add_handlers()
        TgClient.bot.add_handler(
            CallbackQueryHandler(
                restart_sessions_confirm,
                filters=regex("^sessionrestart") & CustomFilters.sudo,
            )
        )
        await edit_message(restart_message, "Session(s) Restarted Successfully!")
    else:
        await delete_message(message)


TgClient.bot.add_handler(
    CallbackQueryHandler(
        restart_sessions_confirm,
        filters=regex("^sessionrestart") & CustomFilters.sudo,
    )
)

LOGGER.info("WZ Client(s) & Services Started !")
bot_loop.run_forever()

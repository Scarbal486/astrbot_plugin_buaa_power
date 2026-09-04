"""AstrBot plugin for monitoring BUAA dormitory electricity balances."""

from __future__ import annotations

import html as html_module
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import error_response, json_response, request

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "campus": "",
    "building": "",
    "floor": "",
    "room": "",
    "air_meter_id": "",
    "lighting_meter_id": "",
    "air_threshold": 5.0,
    "lighting_threshold": 10.0,
    "check_time": "08:00",
    "notify_qq": "",
}


def parse_schedule_time(value: str) -> tuple[int, int]:
    """Parse a daily 24-hour schedule value.

    Args:
        value: Time text in ``HH:MM`` format.

    Returns:
        Hour and minute as integers.

    Raises:
        ValueError: If the value is not a valid 24-hour time.
    """
    match = re.fullmatch(r"(\d{2}):(\d{2})", str(value).strip())
    if not match:
        raise ValueError("检查时间必须使用 HH:MM 格式")
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        raise ValueError("检查时间必须是有效的 24 小时时间")
    return hour, minute


def normalize_config(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge and validate plugin configuration values.

    Args:
        values: Partial configuration mapping.

    Returns:
        A normalized complete configuration dictionary.

    Raises:
        ValueError: If a schedule, threshold, meter ID, or QQ value is invalid.
    """
    config = {**DEFAULT_CONFIG, **dict(values or {})}
    parse_schedule_time(str(config["check_time"]))

    for key, label in (
        ("air_threshold", "空调阈值"),
        ("lighting_threshold", "照明阈值"),
    ):
        try:
            threshold = float(config[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须是非负数字") from exc
        if not math.isfinite(threshold) or threshold < 0:
            raise ValueError(f"{label}必须是非负数字")
        config[key] = threshold

    raw_enabled = config["enabled"]
    if isinstance(raw_enabled, str):
        config["enabled"] = raw_enabled.strip().lower() in {"1", "true", "yes", "on"}
    else:
        config["enabled"] = bool(raw_enabled)
    for key in (
        "campus",
        "building",
        "floor",
        "room",
        "air_meter_id",
        "lighting_meter_id",
        "notify_qq",
    ):
        config[key] = str(config.get(key) or "").strip()
    if config["notify_qq"] and not re.fullmatch(r"[0-9]+", config["notify_qq"]):
        raise ValueError("通知 QQ 号必须是纯数字")
    config["check_time"] = str(config["check_time"]).strip()
    return config


def parse_meter_detail(page_html: str) -> dict[str, Any]:
    """Parse balance metadata from an electricity meter detail page.

    Args:
        page_html: Detail page HTML containing text or SVG labels.

    Returns:
        Parsed balance, power, address, and reading time.

    Raises:
        ValueError: If no balance value can be found.
    """
    decoded_html = html_module.unescape(page_html)
    text = html_module.unescape(re.sub(r"<[^>]+>", " ", decoded_html))
    text = re.sub(r"\s+", " ", text).strip()

    balance_match = re.search(
        r"(?:剩余电量|剩余电费|余额)\s*[:：]?\s*(-?\d+(?:\.\d+)?)",
        text,
    )
    canvas_balance = re.search(
        r'<svg[^>]+id=["\']canvas1["\'][^>]*>.*?<tspan[^>]*>\s*(-?\d+(?:\.\d+)?)\s*</tspan>',
        decoded_html,
        re.IGNORECASE | re.DOTALL,
    )
    if balance_match is None and canvas_balance is not None:
        balance_match = canvas_balance
    if not balance_match:
        raise ValueError("电表详情中未找到余额")

    power_match = re.search(
        r"(?:当前功率|实时功率|功率)\s*[:：]?\s*(-?\d+(?:\.\d+)?)",
        text,
    )
    canvas_power = re.search(
        r'<svg[^>]+id=["\']canvas2["\'][^>]*>.*?<tspan[^>]*>\s*(-?\d+(?:\.\d+)?)\s*</tspan>',
        decoded_html,
        re.IGNORECASE | re.DOTALL,
    )
    if power_match is None and canvas_power is not None:
        power_match = canvas_power
    address_match = re.search(
        r"(?:地址|房间)\s*[:：]\s*(.+?)(?=\s+(?:截止时间|抄表时间|更新时间)\s*[:：]|$)",
        text,
    )
    if address_match is None:
        address_match = re.search(
            r"(?:地址|房间)\s*[:：]\s*(.+?)(?=\s+(?:电价|电表号)\s*[:：]|$)",
            text,
        )
    if address_match is None:
        header_fields = re.findall(
            r"<p[^>]*font-size:\s*12px[^>]*>(.*?)</p>",
            decoded_html,
            re.IGNORECASE | re.DOTALL,
        )
        header_texts = [
            html_module.unescape(re.sub(r"<[^>]+>", "", field)).strip()
            for field in header_fields
        ]
        address_candidates = [
            value for value in header_texts if "[" in value and "]" in value
        ]
        if address_candidates:
            address_match = re.match(r"[^:：]*[:：]\s*(.+)", address_candidates[0])
        elif len(header_texts) >= 3:
            address_match = re.match(r"[^:：]*[:：]\s*(.+)", header_texts[2])
    time_match = re.search(
        r"(?:截止时间|抄表时间|更新时间)\s*[:：]\s*(\d{4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)",
        text,
    )
    if time_match is None:
        time_match = re.search(
            r"\[[^\d\]]*(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2})\]",
            decoded_html,
        )
    return {
        "balance": float(balance_match.group(1)),
        "power": float(power_match.group(1)) if power_match else None,
        "address": address_match.group(1).strip() if address_match else "",
        "reading_time": time_match.group(1) if time_match else "",
    }


def build_alert_message(low_meters: list[Mapping[str, Any]]) -> str:
    """Build one QQ notification for all low-balance meters.

    Args:
        low_meters: Meter names, balances, and configured thresholds.

    Returns:
        A human-readable low-balance notification.
    """
    lines = ["宿舍电量余额预警"]
    for meter in low_meters:
        lines.append(
            f"{meter['name']}电表：{meter['balance']:g} kWh "
            f"（预警阈值 {meter['threshold']:g} kWh）"
        )
    lines.append(f"检查时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


def build_option_payload(
    rows: list[Mapping[str, Any]], selected: Mapping[str, str]
) -> dict[str, Any]:
    """Build cascading choices and meter rows for a selected location.

    Args:
        rows: Raw upstream location and meter rows.
        selected: Optional campus, building, floor, and room values.

    Returns:
        Cascading options and meter metadata. Meters are included only when all
        four location fields are selected.
    """
    keys = ("campus", "building", "floor", "room")
    option_names = {
        "campus": "campuses",
        "building": "buildings",
        "floor": "floors",
        "room": "rooms",
    }
    normalized = {key: str(selected.get(key) or "").strip() for key in keys}
    options: dict[str, list[str]] = {}
    for index, key in enumerate(keys):
        parent_keys = keys[:index]
        if parent_keys and not all(normalized[parent] for parent in parent_keys):
            options[option_names[key]] = []
            continue
        choices = [
            row
            for row in rows
            if all(
                not normalized[parent] or str(row.get(parent, "")) == normalized[parent]
                for parent in parent_keys
            )
        ]
        options[option_names[key]] = sorted(
            {str(row.get(key, "")) for row in choices if row.get(key)}
        )

    meters: list[dict[str, str]] = []
    if all(normalized.values()):
        filtered = [
            row
            for row in rows
            if all(str(row.get(key, "")) == normalized[key] for key in keys)
        ]
        meters = [
            {
                "id": str(row.get("identityNo") or row.get("id") or ""),
                "name": str(row.get("name") or ""),
                "address": str(row.get("address") or ""),
                "meter_no": str(row.get("meterNo") or ""),
            }
            for row in filtered
            if row.get("identityNo") is not None or row.get("id") is not None
        ]
    return {**options, "meters": meters}


PLUGIN_NAME = "astrbot_plugin_buaa_power"
BASE_URL = "http://shsd.buaa.edu.cn/PubBuaa"


@register(
    PLUGIN_NAME,
    "Scarbal486",
    "北航宿舍空调与照明电量监控，可通过仪表盘配置每日余额通知和低余额预警。",
    "1.0.1",
    "https://github.com/Scarbal486/astrbot_plugin_buaa_power",
)
class BuaaPowerPlugin(Star):
    """Monitor BUAA dormitory meters and send daily balances and alerts."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        """Initialize the plugin and register its dashboard APIs.

        Args:
            context: AstrBot runtime context.
            config: Plugin configuration object.
        """
        super().__init__(context)
        self.context = context
        self.config = config
        self.http_client = httpx.AsyncClient(
            base_url=BASE_URL.rsplit("/", 1)[0],
            trust_env=False,
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
        )
        self.scheduler: AsyncIOScheduler | None = None
        self.state_path = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "state.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._options_cache: tuple[float, list[dict[str, Any]]] | None = None

        prefix = f"/{PLUGIN_NAME}/page"
        context.register_web_api(
            f"{prefix}/config", self.page_config, ["GET"], "Get power monitor config"
        )
        context.register_web_api(
            f"{prefix}/config",
            self.save_page_config,
            ["POST"],
            "Save power monitor config",
        )
        context.register_web_api(
            f"{prefix}/options", self.page_options, ["GET"], "Get BUAA meter options"
        )
        context.register_web_api(
            f"{prefix}/status", self.page_status, ["GET"], "Get power monitor status"
        )
        context.register_web_api(
            f"{prefix}/check", self.page_check, ["POST"], "Run power monitor check"
        )

        self.config.update(normalize_config(self.config))
        self._restart_scheduler()

    def _read_state(self) -> dict[str, Any]:
        """Read persisted monitor state.

        Returns:
            The persisted state, or an empty dictionary when unavailable.
        """
        try:
            if self.state_path.exists():
                value = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
        except (OSError, ValueError) as exc:
            logger.warning("BUAA power state read failed: %s", exc)
        return {}

    def _write_state(self, state: Mapping[str, Any]) -> None:
        """Persist monitor state without exposing it in logs.

        Args:
            state: JSON-serializable monitor state.
        """
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(dict(state), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("BUAA power state write failed: %s", exc)

    async def fetch_index(self) -> list[dict[str, Any]]:
        """Fetch the public location and meter index.

        Returns:
            Meter index rows returned by the upstream site.

        Raises:
            RuntimeError: If the upstream response is not a JSON list.
        """
        now = datetime.now().timestamp()
        if self._options_cache and now - self._options_cache[0] < 300:
            return self._options_cache[1]
        response = await self.http_client.get(
            "/PubBuaa/QueryIdData", params={"refresh": "false"}
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("电表列表响应格式不正确")
        rows = [row for row in payload if isinstance(row, dict)]
        self._options_cache = (now, rows)
        return rows

    async def fetch_meter(self, meter_id: str) -> dict[str, Any]:
        """Fetch and parse one meter detail page.

        Args:
            meter_id: Upstream identity number.

        Returns:
            Parsed meter detail with the requested identity number.

        Raises:
            ValueError: If the detail page has no balance.
            httpx.HTTPError: If the upstream request fails.
        """
        response = await self.http_client.get("/PubBuaa", params={"id": meter_id})
        response.raise_for_status()
        result = parse_meter_detail(response.text)
        result["meter_id"] = meter_id
        return result

    async def _check_once(
        self,
        send_notification: bool = True,
        send_balance_report: bool = False,
    ) -> dict[str, Any]:
        """Check configured meters and optionally send proactive messages.

        Args:
            send_notification: Whether to send a low-balance alert after querying.
            send_balance_report: Whether to always send the queried balances.

        Returns:
            A result dictionary suitable for the status API.
        """
        config = normalize_config(self.config)
        state = self._read_state()
        checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
        state["last_check"] = checked_at
        if not config["air_meter_id"] or not config["lighting_meter_id"]:
            state["last_status"] = "incomplete"
            state["last_error"] = "请先配置空调和照明电表号"
            self._write_state(state)
            return {
                "status": "incomplete",
                "message": state["last_error"],
                "state": state,
            }

        meter_specs = (
            ("空调", "air_meter_id", "air_threshold"),
            ("照明", "lighting_meter_id", "lighting_threshold"),
        )
        low_meters: list[dict[str, Any]] = []
        errors: list[str] = []
        results: list[dict[str, Any]] = []
        for name, id_key, threshold_key in meter_specs:
            meter_id = config[id_key]
            try:
                detail = await self.fetch_meter(meter_id)
                detail["name"] = name
                detail["threshold"] = config[threshold_key]
                state_key = "air" if name == "空调" else "lighting"
                state[state_key] = detail
                results.append(detail)
                if (
                    detail.get("balance") is not None
                    and detail["balance"] < config[threshold_key]
                ):
                    low_meters.append(detail)
            except Exception as exc:
                errors.append(f"{name}电表查询失败：{exc}")

        state["last_report_send"] = None
        state["last_alert_send"] = None
        send_results: list[bool] = []
        delivery_errors: list[str] = []

        if send_balance_report:
            if config["notify_qq"]:
                lines = ["宿舍电量日报"]
                for meter in results:
                    balance = meter.get("balance")
                    balance_text = (
                        "未知" if balance is None else f"{float(balance):g}"
                    )
                    lines.append(f"{meter['name']}：{balance_text} kWh")
                if errors:
                    lines.append(f"查询异常：{'；'.join(errors)}")
                lines.append(
                    f"检查时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                try:
                    report_sent = bool(await self.send_alert("\n".join(lines)))
                except Exception as exc:
                    report_sent = False
                    delivery_errors.append(f"余额通知发送失败：{exc}")
                else:
                    if not report_sent:
                        delivery_errors.append(
                            "余额通知发送失败：没有可用的 QQ 平台或平台未接受消息"
                        )
                state["last_report_send"] = report_sent
                send_results.append(report_sent)
            else:
                state["last_report_send"] = False
                delivery_errors.append("未配置通知 QQ")
                send_results.append(False)

        if send_notification and low_meters:
            if config["notify_qq"]:
                try:
                    alert_sent = bool(
                        await self.send_alert(build_alert_message(low_meters))
                    )
                except Exception as exc:
                    alert_sent = False
                    delivery_errors.append(f"预警通知发送失败：{exc}")
                else:
                    if not alert_sent:
                        delivery_errors.append(
                            "预警通知发送失败：没有可用的 QQ 平台或平台未接受消息"
                        )
                state["last_alert_send"] = alert_sent
                send_results.append(alert_sent)
            else:
                state["last_alert_send"] = False
                if "未配置通知 QQ" not in delivery_errors:
                    delivery_errors.append("未配置通知 QQ")
                send_results.append(False)

        state["last_send"] = all(send_results) if send_results else None
        state["last_status"] = (
            "error" if (errors and not results) or delivery_errors else "ok"
        )
        state["last_error"] = "；".join([*errors, *delivery_errors])
        self._write_state(state)
        return {
            "status": state["last_status"],
            "checked_at": checked_at,
            "meters": results,
            "low_meters": [meter["name"] for meter in low_meters],
            "error": state["last_error"],
            "sent": state["last_send"],
            "state": state,
        }

    @filter.command("查询宿舍电量", alias={"宿舍电量"})
    async def query_power_command(self, event: AstrMessageEvent):
        """Query the configured air-conditioner and lighting balances.

        Args:
            event: Message event that invoked the command.

        Yields:
            A message containing the latest balance data or an error.
        """
        try:
            result = await self._check_once(send_notification=False)
        except ValueError as exc:
            yield event.plain_result(f"配置无效：{exc}")
            return
        except Exception as exc:
            logger.error("BUAA power command query failed: %s", exc)
            yield event.plain_result(f"宿舍电量查询失败：{exc}")
            return

        if result["status"] == "incomplete":
            yield event.plain_result(result["message"])
            return

        lines = ["宿舍电量查询"]
        for meter in result.get("meters", []):
            balance = meter.get("balance")
            balance_text = "未知" if balance is None else f"{float(balance):g}"
            lines.append(f"{meter['name']}：{balance_text} kWh")
            if meter.get("power") is not None:
                lines.append(f"  当前功率：{meter['power']:g}")
            if meter.get("reading_time"):
                lines.append(f"  抄表时间：{meter['reading_time']}")
        if result.get("error"):
            lines.append(f"查询异常：{result['error']}")
        if not result.get("meters"):
            lines.append("本次没有查询到可用电表数据。")
        yield event.plain_result("\n".join(lines))

    async def send_alert(self, message: str) -> bool:
        """Send an alert through an available proactive QQ platform.

        Args:
            message: Notification text.

        Returns:
            Whether AstrBot accepted the message for delivery.
        """
        qq = str(self.config.get("notify_qq") or "").strip()
        if not qq or not hasattr(self.context, "platform_manager"):
            return False
        for platform in self.context.platform_manager.get_insts():
            meta = platform.meta()
            if meta.name != "aiocqhttp" or not meta.support_proactive_message:
                continue
            session = f"{meta.id or 'NapCatQQ'}:FriendMessage:{qq}"
            try:
                return bool(
                    await self.context.send_message(
                        session, MessageChain().message(message)
                    )
                )
            except Exception as exc:
                logger.error("BUAA power alert send failed: %s", exc)
                return False
        logger.warning(
            "BUAA power alert skipped because no proactive QQ platform is available"
        )
        return False

    def _restart_scheduler(self) -> None:
        """Replace the daily scheduler using the current configuration."""
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.scheduler = None
        config = normalize_config(self.config)
        if not config["enabled"]:
            return
        hour, minute = parse_schedule_time(config["check_time"])
        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_job(
            self._scheduled_check,
            CronTrigger(hour=hour, minute=minute),
            id=f"{PLUGIN_NAME}_daily_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        self.scheduler.start()

    async def _scheduled_check(self) -> None:
        """Run one scheduled report and retain errors inside plugin state."""
        try:
            await self._check_once(send_balance_report=True)
        except Exception as exc:
            logger.error("BUAA power scheduled check failed: %s", exc)
            state = self._read_state()
            state["last_status"] = "error"
            state["last_error"] = str(exc)
            self._write_state(state)

    async def page_config(self):
        """Return editable plugin configuration."""
        return json_response(normalize_config(self.config))

    async def save_page_config(self):
        """Validate and save dashboard configuration."""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("配置格式不正确")
        try:
            normalized = normalize_config(payload)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        self.config.update(normalized)
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config(normalized)
        self._restart_scheduler()
        return json_response({"message": "设置已保存", "config": normalized})

    async def page_options(self):
        """Return cascading location options and filtered meter rows."""
        try:
            rows = await self.fetch_index()
        except Exception as exc:
            return error_response(f"获取电表列表失败：{exc}", status_code=502)
        selected = {
            "campus": str(request.query.get("campus") or "").strip(),
            "building": str(request.query.get("building") or "").strip(),
            "floor": str(request.query.get("floor") or "").strip(),
            "room": str(request.query.get("room") or "").strip(),
        }
        return json_response(build_option_payload(rows, selected))

    async def page_status(self):
        """Return current persisted status and scheduler state."""
        state = self._read_state()
        return json_response(
            {
                "config": normalize_config(self.config),
                "state": state,
                "scheduler_running": bool(self.scheduler and self.scheduler.running),
            }
        )

    async def page_check(self):
        """Run and return one immediate check without proactive messages."""
        try:
            return json_response(await self._check_once(send_notification=False))
        except Exception as exc:
            logger.error("BUAA power manual check failed: %s", exc)
            return error_response(f"检查失败：{exc}", status_code=502)

    async def terminate(self) -> None:
        """Stop scheduled work and close the HTTP client."""
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.http_client.aclose()

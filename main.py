"""AstrBot plugin for monitoring BUAA dormitory electricity balances."""

from __future__ import annotations

import html as html_module
import json
import math
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

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
        Parsed balance, address, and reading time.

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
    dormitory_match = re.search(
        r'<p[^>]*style=["\'][^"\']*font-size\s*:\s*20px[^"\']*["\'][^>]*>(.*?)</p>',
        decoded_html,
        re.IGNORECASE | re.DOTALL,
    )
    dormitory = (
        html_module.unescape(re.sub(r"<[^>]+>", "", dormitory_match.group(1))).strip()
        if dormitory_match
        else ""
    )

    recharge_records: list[dict[str, Any]] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", decoded_html, re.IGNORECASE | re.DOTALL):
        cells = [
            re.sub(r"\s+", " ", html_module.unescape(re.sub(r"<[^>]+>", "", cell))).strip()
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.IGNORECASE | re.DOTALL)
        ]
        if len(cells) < 4 or not re.search(r"\d{4}年\d{1,2}月\d{1,2}日", cells[0]):
            continue
        quantity_match = re.search(r"-?\d+(?:\.\d+)?", cells[1])
        if quantity_match:
            recharge_records.append(
                {
                    "date": cells[0],
                    "quantity": float(quantity_match.group()),
                    "amount": cells[2],
                    "operator": cells[3],
                }
            )

    daily_usage: list[dict[str, Any]] = []
    x_axis_match = re.search(
        r"xAxis\s*:\s*\{[^{}]*?data\s*:\s*\[([^\]]*)\]",
        decoded_html,
        re.IGNORECASE | re.DOTALL,
    )
    series_match = re.search(
        r"series\s*:\s*\[.*?data\s*:\s*\[([^\]]*)\]",
        decoded_html,
        re.IGNORECASE | re.DOTALL,
    )
    if x_axis_match and series_match:
        dates = re.findall(r"['\"]([^'\"]+)['\"]", x_axis_match.group(1))
        usages = re.findall(r"-?\d+(?:\.\d+)?", series_match.group(1))
        daily_usage = [
            {"date": date, "usage": float(usage)}
            for date, usage in zip(dates, usages)
        ]

    return {
        "balance": float(balance_match.group(1)),
        "address": address_match.group(1).strip() if address_match else "",
        "reading_time": time_match.group(1) if time_match else "",
        "dormitory": dormitory,
        "recharge_records": recharge_records,
        "daily_usage": daily_usage,
    }


def build_daily_usage_summary(detail: Mapping[str, Any]) -> dict[str, float | None]:
    """Return usage for the reading date and its previous calendar day."""
    reading_match = re.search(
        r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", str(detail.get("reading_time") or "")
    )
    if not reading_match:
        return {"today": None, "yesterday": None, "delta": None}
    reading_date = datetime(*map(int, reading_match.groups())).date()
    values: dict[Any, float] = {}
    for item in detail.get("daily_usage") or []:
        if not isinstance(item, Mapping):
            continue
        date_match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", str(item.get("date") or ""))
        try:
            usage = float(item.get("usage"))
        except (TypeError, ValueError):
            continue
        if date_match:
            values[datetime(*map(int, date_match.groups())).date()] = usage
    today = values.get(reading_date)
    yesterday = values.get(reading_date.fromordinal(reading_date.toordinal() - 1))
    return {
        "today": today,
        "yesterday": yesterday,
        "delta": today - yesterday if today is not None and yesterday is not None else None,
    }


def _parse_date(value: Any) -> date | None:
    match = re.search(
        r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?", str(value or "")
    )
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def _recharge_between(
    records: Any, start: date, end: date
) -> float:
    total = 0.0
    for record in records or []:
        if not isinstance(record, Mapping):
            continue
        record_date = _parse_date(record.get("date"))
        if record_date is None or not start <= record_date < end:
            continue
        try:
            total += float(record.get("quantity"))
        except (TypeError, ValueError):
            continue
    return total


def update_usage_history(
    history: Any, detail: Mapping[str, Any], limit: int = 60
) -> list[dict[str, Any]]:
    """Store one compact daily meter snapshot, replacing same-day data."""
    reading_date = _parse_date(detail.get("reading_time"))
    try:
        balance = float(detail.get("balance"))
    except (TypeError, ValueError):
        return [item for item in history or [] if isinstance(item, Mapping)][-limit:]
    if reading_date is None:
        return [item for item in history or [] if isinstance(item, Mapping)][-limit:]
    snapshots = {
        str(item.get("date")): dict(item)
        for item in history or []
        if isinstance(item, Mapping) and _parse_date(item.get("date")) is not None
    }
    key = reading_date.isoformat()
    snapshots[key] = {
        "date": key,
        "balance": balance,
        "recharge_records": list(detail.get("recharge_records") or []),
    }
    return [snapshots[key] for key in sorted(snapshots)[-limit:]]


def build_local_usage_summary(
    detail: Mapping[str, Any], history: Any
) -> dict[str, float | None]:
    """Calculate current and previous daily usage from local balance snapshots."""
    current_date = _parse_date(detail.get("reading_time"))
    if current_date is None:
        return {"today": None, "yesterday": None, "delta": None}
    snapshots: dict[date, Mapping[str, Any]] = {}
    for item in history or []:
        if isinstance(item, Mapping):
            item_date = _parse_date(item.get("date"))
            if item_date is not None:
                snapshots[item_date] = item
    snapshots[current_date] = detail

    def usage_for(end_date: date) -> float | None:
        previous_date = end_date - timedelta(days=1)
        previous = snapshots.get(previous_date)
        current = snapshots.get(end_date)
        if not previous or not current:
            return None
        try:
            previous_balance = float(previous.get("balance"))
            current_balance = float(current.get("balance"))
        except (TypeError, ValueError):
            return None
        recharge = _recharge_between(
            current.get("recharge_records"), previous_date, end_date
        )
        return previous_balance + recharge - current_balance

    today = usage_for(current_date)
    yesterday = usage_for(current_date - timedelta(days=1))
    return {
        "today": today,
        "yesterday": yesterday,
        "delta": today - yesterday if today is not None and yesterday is not None else None,
    }


def _reading_date(detail: Mapping[str, Any]):
    match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", str(detail.get("reading_time") or ""))
    return datetime(*map(int, match.groups())).date() if match else None


def build_meter_extra_lines(
    detail: Mapping[str, Any], usage_summary: Mapping[str, Any] | None = None
) -> list[str]:
    """Format today's recharge and the available daily-usage comparison."""
    reading_date = _reading_date(detail)
    today_records = []
    if reading_date:
        for record in detail.get("recharge_records") or []:
            match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", str(record.get("date") or ""))
            if match and datetime(*map(int, match.groups())).date() == reading_date:
                today_records.append(record)
    lines: list[str] = []
    if today_records:
        recharge = "；".join(
            f"{float(record['quantity']):g} kWh（{record['date']}）" for record in today_records
        )
        lines.append(f"今日充值：{recharge}")

    usage = usage_summary or detail.get("local_usage") or build_daily_usage_summary(detail)
    yesterday = (
        "暂无可用数据"
        if usage["today"] is None
        else f"{usage['today']:g} kWh"
    )
    lines.append(f"昨日用电：{yesterday}")
    return lines


def build_alert_message(low_meters: list[Mapping[str, Any]]) -> str:
    """Build one QQ notification for all low-balance meters.

    Args:
        low_meters: Meter names, balances, and configured thresholds.

    Returns:
        A human-readable low-balance notification.
    """
    dormitory = next((str(meter.get("dormitory") or "") for meter in low_meters if meter.get("dormitory")), "")
    lines = ["宿舍电量余额预警"]
    if dormitory:
        lines.append(f"宿舍：{dormitory}")
    for meter in low_meters:
        lines.append(
            f"{meter['name']}电表：{meter['balance']:g} kWh "
            f"（预警阈值 {meter['threshold']:g} kWh）"
        )
        lines.extend(f"  {line}" for line in build_meter_extra_lines(meter))
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
    "北航宿舍空调与照明电量监控，显示昨日用电并支持每日余额通知和每 6 小时低余额预警。",
    "1.0.5",
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
                history_key = f"{state_key}_history"
                previous_history = state.get(history_key, [])
                detail["local_usage"] = build_local_usage_summary(detail, previous_history)
                state[state_key] = detail
                state[history_key] = update_usage_history(previous_history, detail)
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
                dormitory = next(
                    (str(meter.get("dormitory") or "") for meter in results if meter.get("dormitory")),
                    "",
                )
                lines = ["宿舍电量日报"]
                if dormitory:
                    lines.append(f"宿舍：{dormitory}")
                for meter in results:
                    balance = meter.get("balance")
                    balance_text = "未知" if balance is None else f"{float(balance):g}"
                    lines.append(f"{meter['name']}：{balance_text} kWh")
                    lines.extend(f"  {line}" for line in build_meter_extra_lines(meter))
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
        dormitory = next(
            (str(meter.get("dormitory") or "") for meter in result.get("meters", []) if meter.get("dormitory")),
            "",
        )
        if dormitory:
            lines.append(f"宿舍：{dormitory}")
        for meter in result.get("meters", []):
            balance = meter.get("balance")
            balance_text = "未知" if balance is None else f"{float(balance):g}"
            lines.append(f"{meter['name']}：{balance_text} kWh")
            if meter.get("reading_time"):
                lines.append(f"  抄表时间：{meter['reading_time']}")
            lines.extend(f"  {line}" for line in build_meter_extra_lines(meter))
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
        self.scheduler.add_job(
            self._scheduled_alert_check,
            IntervalTrigger(hours=6),
            id=f"{PLUGIN_NAME}_alert_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        self.scheduler.start()

    async def _scheduled_check(self) -> None:
        """Run one scheduled report and retain errors inside plugin state."""
        try:
            await self._check_once(send_notification=False, send_balance_report=True)
        except Exception as exc:
            logger.error("BUAA power scheduled check failed: %s", exc)
            state = self._read_state()
            state["last_status"] = "error"
            state["last_error"] = str(exc)
            self._write_state(state)

    async def _scheduled_alert_check(self) -> None:
        """Run the six-hour low-balance check without sending a daily report."""
        try:
            await self._check_once(send_balance_report=False)
        except Exception as exc:
            logger.error("BUAA power alert check failed: %s", exc)
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

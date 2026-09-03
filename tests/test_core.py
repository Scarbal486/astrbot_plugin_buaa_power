from astrbot_plugin_buaa_power import main


def test_defaults_do_not_contain_personal_location_meter_or_qq():
    assert main.DEFAULT_CONFIG["campus"] == ""
    assert main.DEFAULT_CONFIG["building"] == ""
    assert main.DEFAULT_CONFIG["floor"] == ""
    assert main.DEFAULT_CONFIG["room"] == ""
    assert main.DEFAULT_CONFIG["air_meter_id"] == ""
    assert main.DEFAULT_CONFIG["lighting_meter_id"] == ""
    assert main.DEFAULT_CONFIG["notify_qq"] == ""


def test_normalize_config_rejects_invalid_time_and_negative_threshold():
    try:
        main.normalize_config({"check_time": "25:61", "air_threshold": -1})
    except ValueError as exc:
        assert "时间" in str(exc) or "阈值" in str(exc)
    else:
        raise AssertionError("invalid configuration was accepted")


def test_parse_schedule_time_accepts_daily_hh_mm():
    assert main.parse_schedule_time("08:05") == (8, 5)


def test_normalize_config_parses_string_boolean_without_enabling_monitor():
    assert main.normalize_config({"enabled": "false"})["enabled"] is False


def test_normalize_config_rejects_non_numeric_notify_qq():
    try:
        main.normalize_config({"notify_qq": "qq-user"})
    except ValueError as exc:
        assert "QQ" in str(exc)
    else:
        raise AssertionError("invalid QQ target was accepted")


def test_normalize_config_rejects_non_ascii_notify_qq():
    try:
        main.normalize_config({"notify_qq": "１２３４５６"})
    except ValueError as exc:
        assert "QQ" in str(exc)
    else:
        raise AssertionError("non-ASCII QQ target was accepted")


def test_parse_meter_detail_reads_balance_and_keeps_unknown_power():
    html = """
    <div>地址：A区 1号楼 2层 201</div>
    <div>截止时间：2026-09-04 00:00:00</div>
    <svg><text>余额：12.50</text></svg>
    """
    result = main.parse_meter_detail(html)
    assert result["balance"] == 12.5
    assert result["power"] is None


def test_parse_meter_detail_reads_live_page_structure_metadata():
    html = """
    <p class="shadow" style="font-size: 20px;">测试楼-1-101</p>
    <p class="shadow" style="font-size: 12px;">字段: 10001</p>
    <p class="shadow" style="font-size: 12px;">字段: 0.4800</p>
    <p class="shadow" style="font-size: 12px;">字段: 测试楼-1-101[空调]</p>
    <p class="text-center text-muted">[截止 2026/9/3 0:00:00]</p>
    <svg id="canvas1"><tspan x="100" y="114">38</tspan></svg>
    <svg id="canvas2"><tspan x="100" y="114">未知</tspan></svg>
    """
    result = main.parse_meter_detail(html)
    assert result["address"] == "测试楼-1-101[空调]"
    assert result["reading_time"] == "2026/9/3 0:00:00"


def test_build_alert_message_includes_each_low_meter():
    message = main.build_alert_message(
        [
            {"name": "空调", "balance": 2.5, "threshold": 5},
            {"name": "照明", "balance": 8, "threshold": 10},
        ]
    )
    assert "空调" in message and "照明" in message
    assert "2.5" in message and "8" in message


def test_build_option_payload_only_returns_meters_for_complete_location():
    rows = [
        {
            "identityNo": 1,
            "name": "A-1-101[空调]",
            "address": "A-1-101[空调]",
            "meterNo": "*0001",
            "campus": "校区A",
            "building": "楼A",
            "floor": "1",
            "room": "101",
        },
        {
            "identityNo": 2,
            "name": "B-2-202[照明]",
            "address": "B-2-202[照明]",
            "meterNo": "*0002",
            "campus": "校区B",
            "building": "楼B",
            "floor": "2",
            "room": "202",
        },
    ]

    initial = main.build_option_payload(rows, {})
    selected = main.build_option_payload(
        rows,
        {"campus": "校区A", "building": "楼A", "floor": "1", "room": "101"},
    )

    assert initial["campuses"] == ["校区A", "校区B"]
    assert initial["buildings"] == []
    assert initial["meters"] == []
    assert selected["meters"] == [
        {"id": "1", "name": "A-1-101[空调]", "address": "A-1-101[空调]", "meter_no": "*0001"}
    ]

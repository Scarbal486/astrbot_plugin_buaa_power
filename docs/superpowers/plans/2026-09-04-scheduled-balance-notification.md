# Scheduled Balance Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send the current air-conditioner and lighting balances to the configured QQ friend at the daily check time, followed by a separate low-balance alert when required.

**Architecture:** Reuse the existing APScheduler job, meter query loop, QQ sender, time, and target configuration. Add an opt-in balance-report branch to `_check_once` so only `_scheduled_check` enables proactive daily reports; command and dashboard checks remain silent. Both scheduled messages use the same query result and are attempted independently.

**Tech Stack:** Python 3.10+, AstrBot plugin API, APScheduler, httpx, pytest, Ruff

---

### Task 1: Lock Scheduled And Manual Notification Behavior

**Files:**
- Modify: `tests/test_plugin.py`

- [ ] **Step 1: Write failing scheduled notification tests**

Add tests that call the real `_check_once` query flow with mocked upstream and QQ boundaries:

```python
@pytest.mark.asyncio
async def test_scheduled_check_sends_balance_report_for_normal_balances(tmp_path):
    plugin = BuaaPowerPlugin.__new__(BuaaPowerPlugin)
    plugin.state_path = tmp_path / "state.json"
    plugin.config = {
        "enabled": True,
        "air_meter_id": "air",
        "lighting_meter_id": "light",
        "air_threshold": 5,
        "lighting_threshold": 10,
        "notify_qq": "123456",
    }
    plugin.fetch_meter = AsyncMock(side_effect=[{"balance": 20}, {"balance": 30}])
    plugin.send_alert = AsyncMock(return_value=True)

    result = await plugin._check_once(send_balance_report=True)

    plugin.send_alert.assert_awaited_once()
    assert "宿舍电量日报" in plugin.send_alert.await_args.args[0]
    assert "空调：20 kWh" in plugin.send_alert.await_args.args[0]
    assert "照明：30 kWh" in plugin.send_alert.await_args.args[0]
    assert result["sent"] is True


@pytest.mark.asyncio
async def test_scheduled_low_balance_sends_report_and_separate_alert(tmp_path):
    plugin = BuaaPowerPlugin.__new__(BuaaPowerPlugin)
    plugin.state_path = tmp_path / "state.json"
    plugin.config = {
        "enabled": True,
        "air_meter_id": "air",
        "lighting_meter_id": "light",
        "air_threshold": 5,
        "lighting_threshold": 10,
        "notify_qq": "123456",
    }
    plugin.fetch_meter = AsyncMock(side_effect=[{"balance": 2}, {"balance": 8}])
    plugin.send_alert = AsyncMock(side_effect=[False, True])

    result = await plugin._check_once(send_balance_report=True)

    assert plugin.send_alert.await_count == 2
    assert "宿舍电量日报" in plugin.send_alert.await_args_list[0].args[0]
    assert "宿舍电量余额预警" in plugin.send_alert.await_args_list[1].args[0]
    assert result["sent"] is False
    assert "余额通知发送失败" in result["error"]
```

- [ ] **Step 2: Write a failing dashboard manual-check test**

Import the plugin module as `plugin_module`, replace `json_response` at the API boundary, and assert the immediate check disables proactive notifications:

```python
@pytest.mark.asyncio
async def test_dashboard_manual_check_does_not_send_proactive_messages(monkeypatch):
    plugin = BuaaPowerPlugin.__new__(BuaaPowerPlugin)
    plugin._check_once = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(plugin_module, "json_response", lambda value: value)

    result = await plugin.page_check()

    assert result == {"status": "ok"}
    plugin._check_once.assert_awaited_once_with(send_notification=False)
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```powershell
uv run pytest -q data/plugins/astrbot_plugin_buaa_power/tests/test_plugin.py -k "scheduled_check_sends_balance_report or scheduled_low_balance or dashboard_manual"
```

Expected: failures because `_check_once` does not accept `send_balance_report`, normal balances are not sent, and `page_check` calls `_check_once` without disabling notifications.

- [ ] **Step 4: Commit the failing tests**

```powershell
git add tests/test_plugin.py
git commit -m "test: cover scheduled balance notifications"
```

### Task 2: Implement Separate Daily Balance And Warning Messages

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add the opt-in scheduled report parameter**

Change the signature and docstring without changing the default low-balance behavior:

```python
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
```

- [ ] **Step 2: Send the daily report before the independent alert**

After the meter query loop, reset `last_report_send` and `last_alert_send`, build the report from `results` and query `errors`, then attempt each required message independently. Keep delivery errors separate, track every required send in `send_results`, and set `last_send` to `all(send_results)`:

```python
state["last_report_send"] = None
state["last_alert_send"] = None
send_results: list[bool] = []
delivery_errors: list[str] = []

if send_balance_report:
    if config["notify_qq"]:
        lines = ["宿舍电量日报"]
        for meter in results:
            balance = meter.get("balance")
            balance_text = "未知" if balance is None else f"{float(balance):g}"
            lines.append(f"{meter['name']}：{balance_text} kWh")
        if errors:
            lines.append(f"查询异常：{'；'.join(errors)}")
        lines.append(f"检查时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
            alert_sent = bool(await self.send_alert(build_alert_message(low_meters)))
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
```

Keep partial query failures visible in `last_error` while preserving the existing `ok` status when at least one meter succeeds.

- [ ] **Step 3: Enable reports only for scheduled checks**

Use the new parameter only in `_scheduled_check`, and explicitly disable proactive sends in the dashboard endpoint:

```python
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


async def page_check(self):
    """Run and return one immediate check without proactive messages."""
    try:
        return json_response(await self._check_once(send_notification=False))
    except Exception as exc:
        logger.error("BUAA power manual check failed: %s", exc)
        return error_response(f"检查失败：{exc}", status_code=502)
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```powershell
uv run pytest -q data/plugins/astrbot_plugin_buaa_power/tests/test_plugin.py
```

Expected: all plugin behavior tests pass, including the new report ordering and manual silence cases.

- [ ] **Step 5: Commit the implementation**

```powershell
git add main.py
git commit -m "feat: send scheduled balance reports"
```

### Task 3: Update User-Facing Copy And Version

**Files:**
- Modify: `_conf_schema.json`
- Modify: `pages/dashboard/index.html`
- Modify: `README.md`
- Modify: `metadata.yaml`
- Modify: `main.py`
- Modify: `tests/test_plugin.py`

- [ ] **Step 1: Add failing copy and version assertions**

Extend the dashboard asset test to assert that the page says `每日通知时间` and explains that the normal report and warning are separate. Add metadata assertions for version `1.0.1`.

- [ ] **Step 2: Run the copy test and verify RED**

Run:

```powershell
uv run pytest -q data/plugins/astrbot_plugin_buaa_power/tests/test_plugin.py::test_dashboard_assets_exist_and_use_plugin_page_bridge
```

Expected: failure because the page still says `每日检查时间` and the metadata version is `1.0.0`.

- [ ] **Step 3: Update the visible descriptions**

Change the schema and dashboard copy to state that the configured time sends current balances and sends an additional independent warning below the threshold. Update README feature and setup descriptions with the same behavior.

- [ ] **Step 4: Bump the plugin patch version**

Set both `metadata.yaml` and the `@register` compatibility metadata in `main.py` to `1.0.1`. Update `desc` and `short_desc` to mention daily balance notification.

- [ ] **Step 5: Run the copy test and verify GREEN**

Run:

```powershell
uv run pytest -q data/plugins/astrbot_plugin_buaa_power/tests/test_plugin.py::test_dashboard_assets_exist_and_use_plugin_page_bridge
```

Expected: pass.

- [ ] **Step 6: Commit documentation and version updates**

```powershell
git add _conf_schema.json pages/dashboard/index.html README.md metadata.yaml main.py tests/test_plugin.py
git commit -m "docs: describe daily balance notifications"
```

### Task 4: Verify And Publish

**Files:**
- Verify all tracked plugin files

- [ ] **Step 1: Run the full plugin test suite**

```powershell
uv run pytest -q data/plugins/astrbot_plugin_buaa_power/tests
```

Expected: all tests pass.

- [ ] **Step 2: Run syntax and lint checks**

```powershell
uv run python -m compileall -q data/plugins/astrbot_plugin_buaa_power
uv run ruff format --check data/plugins/astrbot_plugin_buaa_power
uv run ruff check data/plugins/astrbot_plugin_buaa_power
node --check data/plugins/astrbot_plugin_buaa_power/pages/dashboard/app.js
```

Expected: every command exits with status 0.

- [ ] **Step 3: Audit the outgoing change**

```powershell
git diff origin/main...HEAD --check
git status --short --branch
git diff origin/main...HEAD --stat
```

Expected: only the specification, plan, tests, implementation, user-facing copy, and version files differ; no runtime data or personal configuration appears.

- [ ] **Step 4: Push and verify the remote branch**

```powershell
git push origin main
git fetch origin main
git rev-parse HEAD
git rev-parse origin/main
```

Expected: local `HEAD` equals `origin/main`.

const bridge = window.AstrBotPluginPage;

const ids = ["campus", "building", "floor", "room"];
const elements = Object.fromEntries(
  [
    ...ids,
    "enabled",
    "air_meter_id",
    "lighting_meter_id",
    "air_threshold",
    "lighting_threshold",
    "notify_qq",
    "check_time",
    "message",
    "meter-results",
    "air-balance",
    "lighting-balance",
    "air-meta",
    "lighting-meta",
    "last-check",
    "last-error",
    "scheduler-state",
  ].map((id) => [id, document.getElementById(id)]),
);

let config = {};

function setMessage(text, isError = false) {
  elements.message.textContent = text || "";
  elements.message.classList.toggle("error", isError);
}

function setSelectOptions(id, values, selected = "") {
  const select = elements[id];
  select.replaceChildren(new Option("请选择", ""));
  for (const value of values || []) select.add(new Option(value, value));
  select.value = selected || "";
  select.disabled = !(values && values.length);
}

async function loadOptions(selection = {}) {
  const data = await bridge.apiGet("page/options", selection);
  setSelectOptions("campus", data.campuses, selection.campus);
  setSelectOptions("building", data.buildings, selection.building);
  setSelectOptions("floor", data.floors, selection.floor);
  setSelectOptions("room", data.rooms, selection.room);
  return data;
}

function currentSelection() {
  return Object.fromEntries(ids.map((id) => [id, elements[id].value]));
}

async function refreshCascade(changedIndex) {
  const selection = currentSelection();
  for (let index = changedIndex + 1; index < ids.length; index += 1) {
    selection[ids[index]] = "";
  }
  try {
    await loadOptions(selection);
  } catch (error) {
    setMessage(error.message, true);
  }
}

function renderMeters(meters) {
  elements["meter-results"].replaceChildren();
  if (!meters.length) {
    elements["meter-results"].textContent = "当前位置没有可用电表";
    return;
  }
  for (const meter of meters) {
    const row = document.createElement("div");
    row.className = "meter-row";
    const text = document.createElement("span");
    text.textContent = `${meter.name || meter.address || "电表"} · ${meter.id}`;
    row.append(text);
    const target = `${meter.name || ""} ${meter.address || ""}`;
    for (const [label, field, pattern] of [
      ["填入空调", "air_meter_id", /空调/],
      ["填入照明", "lighting_meter_id", /照明|灯/],
    ]) {
      if (pattern.test(target) || !/空调|照明|灯/.test(target)) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "link-button";
        button.textContent = label;
        button.addEventListener("click", () => {
          elements[field].value = meter.id;
          setMessage(`${label.replace("填入", "")}电表号已填入，请保存设置。`);
        });
        row.append(button);
      }
    }
    elements["meter-results"].append(row);
  }
}

function applyConfig(value) {
  config = value || {};
  for (const key of [
    "enabled",
    "air_meter_id",
    "lighting_meter_id",
    "air_threshold",
    "lighting_threshold",
    "notify_qq",
    "check_time",
  ]) {
    if (key === "enabled") elements[key].checked = Boolean(config[key]);
    else elements[key].value = config[key] ?? "";
  }
}

function renderStatus(payload) {
  const state = payload?.state || {};
  for (const [key, balanceId, metaId] of [
    ["air", "air-balance", "air-meta"],
    ["lighting", "lighting-balance", "lighting-meta"],
  ]) {
    const meter = state[key];
    elements[balanceId].textContent = meter?.balance == null ? "--" : `${meter.balance} kWh`;
    elements[metaId].textContent = meter?.power == null ? "功率：未知" : `功率：${meter.power}`;
  }
  elements["last-check"].textContent = state.last_check || "--";
  elements["last-error"].textContent = state.last_error || "无错误";
  elements["scheduler-state"].textContent = `调度状态：${payload?.scheduler_running ? "运行中" : "未启用"}`;
}

async function loadPage() {
  await bridge.ready();
  try {
    const [savedConfig, status] = await Promise.all([
      bridge.apiGet("page/config"),
      bridge.apiGet("page/status"),
    ]);
    applyConfig(savedConfig);
    await loadOptions({
      campus: savedConfig.campus,
      building: savedConfig.building,
      floor: savedConfig.floor,
      room: savedConfig.room,
    });
    renderStatus(status);
  } catch (error) {
    setMessage(error.message, true);
  }
}

document.getElementById("lookup").addEventListener("click", async () => {
  const selection = currentSelection();
  if (ids.some((id) => !selection[id])) {
    setMessage("请先选择完整的校区、楼宇、楼层和房间。", true);
    return;
  }
  try {
    const data = await bridge.apiGet("page/options", selection);
    renderMeters(data.meters || []);
    setMessage(`找到 ${data.meters?.length || 0} 个电表。`);
  } catch (error) {
    setMessage(error.message, true);
  }
});

ids.forEach((id, index) => {
  elements[id].addEventListener("change", () => refreshCascade(index));
});

document.getElementById("save").addEventListener("click", async () => {
  const payload = {
    ...currentSelection(),
    enabled: elements.enabled.checked,
    air_meter_id: elements.air_meter_id.value.trim(),
    lighting_meter_id: elements.lighting_meter_id.value.trim(),
    air_threshold: Number(elements.air_threshold.value),
    lighting_threshold: Number(elements.lighting_threshold.value),
    notify_qq: elements.notify_qq.value.trim(),
    check_time: elements.check_time.value,
  };
  try {
    const result = await bridge.apiPost("page/config", payload);
    applyConfig(result.config);
    setMessage("设置已保存。每次低于阈值的检查都会提醒。", false);
    renderStatus(await bridge.apiGet("page/status"));
  } catch (error) {
    setMessage(error.message, true);
  }
});

document.getElementById("check").addEventListener("click", async () => {
  try {
    setMessage("正在检查…");
    const result = await bridge.apiPost("page/check", {});
    renderStatus(await bridge.apiGet("page/status"));
    setMessage(result.error || (result.sent ? "检查完成，已发送提醒。" : "检查完成。"), Boolean(result.error));
  } catch (error) {
    setMessage(error.message, true);
  }
});

loadPage();

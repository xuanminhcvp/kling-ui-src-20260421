#!/usr/bin/env python3
"""
Trích xuất bảng mapping thao tác setting từ log debug Kling.

Mục tiêu:
- Đọc `settings_ui_events.json` + `settings_network_events.json` của 1 session.
- Gom các thao tác UI quan trọng (setting) thành bảng dễ đọc.
- Gắn kèm API gần thời điểm thao tác để thấy request/response liên quan.

Cách chạy:
  python3 extract_kling_setting_mapping.py --session debug_sessions/kling_settings_20260418_043228

Nếu không truyền --session, script sẽ tự lấy session `kling_settings_*` mới nhất.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SETTING_HINTS = [
    "custom multi-shot",
    "mode",
    "length",
    "720p",
    "1080p",
    "5s",
    "8s",
    "10s",
    "15s",
    "16:9",
    "9:16",
    "1:1",
    "camera",
    "motion",
    "cfg",
    "setting-select",
    "feature-btn-text",
    "panel-prompt",
]

API_HINTS = [
    "/api/task/price",
    "/api/task/support-version",
    "/api/libraries/text-to-video",
    "/api/lora/availablelist",
    "/api/modality_asset",
    "/api/user/works/personal/feeds",
]


@dataclass
class UiRow:
    ts: int
    iso: str
    event_type: str
    selector: str
    text: str
    value: str


def _load_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _looks_like_setting_event(row: dict[str, Any]) -> bool:
    blob = " ".join(
        [
            str(row.get("selector") or ""),
            str(row.get("text") or ""),
            str(row.get("value") or ""),
            str(row.get("ariaLabel") or ""),
        ]
    ).lower()
    return any(k in blob for k in SETTING_HINTS)


def _looks_like_setting_api(url: str) -> bool:
    low = (url or "").lower()
    return any(k in low for k in API_HINTS)


def _pick_latest_session(root: Path) -> Path | None:
    candidates = sorted(root.glob("kling_settings_*"))
    if not candidates:
        return None
    return candidates[-1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", default="", help="Đường dẫn session debug cụ thể")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent
    debug_root = repo / "debug_sessions"

    if args.session:
        session_dir = Path(args.session).resolve()
    else:
        picked = _pick_latest_session(debug_root)
        if not picked:
            print("[ERR] Không tìm thấy session kling_settings_*")
            return
        session_dir = picked

    ui_path = session_dir / "settings_ui_events.json"
    net_path = session_dir / "settings_network_events.json"

    ui_raw = _load_json(ui_path)
    net_raw = _load_json(net_path)

    ui_rows: list[UiRow] = []
    for row in ui_raw:
        if not _looks_like_setting_event(row):
            continue
        ui_rows.append(
            UiRow(
                ts=int(row.get("ts") or 0),
                iso=str(row.get("iso") or ""),
                event_type=str(row.get("type") or ""),
                selector=str(row.get("selector") or ""),
                text=str(row.get("text") or "").strip(),
                value=str(row.get("value") or "").strip(),
            )
        )

    # Giữ API setting để map gần mốc thời gian UI.
    apis = [
        x
        for x in net_raw
        if str(x.get("phase") or "") in {"request", "response", "request_blocked"}
        and _looks_like_setting_api(str(x.get("url") or ""))
    ]
    apis.sort(key=lambda x: int(x.get("ts") or 0))

    # Kết quả markdown cho dễ đọc trong IDE.
    out_md = session_dir / "setting_selector_mapping.md"
    lines: list[str] = []
    lines.append("# Mapping Setting (UI -> API)\n")
    lines.append(f"- Session: `{session_dir}`")
    lines.append(f"- UI events lọc được: `{len(ui_rows)}`")
    lines.append(f"- API events lọc được: `{len(apis)}`\n")
    lines.append("| Thời gian | Sự kiện UI | Selector | Nội dung | Value | API gần nhất |")
    lines.append("|---|---|---|---|---|---|")

    for row in ui_rows:
        nearest = ""
        nearest_delta = 10_000_000
        for api in apis:
            d = abs(int(api.get("ts") or 0) - row.ts)
            if d < nearest_delta:
                nearest_delta = d
                phase = str(api.get("phase") or "")
                method = str(api.get("method") or "")
                path = str((api.get("url_info") or {}).get("path") or "")
                status = str(api.get("status") or "")
                nearest = f"{phase} {method} {status} {path}".strip()

        text = row.text.replace("|", "\\|")[:120]
        value = row.value.replace("|", "\\|")[:80]
        selector = row.selector.replace("|", "\\|")[:120]
        lines.append(
            f"| {row.iso} | {row.event_type} | `{selector}` | {text} | {value} | `{nearest}` |"
        )

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[OK] Session: {session_dir}")
    print(f"[OK] File mapping: {out_md}")
    print(f"[OK] UI rows: {len(ui_rows)} | API rows: {len(apis)}")


if __name__ == "__main__":
    main()

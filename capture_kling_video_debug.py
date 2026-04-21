#!/usr/bin/env python3
"""
Mở Chrome profile riêng cho Kling và bắt debug đầy đủ cho luồng tạo video.

Mục tiêu script:
1) Cho bạn login/tạo video mẫu bằng tay trên giao diện Kling.
2) Bắt request/response/requestfailed để thấy API gửi gì, nhận gì.
3) Bắt thao tác UI (click/change) + snapshot element để map selector automation ổn định.
4) Lưu log thành JSON + timeline text dễ đọc trong debug_sessions.

Cách chạy nhanh:
  python3 capture_kling_video_debug.py

Biến môi trường hỗ trợ:
  KLING_PROFILE_DIR   : thư mục Chrome profile riêng cho Kling
  KLING_HOME_URL      : URL mở mặc định khi khởi chạy
  KLING_CAPTURE_KEYS  : chuỗi keyword lọc network (phân tách bằng dấu phẩy)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.request
from urllib.error import HTTPError, URLError
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

# ─────────────────────────────────────────────────────────────────────────────
# Cấu hình mặc định
# ─────────────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROFILE_DIR = os.path.join(SCRIPT_DIR, "chrome_profiles", "kling_video_debug_profile")
DEFAULT_HOME_URL = "https://app.klingai.com/"

PROFILE_DIR = os.environ.get("KLING_PROFILE_DIR", DEFAULT_PROFILE_DIR).strip() or DEFAULT_PROFILE_DIR
HOME_URL = os.environ.get("KLING_HOME_URL", DEFAULT_HOME_URL).strip() or DEFAULT_HOME_URL

# Keyword để lọc request/response liên quan video generation.
# Có thể tinh chỉnh bằng env KLING_CAPTURE_KEYS="kling,video,generate,..."
DEFAULT_CAPTURE_KEYS = [
    "video",
    "generate",
    "creation",
    "task",
    "job",
    "submit",
    "upload",
    "media",
    "graphql",
    "text2video",
    "image2video",
    "txt2video",
]
ENV_CAPTURE_KEYS = [
    key.strip().lower() for key in os.environ.get("KLING_CAPTURE_KEYS", "").split(",") if key.strip()
]
CAPTURE_KEYS = ENV_CAPTURE_KEYS if ENV_CAPTURE_KEYS else DEFAULT_CAPTURE_KEYS

# Các endpoint nhiễu thường xuất hiện khi mở trang chủ/community của Kling.
NOISY_SUBSTRINGS = [
    "/api/mixfeeds",
    "/api/user/communityhistory",
    "/api/elements",
    "/api/tags",
    "/api/activities",
    "/api/letter/webbanner",
    "/api/pay/package",
    "/api/product/list",
    "/api/reward/activity/list",
    "/api/config/work_category_type",
    "/api/inspiration/",
]

# Thư mục debug theo session, luôn tách riêng từng lần chạy.
DEBUG_ROOT = Path(SCRIPT_DIR) / "debug_sessions"
SESSION_DIR = DEBUG_ROOT / f"kling_capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

# Không in quá dài body để terminal vẫn đọc được.
TERMINAL_BODY_PREVIEW_LIMIT = 380
JSON_BODY_MAX_LENGTH = 120_000
KEEP_OPEN_FOR_LIVE_TEST = os.environ.get("KLING_KEEP_OPEN_FOR_LIVE_TEST", "1").strip().lower() in {
    "1",
    "true",
    "yes",
}
AUTO_DOWNLOAD_COMPLETED_VIDEO = os.environ.get("KLING_AUTO_DOWNLOAD_COMPLETED_VIDEO", "1").strip().lower() in {
    "1",
    "true",
    "yes",
}
DOWNLOAD_DIR = SESSION_DIR / "downloaded_videos"


@dataclass
class CaptureState:
    """Giữ toàn bộ dữ liệu runtime để cuối phiên ghi file debug."""

    network_events: list[dict[str, Any]]
    ui_events: list[dict[str, Any]]
    element_snapshots: list[dict[str, Any]]
    submitted_tasks: list[dict[str, Any]]
    completed_videos: list[dict[str, Any]]


def _extract_argument_value(arguments: list[Any], name: str, default: Any = "") -> Any:
    """
    Lấy giá trị argument theo tên từ mảng `arguments`.
    """
    for item in arguments or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("name", "")).strip() == name:
            return item.get("value", default)
    return default


def _safe_filename_text(value: str, fallback: str = "video") -> str:
    """Chuẩn hóa text thành tên file an toàn cho hệ điều hành."""
    raw = (value or "").strip()
    if not raw:
        raw = fallback
    raw = re.sub(r"[^\w\-.]+", "_", raw)
    raw = raw.strip("._")
    return raw or fallback


def _download_video_file(url: str, output_path: Path) -> tuple[bool, str]:
    """
    Tải video MP4 từ URL về đường dẫn chỉ định.
    """
    try:
        req = urllib.request.Request(
            url=url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
        return True, ""
    except HTTPError as exc:
        return False, f"HTTPError {exc.code}: {exc.reason}"
    except URLError as exc:
        return False, f"URLError: {exc.reason}"
    except Exception as exc:
        return False, str(exc)


def ts_iso() -> str:
    """Timestamp ISO để log đọc dễ hơn."""
    return datetime.now().isoformat(timespec="milliseconds")


def now_ms() -> int:
    """Timestamp milliseconds để tiện sort timeline."""
    return int(time.time() * 1000)


def ensure_debug_dirs() -> None:
    """Tạo thư mục debug/session nếu chưa tồn tại."""
    DEBUG_ROOT.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def looks_like_relevant_url(url: str, resource_type: str = "", method: str = "") -> bool:
    """
    Lọc request/response đủ chặt để debug "tạo video" mà không ngập log tĩnh.

    Quy tắc chính:
    - Chỉ ưu tiên domain Kling.
    - Bỏ qua file tĩnh (.js/.css/.png/...).
    - Giữ lại fetch/xhr/websocket/media hoặc request POST/PUT/PATCH quan trọng.
    """
    low = (url or "").lower()
    if not low:
        return False

    # Chỉ tập trung domain Kling thật sự (theo hostname) để tránh analytics ngoài hệ thống.
    if low.startswith("blob:https://kling.ai"):
        host_ok = True
    else:
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        host_ok = (
            host == "kling.ai"
            or host.endswith(".kling.ai")
            or host == "klingai.com"
            or host.endswith(".klingai.com")
        )
    if not host_ok:
        return False

    # Loại endpoint nhiễu không phục vụ debug pipeline tạo video.
    for noisy in NOISY_SUBSTRINGS:
        if noisy in low:
            return False

    # Loại trừ file tĩnh gây nhiễu khi app load.
    static_exts = (
        ".js",
        ".css",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".woff",
        ".woff2",
        ".ttf",
        ".map",
        ".ico",
    )
    base_url = low.split("?", 1)[0]
    if base_url.endswith(static_exts):
        return False

    rt = (resource_type or "").lower()
    m = (method or "").upper()

    # Các request dữ liệu chính cho automation.
    if rt in {"xhr", "fetch", "websocket"}:
        return True

    # Media giữ lại khi có dấu hiệu video thực tế.
    if rt == "media":
        return any(k in low for k in ("video", ".mp4", ".m3u8", "stream", "playback"))

    # Request mutation thường là submit job/generate.
    if m in {"POST", "PUT", "PATCH", "DELETE"}:
        return True

    # Fallback theo keyword nếu vẫn chưa match.
    return any(key in low for key in CAPTURE_KEYS)


def safe_json_parse(raw: str) -> Any:
    """Parse JSON an toàn; lỗi thì trả chuỗi gốc."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def compact_text(value: Any, limit: int = TERMINAL_BODY_PREVIEW_LIMIT) -> str:
    """Chuẩn hóa text để in 1 dòng ngắn gọn trên terminal."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def truncate_for_json(value: Any, max_len: int = JSON_BODY_MAX_LENGTH) -> Any:
    """
    Cắt bớt payload quá lớn trước khi ghi file JSON.
    Mục tiêu: tránh file debug phình quá to gây chậm mở.
    """
    if value is None:
        return None

    try:
        if isinstance(value, (dict, list)):
            raw = json.dumps(value, ensure_ascii=False)
            if len(raw) <= max_len:
                return value
            return {
                "_truncated": True,
                "_raw_preview": raw[:max_len] + "...<truncated>",
            }

        raw = str(value)
        if len(raw) <= max_len:
            return value
        return raw[:max_len] + "...<truncated>"
    except Exception:
        return "<unserializable_payload>"


async def install_ui_probe(context: BrowserContext) -> None:
    """
    Inject script trước khi page load để bắt click/change trên mọi trang Kling.

    Dữ liệu sẽ lưu vào window.__klingUiEvents để Python đọc định kỳ.
    """
    await context.add_init_script(
        r"""
        (() => {
          const MAX_EVENTS = 1200;

          // Tạo biến toàn cục để lưu event UI phía browser.
          if (!window.__klingUiEvents) {
            window.__klingUiEvents = [];
          }

          // Hàm dựng CSS selector đơn giản nhưng đủ ổn định để debug nhanh.
          function buildSelector(el) {
            if (!el || el.nodeType !== 1) return '';
            if (el.id) return `#${el.id}`;

            const parts = [];
            let cur = el;
            let depth = 0;

            while (cur && cur.nodeType === 1 && depth < 5) {
              let part = cur.tagName.toLowerCase();
              const cls = (cur.className || '').toString().trim().split(/\s+/).filter(Boolean).slice(0, 2);
              if (cls.length) part += '.' + cls.join('.');

              // Khi cùng tag/class, thêm nth-of-type để giảm mơ hồ.
              const parent = cur.parentElement;
              if (parent) {
                const sameTag = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
                if (sameTag.length > 1) {
                  const idx = sameTag.indexOf(cur) + 1;
                  part += `:nth-of-type(${idx})`;
                }
              }

              parts.unshift(part);
              cur = cur.parentElement;
              depth += 1;
            }

            return parts.join(' > ');
          }

          function pickText(el) {
            if (!el) return '';
            const txt = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
            if (!txt) return '';
            return txt.slice(0, 180);
          }

          function pushEvent(type, el) {
            try {
              const rect = el && el.getBoundingClientRect ? el.getBoundingClientRect() : null;
              const payload = {
                ts: Date.now(),
                iso: new Date().toISOString(),
                type,
                tag: (el && el.tagName ? el.tagName.toLowerCase() : ''),
                selector: buildSelector(el),
                text: pickText(el),
                ariaLabel: (el && el.getAttribute ? (el.getAttribute('aria-label') || '') : ''),
                role: (el && el.getAttribute ? (el.getAttribute('role') || '') : ''),
                nameAttr: (el && el.getAttribute ? (el.getAttribute('name') || '') : ''),
                className: (el && el.className ? el.className.toString().slice(0, 220) : ''),
                valuePreview: (el && 'value' in el ? String(el.value || '').slice(0, 180) : ''),
                rect: rect ? {
                  x: Math.round(rect.x),
                  y: Math.round(rect.y),
                  w: Math.round(rect.width),
                  h: Math.round(rect.height)
                } : null,
                url: location.href,
              };

              window.__klingUiEvents.push(payload);
              if (window.__klingUiEvents.length > MAX_EVENTS) {
                window.__klingUiEvents = window.__klingUiEvents.slice(-MAX_EVENTS);
              }
            } catch (_) {}
          }

          // Bắt click và change ở capture phase để không phụ thuộc framework.
          window.addEventListener('click', (ev) => pushEvent('click', ev.target), true);
          window.addEventListener('change', (ev) => pushEvent('change', ev.target), true);
        })();
        """
    )


async def snapshot_interactive_elements(page: Page) -> dict[str, Any]:
    """
    Snapshot nhanh các element chính để map selector automation:
    - button
    - input/textarea/contenteditable
    - video
    """
    js = r"""
    () => {
      function buildSelector(el) {
        if (!el || el.nodeType !== 1) return '';
        if (el.id) return `#${el.id}`;

        const parts = [];
        let cur = el;
        let depth = 0;
        while (cur && cur.nodeType === 1 && depth < 5) {
          let part = cur.tagName.toLowerCase();
          const cls = (cur.className || '').toString().trim().split(/\s+/).filter(Boolean).slice(0, 2);
          if (cls.length) part += '.' + cls.join('.');
          const parent = cur.parentElement;
          if (parent) {
            const sameTag = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
            if (sameTag.length > 1) {
              part += `:nth-of-type(${sameTag.indexOf(cur) + 1})`;
            }
          }
          parts.unshift(part);
          cur = cur.parentElement;
          depth += 1;
        }
        return parts.join(' > ');
      }

      function one(el) {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        if (!r || r.width <= 0 || r.height <= 0) return null;
        return {
          tag: el.tagName.toLowerCase(),
          selector: buildSelector(el),
          text: ((el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).slice(0, 180),
          ariaLabel: (el.getAttribute('aria-label') || '').slice(0, 180),
          role: (el.getAttribute('role') || '').slice(0, 80),
          nameAttr: (el.getAttribute('name') || '').slice(0, 80),
          typeAttr: (el.getAttribute('type') || '').slice(0, 40),
          className: (el.className || '').toString().slice(0, 180),
          rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
        };
      }

      const selector = 'button, input, textarea, [contenteditable="true"], video, [role="button"], [role="textbox"]';
      const nodes = Array.from(document.querySelectorAll(selector));
      const out = [];
      for (const n of nodes) {
        const row = one(n);
        if (row) out.push(row);
      }

      // Dedupe tương đối theo selector + text để file debug gọn hơn.
      const dedupe = [];
      const seen = new Set();
      for (const row of out) {
        const key = `${row.selector}__${row.text}`;
        if (seen.has(key)) continue;
        seen.add(key);
        dedupe.push(row);
      }

      return {
        ts: Date.now(),
        iso: new Date().toISOString(),
        url: location.href,
        total: dedupe.length,
        elements: dedupe.slice(0, 350),
      };
    }
    """

    try:
        return await page.evaluate(js)
    except Exception as exc:
        return {
            "ts": now_ms(),
            "iso": ts_iso(),
            "url": page.url,
            "total": 0,
            "elements": [],
            "error": str(exc),
        }


async def read_ui_probe_events(page: Page) -> list[dict[str, Any]]:
    """Lấy event UI từ browser và reset buffer để tránh trùng lặp."""
    js = """
    () => {
      const arr = Array.isArray(window.__klingUiEvents) ? window.__klingUiEvents : [];
      window.__klingUiEvents = [];
      return arr;
    }
    """
    try:
        events = await page.evaluate(js)
        if isinstance(events, list):
            return events
        return []
    except Exception:
        return []


async def flush_periodic_debug(page: Page, state: CaptureState, stop_event: asyncio.Event) -> None:
    """
    Worker nền:
    - Mỗi 2 giây lấy event UI mới.
    - Mỗi 10 giây snapshot element một lần.
    """
    last_snapshot_ms = 0
    while not stop_event.is_set():
        try:
            ui_events = await read_ui_probe_events(page)
            for ev in ui_events:
                ev["kind"] = "ui"
                state.ui_events.append(ev)

            now = now_ms()
            if now - last_snapshot_ms >= 10_000:
                snap = await snapshot_interactive_elements(page)
                state.element_snapshots.append(snap)
                last_snapshot_ms = now
                print(f"[{ts_iso()}] [ELEMENT] Snapshot: {snap.get('total', 0)} element @ {snap.get('url', '')}")

        except Exception as exc:
            print(f"[{ts_iso()}] [WARN] flush_periodic_debug error: {exc}")

        await asyncio.sleep(2)


def build_readable_timeline(state: CaptureState) -> list[str]:
    """
    Dựng timeline text dễ đọc:
    - dòng request
    - dòng response
    - dòng lỗi request
    - dòng UI click/change
    """
    rows: list[tuple[int, str]] = []

    for ev in state.network_events:
        ts = ev.get("iso", "")
        ts_ms = int(ev.get("ts", 0) or 0)
        url = str(ev.get("url", ""))
        if len(url) > 150:
            url = url[:150] + "..."

        if ev.get("type") == "request":
            body_preview = compact_text(ev.get("post_data"), 220)
            line = (
                f"[{ts}] REQUEST method={ev.get('method','')} status=- url={url}"
                + (f" | body={body_preview}" if body_preview else "")
            )
            rows.append((ts_ms, line))

        elif ev.get("type") == "response":
            body_preview = compact_text(ev.get("body"), 220)
            line = (
                f"[{ts}] RESPONSE status={ev.get('status','')} url={url}"
                + (f" | body={body_preview}" if body_preview else "")
            )
            rows.append((ts_ms, line))

        elif ev.get("type") == "request_failed":
            line = (
                f"[{ts}] REQUEST_FAILED method={ev.get('method','')} url={url} "
                f"| error={ev.get('failure_text','')}"
            )
            rows.append((ts_ms, line))

    for ev in state.ui_events:
        ts = ev.get("iso", "")
        ts_ms = int(ev.get("ts", 0) or 0)
        action = ev.get("type", "")
        selector = str(ev.get("selector", ""))
        text = compact_text(ev.get("text", ""), 120)
        line = f"[{ts}] UI_{action.upper()} selector={selector}"
        if text:
            line += f" | text={text}"
        rows.append((ts_ms, line))

    rows.sort(key=lambda x: x[0])
    return [line for _, line in rows]


def write_session_files(state: CaptureState) -> dict[str, str]:
    """Ghi toàn bộ file debug của session ra ổ đĩa."""
    payload = {
        "generated_at": ts_iso(),
        "home_url": HOME_URL,
        "profile_dir": PROFILE_DIR,
        "capture_keys": CAPTURE_KEYS,
        "network_events_count": len(state.network_events),
        "ui_events_count": len(state.ui_events),
        "element_snapshots_count": len(state.element_snapshots),
        "submitted_tasks_count": len(state.submitted_tasks),
        "completed_videos_count": len(state.completed_videos),
        "network_events": state.network_events,
        "ui_events": state.ui_events,
        "element_snapshots": state.element_snapshots,
        "submitted_tasks": state.submitted_tasks,
        "completed_videos": state.completed_videos,
    }

    out_all = SESSION_DIR / "kling_capture_all.json"
    out_network = SESSION_DIR / "kling_network_events.json"
    out_ui = SESSION_DIR / "kling_ui_events.json"
    out_elements = SESSION_DIR / "kling_element_snapshots.json"
    out_task_summary = SESSION_DIR / "kling_task_summary.json"
    out_timeline = SESSION_DIR / "kling_timeline_readable.txt"

    out_all.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_network.write_text(
        json.dumps(state.network_events, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    out_ui.write_text(json.dumps(state.ui_events, ensure_ascii=False, indent=2), encoding="utf-8")
    out_elements.write_text(
        json.dumps(state.element_snapshots, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    out_task_summary.write_text(
        json.dumps(
            {
                "submitted_tasks": state.submitted_tasks,
                "completed_videos": state.completed_videos,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    timeline_lines = build_readable_timeline(state)
    out_timeline.write_text("\n".join(timeline_lines), encoding="utf-8")

    return {
        "all": str(out_all),
        "network": str(out_network),
        "ui": str(out_ui),
        "elements": str(out_elements),
        "task_summary": str(out_task_summary),
        "timeline": str(out_timeline),
    }


async def main() -> None:
    """Luồng chạy chính: mở Chrome, bắt log realtime, chờ bạn thao tác, rồi ghi file."""
    ensure_debug_dirs()

    # State runtime dùng xuyên suốt phiên bắt dữ liệu.
    state = CaptureState(
        network_events=[],
        ui_events=[],
        element_snapshots=[],
        submitted_tasks=[],
        completed_videos=[],
    )

    print("=" * 78)
    print("  KLING VIDEO DEBUG CAPTURE")
    print(f"  Profile riêng: {PROFILE_DIR}")
    print(f"  URL mở mặc định: {HOME_URL}")
    print(f"  Session log: {SESSION_DIR}")
    print("=" * 78)
    print()
    print("Bạn thao tác thủ công trên Chrome:")
    print("1) Login Kling")
    print("2) Tạo 1 video mẫu")
    if KEEP_OPEN_FOR_LIVE_TEST:
        print("3) Chrome sẽ luôn mở để bạn test liên tục")
        print("4) Enter/save để lưu nhanh log, gõ quit để kết thúc")
    else:
        print("3) Quay lại terminal nhấn Enter để kết thúc capture")
    print()

    Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            channel="chrome",
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            ignore_default_args=["--enable-automation"],
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
            record_har_path=str(SESSION_DIR / "kling_capture.har"),
        )

        # Cài probe UI từ đầu để page nào cũng bắt được click/change.
        await install_ui_probe(context)

        page = await context.new_page()

        # Dedupe nhẹ với stream chunk để tránh log quá dày (nhất là status 206).
        seen_stream_chunk_urls: set[str] = set()
        seen_stream_chunk_request_urls: set[str] = set()
        # Theo dõi task mới vừa submit trong phiên hiện tại để tránh bắt nhầm video cũ.
        tracked_submit_task_ids: set[int] = set()
        completed_video_keys: set[str] = set()

        # ── Network listeners ────────────────────────────────────────────────
        async def on_request(req):
            if not looks_like_relevant_url(req.url, req.resource_type, req.method):
                return

            # Chunk media lặp rất nhiều lần; chỉ log request 1 lần mỗi URL base.
            low_url = (req.url or "").lower()
            url_base = low_url.split("?", 1)[0]
            if (
                req.resource_type == "media"
                and req.method.upper() == "GET"
                and any(k in low_url for k in ("video_mps_multiple_bitrate", ".m4s", ".ts", "segment"))
            ):
                if url_base in seen_stream_chunk_request_urls:
                    return
                seen_stream_chunk_request_urls.add(url_base)

            post_data = None
            try:
                post_data = safe_json_parse(req.post_data or "")
            except Exception:
                post_data = None

            event = {
                "ts": now_ms(),
                "iso": ts_iso(),
                "type": "request",
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
                "post_data": truncate_for_json(post_data),
            }
            state.network_events.append(event)

            preview = compact_text(event.get("post_data"), TERMINAL_BODY_PREVIEW_LIMIT)
            print(f"[{event['iso']}] [REQ] {req.method:<6} {req.url}")
            if preview:
                print(f"    post_data: {preview}")

        async def on_response(resp):
            req = resp.request
            if not looks_like_relevant_url(resp.url, req.resource_type, req.method):
                return

            # Với chunk streaming lặp lại liên tục (vd: video_mps_multiple_bitrate),
            # chỉ giữ 1 lần mỗi URL base để timeline dễ đọc.
            low_url = (resp.url or "").lower()
            url_base = low_url.split("?", 1)[0]
            if (
                req.resource_type == "media"
                and resp.status in {200, 206}
                and any(k in low_url for k in ("video_mps_multiple_bitrate", ".m4s", ".ts", "segment"))
            ):
                if url_base in seen_stream_chunk_urls:
                    return
                seen_stream_chunk_urls.add(url_base)

            body: Any = None
            content_type = ""
            try:
                content_type = (resp.headers.get("content-type") or "").lower()
            except Exception:
                content_type = ""

            try:
                # Ưu tiên JSON để đọc API rõ ràng.
                if "json" in content_type or "graphql" in resp.url.lower():
                    body = await resp.json()
                elif "text" in content_type:
                    body = await resp.text()
                else:
                    # Với media/video binary thì chỉ log kích thước + content-type.
                    raw = await resp.body()
                    body = {
                        "_binary": True,
                        "content_type": content_type,
                        "bytes": len(raw or b""),
                    }
            except Exception as exc:
                body = {"_read_error": str(exc), "content_type": content_type}

            event = {
                "ts": now_ms(),
                "iso": ts_iso(),
                "type": "response",
                "status": resp.status,
                "url": resp.url,
                "resource_type": req.resource_type,
                "content_type": content_type,
                "body": truncate_for_json(body),
            }
            state.network_events.append(event)

            # Parse riêng cho submit/status để map đúng output của task mới nhất.
            low_resp_url = str(resp.url or "").lower()
            if isinstance(body, dict):
                # 1) Ghi nhận task_id ngay khi submit thành công.
                if "/api/task/submit" in low_resp_url:
                    task = ((body.get("data") or {}).get("task") or {})
                    task_id = int(task.get("id", 0) or 0)
                    if task_id > 0:
                        task_info = (task.get("taskInfo") or {})
                        task_args = task_info.get("arguments") or []
                        duration = _extract_argument_value(task_args, "duration", "")
                        image_count = _extract_argument_value(task_args, "imageCount", "")
                        prompt_preview = str(
                            _extract_argument_value(task_args, "prompt", "")
                            or _extract_argument_value(task_args, "multi_shots_prompt", "")
                            or ""
                        )[:180]

                        tracked_submit_task_ids.add(task_id)
                        state.submitted_tasks.append(
                            {
                                "captured_at": event["iso"],
                                "task_id": task_id,
                                "submit_http_status": event["status"],
                                "task_status": task.get("status", ""),
                                "duration": duration,
                                "image_count": image_count,
                                "prompt_preview": prompt_preview,
                            }
                        )
                        print(
                            f"[{event['iso']}] [TASK] submit task_id={task_id} "
                            f"duration={duration} image_count={image_count}"
                        )

                # 2) Tại task/status: chỉ lấy video có taskId thuộc danh sách submit mới.
                if "/api/task/status" in low_resp_url:
                    data = body.get("data") or {}
                    task_obj = data.get("task") or {}
                    task_id = int(task_obj.get("id", 0) or 0)
                    task_status = int(data.get("status", task_obj.get("status", 0)) or 0)
                    works = data.get("works") or []
                    if not isinstance(works, list):
                        works = []

                    for work in works:
                        if not isinstance(work, dict):
                            continue
                        output_url = str(((work.get("resource") or {}).get("resource") or "")).strip()
                        if not output_url or ".mp4" not in output_url.lower():
                            continue

                        work_task_id = int(work.get("taskId", 0) or 0)
                        owner_task_id = work_task_id or task_id
                        if owner_task_id <= 0 or owner_task_id not in tracked_submit_task_ids:
                            continue

                        dedupe_key = f"{owner_task_id}::{output_url}"
                        if dedupe_key in completed_video_keys:
                            continue
                        completed_video_keys.add(dedupe_key)

                        width = int(((work.get("resource") or {}).get("width", 0)) or 0)
                        height = int(((work.get("resource") or {}).get("height", 0)) or 0)
                        duration_ms = int(((work.get("resource") or {}).get("duration", 0)) or 0)
                        complete_row = {
                            "captured_at": event["iso"],
                            "task_id": owner_task_id,
                            "task_status": task_status,
                            "work_id": int(work.get("workId", 0) or 0),
                            "creative_id": int(work.get("creativeId", 0) or 0),
                            "output_url": output_url,
                            "width": width,
                            "height": height,
                            "duration_ms": duration_ms,
                            "download_path": "",
                            "download_ok": False,
                            "download_error": "",
                        }

                        # Tự tải file output theo đúng task mới nếu bật flag.
                        if AUTO_DOWNLOAD_COMPLETED_VIDEO:
                            guessed_sec = int(round(duration_ms / 1000.0)) if duration_ms > 0 else 0
                            fname = _safe_filename_text(
                                f"task_{owner_task_id}_work_{complete_row['work_id']}_{guessed_sec}s.mp4",
                                fallback=f"task_{owner_task_id}.mp4",
                            )
                            out_path = DOWNLOAD_DIR / fname
                            ok, err = _download_video_file(output_url, out_path)
                            complete_row["download_path"] = str(out_path)
                            complete_row["download_ok"] = ok
                            complete_row["download_error"] = err

                        state.completed_videos.append(complete_row)
                        print(
                            f"[{event['iso']}] [VIDEO_DONE] task_id={owner_task_id} "
                            f"duration_ms={duration_ms} url={output_url[:120]}"
                        )

                # 3) Nhiều phiên Kling trả kết quả completed qua endpoint feeds.
                # Bắt thêm để không bỏ sót video mới khi status poll không có works.
                if "/api/user/works/personal/feeds" in low_resp_url:
                    data = body.get("data") or {}
                    history = data.get("history") or []
                    if not isinstance(history, list):
                        history = []

                    feed_works: list[dict[str, Any]] = []
                    for item in history:
                        if not isinstance(item, dict):
                            continue
                        works = item.get("works") or []
                        if not isinstance(works, list):
                            continue
                        for work in works:
                            if isinstance(work, dict):
                                feed_works.append(work)

                    for work in feed_works:
                        output_url = str(((work.get("resource") or {}).get("resource") or "")).strip()
                        if not output_url or ".mp4" not in output_url.lower():
                            continue

                        owner_task_id = int(work.get("taskId", 0) or 0)
                        if owner_task_id <= 0 or owner_task_id not in tracked_submit_task_ids:
                            continue

                        dedupe_key = f"{owner_task_id}::{output_url}"
                        if dedupe_key in completed_video_keys:
                            continue
                        completed_video_keys.add(dedupe_key)

                        width = int(((work.get("resource") or {}).get("width", 0)) or 0)
                        height = int(((work.get("resource") or {}).get("height", 0)) or 0)
                        duration_ms = int(((work.get("resource") or {}).get("duration", 0)) or 0)
                        complete_row = {
                            "captured_at": event["iso"],
                            "task_id": owner_task_id,
                            "task_status": int(work.get("status", 0) or 0),
                            "work_id": int(work.get("workId", 0) or 0),
                            "creative_id": int(work.get("creativeId", 0) or 0),
                            "output_url": output_url,
                            "width": width,
                            "height": height,
                            "duration_ms": duration_ms,
                            "download_path": "",
                            "download_ok": False,
                            "download_error": "",
                        }

                        if AUTO_DOWNLOAD_COMPLETED_VIDEO:
                            guessed_sec = int(round(duration_ms / 1000.0)) if duration_ms > 0 else 0
                            fname = _safe_filename_text(
                                f"task_{owner_task_id}_work_{complete_row['work_id']}_{guessed_sec}s.mp4",
                                fallback=f"task_{owner_task_id}.mp4",
                            )
                            out_path = DOWNLOAD_DIR / fname
                            ok, err = _download_video_file(output_url, out_path)
                            complete_row["download_path"] = str(out_path)
                            complete_row["download_ok"] = ok
                            complete_row["download_error"] = err

                        state.completed_videos.append(complete_row)
                        print(
                            f"[{event['iso']}] [VIDEO_DONE_FEEDS] task_id={owner_task_id} "
                            f"duration_ms={duration_ms} url={output_url[:120]}"
                        )

            preview = compact_text(event.get("body"), TERMINAL_BODY_PREVIEW_LIMIT)
            print(f"[{event['iso']}] [RES] {resp.status:<3} {resp.url}")
            if preview:
                print(f"    body: {preview}")

        async def on_request_failed(req):
            if not looks_like_relevant_url(req.url, req.resource_type, req.method):
                return

            failure_text = ""
            try:
                failure_text = (req.failure or {}).get("errorText", "")
            except Exception:
                failure_text = ""

            event = {
                "ts": now_ms(),
                "iso": ts_iso(),
                "type": "request_failed",
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
                "failure_text": failure_text,
            }
            state.network_events.append(event)
            print(f"[{event['iso']}] [FAIL] {req.method:<6} {req.url} | {failure_text}")

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)

        # Mở trang Kling mặc định.
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=120_000)
        print(f"[{ts_iso()}] Đã mở trang: {page.url}")

        # Tạo snapshot đầu phiên để có baseline element.
        first_snap = await snapshot_interactive_elements(page)
        state.element_snapshots.append(first_snap)
        print(f"[{ts_iso()}] [ELEMENT] Snapshot đầu phiên: {first_snap.get('total', 0)} element")

        # Chạy worker nền để lấy event UI + snapshot định kỳ.
        stop_event = asyncio.Event()
        bg_task = asyncio.create_task(flush_periodic_debug(page, state, stop_event))

        loop = asyncio.get_running_loop()
        if KEEP_OPEN_FOR_LIVE_TEST:
            print()
            print("Lệnh terminal:")
            print("- Enter hoặc save: lưu nhanh log, KHÔNG đóng Chrome")
            print("- quit: lưu log và đóng Chrome")
            while True:
                try:
                    cmd = await loop.run_in_executor(None, input, "\n>>> [save/quit] ")
                except (KeyboardInterrupt, EOFError):
                    print(f"[{ts_iso()}] Nhận tín hiệu dừng từ terminal, tiến hành kết thúc phiên...")
                    break

                cmd = (cmd or "").strip().lower()
                if cmd in {"", "save", "s"}:
                    paths_live = write_session_files(state)
                    print(
                        f"[{ts_iso()}] Đã lưu nhanh log: network={len(state.network_events)} "
                        f"ui={len(state.ui_events)} submitted={len(state.submitted_tasks)} "
                        f"completed={len(state.completed_videos)}"
                    )
                    print(f"  - task summary: {paths_live['task_summary']}")
                    continue
                if cmd in {"quit", "q", "exit"}:
                    break
                print("Lệnh không hợp lệ. Dùng Enter/save hoặc quit.")
        else:
            try:
                await loop.run_in_executor(
                    None,
                    input,
                    "\n>>> Hoàn tất tạo video mẫu trên Kling -> Nhấn Enter để lưu log: ",
                )
            except (KeyboardInterrupt, EOFError):
                print(f"[{ts_iso()}] Nhận tín hiệu dừng từ terminal, tiến hành lưu log hiện có...")

        # Dừng worker nền và flush lần cuối.
        stop_event.set()
        await asyncio.sleep(0.4)
        if not bg_task.done():
            bg_task.cancel()
            try:
                await bg_task
            except asyncio.CancelledError:
                pass

        # Snapshot cuối phiên để so sánh sau thao tác tạo video.
        final_snap = await snapshot_interactive_elements(page)
        state.element_snapshots.append(final_snap)
        print(f"[{ts_iso()}] [ELEMENT] Snapshot cuối phiên: {final_snap.get('total', 0)} element")

        # Đóng context để flush HAR ra file.
        await context.close()

    # Ghi file debug toàn phiên.
    paths = write_session_files(state)

    print()
    print("=" * 78)
    print("  CAPTURE HOÀN TẤT")
    print(f"  Network events: {len(state.network_events)}")
    print(f"  UI events     : {len(state.ui_events)}")
    print(f"  Snapshots     : {len(state.element_snapshots)}")
    print("  File kết quả:")
    print(f"    - ALL      : {paths['all']}")
    print(f"    - NETWORK  : {paths['network']}")
    print(f"    - UI       : {paths['ui']}")
    print(f"    - ELEMENTS : {paths['elements']}")
    print(f"    - TASK_SUM : {paths['task_summary']}")
    print(f"    - TIMELINE : {paths['timeline']}")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())

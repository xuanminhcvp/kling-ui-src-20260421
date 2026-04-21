#!/usr/bin/env python3
"""
Debug phần UI setting của Kling mà KHÔNG tạo video.

Mục tiêu:
1) Mở Chrome profile riêng để giữ session đăng nhập.
2) Bắt thao tác UI (click/change/input) khi bạn đổi setting trên giao diện.
3) Bắt request/response liên quan setting để biết request gửi gì, response trả gì.
4) Chặn nút Generate mặc định để tránh bấm nhầm gây tốn tiền.

Cách chạy:
  python3 capture_kling_settings_debug.py

Biến môi trường:
  KLING_SETTINGS_PROFILE_DIR        : thư mục profile chrome riêng (mặc định: chrome_profiles/kling_settings_debug_profile)
  KLING_SETTINGS_HOME_URL           : URL mở mặc định (mặc định: https://app.klingai.com/)
  KLING_SETTINGS_BLOCK_GENERATE     : 1/0 (mặc định 1, nghĩa là CHẶN generate)
  KLING_SETTINGS_VERBOSE_TERMINAL   : 1/0 (mặc định 1, in log dễ đọc ra terminal)
  KLING_SETTINGS_AUTO_SCENARIO      : 1/0 (mặc định 0, tự chạy kịch bản setting mẫu)
  KLING_SETTINGS_SCENARIO_IMAGE     : đường dẫn ảnh local dùng cho kịch bản setting mẫu
"""

from __future__ import annotations

import asyncio
from collections import Counter
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.async_api import BrowserContext, Page, async_playwright


# =========================
# Cấu hình cơ bản
# =========================
SCRIPT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = Path(
    os.environ.get(
        "KLING_SETTINGS_PROFILE_DIR",
        str(SCRIPT_DIR / "chrome_profiles" / "kling_settings_debug_profile"),
    )
)
HOME_URL = os.environ.get("KLING_SETTINGS_HOME_URL", "https://app.klingai.com/")
BLOCK_GENERATE = os.environ.get("KLING_SETTINGS_BLOCK_GENERATE", "1").strip().lower() in {
    "1",
    "true",
    "yes",
}
VERBOSE_TERMINAL = os.environ.get("KLING_SETTINGS_VERBOSE_TERMINAL", "1").strip().lower() in {
    "1",
    "true",
    "yes",
}
DEEP_UI_DEBUG = os.environ.get("KLING_SETTINGS_DEEP_UI_DEBUG", "1").strip().lower() in {
    "1",
    "true",
    "yes",
}
AUTO_SELF_TEST = os.environ.get("KLING_SETTINGS_AUTO_SELF_TEST", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}
AUTO_SCENARIO = os.environ.get("KLING_SETTINGS_AUTO_SCENARIO", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}
SCENARIO_IMAGE_PATH = os.environ.get(
    "KLING_SETTINGS_SCENARIO_IMAGE",
    str(SCRIPT_DIR / "output_images" / "image1.png"),
).strip()
SCENARIO_VIDEO_URL = os.environ.get("KLING_SETTINGS_SCENARIO_VIDEO_URL", "").strip()
SCENARIO_KEEP_OPEN = os.environ.get("KLING_SETTINGS_SCENARIO_KEEP_OPEN", "1").strip().lower() in {
    "1",
    "true",
    "yes",
}

# Mỗi lần chạy tạo 1 thư mục debug riêng để không đè dữ liệu cũ.
SESSION_DIR = SCRIPT_DIR / "debug_sessions" / f"kling_settings_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
SESSION_DIR.mkdir(parents=True, exist_ok=True)


# =========================
# Hàm tiện ích
# =========================
def ts_iso() -> str:
    """Trả timestamp dạng ISO để đọc log theo thời gian thực dễ hơn."""
    return datetime.now().isoformat(timespec="milliseconds")


def now_ms() -> int:
    """Trả timestamp milliseconds để sort timeline chính xác."""
    return int(time.time() * 1000)


def compact_text(value: Any, limit: int = 260) -> str:
    """
    Rút gọn text để in terminal không bị quá dài.
    - Nếu là dict/list sẽ stringify thành JSON 1 dòng.
    - Nếu dài hơn limit thì cắt và thêm dấu ...
    """
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def safe_json_parse(raw: str) -> Any:
    """Parse JSON an toàn; lỗi thì trả text gốc."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def build_ui_selector_candidates(ui_events: list[dict[str, Any]], top_n: int = 40) -> dict[str, Any]:
    """
    Tổng hợp các selector quan trọng từ log UI để chọn locator ổn định.
    Trả về:
    - selector phổ biến
    - clickable selector phổ biến
    - combo (selector + text)
    """
    selector_counter: Counter[str] = Counter()
    clickable_counter: Counter[str] = Counter()
    combo_counter: Counter[str] = Counter()

    for ev in ui_events:
        if (ev.get("type") or "").lower() != "click":
            continue
        sel = str(ev.get("selector") or "").strip()
        clickable = str(ev.get("closestClickableSelector") or "").strip()
        text = compact_text(ev.get("text", ""), limit=60)

        if sel:
            selector_counter[sel] += 1
        if clickable:
            clickable_counter[clickable] += 1
        if sel:
            combo_counter[f"{sel} | text={text}"] += 1

    return {
        "top_selector": selector_counter.most_common(top_n),
        "top_clickable_selector": clickable_counter.most_common(top_n),
        "top_selector_text_combo": combo_counter.most_common(top_n),
    }


def parse_url_info(url: str) -> dict[str, Any]:
    """
    Tách URL để debug dễ đọc:
    - host
    - path
    - query params
    """
    try:
        p = urlparse(url)
        return {
            "host": p.hostname or "",
            "path": p.path or "",
            "query": {k: v for k, v in parse_qs(p.query).items()},
        }
    except Exception:
        return {"host": "", "path": "", "query": {}}


def is_relevant_setting_url(url: str, resource_type: str, method: str) -> bool:
    """
    Lọc request/response có khả năng liên quan setting UI.

    Ý tưởng:
    - Chỉ giữ domain Kling.
    - Giữ fetch/xhr/websocket (đa phần là API data).
    - Giữ request mutation (POST/PUT/PATCH/DELETE).
    - Bỏ file tĩnh (.js, .css, .png...) để log gọn.
    """
    low = (url or "").lower()
    if not low:
        return False

    host = (urlparse(url).hostname or "").lower()
    if not (
        host == "kling.ai"
        or host.endswith(".kling.ai")
        or host == "klingai.com"
        or host.endswith(".klingai.com")
    ):
        return False

    # Bỏ file tĩnh.
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
        ".ico",
        ".map",
    )
    if low.split("?", 1)[0].endswith(static_exts):
        return False

    rt = (resource_type or "").lower()
    m = (method or "").upper()

    if rt in {"xhr", "fetch", "websocket"}:
        return True

    if m in {"POST", "PUT", "PATCH", "DELETE"}:
        return True

    # Với mode setting-only, vẫn cho phép bắt endpoint chứa từ khóa setting/config/preferences.
    for key in ("setting", "config", "preference", "option", "profile", "task", "workflow"):
        if key in low:
            return True

    return False


async def install_ui_probe(page: Page, block_generate: bool, deep_ui_debug: bool) -> None:
    """
    Inject script vào browser để:
    - Bắt event click/change/input/focus.
    - Trả về selector dễ đọc để phục vụ automation sau này.
    - Tùy chọn chặn Generate nhằm tránh tạo video ngoài ý muốn.
    """
    block_generate_literal = "true" if block_generate else "false"
    deep_ui_debug_literal = "true" if deep_ui_debug else "false"
    script = """
        (() => {
          const blockGenerate = __BLOCK_GENERATE__;
          const deepUiDebug = __DEEP_UI_DEBUG__;
          if (window.__klingSettingsProbeInstalled) return;
          window.__klingSettingsProbeInstalled = true;
          window.__klingUiEvents = [];

          const BUILD_SELECTOR = (el) => {
            if (!el || !el.tagName) return '';
            const tag = el.tagName.toLowerCase();
            const id = el.id ? ('#' + el.id) : '';
            const cls = (el.className && typeof el.className === 'string')
              ? '.' + el.className.trim().split(/\\s+/).filter(Boolean).slice(0, 3).join('.')
              : '';
            const name = el.getAttribute && el.getAttribute('name') ? `[name="${el.getAttribute('name')}"]` : '';
            const role = el.getAttribute && el.getAttribute('role') ? `[role="${el.getAttribute('role')}"]` : '';
            return `${tag}${id}${name}${role}${cls}`;
          };

          const GET_TEXT = (el) => {
            const raw = (el && (el.innerText || el.textContent || '')) || '';
            return String(raw).replace(/\\s+/g, ' ').trim().slice(0, 220);
          };

          const GET_VALUE = (el) => {
            if (!el) return '';
            if ('value' in el) return String(el.value ?? '').slice(0, 260);
            return (el.getAttribute && (el.getAttribute('value') || el.getAttribute('aria-valuenow'))) || '';
          };

          const GET_RECT = (el) => {
            try {
              const r = el && el.getBoundingClientRect ? el.getBoundingClientRect() : null;
              if (!r) return null;
              return {
                x: Math.round(r.x || 0),
                y: Math.round(r.y || 0),
                width: Math.round(r.width || 0),
                height: Math.round(r.height || 0),
              };
            } catch (_) {
              return null;
            }
          };

          const PATH_SELECTOR = (el) => {
            try {
              let node = el;
              const segments = [];
              let guard = 0;
              while (node && node.nodeType === 1 && guard < 6) {
                const tag = (node.tagName || '').toLowerCase();
                if (!tag) break;
                if (node.id) {
                  segments.unshift(`${tag}#${node.id}`);
                  break;
                }
                const cls = (node.className && typeof node.className === 'string')
                  ? node.className.trim().split(/\\s+/).filter(Boolean).slice(0, 2).join('.')
                  : '';
                const part = cls ? `${tag}.${cls}` : tag;
                segments.unshift(part);
                node = node.parentElement;
                guard += 1;
              }
              return segments.join(' > ').slice(0, 360);
            } catch (_) {
              return '';
            }
          };

          const GET_VISIBLE_TOOLTIP_OPTIONS = () => {
            try {
              const popup = Array.from(document.querySelectorAll('[role="tooltip"],.el-popper,.el-tooltip'))
                .find((x) => {
                  const r = x.getBoundingClientRect();
                  return r && r.width > 0 && r.height > 0;
                });
              if (!popup) return [];
              const nodes = Array.from(popup.querySelectorAll('.inner,button,[role="button"],span,div'));
              const out = [];
              for (const n of nodes) {
                const txt = (n.innerText || n.textContent || '').replace(/\\s+/g, ' ').trim();
                if (!txt) continue;
                if (txt.length > 80) continue;
                out.push({
                  text: txt,
                  selector: BUILD_SELECTOR(n),
                  path: PATH_SELECTOR(n),
                });
                if (out.length >= 25) break;
              }
              return out;
            } catch (_) {
              return [];
            }
          };

          const IS_GENERATE_LIKE = (el) => {
            const txt = (GET_TEXT(el) + ' ' + (el?.getAttribute?.('aria-label') || '')).toLowerCase();
            if (!txt) return false;
            return txt.includes('generate') || txt.includes('create video') || txt.includes('tao video') || txt.includes('tạo video');
          };

          const capture = (type, e) => {
            const el = e?.target;
            if (!el) return;
            const clickable = el.closest ? (el.closest('button,[role="button"],a,[tabindex]') || null) : null;
            const shotBox = el.closest
              ? (el.closest('[class*="shot"],[class*="storyboard"],section,article,li') || null)
              : null;
            const pathNodes = (e && e.composedPath) ? e.composedPath() : [];
            const shortPath = Array.isArray(pathNodes)
              ? pathNodes
                  .filter((n) => n && n.tagName)
                  .slice(0, 6)
                  .map((n) => BUILD_SELECTOR(n))
              : [];

            const item = {
              ts: Date.now(),
              iso: new Date().toISOString(),
              type,
              selector: BUILD_SELECTOR(el),
              tag: (el.tagName || '').toLowerCase(),
              role: el.getAttribute ? (el.getAttribute('role') || '') : '',
              name: el.getAttribute ? (el.getAttribute('name') || '') : '',
              ariaLabel: el.getAttribute ? (el.getAttribute('aria-label') || '') : '',
              text: GET_TEXT(el),
              value: GET_VALUE(el),
              checked: (typeof el.checked === 'boolean') ? el.checked : null,
              url: location.href,
              pointer: {
                x: typeof e?.clientX === 'number' ? Math.round(e.clientX) : null,
                y: typeof e?.clientY === 'number' ? Math.round(e.clientY) : null,
              },
              rect: GET_RECT(el),
              pathSelector: PATH_SELECTOR(el),
              closestClickableSelector: BUILD_SELECTOR(clickable || el),
              closestClickableText: GET_TEXT(clickable || el),
              shotContainerSelector: BUILD_SELECTOR(shotBox),
              shotContainerText: GET_TEXT(shotBox),
              composedPathSelectors: shortPath,
            };

            if (deepUiDebug) {
              item.tooltipOptions = GET_VISIBLE_TOOLTIP_OPTIONS();
            }

            if (blockGenerate && type === 'click' && IS_GENERATE_LIKE(el)) {
              try {
                e.preventDefault();
                e.stopPropagation();
                e.stopImmediatePropagation();
              } catch (_) {}
              item.blockedGenerate = true;
              item.blockReason = 'blocked_by_settings_probe';
              item.note = 'Generate click đã bị chặn để tránh tạo video ngoài ý muốn';
            } else {
              item.blockedGenerate = false;
            }

            window.__klingUiEvents.push(item);
          };

          document.addEventListener('click', (e) => capture('click', e), true);
          document.addEventListener('change', (e) => capture('change', e), true);
          document.addEventListener('input', (e) => capture('input', e), true);
          document.addEventListener('focusin', (e) => capture('focusin', e), true);
        })();
        """
    script = script.replace("__BLOCK_GENERATE__", block_generate_literal)
    script = script.replace("__DEEP_UI_DEBUG__", deep_ui_debug_literal)
    await page.add_init_script(script=script)
    # Cài ngay trên trang hiện tại (nếu đã mở sẵn) để không cần chờ reload.
    try:
        await page.evaluate(script)
    except Exception:
        pass


async def read_ui_events(page: Page, offset: int) -> tuple[list[dict[str, Any]], int]:
    """
    Đọc các UI event mới từ phía browser.
    offset giúp chỉ lấy phần chưa đọc để tránh trùng dữ liệu.
    """
    rows = await page.evaluate(
        """
        (start) => {
          const all = window.__klingUiEvents || [];
          return { rows: all.slice(start), next: all.length };
        }
        """,
        offset,
    )
    return list(rows.get("rows") or []), int(rows.get("next") or offset)


async def run_auto_self_test_settings(page: Page) -> dict[str, Any]:
    """
    Tự test nhanh phần setting mà không generate:
    - Thử click một số nút setting phổ biến (duration, ratio, motion, mode).
    - Không đụng nút Generate.
    - Trả kết quả để ghi vào summary.
    """
    result: dict[str, Any] = {
        "ran": True,
        "clicked": [],
        "not_found": [],
        "errors": [],
    }

    # Danh sách text setting phổ biến để thử click.
    # Dùng theo text vì Kling thay class khá thường xuyên.
    candidate_texts = ["5s", "10s", "16:9", "9:16", "1:1", "Camera", "Motion", "Settings"]

    # Đợi tối đa ~30s để UI sau login ổn định.
    # Mỗi vòng sẽ thử click một lượt tất cả setting text.
    clicked_set: set[str] = set()
    for _ in range(10):
        for text in candidate_texts:
            if text in clicked_set:
                continue
            try:
                locator = page.get_by_role("button", name=text, exact=False).first
                if await locator.count() > 0 and await locator.is_visible(timeout=500):
                    await locator.click(timeout=1200)
                    await asyncio.sleep(0.22)
                    clicked_set.add(text)
                    result["clicked"].append(f"button:{text}")
                    continue
            except Exception:
                pass

            # Fallback: tìm phần tử chứa text rồi click nút cha gần nhất bằng JS.
            try:
                clicked = await page.evaluate(
                    """
                    (needle) => {
                      const lowNeedle = String(needle || '').toLowerCase();
                      const nodes = Array.from(document.querySelectorAll('button,[role=\"button\"],span,div'));
                      for (const n of nodes) {
                        const txt = ((n.innerText || n.textContent || '') + ' ' + (n.getAttribute?.('aria-label') || '')).toLowerCase();
                        if (!txt.includes(lowNeedle)) continue;
                        const btn = n.closest('button,[role=\"button\"]') || n;
                        const r = btn.getBoundingClientRect();
                        if (!r || r.width <= 0 || r.height <= 0) continue;
                        try { btn.click(); return true; } catch (_) {}
                      }
                      return false;
                    }
                    """,
                    text,
                )
                if clicked:
                    await asyncio.sleep(0.22)
                    clicked_set.add(text)
                    result["clicked"].append(f"text_fallback:{text}")
            except Exception as exc:
                result["errors"].append(f"text_fallback:{text}: {exc}")

        # Nếu đã click được ít nhất 3 setting thì coi như self-test pass cơ bản.
        if len(clicked_set) >= 3:
            break
        await asyncio.sleep(3.0)

    for text in candidate_texts:
        if text not in clicked_set:
            result["not_found"].append(text)

    return result


async def _click_by_text_js(page: Page, text: str) -> bool:
    """
    Click phần tử theo text bằng JavaScript.
    Dùng fallback khi locator chuẩn không bắt được do UI đổi class liên tục.
    """
    try:
        return bool(
            await page.evaluate(
                """
                (needle) => {
                  const lowNeedle = String(needle || '').toLowerCase();
                  const shortNeedle = lowNeedle.length <= 3; // ví dụ: "1", "2", "15s"
                  const vw = Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0);
                  const vh = Math.max(document.documentElement.clientHeight || 0, window.innerHeight || 0);
                  const maxArea = Math.max(1, vw * vh * 0.30); // loại container quá lớn (vd #app)

                  const nodes = Array.from(document.querySelectorAll('button,[role="button"],a,span,div'));
                  const scored = [];

                  for (const n of nodes) {
                    const raw = ((n.innerText || n.textContent || '') + ' ' + (n.getAttribute?.('aria-label') || '')).replace(/\\s+/g, ' ').trim();
                    const txt = raw.toLowerCase();
                    if (!txt.includes(lowNeedle)) continue;
                    // Tránh tuyệt đối click Generate do match số/text chung.
                    if (txt.includes('generate') || txt.includes('create video')) continue;

                    const btn = n.closest('button,[role="button"],a') || n;
                    const r = btn.getBoundingClientRect();
                    if (!r || r.width <= 0 || r.height <= 0) continue;
                    const area = r.width * r.height;
                    if (area > maxArea) continue;
                    if (raw.length > 140) continue;

                    // Với needle ngắn, yêu cầu match theo token chính xác để tránh nhiễu.
                    if (shortNeedle) {
                      const tokens = txt.split(/\\s+/).filter(Boolean);
                      if (!tokens.includes(lowNeedle)) continue;
                    }

                    let score = 0;
                    // Ưu tiên match chính xác text.
                    if (txt === lowNeedle) score += 10;
                    else if (txt.startsWith(lowNeedle)) score += 7;
                    else score += 4;

                    // Ưu tiên phần tử dạng button thật.
                    const tag = (btn.tagName || '').toLowerCase();
                    if (tag === 'button' || tag === 'a' || btn.getAttribute('role') === 'button') score += 5;

                    // Ưu tiên element nhỏ gọn hơn để tránh click container.
                    score += Math.max(0, 6 - Math.floor(Math.sqrt(area) / 30));

                    scored.push({ btn, score });
                  }

                  scored.sort((a, b) => b.score - a.score);
                  for (const item of scored) {
                    try { item.btn.click(); return true; } catch (_) {}
                  }
                  return false;
                }
                """,
                text,
            )
        )
    except Exception:
        return False


async def _set_first_file_input(page: Page, file_path: str) -> bool:
    """
    Gán file ảnh vào input[type=file] đầu tiên có trong DOM.
    """
    if not file_path:
        return False
    if not Path(file_path).exists():
        return False
    try:
        count = await page.locator("input[type='file']").count()
        if count <= 0:
            return False
        # Dùng input đầu tiên để tăng cơ hội thành công với component upload custom.
        await page.locator("input[type='file']").first.set_input_files(file_path, timeout=4000)
        return True
    except Exception:
        return False


async def _fill_textareas_three_shots(page: Page, shots: list[str]) -> int:
    """
    Điền 3 prompt vào các textarea/ô nhập shot đầu tiên tìm thấy trên UI.
    Trả về số ô đã điền thành công.
    """
    filled = 0
    if not shots:
        return filled
    try:
        count = await page.locator("textarea").count()
    except Exception:
        count = 0

    # Ưu tiên điền textarea thật trước.
    for i in range(min(count, len(shots))):
        try:
            loc = page.locator("textarea").nth(i)
            await loc.click(timeout=1200)
            await loc.fill(shots[i], timeout=1800)
            filled += 1
        except Exception:
            continue

    # Fallback cho input/textbox dùng contenteditable.
    if filled < len(shots):
        try:
            done = await page.evaluate(
                """
                (payload) => {
                  const shots = Array.isArray(payload) ? payload : [];
                  const boxes = Array.from(document.querySelectorAll('[contenteditable="true"],[role="textbox"]'));
                  let idx = 0;
                  for (const b of boxes) {
                    if (idx >= shots.length) break;
                    const r = b.getBoundingClientRect();
                    if (!r || r.width <= 0 || r.height <= 0) continue;
                    try {
                      b.focus();
                      if ('value' in b) {
                        b.value = shots[idx];
                      } else {
                        b.textContent = shots[idx];
                      }
                      b.dispatchEvent(new Event('input', { bubbles: true }));
                      b.dispatchEvent(new Event('change', { bubbles: true }));
                      idx += 1;
                    } catch (_) {}
                  }
                  return idx;
                }
                """,
                shots,
            )
            filled = max(filled, int(done or 0))
        except Exception:
            pass

    return filled


async def run_auto_setting_scenario(page: Page, image_path: str, video_url: str = "") -> dict[str, Any]:
    """
    Kịch bản setting mẫu theo yêu cầu:
    - Mở trang video.
    - Upload 1 ảnh local.
    - Bật Custom Multi-Shot.
    - Điền prompt 3 shot.
    - Chỉ dừng ở mức setting + pricing; tuyệt đối không generate.
    """
    result: dict[str, Any] = {
        "ran": True,
        "image_path": str(image_path),
        "image_exists": Path(image_path).exists() if image_path else False,
        "steps": [],
        "three_shot_prompts": [
            "prompt 1",
            "prompt 2",
            "prompt 3",
        ],
        "uploaded": False,
        "filled_shots": 0,
        "shot_duration_targets": [1, 2, 3],
        "shot_duration_applied": [],
        "three_shot_price_probe": {},
        "errors": [],
    }
    target_video_url = video_url or "https://kling.ai/app/video/new?ac=1"

    async def _ensure_video_composer(note: str) -> None:
        """
        Đảm bảo luôn đứng trong màn hình Video composer.
        Nếu bị lệch sang trang khác (vd /app/image), điều hướng lại ngay.
        """
        try:
            current = str(page.url or "")
        except Exception:
            current = ""
        if "/app/video" in current:
            return
        try:
            await page.goto(target_video_url, wait_until="domcontentloaded")
            await asyncio.sleep(1.0)
            result["steps"].append(f"recover_to_video:{note}")
        except Exception as exc:
            result["errors"].append(f"recover_to_video:{note}: {exc}")

    # 1) Vào trang video để đảm bảo đúng composer.
    try:
        await page.goto(target_video_url, wait_until="domcontentloaded")
        await asyncio.sleep(2.0)
        result["steps"].append(f"goto_video_page:{target_video_url}")
    except Exception as exc:
        result["errors"].append(f"goto_video_page: {exc}")

    # Chờ một nhịp để user session đồng bộ (tránh lúc vừa mở còn login=false).
    for _ in range(8):
        try:
            url_now = str(page.url or "")
        except Exception:
            url_now = ""
        if "/app/video" in url_now:
            break
        await asyncio.sleep(1.2)

    # 2) Cố mở đúng tab tạo video nếu cần.
    for key in ("Video Generation", "Text to Video", "Image to Video"):
        try:
            clicked = await _click_by_text_js(page, key)
            if clicked:
                result["steps"].append(f"click:{key}")
                await asyncio.sleep(0.8)
                break
        except Exception as exc:
            result["errors"].append(f"click:{key}: {exc}")
    await _ensure_video_composer("after_video_tab_click")

    # 3) Upload ảnh local.
    upload_clicked = False
    # Không click từ khóa "Image" để tránh nhảy sang tab Image Generation.
    for text in ("Upload", "Upload Image", "Reference", "Add image"):
        try:
            if await _click_by_text_js(page, text):
                upload_clicked = True
                result["steps"].append(f"click_upload_hint:{text}")
                await asyncio.sleep(0.5)
                break
        except Exception as exc:
            result["errors"].append(f"click_upload_hint:{text}: {exc}")

    try:
        uploaded = await _set_first_file_input(page, image_path)
        result["uploaded"] = bool(uploaded)
        if uploaded:
            result["steps"].append("upload_image_via_input_file")
        elif upload_clicked:
            result["steps"].append("upload_clicked_but_input_not_found")
        else:
            result["steps"].append("upload_input_not_found")
    except Exception as exc:
        result["errors"].append(f"upload_image: {exc}")

    await asyncio.sleep(1.6)
    await _ensure_video_composer("after_upload")

    # 4) Bật custom multi-shot.
    try:
        if await _click_by_text_js(page, "Custom Multi-Shot"):
            result["steps"].append("enable_custom_multi_shot")
            await asyncio.sleep(0.8)
    except Exception as exc:
        result["errors"].append(f"enable_custom_multi_shot: {exc}")
    await _ensure_video_composer("after_enable_multi_shot")

    # 4.1) Set thông số tổng trước (đúng thứ tự user yêu cầu):
    # mở panel "1080p · 6s · 1" -> chọn 15s -> chọn 1080p -> output 1.
    try:
        trigger = page.locator("div.setting-select.border-gradient-hover.el-tooltip__trigger").first
        if await trigger.count() > 0 and await trigger.is_visible(timeout=900):
            await trigger.click(timeout=1200)
            result["steps"].append("open_global_setting_panel")
            await asyncio.sleep(0.45)
    except Exception as exc:
        result["errors"].append(f"open_global_setting_panel: {exc}")

    async def _click_inner_option(label: str) -> bool:
        """
        Trong popup setting của Kling, option thường nằm ở `div.inner`.
        Hàm này ưu tiên click đúng vùng popup để tránh bấm nhầm nơi khác.
        """
        try:
            ok = await page.evaluate(
                """
                (label) => {
                  const low = String(label || '').toLowerCase();
                  const pop = Array.from(document.querySelectorAll('[role="tooltip"],.el-popper,.el-tooltip'))
                    .find(x => {
                      const r = x.getBoundingClientRect();
                      return r && r.width > 0 && r.height > 0;
                    });
                  if (!pop) return false;
                  const nodes = Array.from(pop.querySelectorAll('.inner,button,[role="button"],span,div'));
                  const exact = [];
                  const partial = [];
                  for (const n of nodes) {
                    const txtRaw = (n.innerText || n.textContent || '').replace(/\\s+/g, ' ').trim();
                    const txt = txtRaw.toLowerCase();
                    if (!txt) continue;
                    if (txt.includes('generate') || txt.includes('create video')) continue;
                    // Loại text quá dài để tránh click wrapper.
                    if (txtRaw.length > 32) continue;
                    if (txt === low) exact.push(n);
                    else if (txt.includes(low)) partial.push(n);
                  }
                  for (const n of exact.concat(partial)) {
                    try { n.click(); return true; } catch (_) {}
                  }
                  return false;
                }
                """,
                label,
            )
            return bool(ok)
        except Exception:
            return False

    # Bắt buộc set duration tổng 15 trước.
    global_duration_15_ok = False
    for text in ("15s", "1080p", "1"):
        try:
            if await _click_inner_option(text):
                result["steps"].append(f"global_setting:{text}")
                if text == "15s":
                    global_duration_15_ok = True
                await asyncio.sleep(0.45)
            else:
                # fallback theo text toàn trang nếu popup thay cấu trúc.
                if await _click_by_text_js(page, text):
                    result["steps"].append(f"global_setting_fallback:{text}")
                    if text == "15s":
                        global_duration_15_ok = True
                    await asyncio.sleep(0.45)
        except Exception as exc:
            result["errors"].append(f"global_setting:{text}: {exc}")

    # 4.2) Cố tạo shot thứ 3 nếu UI đang chỉ có 2 ô.
    try:
        # Chỉ thêm shot tới khi đủ 3, không bấm dư thành shot 4/5.
        for _ in range(4):
            textbox_count = int(
                await page.evaluate("() => document.querySelectorAll('[role=\"textbox\"],[contenteditable=\"true\"]').length")
            )
            if textbox_count >= 3:
                break
            # Ưu tiên selector đã bắt được thực tế từ log user thao tác.
            clicked_selector = False
            try:
                shot_btn = page.locator("button.generic-button.secondary.medium").filter(has_text="Shot").first
                if await shot_btn.count() > 0 and await shot_btn.is_visible(timeout=600):
                    await shot_btn.click(timeout=1200)
                    result["steps"].append("add_shot_click:button.generic-button.secondary.medium[text*=Shot]")
                    await asyncio.sleep(0.55)
                    clicked_selector = True
            except Exception:
                clicked_selector = False

            if clicked_selector:
                continue

            for t in ("Add Shot", "Add shot", "+ Shot"):
                clicked = await _click_by_text_js(page, t)
                if clicked:
                    result["steps"].append(f"add_shot_click:{t}")
                    await asyncio.sleep(0.55)
                    break
    except Exception as exc:
        result["errors"].append(f"ensure_third_shot: {exc}")

    # 5) Điền prompt 3 shot.
    try:
        filled = await _fill_textareas_three_shots(page, result["three_shot_prompts"])
        result["filled_shots"] = int(filled)
        result["steps"].append(f"fill_shots:{filled}")
    except Exception as exc:
        result["errors"].append(f"fill_shots: {exc}")

    # 6) Set thời gian từng shot sau khi đã set tổng duration=15s.
    # Mục tiêu: Shot1=1s, Shot2=2s, Shot3=3s.
    async def _set_single_shot_duration(shot_idx: int, seconds: int) -> bool:
        """
        Cố gắng set duration cho từng shot bằng đúng thứ tự:
        - Xác định block shot theo text Shot N / Storyboard N / prompt N.
        - Click control duration trong block shot.
        - Chọn option trong popup (dạng `div.inner`) với giá trị mong muốn.
        """
        # Tập pattern để bắt các biến thể text của shot block.
        patterns: list[re.Pattern[str]] = [
            re.compile(rf"Shot\s*{shot_idx}\b", re.IGNORECASE),
            re.compile(rf"Storyboard\s*{shot_idx}\b", re.IGNORECASE),
            re.compile(rf"prompt\s*{shot_idx}\b", re.IGNORECASE),
        ]

        # Các selector block thường gặp trên giao diện storyboard/shot.
        block_selectors = [
            "div[class*='storyboard']",
            "div[class*='shot']",
            "section[class*='storyboard']",
            "section[class*='shot']",
        ]
        target_label = f"{seconds}s"

        for pat in patterns:
            for block_sel in block_selectors:
                try:
                    block = page.locator(block_sel).filter(has_text=pat).first
                    if await block.count() == 0:
                        continue
                    if not await block.is_visible(timeout=900):
                        continue

                    # Tìm control duration trong từng block shot.
                    controls = [
                        "button:has-text('s')",
                        "div[role='button']:has-text('s')",
                        ".setting-select",
                        ".el-tooltip__trigger",
                    ]
                    clicked_control = False
                    for ctl in controls:
                        cand = block.locator(ctl).first
                        if await cand.count() > 0 and await cand.is_visible(timeout=600):
                            await cand.click(timeout=1200)
                            clicked_control = True
                            await asyncio.sleep(0.35)
                            break
                    if not clicked_control:
                        # Fallback: click text thời lượng phổ biến trong block (vd: 2s).
                        cand = block.locator("text=/\\b\\d+\\s*s\\b/i").first
                        if await cand.count() > 0 and await cand.is_visible(timeout=600):
                            await cand.click(timeout=1200)
                            clicked_control = True
                            await asyncio.sleep(0.35)

                    if not clicked_control:
                        continue

                    # Chọn duration đích trong popup.
                    if await _click_inner_option(target_label):
                        await asyncio.sleep(0.35)
                        return True
                    if await _click_inner_option(str(seconds)):
                        await asyncio.sleep(0.35)
                        return True
                except Exception:
                    continue

        # Fallback cuối: thử click text shot rồi chọn duration.
        try:
            if await _click_by_text_js(page, f"Shot {shot_idx}"):
                await asyncio.sleep(0.35)
                if await _click_inner_option(target_label):
                    await asyncio.sleep(0.35)
                    return True
                if await _click_inner_option(str(seconds)):
                    await asyncio.sleep(0.35)
                    return True
        except Exception:
            pass
        return False

    if not global_duration_15_ok:
        # Rule cứng theo yêu cầu: không set per-shot nếu chưa set được duration tổng = 15s.
        result["errors"].append("global_duration_15_not_confirmed: skip_per_shot_duration")
        result["steps"].append("skip_set_shot_duration_due_to_missing_global_15s")
    else:
        for i, sec in enumerate(result["shot_duration_targets"], start=1):
            try:
                ok = await _set_single_shot_duration(i, int(sec))
                result["shot_duration_applied"].append({"shot": i, "target": int(sec), "ok": bool(ok)})
                result["steps"].append(f"set_shot_duration:shot={i},value={sec},ok={ok}")
            except Exception as exc:
                result["shot_duration_applied"].append({"shot": i, "target": int(sec), "ok": False})
                result["errors"].append(f"set_shot_duration:shot={i}: {exc}")

    # 6.1) Click lại tổng duration/output để đồng bộ pricing preview sau khi set từng shot.
    for text in ("15s", "1"):
        try:
            # Mở lại panel global trước khi chọn option để tránh click nhầm element ngoài popup.
            trigger = page.locator("div.setting-select.border-gradient-hover.el-tooltip__trigger").first
            if await trigger.count() > 0 and await trigger.is_visible(timeout=700):
                await trigger.click(timeout=1000)
                await asyncio.sleep(0.3)
            if await _click_inner_option(text):
                result["steps"].append(f"refresh_global_setting:{text}")
                await asyncio.sleep(0.45)
        except Exception as exc:
            result["errors"].append(f"refresh_global_setting:{text}: {exc}")

    await asyncio.sleep(2.0)
    # 7) Gửi một request tính giá với payload 3-shot để xác nhận pipeline API 3 shot hoạt động.
    # Không gọi submit, nên không phát sinh generate.
    try:
        shot_payload = {
            "shots": [
                {"index": 1, "duration": 1, "prompt": result["three_shot_prompts"][0], "rich_prompt": result["three_shot_prompts"][0]},
                {"index": 2, "duration": 2, "prompt": result["three_shot_prompts"][1], "rich_prompt": result["three_shot_prompts"][1]},
                {"index": 3, "duration": 3, "prompt": result["three_shot_prompts"][2], "rich_prompt": result["three_shot_prompts"][2]},
            ]
        }
        body = {
            "type": "m2v_aio2video",
            "arguments": [
                {"name": "negative_prompt", "value": ""},
                {"name": "duration", "value": 15},
                {"name": "imageCount", "value": "1"},
                {"name": "kling_version", "value": "3.0"},
                {"name": "mode", "value": "1080p"},
                {"name": "resolution", "value": "1080p"},
                {"name": "customize_multi_shots", "value": "true"},
                {"name": "multi_shots_prompt", "value": json.dumps(shot_payload, ensure_ascii=False)},
                {"name": "cfg", "value": "0.5"},
            ],
        }
        probe = await page.evaluate(
            """
            async (payload) => {
              try {
                const resp = await fetch('/api/task/price', {
                  method: 'POST',
                  headers: { 'content-type': 'application/json' },
                  body: JSON.stringify(payload),
                  credentials: 'include',
                });
                const text = await resp.text();
                let data = null;
                try { data = JSON.parse(text); } catch (_) {}
                return { ok: resp.ok, status: resp.status, data };
              } catch (err) {
                return { ok: false, status: 0, error: String(err || '') };
              }
            }
            """,
            body,
        )
        result["three_shot_price_probe"] = probe
        result["steps"].append("post_task_price_three_shot_probe")
    except Exception as exc:
        result["errors"].append(f"three_shot_price_probe: {exc}")

    await asyncio.sleep(1.0)

    # Chụp thông tin chẩn đoán nhanh để debug selector nếu tự động chưa điền đủ.
    try:
        diag = await page.evaluate(
            """
            () => {
              return {
                url: location.href,
                fileInputs: document.querySelectorAll('input[type="file"]').length,
                textareas: document.querySelectorAll('textarea').length,
                textboxes: document.querySelectorAll('[role="textbox"],[contenteditable="true"]').length,
                title: document.title || ''
              };
            }
            """
        )
        result["diagnostic"] = diag
    except Exception:
        result["diagnostic"] = {}
    return result


async def main() -> None:
    # Bộ nhớ runtime để cuối phiên xuất file.
    network_events: list[dict[str, Any]] = []
    ui_events: list[dict[str, Any]] = []

    # Đường dẫn file output.
    file_network = SESSION_DIR / "settings_network_events.json"
    file_ui = SESSION_DIR / "settings_ui_events.json"
    file_timeline = SESSION_DIR / "settings_timeline_readable.txt"
    file_selector_candidates = SESSION_DIR / "settings_selector_candidates.json"
    file_summary = SESSION_DIR / "settings_summary.json"
    self_test_result: dict[str, Any] = {"ran": False}
    scenario_result: dict[str, Any] = {"ran": False}

    print(f"[INFO] Session debug: {SESSION_DIR}")
    print(f"[INFO] Profile chrome: {PROFILE_DIR}")
    print(f"[INFO] Home URL: {HOME_URL}")
    print(f"[INFO] Block generate: {BLOCK_GENERATE}")
    print(f"[INFO] Deep UI debug: {DEEP_UI_DEBUG}")
    print(f"[INFO] Auto self-test settings: {AUTO_SELF_TEST}")
    print(f"[INFO] Auto scenario settings: {AUTO_SCENARIO}")
    print(f"[INFO] Scenario image path: {SCENARIO_IMAGE_PATH}")
    if SCENARIO_VIDEO_URL:
        print(f"[INFO] Scenario video URL: {SCENARIO_VIDEO_URL}")
    print(f"[INFO] Scenario keep open: {SCENARIO_KEEP_OPEN}")

    async with async_playwright() as p:
        # Dùng Chromium persistent context để giữ cookie/session qua nhiều lần chạy.
        context: BrowserContext = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            viewport={"width": 1520, "height": 920},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            ignore_default_args=["--enable-automation"],
            record_har_path=str(SESSION_DIR / "settings_capture.har"),
            record_har_content="embed",
        )

        page: Page
        if context.pages:
            page = context.pages[0]
        else:
            page = await context.new_page()

        # Chặn cứng endpoint submit để dù bấm nhầm Generate cũng không tạo job.
        # Cách này là lớp an toàn bổ sung bên cạnh chặn click ở UI probe.
        async def block_generate_submit_route(route):
            req = route.request
            low = (req.url or "").lower()
            if "/api/task/submit" in low or "/task/submit" in low:
                payload = req.post_data or ""
                network_events.append(
                    {
                        "phase": "request_blocked",
                        "ts": now_ms(),
                        "iso": ts_iso(),
                        "method": req.method,
                        "resource_type": req.resource_type,
                        "url": req.url,
                        "url_info": parse_url_info(req.url),
                        "reason": "blocked_submit_in_settings_test",
                        "post_data_text": payload,
                        "post_data_json": safe_json_parse(payload),
                    }
                )
                if VERBOSE_TERMINAL:
                    print("[BLOCK] Submit API bị chặn:", compact_text(req.url))
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await page.route("**/*", block_generate_submit_route)

        await install_ui_probe(page, block_generate=BLOCK_GENERATE, deep_ui_debug=DEEP_UI_DEBUG)

        # Lưu index để chỉ đọc event mới từ lần poll trước.
        ui_offset = 0

        # Handler request: lưu payload để biết UI setting gửi lên API gì.
        async def on_request(req):
            url = req.url
            method = req.method
            rt = req.resource_type
            if not is_relevant_setting_url(url, rt, method):
                return

            try:
                post_data = req.post_data
            except Exception:
                # Một số request upload binary không decode UTF-8 được.
                post_data = ""
            post_json = safe_json_parse(post_data) if post_data else None

            item = {
                "phase": "request",
                "ts": now_ms(),
                "iso": ts_iso(),
                "method": method,
                "resource_type": rt,
                "url": url,
                "url_info": parse_url_info(url),
                "headers": req.headers,
                "post_data_text": post_data,
                "post_data_json": post_json,
            }
            network_events.append(item)

            if VERBOSE_TERMINAL:
                print(
                    "[REQ]",
                    method,
                    compact_text(item["url_info"].get("path", "")),
                    "| payload=",
                    compact_text(post_json if post_json is not None else post_data),
                )

        # Handler response: lưu status + body để biết server trả về gì sau khi đổi setting.
        async def on_response(res):
            req = res.request
            url = res.url
            method = req.method
            rt = req.resource_type
            if not is_relevant_setting_url(url, rt, method):
                return

            body_text = ""
            body_json = None
            try:
                text = await res.text()
                body_text = text
                body_json = safe_json_parse(text)
            except Exception:
                body_text = ""
                body_json = None

            item = {
                "phase": "response",
                "ts": now_ms(),
                "iso": ts_iso(),
                "status": res.status,
                "ok": res.ok,
                "method": method,
                "resource_type": rt,
                "url": url,
                "url_info": parse_url_info(url),
                "headers": dict(res.headers),
                "body_text": body_text,
                "body_json": body_json,
            }
            network_events.append(item)

            if VERBOSE_TERMINAL:
                print(
                    "[RES]",
                    res.status,
                    compact_text(item["url_info"].get("path", "")),
                    "| body=",
                    compact_text(body_json if body_json is not None else body_text),
                )

        # Handler failed request: ghi rõ lỗi mạng nếu có.
        async def on_request_failed(req):
            url = req.url
            method = req.method
            rt = req.resource_type
            if not is_relevant_setting_url(url, rt, method):
                return

            failure = req.failure
            item = {
                "phase": "request_failed",
                "ts": now_ms(),
                "iso": ts_iso(),
                "method": method,
                "resource_type": rt,
                "url": url,
                "url_info": parse_url_info(url),
                "failure": failure,
            }
            network_events.append(item)

            if VERBOSE_TERMINAL:
                print("[ERR]", method, compact_text(item["url_info"].get("path", "")), "|", compact_text(failure))

        # Đăng ký listener mạng.
        page.on("request", lambda req: asyncio.create_task(on_request(req)))
        page.on("response", lambda res: asyncio.create_task(on_response(res)))
        page.on("requestfailed", lambda req: asyncio.create_task(on_request_failed(req)))

        # Mở trang Kling để bạn tự thao tác setting.
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        await asyncio.sleep(1.2)

        # Hàm ghi file để dùng chung cho cả auto-mode và live-mode.
        def write_outputs() -> None:
            # Lưu raw event dạng JSON để phân tích sâu.
            file_network.write_text(json.dumps(network_events, ensure_ascii=False, indent=2), encoding="utf-8")
            file_ui.write_text(json.dumps(ui_events, ensure_ascii=False, indent=2), encoding="utf-8")
            selector_candidates = build_ui_selector_candidates(ui_events, top_n=50)
            file_selector_candidates.write_text(
                json.dumps(selector_candidates, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # Timeline dạng text để xem nhanh bằng mắt.
            lines: list[str] = []
            merged = []
            merged.extend(network_events)
            merged.extend(ui_events)
            merged.sort(key=lambda x: int(x.get("ts", 0)))

            for row in merged:
                kind = row.get("phase") or row.get("type") or "event"
                if kind in {"request", "response", "request_failed", "request_blocked"}:
                    line = (
                        f"[{row.get('iso')}] NET {kind.upper()} "
                        f"{row.get('method', '')} {row.get('status', '')} "
                        f"{compact_text((row.get('url_info') or {}).get('path', ''))}"
                    )
                else:
                    line = (
                        f"[{row.get('iso')}] UI {row.get('type', '').upper()} "
                        f"{compact_text(row.get('selector', ''))} "
                        f"text={compact_text(row.get('text', ''))} "
                        f"value={compact_text(row.get('value', ''))}"
                    )
                    if row.get("blockedGenerate"):
                        line += " [BLOCKED_GENERATE]"
                lines.append(line)

            file_timeline.write_text("\n".join(lines), encoding="utf-8")

            # Summary giúp bạn xem nhanh request/response có bao nhiêu.
            summary = {
                "session_dir": str(SESSION_DIR),
                "profile_dir": str(PROFILE_DIR),
                "home_url": HOME_URL,
                "block_generate": BLOCK_GENERATE,
                "counts": {
                    "network_events": len(network_events),
                    "ui_events": len(ui_events),
                    "blocked_generate_clicks": sum(1 for x in ui_events if x.get("blockedGenerate")),
                },
                "self_test": self_test_result,
                "scenario": scenario_result,
                "saved_at": ts_iso(),
                "files": {
                    "network": str(file_network),
                    "ui": str(file_ui),
                    "timeline": str(file_timeline),
                    "selector_candidates": str(file_selector_candidates),
                    "summary": str(file_summary),
                },
            }
            file_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        # Chế độ tự test: chỉ click thử các control setting an toàn, không generate.
        if AUTO_SELF_TEST:
            self_test_result = await run_auto_self_test_settings(page)
            print(
                "[SELF_TEST]",
                f"clicked={len(self_test_result.get('clicked', []))}",
                f"not_found={len(self_test_result.get('not_found', []))}",
                f"errors={len(self_test_result.get('errors', []))}",
            )

        # Chế độ auto scenario: tự setting ảnh + prompt 3 shot rồi thoát.
        if AUTO_SCENARIO:
            scenario_result = await run_auto_setting_scenario(page, SCENARIO_IMAGE_PATH, SCENARIO_VIDEO_URL)
            # Kéo UI event thêm 1 nhịp sau khi scenario chạy xong để không sót.
            try:
                fresh, ui_offset = await read_ui_events(page, ui_offset)
                if fresh:
                    ui_events.extend(fresh)
            except Exception:
                pass
            write_outputs()
            print(
                "[AUTO_SCENARIO]",
                f"uploaded={scenario_result.get('uploaded')}",
                f"filled_shots={scenario_result.get('filled_shots')}",
                f"steps={len(scenario_result.get('steps', []))}",
                f"errors={len(scenario_result.get('errors', []))}",
            )
            if not SCENARIO_KEEP_OPEN:
                print("[DONE] Đã chạy auto scenario và lưu log.")
                await context.close()
                return
            print("[INFO] Đã setup xong, giữ Chrome mở để bạn kiểm tra. Dùng save/quit khi muốn lưu/thoát.")

        print("\n[HƯỚNG DẪN]")
        print("- Bạn hãy đổi các setting trên UI như bình thường.")
        print("- Script đang KHÔNG generate và mặc định chặn click Generate.")
        print("- Gõ 'save' rồi Enter để lưu log tạm thời.")
        print("- Gõ 'quit' rồi Enter để kết thúc phiên debug.\n")

        # Vòng lặp live: liên tục kéo UI event mới và cho phép save/quit bằng terminal.
        while True:
            # Kéo UI events mới mỗi vòng.
            try:
                fresh, ui_offset = await read_ui_events(page, ui_offset)
            except Exception:
                fresh, ui_offset = [], ui_offset

            if fresh:
                ui_events.extend(fresh)
                if VERBOSE_TERMINAL:
                    for ev in fresh:
                        marker = " [BLOCKED]" if ev.get("blockedGenerate") else ""
                        print(
                            f"[UI]{marker}",
                            ev.get("type", ""),
                            compact_text(ev.get("selector", "")),
                            "| clickable=",
                            compact_text(ev.get("closestClickableSelector", "")),
                            "| text=",
                            compact_text(ev.get("text", "")),
                            "| value=",
                            compact_text(ev.get("value", "")),
                        )

            # Poll stdin không block để bạn chủ động save/quit.
            cmd = await asyncio.to_thread(input, "[save/quit] > ")
            cmd = (cmd or "").strip().lower()

            # Trước khi xử lý lệnh, kéo thêm 1 nhịp event để không sót thao tác vừa xảy ra.
            try:
                fresh2, ui_offset = await read_ui_events(page, ui_offset)
                if fresh2:
                    ui_events.extend(fresh2)
            except Exception:
                pass

            if cmd == "save":
                write_outputs()
                print(
                    f"[SAVED] network={len(network_events)} ui={len(ui_events)} "
                    f"blocked_generate={sum(1 for x in ui_events if x.get('blockedGenerate'))}"
                )
                continue

            if cmd == "quit":
                write_outputs()
                print("[DONE] Đã lưu toàn bộ log và kết thúc session.")
                break

            print("[INFO] Lệnh không hợp lệ. Dùng 'save' hoặc 'quit'.")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

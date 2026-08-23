#!/usr/bin/env python3
"""Collect public Amazon competitor images for visual and Prompt planning."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus, urljoin, urlparse


COLLECTOR_VERSION = 2

BLOCK_PATTERNS = (
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
    "robot check",
    "api-services-support@amazon.com",
)

SPONSORED_TERMS = (
    "sponsored",
    "gesponsert",
    "sponsorisé",
    "patrocinado",
    "sponsorizzato",
    "広告",
    "赞助",
    "贊助",
    "스폰서",
)

SITE_CONTEXT = {
    "amazon.com": {"locale": "en-US", "timezone_id": "America/New_York", "postcode": "10001"},
    "amazon.ca": {"locale": "en-CA", "timezone_id": "America/Toronto", "postcode": "M5V 3L9"},
    "amazon.co.uk": {"locale": "en-GB", "timezone_id": "Europe/London", "postcode": "SW1A 1AA"},
    "amazon.de": {"locale": "de-DE", "timezone_id": "Europe/Berlin", "postcode": "10115"},
    "amazon.fr": {"locale": "fr-FR", "timezone_id": "Europe/Paris", "postcode": "75001"},
    "amazon.it": {"locale": "it-IT", "timezone_id": "Europe/Rome", "postcode": "00118"},
    "amazon.es": {"locale": "es-ES", "timezone_id": "Europe/Madrid", "postcode": "28001"},
    "amazon.co.jp": {"locale": "ja-JP", "timezone_id": "Asia/Tokyo", "postcode": "100-0001"},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def log_event(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields, "at": now_iso()}
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


def detect_blocked_text(text: str) -> bool:
    lowered = (text or "").casefold()
    return any(pattern in lowered for pattern in BLOCK_PATTERNS)


def classify_result_type(card_text: str, has_sponsored_marker: bool = False) -> str:
    lowered = (card_text or "").casefold()
    if has_sponsored_marker or any(term.casefold() in lowered for term in SPONSORED_TERMS):
        return "ad"
    return "organic"


def normalize_media_url(url: str) -> str:
    value = (url or "").strip()
    if not value.startswith(("http://", "https://")):
        return ""
    return re.sub(r"\._[A-Z0-9_,+-]+_\.", ".", value)


def best_srcset_url(value: str) -> str:
    candidates: list[tuple[float, str]] = []
    for item in (value or "").split(","):
        parts = item.strip().split()
        url = normalize_media_url(parts[0] if parts else "")
        if not url:
            continue
        descriptor = parts[1].casefold() if len(parts) > 1 else "1x"
        try:
            score = float(descriptor[:-1]) * (1_000 if descriptor.endswith("x") else 1)
        except (TypeError, ValueError):
            score = 0
        candidates.append((score, url))
    return max(candidates, default=(0, ""), key=lambda candidate: candidate[0])[1]


def dedupe_asins(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for record in records:
        asin = str(record.get("asin", "")).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin) or asin in seen:
            continue
        seen.add(asin)
        item = dict(record)
        item["asin"] = asin
        output.append(item)
    return output


def parse_asins(values: Iterable[str]) -> list[str]:
    records: list[dict[str, str]] = []
    for value in values:
        for candidate in re.split(r"[,\s]+", value or ""):
            if candidate:
                records.append({"asin": candidate})
    return [record["asin"] for record in dedupe_asins(records)]


def batched(records: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [records[index : index + size] for index in range(0, len(records), size)]


def visual_representative_rank(records: list[dict[str, Any]], deep_max: int = 20) -> list[dict[str, Any]]:
    """Greedily select visually diverse listings without price or review signals."""
    remaining = [dict(record) for record in records]
    ranked: list[dict[str, Any]] = []
    seen_brands: set[str] = set()
    seen_result_types: set[str] = set()
    seen_media_shapes: set[str] = set()

    while remaining and len(ranked) < deep_max:
        best_index = 0
        best_score = float("-inf")
        best_reasons: list[str] = []
        for index, record in enumerate(remaining):
            position = int(record.get("position") or 999)
            brand = str(record.get("brand") or "").strip().casefold()
            result_type = str(record.get("result_type") or "organic")
            media_shape = "|".join(
                [
                    str(int(record.get("gallery_slot_count") or 0) // 3),
                    str(bool(record.get("has_a_plus"))),
                ]
            )
            score = max(0.0, 14.0 - min(position, 14))
            reasons = ["search_position"]
            if brand and brand not in seen_brands:
                score += 5.0
                reasons.append("new_brand")
            if result_type not in seen_result_types:
                score += 3.0
                reasons.append(f"new_{result_type}_coverage")
            if media_shape not in seen_media_shapes:
                score += 6.0
                reasons.append("new_media_structure")
            if record.get("has_a_plus"):
                score += 2.0
                reasons.append("a_plus_available")
            if int(record.get("gallery_slot_count") or 0) >= 6:
                score += 1.0
                reasons.append("rich_gallery")
            if score > best_score:
                best_index, best_score, best_reasons = index, score, reasons

        chosen = remaining.pop(best_index)
        brand = str(chosen.get("brand") or "").strip().casefold()
        if brand:
            seen_brands.add(brand)
        result_type = str(chosen.get("result_type") or "organic")
        seen_result_types.add(result_type)
        seen_media_shapes.add(
            "|".join(
                [
                    str(int(chosen.get("gallery_slot_count") or 0) // 3),
                    str(bool(chosen.get("has_a_plus"))),
                ]
            )
        )
        chosen["selection_score"] = round(best_score, 3)
        chosen["selection_reasons"] = best_reasons
        ranked.append(chosen)

    return ranked


def site_context(site: str, language: str = "", postcode: str | None = None) -> dict[str, str]:
    host = urlparse(site).netloc.casefold().removeprefix("www.")
    defaults = SITE_CONTEXT.get(host, {"locale": language or "en-US", "timezone_id": "UTC", "postcode": ""})
    return {
        "host": host or "amazon",
        "locale": language or defaults["locale"],
        "timezone_id": defaults["timezone_id"],
        "postcode": postcode or defaults["postcode"],
    }


def default_profile_dir(site: str) -> Path:
    host = urlparse(site).netloc.casefold().removeprefix("www.") or "amazon"
    return Path.home() / ".codex" / "amazon-image-prompt-production" / "browser-profile" / host


def find_local_browser(explicit: str | None = None) -> str | None:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.name == "nt":
        candidates.extend(
            [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            ]
        )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        )
    else:
        candidates.extend(("/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/microsoft-edge"))
    for candidate in candidates:
        if Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return None


def ensure_playwright(auto_install: bool) -> None:
    if importlib.util.find_spec("playwright") is not None:
        return
    if not auto_install:
        raise RuntimeError("playwright is missing; install it with: python -m pip install playwright")
    subprocess.run([sys.executable, "-m", "pip", "install", "playwright"], check=True)
    if importlib.util.find_spec("playwright") is None:
        raise RuntimeError("playwright installation completed but import still fails")


def image_metadata(body: bytes) -> dict[str, Any]:
    metadata: dict[str, Any] = {"width": None, "height": None, "perceptual_hash": None}
    try:
        from PIL import Image

        with Image.open(io.BytesIO(body)) as image:
            metadata["width"], metadata["height"] = image.size
            grayscale = image.convert("L").resize((8, 8))
            pixels = list(
                grayscale.get_flattened_data() if hasattr(grayscale, "get_flattened_data") else grayscale.getdata()
            )
            average = sum(pixels) / len(pixels)
            bits = "".join("1" if value >= average else "0" for value in pixels)
            metadata["perceptual_hash"] = f"{int(bits, 2):016x}"
    except Exception:
        pass
    return metadata


def safe_inner_text(locator, timeout: int = 2_000) -> str:
    try:
        return locator.first.inner_text(timeout=timeout).strip()
    except Exception:
        return ""


def safe_attribute(locator, name: str, timeout: int = 2_000) -> str:
    try:
        return (locator.first.get_attribute(name, timeout=timeout) or "").strip()
    except Exception:
        return ""


def texts(page, selector: str, limit: int = 50) -> list[str]:
    try:
        values = page.locator(selector).all_inner_texts()
    except Exception:
        return []
    output: list[str] = []
    for value in values[:limit]:
        cleaned = " ".join(value.split())
        if cleaned and cleaned not in output:
            output.append(cleaned)
    return output


def attributes(page, selector: str, names: tuple[str, ...], limit: int = 100) -> list[str]:
    output: list[str] = []
    try:
        available = page.locator(selector).count()
        count = min(available, limit) if limit > 0 else available
    except Exception:
        return output
    for index in range(count):
        node = page.locator(selector).nth(index)
        for name in names:
            try:
                value = node.get_attribute(name, timeout=1_000) or ""
            except Exception:
                value = ""
            normalized = best_srcset_url(value) if name in {"srcset", "data-srcset"} else normalize_media_url(value)
            if normalized and normalized not in output:
                output.append(normalized)
    return output


def extract_gallery_urls_from_html(html: str) -> list[str]:
    output: list[str] = []
    pattern = r'"(?:hiRes|large)"\s*:\s*"(https?:[^"\\]*(?:\\.[^"\\]*)*)"'
    for raw_url in re.findall(pattern, html or ""):
        try:
            decoded = json.loads(f'"{raw_url}"')
        except Exception:
            decoded = raw_url.replace("\\/", "/").replace("\\u0026", "&")
        value = normalize_media_url(decoded)
        if value and "media-amazon.com/images/" in value and value not in output:
            output.append(value)
    return output


def collect_gallery_urls(page, limit: int) -> list[str]:
    output: list[str] = []

    def add_landing_image() -> None:
        landing = page.locator("#landingImage, #imgTagWrapperId img").first
        candidates: list[tuple[int, str]] = []
        high_resolution = normalize_media_url(safe_attribute(landing, "data-old-hires"))
        if high_resolution:
            candidates.append((10**12, high_resolution))
        try:
            dynamic = landing.get_attribute("data-a-dynamic-image") or "{}"
            for raw_url, dimensions in json.loads(dynamic).items():
                value = normalize_media_url(raw_url)
                area = (
                    int(dimensions[0]) * int(dimensions[1])
                    if isinstance(dimensions, list) and len(dimensions) >= 2
                    else 0
                )
                if value:
                    candidates.append((area, value))
        except Exception:
            pass
        source = normalize_media_url(safe_attribute(landing, "src"))
        if source:
            candidates.append((0, source))
        if candidates:
            value = max(candidates, key=lambda item: item[0])[1]
            if value not in output:
                output.append(value)

    thumbnails = page.locator("#altImages li:not(.videoThumbnail) img")
    try:
        available = thumbnails.count()
        count = min(available, limit) if limit > 0 else available
    except Exception:
        count = 0
    for index in range(count):
        try:
            thumbnail = thumbnails.nth(index)
            thumbnail.scroll_into_view_if_needed(timeout=2_000)
            thumbnail.click(timeout=2_000)
            page.wait_for_timeout(250)
            add_landing_image()
        except Exception:
            continue
    if not output:
        try:
            html_urls = extract_gallery_urls_from_html(page.content())
            output.extend(html_urls[:limit] if limit > 0 else html_urls)
        except Exception:
            output.extend(attributes(page, "#altImages img", ("data-old-hires", "src"), limit=limit))
    return output[:limit] if limit > 0 else output


def wait_for_images_ready(page, *, visible_only: bool, timeout_ms: int) -> dict[str, int]:
    """Wait until non-video images have loaded and decoded, within a fixed deadline."""
    result = page.evaluate(
        """
        async ({visibleOnly, timeoutMs}) => {
          const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const deadline = Date.now() + timeoutMs;
          let stableRounds = 0;
          let previousSignature = '';
          let snapshot = {total: 0, pending: 0, failed: 0};

          while (Date.now() < deadline) {
            const images = Array.from(document.images).filter((img) => {
              if (img.closest('video, .videoThumbnail, [data-video-url], [class*="video-block"]')) return false;
              if (!visibleOnly) return true;
              const rect = img.getBoundingClientRect();
              return rect.bottom >= 0 && rect.top <= window.innerHeight && rect.width > 0 && rect.height > 0;
            });
            const pending = images.filter((img) => !img.complete).length;
            const failed = images.filter((img) => img.complete && img.naturalWidth === 0).length;
            const signature = images.map((img) => `${img.currentSrc || img.src}|${img.complete}|${img.naturalWidth}`).join('||');
            snapshot = {total: images.length, pending, failed};
            stableRounds = pending === 0 && signature === previousSignature ? stableRounds + 1 : 0;
            if (stableRounds >= 2) break;
            previousSignature = signature;
            await sleep(200);
          }

          const decodable = Array.from(document.images).filter((img) => {
            if (img.closest('video, .videoThumbnail, [data-video-url], [class*="video-block"]')) return false;
            if (!visibleOnly) return img.complete && img.naturalWidth > 0;
            const rect = img.getBoundingClientRect();
            return img.complete && img.naturalWidth > 0 && rect.bottom >= 0 && rect.top <= window.innerHeight;
          });
          await Promise.allSettled(decodable.map((img) => img.decode ? img.decode() : Promise.resolve()));
          return snapshot;
        }
        """,
        {"visibleOnly": visible_only, "timeoutMs": timeout_ms},
    )
    return {
        "total": int(result.get("total") or 0),
        "pending": int(result.get("pending") or 0),
        "failed": int(result.get("failed") or 0),
    }


def scroll_for_lazy_load(page, max_steps: int = 60) -> list[str]:
    warnings: list[str] = []
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass
    previous_height = 0
    stable_rounds = 0
    for _ in range(max_steps):
        height = int(page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"))
        viewport = int(page.evaluate("window.innerHeight || 900"))
        current = int(page.evaluate("window.scrollY"))
        next_y = min(current + max(600, viewport - 120), height)
        page.evaluate("y => window.scrollTo(0, y)", next_y)
        state = wait_for_images_ready(page, visible_only=True, timeout_ms=2_500)
        if state["pending"]:
            warnings.append(f"visible_images_pending:{state['pending']}")
        if height == previous_height and next_y >= height - viewport:
            stable_rounds += 1
        else:
            stable_rounds = 0
        if stable_rounds >= 2:
            break
        previous_height = height
    page.evaluate("window.scrollTo(0, 0)")
    final_state = wait_for_images_ready(page, visible_only=False, timeout_ms=12_000)
    if final_state["pending"]:
        warnings.append(f"page_images_pending:{final_state['pending']}")
    if final_state["failed"]:
        warnings.append(f"page_images_failed:{final_state['failed']}")
    return list(dict.fromkeys(warnings))


def screenshot_full(page, path: Path) -> list[str]:
    warnings: list[str] = []
    path.parent.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {"path": str(path)}
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        options.update({"type": "jpeg", "quality": 72})
    try:
        state = wait_for_images_ready(page, visible_only=False, timeout_ms=12_000)
        if state["pending"]:
            warnings.append(f"screenshot_images_pending:{state['pending']}")
        page.screenshot(full_page=True, **options)
    except Exception as exc:
        warnings.append(f"full_page_screenshot_failed:{exc}")
        page.screenshot(full_page=False, **options)
        warnings.append("viewport_screenshot_used")
    return warnings


def goto_with_retry(page, url: str, retries: int, timeout_ms: int) -> None:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1_500)
            return
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                page.wait_for_timeout(1_000 * (attempt + 1))
    raise RuntimeError(f"navigation_failed:{url}:{last_error}")


def preload_listing_pages(
    context,
    site: str,
    records: list[dict[str, Any]],
    timeout_ms: int,
) -> list[tuple[dict[str, Any], Any, bool]]:
    """Start a page batch before waiting, so listing resources load concurrently."""
    loaded: list[tuple[dict[str, Any], Any, bool]] = []
    for record in records:
        page = context.new_page()
        try:
            page.goto(
                f"{site.rstrip('/')}/dp/{record['asin']}",
                wait_until="commit",
                timeout=timeout_ms,
            )
            ready = True
        except Exception as exc:
            log_event("listing_preload_failed", asin=record["asin"], error=str(exc))
            ready = False
        loaded.append((record, page, ready))

    output: list[tuple[dict[str, Any], Any, bool]] = []
    for record, page, ready in loaded:
        if ready:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except Exception as exc:
                log_event("listing_preload_wait_failed", asin=record["asin"], error=str(exc))
                ready = False
        output.append((record, page, ready))
    return output


def set_delivery_postcode(page, site: str, postcode: str, timeout_ms: int) -> dict[str, Any]:
    result: dict[str, Any] = {"requested_postcode": postcode or None, "status": "not_requested"}
    if not postcode:
        return result
    try:
        goto_with_retry(page, site.rstrip("/") + "/", retries=1, timeout_ms=timeout_ms)
        page.locator("#nav-global-location-popover-link, #nav-packard-glow-loc-icon").first.click(timeout=4_000)
        field = page.locator(
            "#GLUXZipUpdateInput, #GLUXZipUpdateInput_0, input[name='zipCode'], "
            "[data-action='GLUXPostalInputAction'] input, input[autocomplete='postal-code']"
        ).first
        field.fill(postcode, timeout=4_000)
        page.locator(
            "#GLUXZipUpdate, input[data-action='GLUXPostalUpdateAction'], "
            "button[name='glowDoneButton'], [data-action='GLUXPostalUpdateAction'] input"
        ).first.click(timeout=4_000)
        page.wait_for_timeout(1_500)
        result["status"] = "submitted"
    except Exception as exc:
        result.update({"status": "warning", "warning": f"postcode_setup_failed:{exc}"})
    return result


def collect_search(page, site: str, keyword: str, research_dir: Path, timeout_ms: int) -> list[dict[str, Any]]:
    search_url = f"{site.rstrip('/')}/s?k={quote_plus(keyword)}"
    goto_with_retry(page, search_url, retries=1, timeout_ms=timeout_ms)
    body = safe_inner_text(page.locator("body"), timeout=5_000)
    if detect_blocked_text(body):
        raise PermissionError("amazon_access_blocked_on_search")
    scroll_for_lazy_load(page, max_steps=35)
    screenshot_full(page, research_dir / "search-page-full.jpg")

    cards = page.locator("[data-component-type='s-search-result'][data-asin]")
    records: list[dict[str, Any]] = []
    for index in range(cards.count()):
        card = cards.nth(index)
        asin = (card.get_attribute("data-asin") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue
        title = safe_inner_text(card.locator("h2 span"))
        href = safe_attribute(card.locator("h2 a"), "href")
        thumbnail = normalize_media_url(safe_attribute(card.locator("img.s-image"), "src"))
        card_text = safe_inner_text(card)
        sponsored_marker = card.locator(
            "[data-component-type='sp-sponsored-result'], [data-csa-c-content-id*='sponsored']"
        ).count() > 0
        records.append(
            {
                "asin": asin,
                "position": len(records) + 1,
                "result_type": classify_result_type(card_text, sponsored_marker),
                "title": title,
                "url": urljoin(site, href) if href else f"{site.rstrip('/')}/dp/{asin}",
                "thumbnail_url": thumbnail or None,
            }
        )
    return dedupe_asins(records)


def download_media(
    context,
    urls: list[str],
    target_dir: Path,
    prefix: str,
    *,
    media_kind: str,
) -> list[dict[str, Any]]:
    target_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    seen_sha256: set[str] = set()
    seen_perceptual: set[str] = set()
    for index, url in enumerate(urls, start=1):
        role = "main" if media_kind == "gallery" and index == 1 else "secondary" if media_kind == "gallery" else media_kind
        try:
            response = context.request.get(url, timeout=20_000)
            if not response.ok:
                raise RuntimeError(f"http_{response.status}")
            content_type = (response.headers.get("content-type") or "").lower()
            extension = ".png" if "png" in content_type else ".webp" if "webp" in content_type else ".jpg"
            body = response.body()
            sha256 = hashlib.sha256(body).hexdigest()
            metadata = image_metadata(body)
            perceptual_hash = metadata.get("perceptual_hash")
            duplicate = sha256 in seen_sha256 or bool(perceptual_hash and perceptual_hash in seen_perceptual)
            too_small = (
                media_kind == "aplus"
                and isinstance(metadata.get("width"), int)
                and isinstance(metadata.get("height"), int)
                and (metadata["width"] < 300 or metadata["height"] < 100)
            )
            if duplicate or too_small:
                records.append(
                    {
                        "slot": index,
                        "role": role,
                        "url": url,
                        "path": None,
                        "status": "duplicate" if duplicate else "rejected_low_resolution",
                        "content_type": content_type or None,
                        "size_bytes": len(body),
                        "sha256": sha256,
                        **metadata,
                    }
                )
                continue
            path = target_dir / f"{prefix}-{index:02d}{extension}"
            path.write_bytes(body)
            seen_sha256.add(sha256)
            if perceptual_hash:
                seen_perceptual.add(perceptual_hash)
            records.append(
                {
                    "slot": index,
                    "role": role,
                    "url": url,
                    "path": path.name,
                    "status": "saved",
                    "content_type": content_type or None,
                    "size_bytes": len(body),
                    "sha256": sha256,
                    **metadata,
                }
            )
        except Exception as exc:
            records.append(
                {"slot": index, "role": role, "url": url, "path": None, "status": "failed", "error": str(exc)}
            )
    return records


def capture_is_reusable(asin_dir: Path, previous: dict[str, Any]) -> bool:
    if previous.get("status") != "success" or previous.get("collector_version") != COLLECTOR_VERSION:
        return False
    for name in ("page-full.jpg", "listing.json", "assets.json"):
        if not (asin_dir / name).is_file():
            return False
    try:
        assets = read_json(asin_dir / "assets.json")
    except Exception:
        return False
    saved = 0
    for kind in ("gallery", "aplus"):
        for item in assets.get(kind) or []:
            if item.get("status") != "saved":
                continue
            saved += 1
            relative = item.get("path")
            if not relative or not (asin_dir / "assets" / kind / relative).is_file():
                return False
    return saved > 0


def deep_capture_is_reusable(research_dir: Path, asin: str) -> bool:
    asin_dir = research_dir / "asins" / asin
    capture_path = asin_dir / "capture.json"
    if not capture_path.is_file():
        return False
    try:
        return capture_is_reusable(asin_dir, read_json(capture_path))
    except Exception:
        return False


def collect_listing_lite(
    page,
    site: str,
    item: dict[str, Any],
    research_dir: Path,
    timeout_ms: int,
    *,
    navigate: bool = True,
) -> dict[str, Any]:
    asin = item["asin"]
    asin_dir = research_dir / "asins" / asin
    try:
        if navigate:
            goto_with_retry(page, f"{site.rstrip('/')}/dp/{asin}", retries=1, timeout_ms=timeout_ms)
        body = safe_inner_text(page.locator("body"), timeout=5_000)
        if detect_blocked_text(body):
            raise PermissionError("amazon_access_blocked_on_listing")
        record = {
            **item,
            "title": safe_inner_text(page.locator("#productTitle")) or item.get("title", ""),
            "brand": safe_inner_text(page.locator("#bylineInfo")),
            "gallery_slot_count": page.locator("#altImages li:not(.videoThumbnail)").count(),
            "has_video": page.locator("#altImages li.videoThumbnail, video").count() > 0,
            "has_a_plus": page.locator("#aplus, #aplus_feature_div, .aplus-v2, .premium-aplus").count() > 0,
            "gallery_thumbnails": attributes(page, "#altImages img", ("src",), limit=30),
            "status": "success",
            "captured_at": now_iso(),
        }
        write_json(asin_dir / "lite.json", record)
        return record
    except PermissionError:
        raise
    except Exception as exc:
        record = {**item, "status": "failed", "error": str(exc), "captured_at": now_iso()}
        write_json(asin_dir / "lite.json", record)
        return record


def collect_listing_deep(
    page,
    context,
    site: str,
    item: dict[str, Any],
    research_dir: Path,
    timeout_ms: int,
    region_context: dict[str, Any],
    gallery_limit: int,
    aplus_limit: int,
    *,
    navigate: bool = True,
) -> dict[str, Any]:
    asin = item["asin"]
    asin_dir = research_dir / "asins" / asin
    capture_path = asin_dir / "capture.json"
    if deep_capture_is_reusable(research_dir, asin):
        return {"asin": asin, "status": "skipped_existing"}

    capture: dict[str, Any] = {
        "asin": asin,
        "url": f"{site.rstrip('/')}/dp/{asin}",
        "collector_version": COLLECTOR_VERSION,
        "capture_level": "deep",
        "region_context": region_context,
        "started_at": now_iso(),
        "status": "in_progress",
        "missing_modules": [],
        "warnings": [],
    }
    write_json(capture_path, capture)

    try:
        if navigate:
            goto_with_retry(page, capture["url"], retries=1, timeout_ms=timeout_ms)
        body = safe_inner_text(page.locator("body"), timeout=5_000)
        if detect_blocked_text(body):
            raise PermissionError("amazon_access_blocked_on_listing")
        capture["warnings"].extend(scroll_for_lazy_load(page))
        page_full = asin_dir / "page-full.jpg"
        capture["warnings"].extend(screenshot_full(page, page_full))
        capture["page_full"] = {
            "path": page_full.name,
            "size_bytes": page_full.stat().st_size,
            "sha256": sha256_file(page_full),
        }

        title = safe_inner_text(page.locator("#productTitle")) or item.get("title", "")
        brand = safe_inner_text(page.locator("#bylineInfo"))
        bullets = texts(page, "#feature-bullets li span.a-list-item", limit=12)
        gallery_urls = collect_gallery_urls(page, limit=gallery_limit)
        aplus_selector = (
            "#aplus img, #aplus_feature_div img, .aplus-v2 img, .premium-aplus img, "
            "[data-cel-widget*='aplus'] img, #aplus source, #aplus_feature_div source, "
            ".aplus-v2 source, .premium-aplus source, [data-cel-widget*='aplus'] source"
        )
        aplus_scan_limit = aplus_limit * 3 if aplus_limit > 0 else 0
        aplus_urls = attributes(
            page,
            aplus_selector,
            ("data-src", "data-a-hires", "data-old-hires", "data-srcset", "srcset", "src"),
            limit=aplus_scan_limit,
        )
        if aplus_limit > 0:
            aplus_urls = aplus_urls[:aplus_limit]

        gallery = download_media(
            context, gallery_urls, asin_dir / "assets" / "gallery", "gallery", media_kind="gallery"
        )
        aplus = download_media(context, aplus_urls, asin_dir / "assets" / "aplus", "aplus", media_kind="aplus")

        saved_gallery = sum(1 for record in gallery if record.get("status") == "saved")
        if saved_gallery == 0:
            capture["warnings"].append("gallery_missing")
        if not aplus_urls:
            capture["missing_modules"].append("a_plus:not_displayed")

        write_json(
            asin_dir / "listing.json",
            {
                "asin": asin,
                "url": capture["url"],
                "title": title,
                "brand": brand,
                "bullets": bullets,
                "search_record": item,
                "captured_at": now_iso(),
            },
        )
        write_json(
            asin_dir / "assets.json",
            {
                "capture_level": "deep",
                "gallery": gallery,
                "aplus": aplus,
                "video_files_downloaded": False,
                "video_assets_ignored": True,
                "collection_budget": {
                    "gallery": gallery_limit,
                    "aplus": aplus_limit,
                },
            },
        )
        capture.update({"status": "success", "finished_at": now_iso(), "title": title})
        write_json(capture_path, capture)
        return {"asin": asin, "status": "success", "title": title}
    except PermissionError as exc:
        capture.update({"status": "blocked", "finished_at": now_iso(), "error": str(exc)})
        write_json(capture_path, capture)
        raise
    except Exception as exc:
        capture.update({"status": "failed", "finished_at": now_iso(), "error": str(exc)})
        write_json(capture_path, capture)
        return {"asin": asin, "status": "failed", "error": str(exc)}


def visual_coverage_keys(record: dict[str, Any], assets: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    brand = str(record.get("brand") or "").strip().casefold()
    if brand:
        keys.add(f"brand:{brand}")
    gallery_count = sum(1 for item in assets.get("gallery") or [] if item.get("status") == "saved")
    aplus_count = sum(1 for item in assets.get("aplus") or [] if item.get("status") == "saved")
    keys.add(f"gallery-band:{min(gallery_count // 3, 4)}")
    keys.add(f"a-plus:{bool(aplus_count)}")
    return keys


def build_visual_index(research_dir: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    asins_dir = research_dir / "asins"
    if asins_dir.is_dir():
        for asin_dir in sorted(path for path in asins_dir.iterdir() if path.is_dir()):
            listing_path = asin_dir / "listing.json"
            assets_path = asin_dir / "assets.json"
            if not listing_path.is_file() or not assets_path.is_file():
                continue
            listing = read_json(listing_path)
            assets = read_json(assets_path)
            for kind in ("gallery", "aplus"):
                folder = kind
                for item in assets.get(kind) or []:
                    if item.get("status") != "saved" or not item.get("path"):
                        continue
                    width = item.get("width")
                    height = item.get("height")
                    aspect_ratio = round(width / height, 4) if isinstance(width, int) and isinstance(height, int) and height else None
                    records.append(
                        {
                            "asset_id": f"{asin_dir.name}-{kind.replace('_', '-')}-{int(item.get('slot') or 0):02d}",
                            "asin": asin_dir.name,
                            "title": listing.get("title"),
                            "brand": listing.get("brand"),
                            "kind": kind,
                            "role": item.get("role"),
                            "slot": item.get("slot"),
                            "path": str(Path("asins") / asin_dir.name / "assets" / folder / item["path"]),
                            "width": width,
                            "height": height,
                            "aspect_ratio": aspect_ratio,
                            "sha256": item.get("sha256"),
                            "perceptual_hash": item.get("perceptual_hash"),
                        }
                    )
    return {"created_at": now_iso(), "asset_count": len(records), "assets": records}


def write_summary_csv(research_dir: Path, records: list[dict[str, Any]]) -> None:
    path = research_dir / "summary.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["asin", "tier", "status", "title", "error"])
        writer.writeheader()
        for record in records:
            writer.writerow({key: record.get(key, "") for key in writer.fieldnames})


def write_selection(
    research_dir: Path,
    *,
    mode: str,
    full_research: bool,
    args: argparse.Namespace,
    search_records: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    deep_records: list[dict[str, Any]],
    coverage: set[str],
    stop_reason: str,
) -> None:
    selected_asins = {record["asin"] for record in ranked}
    collected_asins = {record["asin"] for record in deep_records}
    not_selected = [
        {"asin": record["asin"], "reason": "outside_visual_representative_set"}
        for record in search_records
        if record["asin"] not in selected_asins
    ]
    not_selected.extend(
        {"asin": record["asin"], "reason": "stopped_before_collection_completed"}
        for record in ranked
        if record["asin"] not in collected_asins
    )
    write_json(
        research_dir / "selection.json",
        {
            "mode": "full_research" if full_research else mode,
            "collection_concurrency": {
                "lite": args.lite_concurrency,
                "deep": args.deep_concurrency,
            },
            "deep_min": args.deep_min if mode == "keyword" else None,
            "deep_target": args.deep_target if mode == "keyword" else None,
            "deep_max": args.deep_max if mode == "keyword" else None,
            "indexed_count": len(search_records),
            "ranked_candidates": [
                {
                    "asin": record["asin"],
                    "score": record.get("selection_score"),
                    "reasons": record.get("selection_reasons", []),
                }
                for record in ranked
            ],
            "deep_collected_asins": [record["asin"] for record in deep_records],
            "deep_successful_asins": [
                record["asin"]
                for record in deep_records
                if record.get("status") in {"success", "skipped_existing"}
            ],
            "not_selected": not_selected,
            "coverage_keys": sorted(coverage),
            "stop_reason": stop_reason,
            "created_at": now_iso(),
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect public Amazon images for visual and Prompt planning")
    parser.add_argument("--output-dir", required=True, help="Root directory for the research output")
    parser.add_argument("--site", default="https://www.amazon.com")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--keyword")
    source.add_argument("--asin", action="append", default=[], help="Repeat or pass comma-separated ASINs")
    parser.add_argument("--language", default="")
    parser.add_argument("--postcode")
    parser.add_argument("--max-asins", type=int, default=0, help="Search mode only; 0 means all visible ASINs")
    parser.add_argument("--lite-concurrency", type=int, default=4)
    parser.add_argument("--deep-concurrency", type=int, default=3)
    parser.add_argument("--deep-min", type=int, default=10)
    parser.add_argument("--deep-target", type=int, default=16)
    parser.add_argument("--deep-max", type=int, default=20)
    parser.add_argument("--full-research", action="store_true")
    parser.add_argument("--gallery-limit", type=int, default=0, help="Maximum gallery images; 0 saves all")
    parser.add_argument("--aplus-limit", type=int, default=0, help="Maximum A+ images; 0 saves all")
    parser.add_argument("--browser-executable")
    parser.add_argument("--profile-dir")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--no-auto-install", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=45_000)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.keyword and not args.keyword.strip():
        raise ValueError("keyword_must_not_be_empty")
    if args.asin and not parse_asins(args.asin):
        raise ValueError("no_valid_asins")
    if not (0 < args.deep_min <= args.deep_target <= args.deep_max):
        raise ValueError("expected 0 < deep-min <= deep-target <= deep-max")
    if min(args.lite_concurrency, args.deep_concurrency) <= 0:
        raise ValueError("collection concurrency must be positive")
    if min(args.gallery_limit, args.aplus_limit) < 0:
        raise ValueError("media limits must be non-negative")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    output_dir = Path(args.output_dir).expanduser().resolve()
    research_dir = output_dir / "research"
    asins = parse_asins(args.asin)
    context_settings = site_context(args.site, args.language, args.postcode)
    profile_dir = Path(args.profile_dir).expanduser().resolve() if args.profile_dir else default_profile_dir(args.site)
    mode = "keyword" if args.keyword else "asin"

    if args.dry_run:
        payload = {
            "ok": True,
            "dry_run": True,
            "mode": mode,
            "site": args.site,
            "keyword": args.keyword,
            "asins": asins,
            "search_url": f"{args.site.rstrip('/')}/s?k={quote_plus(args.keyword)}" if args.keyword else None,
            "product_urls": [f"{args.site.rstrip('/')}/dp/{asin}" for asin in asins],
            "output_dir": str(output_dir),
            "profile_dir": str(profile_dir),
            "headless": not args.headed,
            "lite_concurrency": args.lite_concurrency,
            "deep_concurrency": args.deep_concurrency,
            "deep_range": {
                "minimum": args.deep_min,
                "target": args.deep_target,
                "maximum": args.deep_max,
            },
            "network_started": False,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    research_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    ensure_playwright(auto_install=not args.no_auto_install)
    from playwright.sync_api import sync_playwright

    browser_path = find_local_browser(args.browser_executable)
    results: list[dict[str, Any]] = []
    search_records: list[dict[str, Any]] = []
    ranked: list[dict[str, Any]] = []
    deep_records: list[dict[str, Any]] = []
    coverage: set[str] = set()
    stop_reason = "not_started"
    blocked = False

    try:
        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "headless": not args.headed,
                "locale": context_settings["locale"],
                "timezone_id": context_settings["timezone_id"],
                "viewport": {"width": 1440, "height": 1000},
            }
            if browser_path:
                launch_options["executable_path"] = browser_path
            else:
                bundled = Path(playwright.chromium.executable_path)
                if not bundled.is_file():
                    if args.no_auto_install:
                        raise RuntimeError("No local browser and Playwright Chromium is not installed")
                    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
            context = playwright.chromium.launch_persistent_context(user_data_dir=str(profile_dir), **launch_options)
            page = context.pages[0] if context.pages else context.new_page()
            region_context = set_delivery_postcode(page, args.site, context_settings["postcode"], args.timeout_ms)
            region_context.update(
                {"locale": context_settings["locale"], "timezone_id": context_settings["timezone_id"]}
            )

            if args.keyword:
                log_event("search_index_started", keyword=args.keyword)
                search_records = collect_search(page, args.site, args.keyword, research_dir, args.timeout_ms)
                if args.max_asins > 0:
                    search_records = search_records[: args.max_asins]
                if not search_records:
                    raise RuntimeError("no_visible_asins")
                write_json(
                    research_dir / "search.json",
                    {
                        "mode": "keyword",
                        "site": args.site,
                        "keyword": args.keyword,
                        "captured_at": now_iso(),
                        "browser_executable": browser_path or str(playwright.chromium.executable_path),
                        "browser_profile": str(profile_dir),
                        "headless": not args.headed,
                        "region_context": region_context,
                        "asins": search_records,
                    },
                )
                lite_records: list[dict[str, Any]] = []
                for lite_batch in batched(search_records, args.lite_concurrency):
                    loaded_pages = preload_listing_pages(context, args.site, lite_batch, args.timeout_ms)
                    try:
                        for item, worker_page, preloaded in loaded_pages:
                            try:
                                result = collect_listing_lite(
                                    worker_page,
                                    args.site,
                                    item,
                                    research_dir,
                                    args.timeout_ms,
                                    navigate=not preloaded,
                                )
                            except PermissionError as exc:
                                results.append(
                                    {
                                        "asin": item["asin"],
                                        "tier": "lite",
                                        "status": "blocked",
                                        "error": str(exc),
                                    }
                                )
                                raise
                            result["tier"] = "lite"
                            lite_records.append(result)
                            results.append(result)
                    finally:
                        for _, worker_page, _ in loaded_pages:
                            worker_page.close()
                successful_lite = [record for record in lite_records if record.get("status") == "success"]
                ranked = (
                    [{**record, "selection_score": None, "selection_reasons": ["explicit_full_research"]} for record in successful_lite]
                    if args.full_research
                    else visual_representative_rank(successful_lite, args.deep_max)
                )
            else:
                search_records = [
                    {
                        "asin": asin,
                        "position": index,
                        "result_type": "direct",
                        "title": "",
                        "url": f"{args.site.rstrip('/')}/dp/{asin}",
                        "thumbnail_url": None,
                    }
                    for index, asin in enumerate(asins, start=1)
                ]
                write_json(
                    research_dir / "search.json",
                    {
                        "mode": "asin",
                        "site": args.site,
                        "keyword": None,
                        "captured_at": now_iso(),
                        "region_context": region_context,
                        "asins": search_records,
                    },
                )
                ranked = [
                    {**record, "selection_score": None, "selection_reasons": ["explicit_asin"]}
                    for record in search_records
                ]

            saturation_streak = 0
            stop_reason = "all_candidates_collected" if args.full_research or mode == "asin" else "deep_max_reached"
            stop_deep_collection = False
            for deep_batch in batched(ranked, args.deep_concurrency):
                to_preload = [
                    candidate
                    for candidate in deep_batch
                    if not deep_capture_is_reusable(research_dir, candidate["asin"])
                ]
                loaded_pages = preload_listing_pages(context, args.site, to_preload, args.timeout_ms)
                loaded_by_asin = {
                    candidate["asin"]: (worker_page, preloaded)
                    for candidate, worker_page, preloaded in loaded_pages
                }
                try:
                    for candidate in deep_batch:
                        worker_page, preloaded = loaded_by_asin.get(candidate["asin"], (page, False))
                        try:
                            result = collect_listing_deep(
                                worker_page,
                                context,
                                args.site,
                                candidate,
                                research_dir,
                                args.timeout_ms,
                                region_context,
                                args.gallery_limit,
                                args.aplus_limit,
                                navigate=not preloaded,
                            )
                        except PermissionError as exc:
                            results.append(
                                {
                                    "asin": candidate["asin"],
                                    "tier": "deep",
                                    "status": "blocked",
                                    "error": str(exc),
                                }
                            )
                            raise
                        result["tier"] = "deep"
                        result["selection_reasons"] = candidate.get("selection_reasons", [])
                        deep_records.append(result)
                        results.append(result)

                        if result.get("status") in {"success", "skipped_existing"}:
                            asin_dir = research_dir / "asins" / candidate["asin"]
                            keys = visual_coverage_keys(
                                read_json(asin_dir / "listing.json"),
                                read_json(asin_dir / "assets.json"),
                            )
                            new_keys = keys - coverage
                            coverage.update(keys)
                            saturation_streak = 0 if new_keys else saturation_streak + 1

                        if mode == "keyword" and not args.full_research:
                            successful_count = sum(
                                1
                                for record in deep_records
                                if record.get("status") in {"success", "skipped_existing"}
                            )
                            if successful_count >= args.deep_target:
                                stop_reason = "deep_target_reached"
                                stop_deep_collection = True
                                break
                            if successful_count >= args.deep_min and saturation_streak >= 2:
                                stop_reason = "visual_structure_saturated_after_two_no-new-signal_asins"
                                stop_deep_collection = True
                                break
                finally:
                    for _, worker_page, _ in loaded_pages:
                        worker_page.close()
                if stop_deep_collection:
                    break

            write_selection(
                research_dir,
                mode=mode,
                full_research=args.full_research,
                args=args,
                search_records=search_records,
                ranked=ranked,
                deep_records=deep_records,
                coverage=coverage,
                stop_reason=stop_reason,
            )
            context.close()
    except PermissionError as exc:
        blocked = True
        stop_reason = str(exc)
        log_event("collection_blocked", error=str(exc))
        write_selection(
            research_dir,
            mode=mode,
            full_research=args.full_research,
            args=args,
            search_records=search_records,
            ranked=ranked,
            deep_records=deep_records,
            coverage=coverage,
            stop_reason=stop_reason,
        )
    except Exception as exc:
        write_selection(
            research_dir,
            mode=mode,
            full_research=args.full_research,
            args=args,
            search_records=search_records,
            ranked=ranked,
            deep_records=deep_records,
            coverage=coverage,
            stop_reason=str(exc),
        )
        write_summary_csv(research_dir, results)
        write_json(research_dir / "visual-index.json", build_visual_index(research_dir))
        print(json.dumps({"ok": False, "blocked": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    write_summary_csv(research_dir, results)
    visual_index = build_visual_index(research_dir)
    write_json(research_dir / "visual-index.json", visual_index)
    output = {
        "ok": not blocked,
        "blocked": blocked,
        "mode": mode,
        "browser": browser_path or "playwright-chromium",
        "headless": not args.headed,
        "profile_dir": str(profile_dir),
        "lite_concurrency": args.lite_concurrency,
        "deep_concurrency": args.deep_concurrency,
        "indexed_count": len(search_records),
        "deep_count": len(deep_records),
        "asset_count": visual_index["asset_count"],
        "stop_reason": stop_reason,
        "results": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 3 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())

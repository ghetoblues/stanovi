#!/usr/bin/env python3
"""Монитор новых квартир на halooglasi → Telegram."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from curl_cffi import CurlMime
from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv

import config

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("halooglasi")

BASE = "https://www.halooglasi.com"
TG_API = "https://api.telegram.org"

ROOMS_TO_QUERY = {
    0.5: "1",
    1.0: "2",
    1.5: "3",
    2.0: "4",
    2.5: "5",
    3.0: "7",
    3.5: "8",
    4.0: "9",
    4.5: "10",
    5.0: "11",
}

ADVERTISER_IDS = {
    "agencija": "387238",
    "vlasnik": "387237",
    "investitor": "387300",
}

FURNISHED_IDS = {
    "prazno": "564",
    "namešteno": "562",
    "nameshteno": "562",
    "namesteno": "562",
    "polunamešteno": "563",
    "polunameshteno": "563",
    "polunamesteno": "563",
}

BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sr-RS,sr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": f"{BASE}/",
}

FLARE_URL = os.environ.get("FLARESOLVERR_URL", "").rstrip("/")
CF_FILE = Path("data/cf.json")
_cf_state: dict = {"cookies": {}, "user_agent": None}


def _load_cf() -> None:
    if not CF_FILE.exists():
        return
    try:
        data = json.loads(CF_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if isinstance(data, dict):
        _cf_state["cookies"] = data.get("cookies") or {}
        _cf_state["user_agent"] = data.get("user_agent")


def _save_cf() -> None:
    CF_FILE.parent.mkdir(parents=True, exist_ok=True)
    CF_FILE.write_text(json.dumps(_cf_state, ensure_ascii=False), encoding="utf-8")


def telegram_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN") or config.TELEGRAM_BOT_TOKEN


def telegram_chat_id() -> str:
    return str(os.environ.get("TELEGRAM_CHAT_ID") or config.TELEGRAM_CHAT_ID)


def _rooms_value(value: float | int | None) -> str | None:
    if value is None:
        return None
    mapped = ROOMS_TO_QUERY.get(float(value))
    if mapped is None:
        raise ValueError(f"неизвестное число комнат {value}, доступны {sorted(ROOMS_TO_QUERY)}")
    return mapped


def _csv_ids(names: list[str], mapping: dict[str, str], label: str) -> str | None:
    if not names:
        return None
    ids = []
    for name in names:
        key = name.strip().lower()
        if key not in mapping:
            raise ValueError(f"неизвестный {label} {name!r}, доступны {sorted(mapping)}")
        ids.append(mapping[key])
    return ",".join(ids)


def build_search_url(page: int = 1) -> str:
    parsed = urlparse(config.SEARCH_URL)
    params = {k: v[-1] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}

    if config.PRICE_FROM is not None:
        params["cena_d_from"] = str(config.PRICE_FROM)
        params["cena_d_unit"] = "4"
    if config.PRICE_TO is not None:
        params["cena_d_to"] = str(config.PRICE_TO)
        params["cena_d_unit"] = "4"
    if config.AREA_FROM is not None:
        params["kvadratura_d_from"] = str(config.AREA_FROM)
    if config.AREA_TO is not None:
        params["kvadratura_d_to"] = str(config.AREA_TO)

    rooms_from = _rooms_value(config.ROOMS_FROM)
    rooms_to = _rooms_value(config.ROOMS_TO)
    if rooms_from:
        params["broj_soba_order_i_from"] = rooms_from
    if rooms_to:
        params["broj_soba_order_i_to"] = rooms_to

    advertisers = _csv_ids(config.ADVERTISERS, ADVERTISER_IDS, "рекламодатель")
    if advertisers:
        params["oglasivac_nekretnine_id_l"] = advertisers

    furnished = _csv_ids(config.FURNISHED, FURNISHED_IDS, "статус мебели")
    if furnished:
        params["namestenost_id_l"] = furnished

    if config.HAS_PHOTO:
        params["sa_fotografijom"] = "true"

    params.update(config.EXTRA_PARAMS)

    if page > 1:
        params["page"] = str(page)
    else:
        params.pop("page", None)

    return urlunparse(parsed._replace(query=urlencode(params)))


def _looks_like_page(html: str) -> bool:
    if not html:
        return False
    head = html[:2500]
    if "Just a moment" in head or "cf-mitigated" in head:
        return False
    return "product-item" in html or '"ImageURLs"' in html


def _direct_get(url: str) -> str | None:
    headers = dict(BROWSER_HEADERS)
    if _cf_state.get("user_agent"):
        headers["User-Agent"] = _cf_state["user_agent"]
    try:
        response = cffi_requests.get(
            url,
            impersonate="chrome",
            headers=headers,
            cookies=_cf_state.get("cookies") or {},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("прямой запрос не удался: %s", exc)
        return None
    if response.status_code == 200 and _looks_like_page(response.text):
        return response.text
    log.warning("прямой запрос HTTP %s", response.status_code)
    return None


def _flare(cmd: str, **payload) -> dict:
    if not FLARE_URL:
        raise RuntimeError("FLARESOLVERR_URL не задан")
    last_error: Exception | str = "unknown"
    for attempt in range(8):
        try:
            response = cffi_requests.post(
                FLARE_URL,
                json={"cmd": cmd, **payload},
                timeout=90,
            )
            response.raise_for_status()
            body = response.json()
            if body.get("status") != "ok":
                raise RuntimeError(body.get("message") or str(body))
            return body
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 — сеть, ждём готовности сервиса
            last_error = exc
            log.warning("flaresolverr попытка %s: %s", attempt + 1, exc)
            time.sleep(2)
    raise RuntimeError(f"flaresolverr недоступен: {last_error}")


def _flaresolverr_get(url: str) -> str:
    try:
        _flare("sessions.create", session="halooglasi")
    except Exception as exc:  # noqa: BLE001 — сессия уже есть
        log.info("flaresolverr session: %s", exc)
    body = _flare(
        "request.get",
        url=url,
        session="halooglasi",
        session_ttl_minutes=20,
        maxTimeout=60000,
    )
    solution = body.get("solution") or {}
    html = solution.get("response") or ""
    cookies = {c["name"]: c["value"] for c in solution.get("cookies") or [] if "name" in c}
    _cf_state["cookies"] = cookies
    _cf_state["user_agent"] = solution.get("userAgent")
    _save_cf()
    if not _looks_like_page(html):
        raise RuntimeError("FlareSolverr вернул страницу без объявлений")
    return html


def fetch_html(url: str) -> str:
    html = _direct_get(url)
    if html:
        return html
    if FLARE_URL:
        log.info("Cloudflare, иду через FlareSolverr")
        return _flaresolverr_get(url)
    last_error: str | Exception = "unknown"
    for impersonate in ("chrome136", "chrome131"):
        try:
            response = cffi_requests.get(
                url,
                impersonate=impersonate,
                headers=BROWSER_HEADERS,
                timeout=30,
            )
            if response.status_code == 200 and _looks_like_page(response.text):
                return response.text
            last_error = f"HTTP {response.status_code} ({impersonate})"
            log.warning("halooglasi вернул %s", last_error)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log.warning("запрос %s не удался: %s", impersonate, exc)
        time.sleep(1.5)
    raise RuntimeError(f"не удалось скачать выдачу: {last_error}")


def _text(el) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _feature(card, legend: str) -> str:
    for item in card.select(".product-features li"):
        legend_el = item.select_one(".legend")
        if legend_el and legend.lower() in _text(legend_el).lower():
            clone = BeautifulSoup(str(item), "lxml")
            legend_node = clone.select_one(".legend")
            if legend_node:
                legend_node.decompose()
            return _text(clone)
    return ""


def parse_listings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    listings = []
    for card in soup.select(".product-item.product-list-item"):
        classes = card.get("class", [])
        if "banner-list" in classes:
            continue
        ad_id = card.get("data-id") or card.get("id")
        link = card.select_one(".product-title a")
        if not ad_id or link is None or not link.get("href"):
            continue
        href = urljoin(BASE, link["href"].split("?")[0])
        price_el = card.select_one(".central-feature span")
        image = card.select_one(".a-images img")
        image_url = image.get("src") if image and image.get("src") else ""
        if image_url.startswith("//"):
            image_url = "https:" + image_url
        listings.append(
            {
                "id": str(ad_id),
                "title": _text(link) or "Stan",
                "url": href,
                "price": price_el.get("data-value") if price_el else "",
                "price_text": _text(card.select_one(".central-feature i")) or "",
                "location": " • ".join(
                    _text(li) for li in card.select(".subtitle-places li") if _text(li)
                ),
                "area": _feature(card, "Kvadratura"),
                "rooms": _feature(card, "Broj soba"),
                "floor": _feature(card, "Spratnost"),
                "date": _text(card.select_one(".publish-date")),
                "advertiser": _text(card.select_one(".basic-info")),
                "description": _text(card.select_one(".product-description")),
                "image": image_url,
                "premium": "Premium" in classes,
            }
        )
    return listings


def load_seen(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [str(x) for x in data]
    return []


def save_seen(path: Path, ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    unique = list(dict.fromkeys(ids))[: config.MAX_SEEN]
    path.write_text(json.dumps(unique, ensure_ascii=False, indent=0), encoding="utf-8")


def format_caption(ad: dict) -> str:
    lines = [f"<b>{escape(ad['title'])}</b>"]
    if ad["price_text"]:
        lines.append(f"💰 {escape(ad['price_text'])}")
    if ad["location"]:
        lines.append(f"📍 {escape(ad['location'])}")
    specs = [x for x in (ad["area"], ad["rooms"], ad["floor"]) if x]
    if specs:
        lines.append("📐 " + " · ".join(escape(x) for x in specs))
    meta = [x for x in (ad["advertiser"], ad["date"]) if x]
    if meta:
        lines.append("🏷️ " + " · ".join(escape(x) for x in meta))
    if ad["description"]:
        desc = ad["description"]
        if len(desc) > 280:
            desc = desc[:277].rstrip() + "…"
        lines.append("")
        lines.append(escape(desc))
    lines.append("")
    lines.append(ad["url"])
    caption = "\n".join(lines)
    return caption[:1024]


def _normalize_image_url(path: str) -> str:
    path = path.replace("/m/", "/l/")
    if path.startswith("//"):
        url = "https:" + path
    elif path.startswith("http"):
        url = path
    else:
        url = "https://img.halooglasi.com/" + path.lstrip("/")
    return url.replace("halooglasi.com//", "halooglasi.com/")


def parse_detail_images(html: str, ad_id: str) -> list[str]:
    match = re.search(r'"ImageURLs"\s*:\s*(\[[^\]]*\])', html)
    if not match:
        return []
    try:
        paths = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    images: list[str] = []
    skip = ("logoi", "kategorije", "no-image", "promo", "share-image")
    for path in paths:
        if not isinstance(path, str) or ad_id not in path:
            continue
        lowered = path.lower()
        if any(part in lowered for part in skip):
            continue
        url = _normalize_image_url(path)
        if url not in images:
            images.append(url)
        if len(images) >= config.MAX_PHOTOS:
            break
    return images


def enrich_images(ad: dict) -> dict:
    if ad.get("images"):
        return ad
    images: list[str] = []
    try:
        html = fetch_html(ad["url"])
        images = parse_detail_images(html, ad["id"])
    except Exception:
        log.exception("не смог взять галерею %s", ad["id"])
    if ad.get("image"):
        cover = _normalize_image_url(ad["image"])
        if cover not in images:
            images.insert(0, cover)
        images = list(dict.fromkeys(images))[: config.MAX_PHOTOS]
    ad["images"] = images
    if images:
        ad["image"] = images[0]
    return ad


def _download_image(url: str) -> bytes | None:
    headers = dict(BROWSER_HEADERS)
    if _cf_state.get("user_agent"):
        headers["User-Agent"] = _cf_state["user_agent"]
    try:
        response = cffi_requests.get(
            url,
            impersonate="chrome",
            headers=headers,
            cookies=_cf_state.get("cookies") or {},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("фото не скачалось %s: %s", url, exc)
        return None
    if response.status_code != 200 or len(response.content) < 1000:
        return None
    return response.content


def _send_text(token: str, chat_id: str, caption: str) -> None:
    response = cffi_requests.post(
        f"{TG_API}/bot{token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"telegram error: {body}")


def telegram_post(ad: dict, dry_run: bool) -> None:
    enrich_images(ad)
    images = ad.get("images") or ([ad["image"]] if ad.get("image") else [])
    if dry_run:
        log.info("DRY RUN %s %s %s photos=%s", ad["id"], ad["price_text"], ad["title"], len(images))
        return
    token = telegram_token()
    chat_id = telegram_chat_id()
    caption = format_caption(ad)
    if not images:
        _send_text(token, chat_id, caption)
        return

    files: list[dict] = []
    media: list[dict] = []
    for index, url in enumerate(images):
        blob = _download_image(url)
        if not blob:
            continue
        field = f"photo{index}"
        files.append(
            {
                "name": field,
                "content_type": "image/jpeg",
                "filename": f"{index}.jpg",
                "data": blob,
            }
        )
        item: dict = {"type": "photo", "media": f"attach://{field}"}
        if not media:
            item["caption"] = caption
            item["parse_mode"] = "HTML"
        media.append(item)
        if len(media) >= config.MAX_PHOTOS:
            break

    if not media:
        _send_text(token, chat_id, caption)
        return

    if len(media) == 1:
        endpoint = f"{TG_API}/bot{token}/sendPhoto"
        form = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
        parts = [{**files[0], "name": "photo"}]
    else:
        endpoint = f"{TG_API}/bot{token}/sendMediaGroup"
        form = {"chat_id": str(chat_id), "media": json.dumps(media)}
        parts = files

    mp = CurlMime.from_list(parts)
    try:
        response = cffi_requests.post(endpoint, data=form, multipart=mp, timeout=90)
    finally:
        mp.close()
    if response.status_code != 200 or not response.json().get("ok"):
        log.warning("фото не ушли (%s), шлём текстом: %s", response.status_code, response.text[:300])
        _send_text(token, chat_id, caption)
        return
    log.info("отправил %s, фото %s", ad["id"], len(media))


def collect_listings() -> list[dict]:
    seen_ids: set[str] = set()
    listings: list[dict] = []
    for page in range(1, config.MAX_PAGES + 1):
        url = build_search_url(page)
        log.info("качаю %s", url)
        html = fetch_html(url)
        page_ads = parse_listings(html)
        if not page_ads:
            log.warning("на странице %s нет объявлений", page)
            break
        for ad in page_ads:
            if ad["id"] in seen_ids:
                continue
            seen_ids.add(ad["id"])
            listings.append(ad)
        time.sleep(0.8)
    return listings


def run_once(dry_run: bool) -> int:
    listings = collect_listings()
    if not listings:
        log.warning("выдача пустая")
        return 0

    for ad in listings[:3]:
        log.info("пример: %s | %s | %s | %s", ad["id"], ad["price_text"], ad["location"], ad["title"])

    seen_path = Path(config.SEEN_FILE)
    seen = load_seen(seen_path)
    known = set(seen)

    if not known and config.SEED_ON_FIRST_RUN:
        save_seen(seen_path, [ad["id"] for ad in listings] + seen)
        log.info("первый прогон: запомнил %s объявлений, в группу не пишу", len(listings))
        return 0

    fresh = [ad for ad in listings if ad["id"] not in known]
    # новые сверху — публикуем от старых к новым, чтобы в чате свежие были последними
    fresh.reverse()
    log.info("всего %s, новых %s", len(listings), len(fresh))

    posted = 0
    for ad in fresh:
        try:
            telegram_post(ad, dry_run=dry_run)
            posted += 1
            if not dry_run:
                time.sleep(0.5)
        except Exception:
            log.exception("не смог запостить %s", ad["id"])
            break
        seen.insert(0, ad["id"])

    if posted or not seen_path.exists():
        save_seen(seen_path, seen)
    return posted


def post_latest(dry_run: bool) -> None:
    listings = collect_listings()
    if not listings:
        raise SystemExit("выдача пустая")
    ad = listings[0]
    log.info("постю последнее: %s | %s | %s", ad["id"], ad["price_text"], ad["title"])
    telegram_post(ad, dry_run=dry_run)
    seen_path = Path(config.SEEN_FILE)
    seen = load_seen(seen_path)
    if ad["id"] in seen:
        seen.remove(ad["id"])
    seen.insert(0, ad["id"])
    save_seen(seen_path, seen)


def main() -> None:
    parser = argparse.ArgumentParser(description="Halooglasi → Telegram")
    parser.add_argument("--once", action="store_true", help="один прогон и выход")
    parser.add_argument("--dry-run", action="store_true", help="не постить в Telegram")
    parser.add_argument("--post-latest", action="store_true", help="отправить самое свежее объявление и выйти")
    args = parser.parse_args()
    dry_run = args.dry_run or config.DRY_RUN
    _load_cf()

    if not dry_run and (not telegram_token() or not telegram_chat_id()):
        raise SystemExit("заполни TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в config.py или .env")

    if args.post_latest:
        post_latest(dry_run=dry_run)
        return

    log.info("старт, url=%s, poll=%ss", build_search_url(), config.POLL_SECONDS)
    while True:
        try:
            run_once(dry_run=dry_run)
        except Exception:
            log.exception("прогон упал")
        if args.once:
            return
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()

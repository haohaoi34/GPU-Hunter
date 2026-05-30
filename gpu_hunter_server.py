#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import http.client
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


VAST_API_KEY = ""
CLORE_API_KEY = ""
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
VAST_JUPYTER_TOKEN = ""

TARGET_GPUS = ["RTX 5090", "RTX 4090"]
PRICE_CAPS: dict[str, float] = {}
GPU_COUNT_TARGETS = [1, 2, 4, 8, 9]

VAST_HOST = "console.vast.ai"
VAST_BASE_URL = f"https://{VAST_HOST}/api/v0"
VAST_IMAGE = "vastai/base-image:cuda-13.0.2-auto"
VAST_DISK_GB = 50
VAST_RUNTYPE = "jupyter_direc ssh_direc ssh_proxy"
VAST_MIN_RELIABILITY = 0.99
VAST_JUPYTER_DIR = "/"
VAST_ONSTART = "entrypoint.sh"
VAST_JUPYTER_ENV = {
    "-p 1111:1111": "1",
    "-p 6006:6006": "1",
    "-p 8080:8080": "1",
    "-p 8384:8384": "1",
    "OPEN_BUTTON_PORT": "1111",
    "OPEN_BUTTON_TOKEN": "",
    "JUPYTER_DIR": VAST_JUPYTER_DIR,
    "DATA_DIRECTORY": "/workspace/",
    "PORTAL_CONFIG": (
        "localhost:1111:11111:/:Instance Portal|"
        "localhost:8080:18080:/:Jupyter|"
        "localhost:8080:8080:/terminals/1:Jupyter Terminal|"
        "localhost:8384:18384:/:Syncthing|"
        "localhost:6006:16006:/:Tensorboard"
    ),
}

CLORE_WEBAPI_BASE = "https://clore.ai/webapi"
CLORE_API_BASE = "https://api.clore.ai"
CLORE_IMAGE = "cloreai/jupyter:ubuntu24.04-v2"
CLORE_CURRENCY = "USD-Blockchain"
CLORE_PORTS = {"22": "tcp", "8888": "http"}
CLORE_ACCESS_PASSWORD = ""
CLORE_DEFAULT_RENTER_FEE_PCT = 5.0

REQUEST_TIMEOUT_SECONDS = 4
CLORE_TIMEOUT_SECONDS = 5
DEFAULT_INTERVAL = 0.1
DEFAULT_VAST_WORKERS = 8
BAD_PROXY_COOLDOWN_SECONDS = 60.0
DNS_FAILURE_COOLDOWN_SECONDS = 2.0
RECENT_EVENTS_LIMIT = 12
NOTIFY_DEDUP_SECONDS = 60.0
CLORE_RENTED_COOLDOWN_SECONDS = 300.0
CLORE_CREATE_ORDER_COOLDOWN_SECONDS = 5.2
CLORE_ORDER_LOOKUP_ATTEMPTS = 10
CLORE_ORDER_LOOKUP_DELAY_SECONDS = 2.0
VAST_OFFER_COOLDOWN_SECONDS = 60.0
VAST_RACE_ERROR_PATTERNS = ("no_such_ask", "is not available", "already rented")


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def short_error(exc: BaseException | str, limit: int = 240) -> str:
    text = str(exc).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def format_age(ts: float | None) -> str:
    if not ts:
        return "-"
    seconds = max(0, int(time.time() - ts))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def money(value: float | None) -> str:
    if value is None:
        return "-"
    return f"${value:.4f}"


def count_label(count: int) -> str:
    return "9x+" if count == 9 else f"{count}x"


def normalize_gpu_name(gpu_name: str) -> str:
    return gpu_name.replace(" ", "_")


def parse_proxy_line(line: str) -> str:
    text = line.strip()
    if not text or text.startswith("#"):
        return ""
    if text.startswith(("http://", "https://")):
        return text
    parts = text.split(":")
    if len(parts) == 4:
        host, port, username, password = [part.strip() for part in parts]
        if host and port and username and password:
            user = urllib.parse.quote(username, safe="")
            pwd = urllib.parse.quote(password, safe="")
            return f"http://{user}:{pwd}@{host}:{port}/"
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return f"http://{parts[0].strip()}:{parts[1].strip()}/"
    return ""


def fail_config(message: str) -> None:
    raise SystemExit(f"配置缺失: {message}")


def ask_required_secret(label: str) -> str:
    while True:
        value = getpass.getpass(f"{label}: ").strip()
        if value:
            return value
        print(f"{label} 不能为空。")


def ask_optional_secret(label: str) -> str:
    return getpass.getpass(f"{label}（可选，直接回车跳过）: ").strip()


def ask_optional_text(label: str) -> str:
    return input(f"{label}（可选，直接回车跳过）: ").strip()


def env_first(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def pick_required_value(args: argparse.Namespace, attr: str, env_names: tuple[str, ...], label: str) -> str:
    value = (getattr(args, attr, "") or "").strip() or env_first(*env_names)
    if value:
        return value
    if args.no_prompt:
        fail_config(f"请通过 --{attr.replace('_', '-')} 或环境变量 {env_names[0]} 提供 {label}")
    return ask_required_secret(label)


def pick_optional_value(args: argparse.Namespace, attr: str, env_names: tuple[str, ...], label: str) -> str:
    value = getattr(args, attr, None)
    if value is not None:
        return str(value).strip()
    value = env_first(*env_names)
    if value or args.no_prompt:
        return value
    return ask_optional_secret(label)


def ask_price_cap(gpu: str) -> float:
    while True:
        raw = input(f"{gpu} 单卡最高价格 USD/h: ").strip()
        if not raw:
            print("价格不能为空，请输入数字，例如 0.7")
            continue
        try:
            value = float(raw)
        except ValueError:
            print("请输入数字，例如 0.7")
            continue
        if value <= 0:
            print("价格必须大于 0")
            continue
        return value


def price_from_env(*names: str) -> float | None:
    value = env_first(*names)
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        fail_config(f"环境变量 {names[0]} 必须是数字")
        raise exc
    if parsed <= 0:
        fail_config(f"环境变量 {names[0]} 必须大于 0")
    return parsed


def configure_price_caps(args: argparse.Namespace) -> None:
    global PRICE_CAPS
    price_5090 = args.price_5090 if args.price_5090 is not None else price_from_env("PRICE_5090", "GPU_HUNTER_PRICE_5090")
    price_4090 = args.price_4090 if args.price_4090 is not None else price_from_env("PRICE_4090", "GPU_HUNTER_PRICE_4090")
    if price_5090 is not None or price_4090 is not None or args.no_prompt:
        if price_5090 is None:
            fail_config("请提供 RTX 5090 价格，例如 --price-5090 0.7 或环境变量 PRICE_5090")
        if price_4090 is None:
            fail_config("请提供 RTX 4090 价格，例如 --price-4090 0.4 或环境变量 PRICE_4090")
        if float(price_5090) <= 0 or float(price_4090) <= 0:
            fail_config("价格必须大于 0")
        PRICE_CAPS = {
            "RTX 5090": float(price_5090),
            "RTX 4090": float(price_4090),
        }
        return
    print("启动前设置抢购价格，单位是单卡 USD/h。")
    PRICE_CAPS = {
        "RTX 5090": ask_price_cap("RTX 5090"),
        "RTX 4090": ask_price_cap("RTX 4090"),
    }


def configure_credentials(args: argparse.Namespace) -> None:
    global VAST_API_KEY, CLORE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, VAST_JUPYTER_TOKEN, CLORE_ACCESS_PASSWORD
    VAST_API_KEY = pick_required_value(args, "vast_api_key", ("VAST_API_KEY", "GPU_HUNTER_VAST_API_KEY"), "Vast API Key")
    CLORE_API_KEY = pick_required_value(args, "clore_api_key", ("CLORE_API_KEY", "GPU_HUNTER_CLORE_API_KEY"), "Clore API Key")
    TELEGRAM_BOT_TOKEN = pick_optional_value(
        args,
        "telegram_bot_token",
        ("TELEGRAM_BOT_TOKEN", "GPU_HUNTER_TELEGRAM_BOT_TOKEN"),
        "Telegram Bot Token",
    )
    TELEGRAM_CHAT_ID = pick_optional_value(
        args,
        "telegram_chat_id",
        ("TELEGRAM_CHAT_ID", "GPU_HUNTER_TELEGRAM_CHAT_ID"),
        "Telegram Chat ID",
    )
    VAST_JUPYTER_TOKEN = (args.vast_jupyter_token or env_first("VAST_JUPYTER_TOKEN", "GPU_HUNTER_VAST_JUPYTER_TOKEN")).strip()
    if not VAST_JUPYTER_TOKEN and not args.no_prompt:
        VAST_JUPYTER_TOKEN = ask_optional_text("Vast Jupyter Token")
    if not VAST_JUPYTER_TOKEN:
        VAST_JUPYTER_TOKEN = secrets.token_urlsafe(12)
        print("Vast Jupyter Token 未填写，已为本次运行自动生成随机 token。")
    VAST_JUPYTER_ENV["OPEN_BUTTON_TOKEN"] = VAST_JUPYTER_TOKEN
    CLORE_ACCESS_PASSWORD = (args.clore_password or env_first("CLORE_ACCESS_PASSWORD", "GPU_HUNTER_CLORE_PASSWORD")).strip()
    if not CLORE_ACCESS_PASSWORD and not args.no_prompt:
        CLORE_ACCESS_PASSWORD = ask_optional_text("Clore SSH/Jupyter 密码")
    if not CLORE_ACCESS_PASSWORD:
        CLORE_ACCESS_PASSWORD = secrets.token_urlsafe(12)
        print("Clore SSH/Jupyter 密码未填写，已为本次运行自动生成随机密码。")
    if not CLORE_ACCESS_PASSWORD:
        fail_config("Clore SSH/Jupyter 密码不能为空")


def ask_proxy_lines() -> list[str]:
    print("代理 IP（可选）：逐行粘贴，空行结束；支持 http://user:pass@ip:port/、ip:port:user:pass、ip:port。")
    lines: list[str] = []
    while True:
        try:
            line = input("proxy> ").strip()
        except EOFError:
            break
        if not line:
            break
        lines.append(line)
    return lines


def configure_inline_proxies(args: argparse.Namespace) -> list[str]:
    inline = list(args.proxy or [])
    if args.no_proxy:
        return inline
    if args.no_prompt or args.proxy_file or inline or env_first("PEARL_PROXIES", "GPU_HUNTER_PROXIES"):
        return inline
    return inline + ask_proxy_lines()


def safe_cwd() -> Path | None:
    try:
        return Path.cwd()
    except OSError:
        return None


class ProxyPool:
    def __init__(self, proxy_file: str = "", inline_proxies: list[str] | None = None) -> None:
        self.lock = threading.Lock()
        self.index = 0
        self.loaded_from = ""
        self.bad_until: dict[str, float] = {}
        self.proxy_file = proxy_file
        self.inline_proxies = inline_proxies or []
        self.proxies: list[str] = []

    def _default_proxy_file(self) -> Path | None:
        if self.proxy_file:
            path = Path(self.proxy_file).expanduser()
            return path if path.exists() else None
        return None

    def _load_unlocked(self) -> None:
        path = self._default_proxy_file()
        env_proxies = env_first("PEARL_PROXIES", "GPU_HUNTER_PROXIES")
        source_id = "|".join(
            [
                "\n".join(self.inline_proxies),
                env_proxies,
                str(path or ""),
                str(path.stat().st_mtime if path and path.exists() else ""),
            ]
        )
        if source_id == self.loaded_from and self.proxies:
            return
        lines: list[str] = list(self.inline_proxies)
        if env_proxies:
            lines.extend(env_proxies.replace(",", "\n").splitlines())
        if path and path.exists():
            try:
                lines.extend(path.read_text(encoding="utf-8").splitlines())
            except OSError:
                pass
        proxies = [proxy for proxy in (parse_proxy_line(line) for line in lines) if proxy]
        self.proxies = list(dict.fromkeys(proxies))
        self.bad_until = {proxy: until for proxy, until in self.bad_until.items() if proxy in self.proxies}
        self.loaded_from = source_id
        if self.index >= len(self.proxies):
            self.index = 0

    def count(self) -> int:
        with self.lock:
            self._load_unlocked()
            return len(self.proxies)

    def source(self) -> str:
        sources: list[str] = []
        if self.inline_proxies:
            sources.append("manual input")
        if env_first("PEARL_PROXIES", "GPU_HUNTER_PROXIES"):
            sources.append("env")
        path = self._default_proxy_file()
        if path:
            sources.append(str(path))
        return " + ".join(sources) if sources else "none"

    def next_proxy(self) -> str:
        with self.lock:
            self._load_unlocked()
            if not self.proxies:
                return ""
            now = time.monotonic()
            for _ in range(len(self.proxies)):
                proxy = self.proxies[self.index % len(self.proxies)]
                self.index = (self.index + 1) % len(self.proxies)
                if self.bad_until.get(proxy, 0) <= now:
                    return proxy
            proxy = self.proxies[self.index % len(self.proxies)]
            self.index = (self.index + 1) % len(self.proxies)
            return proxy

    def mark_bad(self, proxy: str) -> None:
        if not proxy:
            return
        with self.lock:
            self.bad_until[proxy] = time.monotonic() + BAD_PROXY_COOLDOWN_SECONDS

    def requests_proxies(self) -> tuple[dict[str, str] | None, str]:
        proxy = self.next_proxy()
        if not proxy:
            return None, ""
        return {"http": proxy, "https": proxy}, proxy

    def open_request(self, req: urllib.request.Request, timeout: int, proxy: str = "") -> Any:
        if not proxy:
            return urllib.request.urlopen(req, timeout=timeout)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return opener.open(req, timeout=timeout)

    def urlopen(self, req: urllib.request.Request, timeout: int, use_proxy: bool = True) -> Any:
        if not use_proxy or self.count() <= 0:
            return urllib.request.urlopen(req, timeout=timeout)
        last_error: Exception | None = None
        attempts = min(self.count(), 8)
        for _ in range(attempts):
            proxy = self.next_proxy()
            if not proxy:
                break
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            try:
                return opener.open(req, timeout=timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                self.mark_bad(proxy)
        if last_error is not None:
            raise last_error
        return urllib.request.urlopen(req, timeout=timeout)


@dataclass
class TargetStatus:
    platform: str
    gpu: str
    label: str
    cap: float
    offers: int = 0
    best_unit: float | None = None
    best_total: float | None = None
    best_cards: int | None = None
    best_id: str = "-"
    last_error: str = ""
    updated_at: float | None = None
    hits: int = 0
    attempts: int = 0
    failures: int = 0
    successes: int = 0


@dataclass
class AppState:
    started_at: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)
    iterations: dict[str, int] = field(default_factory=lambda: {"Vast": 0, "Clore": 0})
    statuses: dict[str, TargetStatus] = field(default_factory=dict)
    events: deque[str] = field(default_factory=lambda: deque(maxlen=RECENT_EVENTS_LIMIT))
    last_notify: dict[str, float] = field(default_factory=dict)
    clore_skip_until: dict[int, float] = field(default_factory=dict)
    vast_skip_until: dict[int, float] = field(default_factory=dict)
    last_clore_order_attempt: float = 0.0
    telegram_failures: int = 0

    def key(self, platform: str, gpu: str, label: str) -> str:
        return f"{platform}|{gpu}|{label}"

    def ensure_status(self, platform: str, gpu: str, label: str, cap: float) -> TargetStatus:
        key = self.key(platform, gpu, label)
        item = self.statuses.get(key)
        if item is None:
            item = TargetStatus(platform=platform, gpu=gpu, label=label, cap=cap)
            self.statuses[key] = item
        return item

    def add_event(self, message: str) -> None:
        with self.lock:
            self.events.appendleft(f"[{now_text()}] {message}")

    def update_status(self, platform: str, gpu: str, label: str, cap: float, **updates: Any) -> None:
        with self.lock:
            item = self.ensure_status(platform, gpu, label, cap)
            for key, value in updates.items():
                setattr(item, key, value)
            item.updated_at = time.time()

    def bump(self, platform: str, gpu: str, label: str, cap: float, field_name: str) -> None:
        with self.lock:
            item = self.ensure_status(platform, gpu, label, cap)
            setattr(item, field_name, getattr(item, field_name) + 1)
            item.updated_at = time.time()

    def should_notify(self, key: str, cooldown: float = NOTIFY_DEDUP_SECONDS) -> bool:
        with self.lock:
            now = time.time()
            if now - self.last_notify.get(key, 0) < cooldown:
                return False
            self.last_notify[key] = now
            return True

    def cool_down_clore_server(self, server_id: int, seconds: float = CLORE_RENTED_COOLDOWN_SECONDS) -> None:
        with self.lock:
            self.clore_skip_until[server_id] = time.time() + seconds

    def should_skip_clore_server(self, server_id: int) -> bool:
        with self.lock:
            until = self.clore_skip_until.get(server_id, 0)
            if until <= time.time():
                self.clore_skip_until.pop(server_id, None)
                return False
            return True

    def cool_down_vast_offer(self, offer_id: int, seconds: float = VAST_OFFER_COOLDOWN_SECONDS) -> None:
        with self.lock:
            self.vast_skip_until[offer_id] = time.time() + seconds

    def should_skip_vast_offer(self, offer_id: int) -> bool:
        with self.lock:
            until = self.vast_skip_until.get(offer_id, 0)
            if until <= time.time():
                self.vast_skip_until.pop(offer_id, None)
                return False
            return True

    def claim_clore_order_slot(self) -> bool:
        with self.lock:
            now = time.time()
            if now - self.last_clore_order_attempt < CLORE_CREATE_ORDER_COOLDOWN_SECONDS:
                return False
            self.last_clore_order_attempt = now
            return True

    def snapshot(self) -> tuple[dict[str, int], list[TargetStatus], list[str], int]:
        with self.lock:
            gpu_order = {gpu: index for index, gpu in enumerate(TARGET_GPUS)}
            platform_order = {"Vast": 0, "Clore": 1}
            statuses = sorted(
                self.statuses.values(),
                key=lambda item: (
                    platform_order.get(item.platform, 9),
                    gpu_order.get(item.gpu, 99),
                    GPU_COUNT_TARGETS.index(9 if item.label == "9x+" else int(item.label.rstrip("x"))) if item.label != "any" else 99,
                ),
            )
            return dict(self.iterations), [TargetStatus(**vars(item)) for item in statuses], list(self.events), self.telegram_failures


def telegram_notify(state: AppState, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not data.get("ok"):
            raise RuntimeError(raw)
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.telegram_failures += 1
        state.add_event(f"Telegram 通知失败: {short_error(exc)}")


class VastClient:
    def __init__(self, proxy_pool: ProxyPool) -> None:
        self.proxy_pool = proxy_pool
        self.dns_failed_until = 0.0
        self.dns_lock = threading.Lock()

    def dns_available(self) -> bool:
        with self.dns_lock:
            if time.monotonic() < self.dns_failed_until:
                return False
        try:
            socket.getaddrinfo(VAST_HOST, 443, type=socket.SOCK_STREAM)
            return True
        except OSError:
            with self.dns_lock:
                self.dns_failed_until = time.monotonic() + DNS_FAILURE_COOLDOWN_SECONDS
            return False

    def put(self, path: str, payload: dict[str, Any], use_proxy: bool) -> dict[str, Any]:
        if not self.dns_available():
            raise RuntimeError(f"Vast DNS 暂时不可用：无法解析 {VAST_HOST}")
        last_error: Exception | None = None
        for attempt in range(3):
            proxy = ""
            if use_proxy:
                proxy = self.proxy_pool.next_proxy()
            body = json.dumps(payload).encode("utf-8")
            url = f"{VAST_BASE_URL}{path}?api_key={urllib.parse.quote(VAST_API_KEY)}"
            req = urllib.request.Request(
                url,
                data=body,
                method="PUT",
                headers={
                    "Authorization": f"Bearer {VAST_API_KEY}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            try:
                with self.proxy_pool.open_request(req, timeout=REQUEST_TIMEOUT_SECONDS, proxy=proxy) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Vast HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                self.proxy_pool.mark_bad(proxy)
                time.sleep(0.05 * (attempt + 1))
        if last_error is not None:
            raise last_error
        raise RuntimeError("Vast 请求失败")

    def search(self, gpu: str, count: int, use_proxy: bool) -> list[dict[str, Any]]:
        parsed: dict[str, Any] = {
            "verified": {"eq": True},
            "external": {"eq": False},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "reliability": {"gte": VAST_MIN_RELIABILITY},
            "num_gpus": {"gte" if count == 9 else "eq": count},
            "gpu_name": {"eq": gpu},
            "order": [["score", "desc"]],
            "type": "on-demand",
            "limit": 20,
            "allocated_storage": float(VAST_DISK_GB),
        }
        data = self.put("/search/asks/", {"q": parsed}, use_proxy=use_proxy)
        return data.get("offers", []) if isinstance(data, dict) else []

    def create_instance(self, offer_id: int) -> dict[str, Any]:
        payload = {
            "client_id": "me",
            "image": VAST_IMAGE,
            "disk": VAST_DISK_GB,
            "runtype": VAST_RUNTYPE,
            "env": VAST_JUPYTER_ENV.copy(),
            "use_jupyter_lab": True,
            "jupyter_dir": VAST_JUPYTER_DIR,
            "onstart": VAST_ONSTART,
        }
        return self.put(f"/asks/{offer_id}/", payload, use_proxy=False)


def vast_per_gpu_price(offer: dict[str, Any]) -> float | None:
    try:
        total = float(offer.get("dph_total"))
        count = int(offer.get("num_gpus") or 0)
    except (TypeError, ValueError):
        return None
    if count <= 0:
        return None
    return total / count


def is_vast_race_error(error: BaseException | str) -> bool:
    text = str(error).lower()
    return any(pattern in text for pattern in VAST_RACE_ERROR_PATTERNS)


def vast_candidate_offers(state: AppState, offers: list[dict[str, Any]], cap: float) -> list[dict[str, Any]]:
    winners = [
        offer
        for offer in offers
        if (vast_per_gpu_price(offer) or 1e9) <= cap and not state.should_skip_vast_offer(int(offer.get("id") or 0))
    ]
    winners.sort(key=lambda offer: (vast_per_gpu_price(offer) or 1e9, float(offer.get("dph_total") or 1e9)))
    return winners


def run_vast_once(args: argparse.Namespace, state: AppState, client: VastClient) -> None:
    if not client.dns_available():
        state.add_event(f"Vast DNS 暂时不可用：无法解析 {VAST_HOST}")
        return
    targets = [(gpu, count) for gpu in TARGET_GPUS for count in GPU_COUNT_TARGETS]

    def search_target(gpu: str, count: int) -> tuple[str, int, list[dict[str, Any]], list[dict[str, Any]]]:
        offers = client.search(gpu, count, use_proxy=not args.no_proxy)
        return gpu, count, offers, vast_candidate_offers(state, offers, PRICE_CAPS[gpu])

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.vast_workers)) as executor:
        futures = {executor.submit(search_target, gpu, count): (gpu, count) for gpu, count in targets}
        for future in concurrent.futures.as_completed(futures):
            gpu, count = futures[future]
            label = count_label(count)
            cap = PRICE_CAPS[gpu]
            try:
                _, _, offers, candidates = future.result()
            except Exception as exc:  # noqa: BLE001
                state.update_status("Vast", gpu, label, cap, last_error=short_error(exc))
                continue
            best = candidates[0] if candidates else None
            best_for_display = best
            if best_for_display is None and offers:
                best_for_display = sorted(offers, key=lambda item: vast_per_gpu_price(item) or 1e9)[0]
            unit = vast_per_gpu_price(best_for_display) if best_for_display else None
            total = float(best_for_display.get("dph_total") or 0) if best_for_display else None
            cards = int(best_for_display.get("num_gpus") or 0) if best_for_display else None
            best_id = str(best_for_display.get("id") or "-") if best_for_display else "-"
            state.update_status(
                "Vast",
                gpu,
                label,
                cap,
                offers=len(offers),
                best_unit=unit,
                best_total=total,
                best_cards=cards,
                best_id=best_id,
                last_error="",
            )
            if not candidates:
                continue
            for best in candidates[:5]:
                state.bump("Vast", gpu, label, cap, "hits")
                offer_id = int(best.get("id"))
                machine_id = best.get("machine_id")
                total = float(best.get("dph_total") or 0)
                cards = int(best.get("num_gpus") or 0)
                unit = vast_per_gpu_price(best) or 0.0
                hit_key = f"vast-hit-{offer_id}"
                hit_message = (
                    f"Vast 命中价格\nGPU: {gpu} {label}\n实际卡数: {cards}\n"
                    f"单卡均价: ${unit:.4f}/h\n总价: ${total:.4f}/h\noffer: {offer_id}\nhost: {machine_id}"
                )
                state.add_event(f"Vast 命中 {gpu} {label} offer={offer_id} unit=${unit:.4f}/h")
                if state.should_notify(hit_key):
                    telegram_notify(state, hit_message)
                if args.dry_run:
                    break
                state.bump("Vast", gpu, label, cap, "attempts")
                try:
                    result = client.create_instance(offer_id)
                except Exception as exc:  # noqa: BLE001
                    state.bump("Vast", gpu, label, cap, "failures")
                    state.cool_down_vast_offer(offer_id)
                    reason = short_error(exc, 700)
                    if is_vast_race_error(exc):
                        state.add_event(f"Vast 竞速失败 offer={offer_id}: 已被抢走，继续下一个")
                        continue
                    state.add_event(f"Vast 下单失败 offer={offer_id}: {reason}")
                    telegram_notify(state, f"Vast 下单失败\nGPU: {gpu} {label}\noffer: {offer_id}\n原因: {reason}")
                    continue
                state.bump("Vast", gpu, label, cap, "successes")
                state.cool_down_vast_offer(offer_id)
                state.add_event(f"Vast 下单成功 offer={offer_id}")
                telegram_notify(state, f"Vast 抢机成功\nGPU: {gpu} {label}\noffer: {offer_id}\n结果: {json.dumps(result, ensure_ascii=False)[:900]}")
                break


def clore_request(proxy_pool: ProxyPool, method: str, base_url: str, path: str, payload: dict[str, Any] | None, use_proxy: bool) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    last_error: Exception | None = None
    attempts = min(proxy_pool.count(), 8) if use_proxy and proxy_pool.count() > 0 else 1
    for _ in range(max(1, attempts)):
        proxy = proxy_pool.next_proxy() if use_proxy else ""
        req = urllib.request.Request(
            f"{base_url}{path}",
            data=body,
            method=method,
            headers={
                "auth": CLORE_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )
        try:
            with proxy_pool.open_request(req, timeout=CLORE_TIMEOUT_SECONDS, proxy=proxy) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (301, 302, 307, 308, 429, 500, 502, 503, 504):
                proxy_pool.mark_bad(proxy)
                continue
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Clore HTTP {exc.code}: {short_error(detail, 700)}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead) as exc:
            last_error = exc
            proxy_pool.mark_bad(proxy)
            continue
    if last_error is not None and use_proxy:
        req = urllib.request.Request(
            f"{base_url}{path}",
            data=body,
            method=method,
            headers={
                "auth": CLORE_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=CLORE_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Clore HTTP {exc.code}: {short_error(detail, 700)}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead) as exc:
            raise RuntimeError(f"Clore 请求失败: {short_error(exc, 240)}") from exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("Clore 请求失败")


def clore_gpu_name(server: dict[str, Any]) -> str:
    specs = server.get("specs") or {}
    raw = str(specs.get("gpu") or "").strip()
    if raw:
        return raw
    arr = server.get("gpu_array") or []
    return f"{len(arr)}x {arr[0]}" if isinstance(arr, list) and arr else ""


def clore_gpu_count(server: dict[str, Any]) -> int:
    match = re.search(r"(\d+)\s*x", clore_gpu_name(server), flags=re.IGNORECASE)
    if match:
        return max(1, int(match.group(1)))
    arr = server.get("gpu_array")
    if isinstance(arr, list) and arr:
        return len(arr)
    return 1


def clore_allows_usd(server: dict[str, Any]) -> bool:
    allowed = server.get("allowed_coins")
    return not isinstance(allowed, list) or CLORE_CURRENCY in allowed


def clore_day_price(server: dict[str, Any]) -> float | None:
    try:
        return float(((server.get("price") or {}).get("on_demand") or {}).get(CLORE_CURRENCY))
    except Exception:
        return None


def clore_fee_pct(fees: dict[str, Any] | None) -> float:
    try:
        on_demand = ((fees or {}).get("renter_fees") or {}).get("on_demand") or {}
        base = (on_demand.get("base") or {}).get(CLORE_CURRENCY, CLORE_DEFAULT_RENTER_FEE_PCT)
        extra = (on_demand.get("extra") or {}).get(CLORE_CURRENCY, 0)
        return max(0.0, float(base) + float(extra))
    except Exception:
        return CLORE_DEFAULT_RENTER_FEE_PCT


def clore_total_hour(server: dict[str, Any], fees: dict[str, Any] | None) -> float | None:
    day = clore_day_price(server)
    if day is None:
        return None
    return day * (1 + clore_fee_pct(fees) / 100) / 24


def clore_unit_hour(server: dict[str, Any], fees: dict[str, Any] | None) -> float | None:
    total = clore_total_hour(server, fees)
    count = clore_gpu_count(server)
    if total is None or count <= 0:
        return None
    return total / count


def clore_matches(server: dict[str, Any], gpu: str) -> bool:
    return gpu.upper() in clore_gpu_name(server).upper()


def choose_clore_server(state: AppState, servers: list[dict[str, Any]], gpu: str, fees: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for server in servers:
        if server.get("rented") is True:
            continue
        try:
            sid = int(server.get("id"))
            if state.should_skip_clore_server(sid):
                continue
        except Exception:
            pass
        if not clore_allows_usd(server):
            continue
        if not clore_matches(server, gpu):
            continue
        if clore_unit_hour(server, fees) is None:
            continue
        candidates.append(server)
    candidates.sort(key=lambda item: (clore_unit_hour(item, fees) or 1e9, clore_total_hour(item, fees) or 1e9, -clore_gpu_count(item)))
    winners = [server for server in candidates if (clore_unit_hour(server, fees) or 1e9) <= PRICE_CAPS[gpu]]
    return winners, candidates


def is_clore_race_result(result: dict[str, Any]) -> bool:
    error = str(result.get("error") or "").strip().lower()
    return error in {"server-already-rented", "already-rented", "already rented"}


def extract_clore_order_id(result: dict[str, Any]) -> int | None:
    for key in ("id", "order_id", "order"):
        value = result.get(key)
        if isinstance(value, dict):
            value = value.get("id") or value.get("order_id")
        try:
            if value is not None:
                return int(value)
        except Exception:
            continue
    return None


def clore_order_access(order: dict[str, Any]) -> dict[str, str]:
    http_pub = str(order.get("http_pub") or "").strip()
    http_port = str(order.get("http_port") or "").strip()
    web_url = f"https://{http_pub}" if http_pub else ""
    if web_url and http_port and http_port != "8888":
        web_url = f"{web_url}:{http_port}"
    ssh_host = ""
    pub_cluster = order.get("pub_cluster")
    if isinstance(pub_cluster, list) and pub_cluster:
        ssh_host = str(pub_cluster[0]).strip()
    elif isinstance(pub_cluster, str):
        ssh_host = pub_cluster.strip()
    ssh_port = ""
    tcp_ports = order.get("tcp_ports")
    if isinstance(tcp_ports, list):
        for item in tcp_ports:
            left, sep, right = str(item).partition(":")
            if sep and left.strip() == "22":
                ssh_port = right.strip()
                break
    ssh_command = f"ssh root@{ssh_host} -p {ssh_port}" if ssh_host and ssh_port else ""
    return {"web_url": web_url, "ssh_command": ssh_command}


def get_clore_orders(proxy_pool: ProxyPool, use_proxy: bool) -> list[dict[str, Any]]:
    data = clore_request(proxy_pool, "GET", CLORE_API_BASE, "/v1/my_orders?return_completed=false", None, use_proxy=use_proxy)
    orders = data.get("orders") if isinstance(data, dict) else None
    return [order for order in orders if isinstance(order, dict)] if isinstance(orders, list) else []


def find_clore_order(
    proxy_pool: ProxyPool,
    server_id: int,
    result: dict[str, Any],
    use_proxy: bool,
    attempts: int = CLORE_ORDER_LOOKUP_ATTEMPTS,
) -> dict[str, Any] | None:
    expected_order_id = extract_clore_order_id(result)
    for attempt in range(attempts):
        if attempt:
            time.sleep(CLORE_ORDER_LOOKUP_DELAY_SECONDS)
        orders = get_clore_orders(proxy_pool, use_proxy=use_proxy)
        for order in orders:
            try:
                if expected_order_id is not None and int(order.get("id")) == expected_order_id:
                    return order
            except Exception:
                pass
            try:
                if int(order.get("si")) == int(server_id):
                    return order
            except Exception:
                pass
    return None


def create_clore_order(proxy_pool: ProxyPool, server_id: int, required_price: float | None, use_proxy: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "currency": CLORE_CURRENCY,
        "image": CLORE_IMAGE,
        "jupyter_token": CLORE_ACCESS_PASSWORD,
        "ports": CLORE_PORTS,
        "renting_server": int(server_id),
        "ssh_password": CLORE_ACCESS_PASSWORD,
        "type": "on-demand",
    }
    if required_price is not None:
        payload["required_price"] = required_price
    return clore_request(proxy_pool, "POST", CLORE_API_BASE, "/v1/create_order", payload, use_proxy=use_proxy)


def run_clore_once(args: argparse.Namespace, state: AppState, proxy_pool: ProxyPool) -> None:
    use_proxy = not args.no_proxy
    fees = clore_request(proxy_pool, "POST", CLORE_WEBAPI_BASE, "/marketplace/renter_fees", {}, use_proxy=use_proxy)
    market = clore_request(proxy_pool, "GET", CLORE_WEBAPI_BASE, "/marketplace/servers", None, use_proxy=use_proxy)
    servers = (market.get("all_servers") or market.get("servers") or []) if isinstance(market, dict) else []
    if not isinstance(servers, list):
        raise RuntimeError(f"Clore marketplace 返回格式异常: {market}")
    for gpu in TARGET_GPUS:
        cap = PRICE_CAPS[gpu]
        winners, candidates = choose_clore_server(state, servers, gpu, fees)
        best = winners[0] if winners else None
        sample = best or (candidates[0] if candidates else None)
        label = "any"
        unit = clore_unit_hour(sample, fees) if sample else None
        total = clore_total_hour(sample, fees) if sample else None
        cards = clore_gpu_count(sample) if sample else None
        server_id = str(sample.get("id") or "-") if sample else "-"
        state.update_status(
            "Clore",
            gpu,
            label,
            cap,
            offers=len(candidates),
            best_unit=unit,
            best_total=total,
            best_cards=cards,
            best_id=server_id,
            last_error="",
        )
        if not winners:
            continue
        for best in winners[:5]:
            state.bump("Clore", gpu, label, cap, "hits")
            server_id_int = int(best.get("id"))
            count = clore_gpu_count(best)
            day_price = clore_day_price(best)
            unit = clore_unit_hour(best, fees) or 0.0
            total = clore_total_hour(best, fees) or 0.0
            hit_key = f"clore-hit-{server_id_int}"
            hit_message = (
                f"Clore 命中价格\nGPU: {gpu}\n实际卡数: {count}\n"
                f"单卡均价: ${unit:.4f}/h\n总价: ${total:.4f}/h\nserver: {server_id_int}"
            )
            state.add_event(f"Clore 命中 {gpu} server={server_id_int} unit=${unit:.4f}/h")
            if state.should_notify(hit_key):
                telegram_notify(state, hit_message)
            if args.dry_run:
                break
            if not state.claim_clore_order_slot():
                state.add_event("Clore 命中后跳过下单: create_order 官方限制 5 秒 1 次")
                break
            state.bump("Clore", gpu, label, cap, "attempts")
            try:
                result = create_clore_order(proxy_pool, server_id_int, day_price, use_proxy=use_proxy)
            except Exception as exc:  # noqa: BLE001
                state.bump("Clore", gpu, label, cap, "failures")
                state.cool_down_clore_server(server_id_int, 30)
                reason = short_error(exc, 700)
                state.add_event(f"Clore 下单失败 server={server_id_int}: {reason}")
                telegram_notify(state, f"Clore 下单失败\nGPU: {gpu}\nserver: {server_id_int}\n原因: {reason}")
                break
            if int(result.get("code", 0 if result.get("status") == "ok" else 1)) != 0 and result.get("status") != "ok":
                state.bump("Clore", gpu, label, cap, "failures")
                reason = json.dumps(result, ensure_ascii=False)[:900]
                if is_clore_race_result(result):
                    state.cool_down_clore_server(server_id_int)
                    state.add_event(f"Clore 竞速失败 server={server_id_int}: 已被抢走")
                    continue
                state.cool_down_clore_server(server_id_int, 30)
                state.add_event(f"Clore 下单失败 server={server_id_int}: {reason}")
                telegram_notify(state, f"Clore 下单失败\nGPU: {gpu}\nserver: {server_id_int}\n原因: {reason}")
                break
            state.bump("Clore", gpu, label, cap, "successes")
            state.cool_down_clore_server(server_id_int)
            access_lines: list[str] = []
            try:
                order = find_clore_order(proxy_pool, server_id_int, result, use_proxy=use_proxy)
            except Exception as exc:  # noqa: BLE001
                order = None
                access_lines.append(f"查询访问入口失败: {short_error(exc, 200)}")
            if order:
                access = clore_order_access(order)
                if access.get("web_url"):
                    access_lines.append(f"Web: {access['web_url']}")
                if access.get("ssh_command"):
                    access_lines.append(f"SSH: {access['ssh_command']}")
            else:
                access_lines.append("访问入口暂未返回，请稍后在 Clore Orders 刷新查看。")
            access_lines.append(f"SSH/Jupyter 密码: {CLORE_ACCESS_PASSWORD}")
            state.add_event(f"Clore 下单成功 server={server_id_int} {' '.join(access_lines[:1])}")
            telegram_notify(
                state,
                "Clore 抢机成功\n"
                f"GPU: {gpu}\nserver: {server_id_int}\n"
                f"镜像: {CLORE_IMAGE}\n"
                + "\n".join(access_lines)
                + f"\n结果: {json.dumps(result, ensure_ascii=False)[:700]}",
            )
            break


def platform_loop(name: str, args: argparse.Namespace, state: AppState, stop_event: threading.Event, fn) -> None:
    while not stop_event.is_set():
        with state.lock:
            state.iterations[name] = state.iterations.get(name, 0) + 1
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            state.add_event(f"{name} 本轮异常: {short_error(exc)}")
        if args.once:
            return
        if stop_event.wait(max(0.1, float(args.interval))):
            return


def render_dashboard(state: AppState, args: argparse.Namespace, proxy_pool: ProxyPool) -> None:
    sys.stdout.write("\033[2J\033[H")
    iterations, statuses, events, tg_failures = state.snapshot()
    uptime = format_age(state.started_at)
    proxy_text = "off" if args.no_proxy else f"{proxy_pool.count()} ({proxy_pool.source()})"
    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print("GPU Hunter Server | Vast + Clore")
    print(f"time: {now_text()} | uptime: {uptime} | mode: {mode} | scan: {max(0.1, args.interval):.1f}s | vast_workers: {args.vast_workers} | proxy: {proxy_text}")
    print(f"vast_iter: {iterations.get('Vast', 0)} | clore_iter: {iterations.get('Clore', 0)} | tg_fail: {tg_failures}")
    line = "-" * 112
    print(line)
    print(f"{'PLAT':<6} {'GPU':<8} {'CNT':<4} {'ASKS':>4} {'UNIT$/h':>9} {'TOTAL$/h':>10} {'REAL':>4} {'ID':>10} {'CAP':>7} {'H/A/F/S':>9} {'AGE':>5}")
    print(line)
    for item in statuses:
        stats = f"{item.hits}/{item.attempts}/{item.failures}/{item.successes}"
        print(
            f"{item.platform:<6} {item.gpu.replace('RTX ', ''):<8} {item.label:<4} {item.offers:>4} "
            f"{money(item.best_unit):>9} {money(item.best_total):>10} {str(item.best_cards or '-'):>4} "
            f"{item.best_id:>10} {item.cap:>7.2f} {stats:>9} {format_age(item.updated_at):>5}"
        )
        if item.last_error:
            print(f"  error: {item.last_error}")
    print(line)
    print("recent events")
    for event in events:
        print(event)
    if not events:
        print("-")
    sys.stdout.flush()


def dashboard_loop(args: argparse.Namespace, state: AppState, proxy_pool: ProxyPool, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        render_dashboard(state, args, proxy_pool)
        if args.once:
            return
        if stop_event.wait(max(0.2, float(args.refresh))):
            return


def build_parser() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vast + Clore 合并抢机服务器版。")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="扫描间隔，默认 0.1 秒。")
    parser.add_argument("--refresh", type=float, default=1.0, help="屏幕刷新间隔，默认 1 秒。")
    parser.add_argument("--vast-workers", type=int, default=DEFAULT_VAST_WORKERS, help="Vast 并发查询数，默认 8。")
    parser.add_argument("--dry-run", action="store_true", help="只查询和通知，不真实下单。")
    parser.add_argument("--once", action="store_true", help="每个平台只跑一轮。")
    parser.add_argument("--no-proxy", action="store_true", help="不使用代理。")
    parser.add_argument("--proxy", action="append", default=[], help="代理地址，可重复传入；支持 http://user:pass@ip:port/、ip:port:user:pass、ip:port。")
    parser.add_argument("--proxy-file", default="", help="代理文件路径，支持逐行填写 ip:port:user:pass。")
    parser.add_argument("--vast-api-key", default="", help="Vast API Key，也可用环境变量 VAST_API_KEY。")
    parser.add_argument("--clore-api-key", default="", help="Clore API Key，也可用环境变量 CLORE_API_KEY。")
    parser.add_argument("--telegram-bot-token", default=None, help="Telegram Bot Token，可选；不填则不发送 Telegram 通知。")
    parser.add_argument("--telegram-chat-id", default=None, help="Telegram Chat ID，可选；不填则不发送 Telegram 通知。")
    parser.add_argument("--vast-jupyter-token", default="", help="Vast Jupyter Token；不填会在启动时询问，仍为空则自动生成随机 token。")
    parser.add_argument("--clore-password", default="", help="Clore SSH/Jupyter 密码；不填会在启动时询问，仍为空则自动生成随机密码。")
    parser.add_argument("--price-5090", type=float, default=None, help="RTX 5090 单卡最高价格 USD/h。")
    parser.add_argument("--price-4090", type=float, default=None, help="RTX 4090 单卡最高价格 USD/h。")
    parser.add_argument("--no-prompt", action="store_true", help="不交互询问，必须通过命令行参数或环境变量提供 API Key 和价格。")
    return parser.parse_args()


def main() -> int:
    args = build_parser()
    configure_credentials(args)
    configure_price_caps(args)
    inline_proxies = configure_inline_proxies(args)
    proxy_pool = ProxyPool(proxy_file=args.proxy_file, inline_proxies=inline_proxies)
    state = AppState()
    stop_event = threading.Event()

    for gpu in TARGET_GPUS:
        for count in GPU_COUNT_TARGETS:
            state.update_status("Vast", gpu, count_label(count), PRICE_CAPS[gpu])
        state.update_status("Clore", gpu, "any", PRICE_CAPS[gpu])

    def stop(signum: int, _frame: object) -> None:
        state.add_event(f"收到停止信号 {signum}，准备退出")
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    vast_client = VastClient(proxy_pool)
    threads = [
        threading.Thread(target=platform_loop, args=("Vast", args, state, stop_event, lambda: run_vast_once(args, state, vast_client)), daemon=True),
        threading.Thread(target=platform_loop, args=("Clore", args, state, stop_event, lambda: run_clore_once(args, state, proxy_pool)), daemon=True),
        threading.Thread(target=dashboard_loop, args=(args, state, proxy_pool, stop_event), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads[:2]:
        thread.join()
    stop_event.set()
    threads[2].join(timeout=2)
    render_dashboard(state, args, proxy_pool)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

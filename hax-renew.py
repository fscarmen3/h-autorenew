#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, time, platform, requests, re, json, subprocess, socket, threading
os.environ["PATH"] = os.path.expanduser("~/bin") + os.pathsep + os.environ.get("PATH", "")
import tempfile, html as html_mod, random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from DrissionPage import ChromiumPage, ChromiumOptions
from gen_singbox_config import setup_proxy

# ── 可选依赖：reCAPTCHA 语音识别 ──────────────────────────────────────
try:
    import speech_recognition as sr
    from pydub import AudioSegment
    HAS_SPEECH = True
except ImportError as e:
    HAS_SPEECH = False
    print(f"[WARN] 语音识别依赖不可用: {type(e).__name__}: {e}")

# ── 可选依赖：Telethon (Telegram MTProto) ─────────────────────────────
try:
    from telethon import TelegramClient, events, functions
    from telethon.sessions import StringSession
    HAS_TELETHON = True
except ImportError:
    HAS_TELETHON = False

# ── 可选依赖：ddddocr (验证码识别) ────────────────────────────────────
try:
    import ddddocr as _ddddocr_mod
    HAS_DDDDOCR = True
except ImportError:
    HAS_DDDDOCR = False

OUTPUT_DIR = Path(os.environ.get("SCREENSHOT_DIR", "output/screenshots"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# 环境变量（均可通过 workflow yml / shell 覆盖）
#
# 重试分三层，关系是"调用"关系：外层每轮 → 中层每次换IP → 内层多轮重注册。
#
#   外层 MAX_RENEW_RETRIES
#     整个续期流程（登录→进页面→解验证码→提交）失败后整体重跑几次。
#     每次重跑都会重新登录、重新等 TG 验证码。
#
#   中层 MAX_WARP_RETRIES
#     提交验证码后服务器报 captcha 错误时，"换 IP → 重新提交"的循环次数。
#     每一次循环都会调用一次 restart_warp() 触发内层轮换。
#
#   内层 MAX_WARP_ROTATE_ATTEMPTS
#     上面那一次 restart_warp() 内部，warp-cli 重注册的轮数
#     （对应日志"轮换尝试 x/N"）。Cloudflare 立即重注册常返回同一出口 IP，
#     所以每轮之间递增等待 5/10/15/20s 再试。
#     ★ 20 轮都拿不到新 IP → 本次换 IP 失败 → 中层本轮直接终止，
#       向上抛 CaptchaBlocked；下一轮中层/外层重试时会重新执行一次完整的
#       20 轮轮换（而不是接着第 21 轮继续）。
#
# 时序示例（MAX_WARP_RETRIES=10, MAX_WARP_ROTATE_ATTEMPTS=20）：
#   提交验证码 → 被拒 → 换IP(内部最多20轮重注册)
#     ├─ 第3轮拿到新IP → 用新IP重新提交（内层只消耗了3轮）
#     └─ 20轮全是重复IP → 本次提交失败 → 外层整体重跑时再开一轮新的20轮
# ════════════════════════════════════════════════════════════════════
# 账号列表："手机号,tg_bot_token,tg_chat_id,tg_api_id,tg_api_hash,tg_session"，多账号用分号分隔
BATCH = os.environ.get("BATCH", "")
# 登录重试次数（登录页验证码/网络问题导致登录失败时）
MAX_LOGIN_RETRIES = int(os.environ.get("MAX_LOGIN_RETRIES", "1").strip())
# 【外层】每个续期 URL 的整体流程重试次数（实际执行 = 该值 + 1 次）
MAX_RENEW_RETRIES_PER_URL = int(os.environ.get("MAX_RENEW_RETRIES", "5").strip())
# 【中层】提交验证码后被服务器以 captcha 错误拒绝时，"换 WARP IP → 重新提交"
# 的循环次数。每循环一次就触发一次下面的内层轮换。
MAX_WARP_RETRIES = int(os.environ.get("MAX_WARP_RETRIES", "20").strip())
# 【内层】单次换 IP 内部的 warp-cli 重注册轮数。
# Cloudflare 立即重注册往往返回同一出口 IP，因此每轮之间递增等待 5/10/15/20s；
# 全部轮次都拿不到新 IP 时本次换 IP 失败，向上抛 CaptchaBlocked，
# 由下一轮中层/外层重试重新开始一轮完整的轮换（不是续着上一轮继续）。
MAX_WARP_ROTATE_ATTEMPTS = int(os.environ.get("MAX_WARP_ROTATE_ATTEMPTS", "20").strip())
# 单条 warp-cli 命令的超时秒数（正常执行 1-2s；超时说明网络已断/挂起，
# 会强杀整个进程组防止孤儿 warp-cli 卡住管道，然后由轮换循环继续下一轮）
WARP_CMD_TIMEOUT = int(os.environ.get("WARP_CMD_TIMEOUT", "10").strip())

DEBUG = os.environ.get("DEBUG_FLAG", "0").strip() == "1"

def _dbg(msg):
    if DEBUG:
        print(f"  [DEBUG] {msg}")

CN_TZ = timezone(timedelta(hours=8))

# ── Telegram MTProto API（由 BATCH 每账号提供，此处为全局默认值）────
TG_API_ID = 0
TG_API_HASH = ""
TG_SESSION = ""
HAX_BOT = "@HaxTG_bot"
HAX_BOT_ID = 1967189265
TG_OFFICIAL_ID = 777000

HAX_BASE_URL = "https://hax.co.id"
HAX_TITLE = "hax.co.id"
HAX_LOGIN_URL = f"{HAX_BASE_URL}/login"
HAX_VPS_INFO_URL = f"{HAX_BASE_URL}/vps-info"
HAX_VPS_RENEW_URL = f"{HAX_BASE_URL}/vps-renew"
HAX_VPS_RENEW_CODE_URL = f"{HAX_BASE_URL}/vps-renew-code"
HAX_VPS_STATUS_URL = f"{HAX_BASE_URL}/vps-status"
HAX_VPS_CONTROL_URL = f"{HAX_BASE_URL}/vps-control"

def cn_now() -> datetime:
    return datetime.now(CN_TZ)


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  Telegram 验证码提取         ║
# ╚══════════════════════════════════════════════════════════════════════╝

class TGVerificationCodeExtractor:
    """
    从指定 Telegram 账号的指定联系人对话中提取验证码。
    支持两种模式：
      - history: 一次性提取最近 N 条消息中的验证码
      - watch:   实时监听新消息，提取验证码
    """

    CODE_RE = re.compile(
        r'(?:验证码|验证|码|Code|Cod|OTP|Код|код|passcode|pin)[^\d]{0,20}?(\d{4,8})',
        re.IGNORECASE,
    )
    BASE64_CODE_RE = re.compile(
        r'(?:code|Your Code is)[\s:]+([A-Za-z0-9+/=_\-]{20,})',
        re.IGNORECASE,
    )
    FALLBACK_RE = re.compile(r'(?<!\d)(\d{4,8})(?!\d)')

    def __init__(self, api_id: int = None, api_hash: str = None,
                 tg_session: str = None, proxy: str = None):
        if not HAS_TELETHON:
            raise RuntimeError("Telethon 未安装，请运行: pip install telethon")
        self.api_id = api_id if api_id is not None else TG_API_ID
        self.api_hash = api_hash if api_hash is not None else TG_API_HASH
        self._tg_session = tg_session
        self.proxy = proxy
        self._client = None

    # ── 内部: 创建客户端 ──────────────────────────────────────────────
    def _make_client(self, session_str: str = "") -> TelegramClient:
        proxy_cfg = None
        if self.proxy:
            from urllib.parse import urlparse
            p = urlparse(self.proxy)
            proxy_cfg = {
                'proxy_type': 'socks5',
                'addr': p.hostname or '127.0.0.1',
                'port': p.port or 10808,
            }
        return TelegramClient(
            StringSession(session_str), self.api_id, self.api_hash,
            proxy=proxy_cfg, connection_retries=5, timeout=30,
        )

    def _load_session(self) -> str:
        return self._tg_session if self._tg_session else TG_SESSION

    @staticmethod
    async def _resolve_entity(client, target):
        """解析目标实体，支持用户名、数字 ID、手机号"""
        try:
            return await client.get_input_entity(target)
        except Exception:
            pass
        # 尝试 get_entity 在线解析（用户名/手机号等）
        try:
            entity = await client.get_entity(target)
            return await client.get_input_entity(entity)
        except Exception:
            pass
        # 数字 ID 尝试转 int
        if str(target).isdigit():
            try:
                return await client.get_input_entity(int(target))
            except Exception:
                pass
            try:
                entity = await client.get_entity(int(target))
                return await client.get_input_entity(entity)
            except Exception:
                pass
        raise ValueError(f"Cannot find entity: {target}")

    # ── 公开: 提取验证码 ──────────────────────────────────────────────
    @staticmethod
    def extract_codes(text: str) -> List[str]:
        """从文本中提取验证码候选，支持数字码和 base64 码"""
        if not text:
            return []
        found = set()

        # 1. 数字验证码
        m = TGVerificationCodeExtractor.CODE_RE.search(text)
        if m:
            found.add(m[1])

        # 2. Base64 验证码 (Your Code is <base64>)
        for m in TGVerificationCodeExtractor.BASE64_CODE_RE.finditer(text):
            b64_str = m.group(1).strip()
            if len(b64_str) >= 20:
                found.add(b64_str)

        # 3. 兜底: 纯数字
        if not found:
            cleaned = re.sub(r'[+\s-]', '', text)
            for d in TGVerificationCodeExtractor.FALLBACK_RE.findall(cleaned):
                if 4 <= len(d) <= 8 and not re.match(r'^(19|20)\d{2}$', d):
                    found.add(d)

        return list(found)

    async def fetch_history(self, target: str, limit: int = 100) -> List[Dict[str, str]]:
        """
        一次性提取目标对话最近 N 条消息中的验证码。
        target: 机器人用户名如 '@HaxTG_bot' 或联系人 ID
        返回: [{"code": "123456", "message": "...", "time": "..."}]
        """
        session_str = self._load_session()
        if not session_str:
            print("  [WARN] TG session 不存在，跳过验证码提取")
            return []

        client = self._make_client(session_str)
        await client.connect()
        results = []
        try:
            entity = await self._resolve_entity(client, target)
            messages = await client.get_messages(entity, limit=limit)
            for msg in messages:
                if not msg.message:
                    continue
                codes = self.extract_codes(msg.message)
                for c in codes:
                    results.append({
                        "code": c,
                        "message": msg.message[:200],
                        "time": msg.date.isoformat() if msg.date else "",
                    })
                    print(f"  [INFO] TG 提取到验证码: {mask_code(c)}")
        except Exception as e:
            print(f"  [WARN] TG 提取验证码失败: {e}")
        finally:
            await client.disconnect()
        return results

    async def watch_for_code(self, target: str, timeout: int = 120) -> Optional[str]:
        """
        实时监听目标对话的新消息，等待验证码出现。
        timeout: 最长等待秒数
        返回: 第一个提取到的验证码，或 None
        """
        session_str = self._load_session()
        if not session_str:
            print("  [WARN] TG session 不存在，跳过验证码监听")
            return None

        client = self._make_client(session_str)
        await client.connect()
        result_holder: Dict[str, Optional[str]] = {"code": None}

        try:
            entity = await self._resolve_entity(client, target)
            bot_id = entity.channel_id if hasattr(entity, 'channel_id') else (
                entity.user_id if hasattr(entity, 'user_id') else None
            )

            @client.on(events.NewMessage)
            async def handler(event):
                msg = event.message
                from_id = None
                if msg.sender_id:
                    from_id = msg.sender_id
                elif msg.peer_id:
                    from_id = getattr(msg.peer_id, 'channel_id',
                                     getattr(msg.peer_id, 'user_id', None))
                if bot_id and from_id and str(from_id) != str(bot_id):
                    return
                if msg.message:
                    codes = self.extract_codes(msg.message)
                    if codes:
                        result_holder["code"] = codes[0]
                        print(f"  [INFO] TG 实时捕获验证码: {mask_code(codes[0])}")

            print(f"  [INFO] TG 开始监听 {target} 的新消息 (超时 {timeout}s)...")
            start = time.time()
            while time.time() - start < timeout:
                if result_holder["code"]:
                    break
                await asyncio.sleep(1)

        except Exception as e:
            print(f"  [WARN] TG 监听失败: {e}")
        finally:
            await client.disconnect()

        return result_holder["code"]

    async def confirm_tg_auth(self, target: str, timeout: int = 60) -> bool:
        """
        监听目标对话的授权消息，提取 Confirm 按钮的 callback_data 并点击。
        用于 登录流程中777000发送的 Telegram Widget 授权请求。
        返回: 是否成功点击了 Confirm
        """
        session_str = self._load_session()
        if not session_str:
            print("  [WARN] TG session 不存在，跳过授权确认")
            return False

        client = self._make_client(session_str)
        await client.connect()
        confirmed = False

        try:
            entity = await self._resolve_entity(client, target)
            bot_id = entity.channel_id if hasattr(entity, 'channel_id') else (
                entity.user_id if hasattr(entity, 'user_id') else None
            )

            async def try_confirm(msg):
                nonlocal confirmed
                if confirmed or not msg.buttons:
                    return
                for row in msg.buttons:
                    for btn in row:
                        data = btn.data
                        if not data:
                            continue
                        try:
                            data_str = data.decode("utf-8") if isinstance(data, bytes) else str(data)
                        except Exception:
                            continue
                        if "au_confirm" in data_str:
                            print(f"  [INFO] 已找到tg官方联系人对话中的 Confirm 按钮，callback_data: {data_str}")
                            try:
                                await client(functions.messages.GetBotCallbackAnswerRequest(
                                    peer=msg.peer_id,
                                    msg_id=msg.id,
                                    data=data if isinstance(data, bytes) else data.encode("utf-8"),
                                ))
                                print("  [INFO] ✅ Confirm 已完成点击")
                                confirmed = True
                            except Exception as e:
                                err_str = str(e)
                                if "Constructor ID" in err_str or "TLObject" in err_str:
                                    print(f"  [INFO] ✅ Confirm 已点击（response 解析异常，但请求已发送）")
                                    confirmed = True
                                else:
                                    print(f"  [WARN] Confirm 点击失败: {e}")
                            return

            # 先检查最近消息（可能777000消息已经到了）
            print(f"  [INFO] TG 检查 {target} 最近消息...")
            recent = await client.get_messages(entity, limit=10)
            for msg in recent:
                if msg and msg.buttons:
                    await try_confirm(msg)
            if confirmed:
                return True

            # 再监听新消息
            @client.on(events.NewMessage)
            async def handler(event):
                msg = event.message
                from_id = None
                if msg.sender_id:
                    from_id = msg.sender_id
                elif msg.peer_id:
                    from_id = getattr(msg.peer_id, 'channel_id',
                                     getattr(msg.peer_id, 'user_id', None))
                if bot_id and from_id and str(from_id) != str(bot_id):
                    return
                await try_confirm(msg)

            print(f"  [INFO] TG 监听 {target} 等待授权消息 (超时 {timeout}s)...")
            start = time.time()
            while time.time() - start < timeout:
                if confirmed:
                    break
                await asyncio.sleep(1)

        except Exception as e:
            print(f"  [WARN] TG 授权确认失败: {e}")
        finally:
            await client.disconnect()

        return confirmed


# ── 同步封装：在非 async 环境中调用 TG 提取器 ─────────────────────────
import asyncio

def tg_extract_codes_sync(target: str, limit: int = 100,
                          proxy: str = None) -> List[Dict[str, str]]:
    """同步版：一次性提取目标对话验证码"""
    extractor = TGVerificationCodeExtractor(proxy=proxy)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, extractor.fetch_history(target, limit)).result()
        else:
            return loop.run_until_complete(extractor.fetch_history(target, limit))
    except RuntimeError:
        return asyncio.run(extractor.fetch_history(target, limit))

def tg_wait_for_code_sync(target: str, timeout: int = 120,
                          proxy: str = None) -> Optional[str]:
    """同步版：监听目标对话等待新验证码"""
    extractor = TGVerificationCodeExtractor(proxy=proxy)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, extractor.watch_for_code(target, timeout)).result()
        else:
            return loop.run_until_complete(extractor.watch_for_code(target, timeout))
    except RuntimeError:
        return asyncio.run(extractor.watch_for_code(target, timeout))

def tg_confirm_auth_sync(target: str, timeout: int = 60,
                         proxy: str = None) -> bool:
    """同步版：监听目标对话，自动点击 Confirm 按钮确认授权"""
    extractor = TGVerificationCodeExtractor(proxy=proxy)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, extractor.confirm_tg_auth(target, timeout)).result()
        else:
            return loop.run_until_complete(extractor.confirm_tg_auth(target, timeout))
    except RuntimeError:
        return asyncio.run(extractor.confirm_tg_auth(target, timeout))


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  reCAPTCHA 语音识别求解器 (适配 DrissionPage)   ║
# ╚══════════════════════════════════════════════════════════════════════╝

class By:
    TAG_NAME = "tag"
    ID = "id"
    CSS_SELECTOR = "css"

class _DPContext:
    def __init__(self, page, frame=None): self.page, self.frame = page, frame
    def ele(self, loc, timeout=3): return (self.frame or self.page).ele(loc, timeout=timeout)
    def eles(self, loc, timeout=3): return (self.frame or self.page).eles(loc, timeout=timeout) or []
    def run_js(self, script, *args): return (self.frame or self.page).run_js(script, *args)

class _DPSwitch:
    def __init__(self, driver): self.driver = driver
    def frame(self, frame):
        # DrissionPage 的 get_frame() 需要 ChromiumFrame 或 iframe 定位器；
        # 旧业务传入的是 ChromiumElement，因此先从 id/src 还原 iframe 定位。
        if getattr(frame, '_type', None) == 'ChromiumFrame':
            dp_frame = frame
        else:
            frame_id = frame.attr('id') if hasattr(frame, 'attr') else ''
            frame_src = frame.attr('src') if hasattr(frame, 'attr') else ''
            if frame_id:
                locator = f'css:iframe#{frame_id}'
            elif frame_src:
                locator = f'css:iframe[src="{frame_src}"]'
            else:
                locator = 'css:iframe'
            dp_frame = self.driver.browser.active.get_frame(locator, timeout=5)
        self.driver._context = _DPContext(self.driver.browser.active, dp_frame)
    def default_content(self):
        self.driver._context = _DPContext(self.driver.browser.active)
    def window(self, handle):
        self.driver.browser.set_active(handle)
        self.driver._context = _DPContext(self.driver.browser.active)

class _DPDriver:
    def __init__(self, browser):
        self.browser = browser
        self.page = browser.page
        self._context = _DPContext(browser.active)
        self.switch_to = _DPSwitch(self)
    @property
    def title(self): return self.browser.active.title
    @property
    def current_window_handle(self): return self.browser.active.tab_id
    @property
    def window_handles(self): return self.browser.page.get_tabs(as_id=True)
    def _loc(self, by, value):
        if by == By.TAG_NAME: return f'tag:{value}'
        if by == By.ID: return f'#{value}'
        return value if str(value).startswith(('css:', 'tag:', 'xpath:')) else f'css:{value}'
    def find_elements(self, by, value): return self._context.eles(self._loc(by, value))
    def find_element(self, by, value):
        e=self._context.ele(self._loc(by,value), timeout=5)
        if not e: raise RuntimeError(f'element not found: {by}={value}')
        return e
    def execute_script(self, script, *args): return self._context.run_js(script, *args)
    def set_page_load_timeout(self, n): self.browser.page.set.timeouts(page_load=n)
    def set_script_timeout(self, n): self.browser.page.set.timeouts(script=n)

class DPBrowser:
    def __init__(self, page):
        self.page = page
        self.active = page
        self.driver = _DPDriver(self)
    def set_active(self, handle):
        self.page.activate_tab(handle)
        self.active = self.page.get_tab(handle)
    def open(self, url):
        self.active = self.page
        self.driver.switch_to.default_content()
        return self.page.get(url)
    def execute_script(self, script, *args):
        m=re.fullmatch(r"\s*return document\.querySelector\((['\"])(.*?)\1\)\s*;?\s*",script,re.S)
        if m: return self.active.ele(f'css:{m.group(2)}', timeout=3)
        return self.driver._context.run_js(script, *args)
    def get_current_url(self): return self.active.url or ''
    def get_title(self): return self.active.title or ''
    def get_page_source(self): return self.active.html or ''
    def save_screenshot(self,path): return self.active.get_screenshot(path=path)
    def switch_to_frame(self,frame): self.driver.switch_to.frame(frame)
    def click_turnstile(self):
        """按 DrissionPage 范本点击 Cloudflare Turnstile iframe。"""
        try:
            iframe = self.active.get_frame(
                'css:iframe[src*="challenges.cloudflare.com"]', timeout=5
            )
            if not iframe:
                return True
            iframe.frame_ele.click.at(offset_x=25, offset_y=25)
            return True
        except Exception as e:
            print(f"  [WARN] Turnstile 点击异常: {type(e).__name__}: {e}")
            return False
    def quit(self):
        try: self.page.quit()
        except Exception: pass

class CaptchaBlocked(Exception):
    """IP 被 Google reCAPTCHA 封锁"""
    pass

class CaptchaAudioUnavailable(CaptchaBlocked):
    """当前出口无法取得 reCAPTCHA 音频资源，必须换 IP。"""
    pass

class ArithmeticCaptchaUnavailable(CaptchaBlocked):
    """算术验证码无法识别或填入，必须换 IP 重试。"""
    pass


def parse_arithmetic_text(text: str):
    """解析页面文字算式，例如 4 × 6 =、12 + 3 =。"""
    normalized = (text or '').replace('×', '*').replace('✕', '*').replace('x', '*').replace('X', '*')
    normalized = normalized.replace('÷', '/').replace('−', '-').replace('–', '-')
    match = re.search(r'(?<!\d)(\d+)\s*([+*/-])\s*(\d+)(?:\s*=)?', normalized)
    if not match:
        return None
    return int(match.group(1)), match.group(2), int(match.group(3))


class RecaptchaAudioSolver:
    """DrissionPage reCAPTCHA iframe 音频求解器。"""
    MAX_ATTEMPTS=3
    def __init__(self,browser): self.browser=browser
    def _frame(self,k):
        try: return self.browser.page.get_frame(f'css:iframe[src*="recaptcha"][src*="{k}"]',timeout=5)
        except Exception: return None
    def is_solved(self):
        try:
            for frame in self.browser.page.get_frames():
                try:
                    token = frame.run_js("return document.querySelector(\"textarea[name='g-recaptcha-response']\")?.value || ''")
                    if token and len(token) > 30:
                        return True
                except Exception:
                    pass
            token = self.browser.execute_script("return document.querySelector('textarea[name=\\'g-recaptcha-response\\']')?.value || ''")
            if token and len(token) > 30:
                return True
            f = self._frame('anchor')
            return bool(f and f.run_js("return document.querySelector('#recaptcha-anchor')?.getAttribute('aria-checked') === 'true'"))
        except Exception:
            return False
    def _is_blocked(self):
        f=self._frame('bframe')
        try: return bool(f and f.run_js("var h=document.querySelector('.rc-doscaptcha-header-text'),e=document.querySelector('.rc-audiochallenge-error-message');return !!((h&&h.textContent.toLowerCase().includes('try again later'))||(e&&e.offsetParent!==null));"))
        except Exception: return False
    def click_checkbox(self):
        f=self._frame('anchor')
        if not f: raise RuntimeError('未找到 reCAPTCHA anchor iframe')
        f.frame_ele.click.at(offset_x=35,offset_y=35); time.sleep(3)
        if self._is_blocked(): raise CaptchaBlocked('点击复选框后 IP 被封锁')
    def switch_to_audio(self):
        f = self._frame('bframe')
        if not f:
            raise CaptchaAudioUnavailable('未找到 reCAPTCHA challenge frame')
        try:
            box = f.ele('#audio-response', timeout=2)
            if not box:
                button = f.ele('#recaptcha-audio-button', timeout=5)
                if not button:
                    raise CaptchaAudioUnavailable('未找到 reCAPTCHA 音频按钮')
                button.click()
                time.sleep(5)
                box = f.ele('#audio-response', timeout=5)
            if not box:
                raise CaptchaAudioUnavailable('点击音频按钮后未进入音频挑战')
            return True
        except CaptchaAudioUnavailable:
            raise
        except Exception as e:
            raise CaptchaAudioUnavailable(f'切换音频挑战失败: {type(e).__name__}') from e
    def _audio_url(self):
        f = self._frame('bframe')
        if not f:
            raise CaptchaAudioUnavailable('未找到 reCAPTCHA challenge frame')
        try:
            src = ''
            for selector in ('.rc-audiochallenge-tdownload-link', '.rc-audiochallenge-ndownload-link', '#audio-source'):
                element = f.ele(selector, timeout=2)
                if element:
                    src = element.attr('href') or element.attr('src') or ''
                    if src:
                        break
            if not src:
                src = f.run_js("return document.querySelector('#audio-source')?.src || document.querySelector('.rc-audiochallenge-tdownload-link,.rc-audiochallenge-ndownload-link')?.href || ''") or ''
            if not src:
                raise CaptchaAudioUnavailable('音频挑战已打开但未下发 audio-source 链接')
            print(f"  [INFO] 已获取 reCAPTCHA 音频链接（长度 {len(src)}）")
            return src
        except CaptchaAudioUnavailable:
            raise
        except Exception as e:
            raise CaptchaAudioUnavailable(f'读取音频链接失败: {type(e).__name__}') from e

    def _download_audio(self, url):
        if not url:
            raise CaptchaAudioUnavailable('音频链接为空')
        page = self.browser.active
        session = requests.Session()
        try:
            cookies = page.cookies(all_domains=True, all_info=True)
            for cookie in cookies:
                session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain'))
        except Exception as e:
            print(f"  [WARN] 读取浏览器 cookies 失败: {type(e).__name__}")
        try:
            user_agent = page.user_agent
        except Exception:
            user_agent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36'
        headers = {
            'User-Agent': user_agent,
            'Referer': page.url or 'https://www.google.com/recaptcha/',
            'Accept': 'audio/mp3,audio/*;q=0.9,*/*;q=0.8',
        }
        try:
            r = session.get(url, headers=headers, timeout=30)
            content_type = (r.headers.get('content-type') or '').lower()
            r.raise_for_status()
            if len(r.content) < 1000 or ('text/html' in content_type and len(r.content) < 100000):
                raise CaptchaAudioUnavailable(
                    f'音频响应无效: status={r.status_code}, type={content_type or "unknown"}, bytes={len(r.content)}'
                )
            fd, path = tempfile.mkstemp(suffix='.mp3')
            os.close(fd)
            Path(path).write_bytes(r.content)
            print(f"  [INFO] 音频已下载: {len(r.content)} bytes")
            return path
        except CaptchaAudioUnavailable:
            raise
        except Exception as e:
            raise CaptchaAudioUnavailable(f'音频下载失败: {type(e).__name__}') from e
    @classmethod
    def _recognize_audio(cls, mp3):
        if not HAS_SPEECH or not mp3:
            return None
        wav = mp3.replace('.mp3', '.wav')
        try:
            convert = subprocess.run(
                ['ffmpeg', '-y', '-loglevel', 'error', '-i', mp3, wav],
                capture_output=True, text=True, timeout=30,
            )
            if convert.returncode != 0 or not os.path.exists(wav):
                detail = (convert.stderr or '').strip().splitlines()
                detail = detail[-1][:160] if detail else 'no ffmpeg detail'
                print(f"  [WARN] ffmpeg 解码失败: rc={convert.returncode}, {detail}")
                return None
            wav_header = Path(wav).read_bytes()[:12]
            if not (wav_header[:4] == b'RIFF' and wav_header[8:12] == b'WAVE'):
                print("  [WARN] ffmpeg 输出不是有效 WAV")
                return None
            print(f"  [INFO] ffmpeg 解码成功: WAV={os.path.getsize(wav)} bytes")

            rec = sr.Recognizer()
            with sr.AudioFile(wav) as src:
                raw = rec.recognize_google(rec.record(src))
            answer = (raw or '').strip()
            print(f"  [INFO] 音频识别原文: {answer[:200]!r}")
            print(f"  [INFO] 音频识别完成: 原文长度={len(answer)}")
            return answer or None
        except sr.UnknownValueError:
            print("  [WARN] 音频识别失败: Google 无法识别语音")
            return None
        except sr.RequestError as e:
            print(f"  [WARN] 音频识别失败: Google 请求异常 {type(e).__name__}")
            return None
        except Exception as e:
            print(f"  [WARN] 音频处理失败: {type(e).__name__}: {str(e)[:120]}")
            return None
        finally:
            for f in (mp3, wav):
                try:
                    os.remove(f)
                except OSError:
                    pass
    def _fill_verify(self,text):
        f=self._frame('bframe')
        try:
            box=f.ele('#audio-response',timeout=5); btn=f.ele('#recaptcha-verify-button',timeout=5)
            if box and btn: box.input(text,clear=True); btn.click(); return True
        except Exception: pass
        return False
    def _reload(self):
        f=self._frame('bframe')
        try:
            b=f.ele('#recaptcha-reload-button',timeout=3)
            if b: b.click()
        except Exception: pass
    def solve(self):
        if not HAS_SPEECH or not self._frame('anchor'): return False
        for i in range(self.MAX_ATTEMPTS):
            if self.is_solved(): return True
            if self._is_blocked(): raise CaptchaBlocked('IP 被 Google reCAPTCHA 封锁')
            if i==0:
                try: self.click_checkbox(); time.sleep(2)
                except CaptchaBlocked: raise
                except Exception: pass
                if self.is_solved(): return True
            self.switch_to_audio()
            text=self._recognize_audio(self._download_audio(self._audio_url()))
            if text and self._fill_verify(text):
                time.sleep(4)
                if self.is_solved(): return True
            self._reload(); time.sleep(2)
        raise CaptchaBlocked('reCAPTCHA 音频识别连续失败，换 IP 重试')


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  可插拔验证码求解器插件                                                ║
# ╚══════════════════════════════════════════════════════════════════════╝

class CaptchaSolver:
    """验证码求解器基类（可插拔插件模式）"""
    name = "base"

    def is_available(self) -> bool:
        return False

    def solve(self, browser) -> Optional[str]:
        raise NotImplementedError

    @staticmethod
    def _register_stats(name: str, success: bool):
        if not hasattr(CaptchaSolver, '_stats'):
            CaptchaSolver._stats = {}
        if name not in CaptchaSolver._stats:
            CaptchaSolver._stats[name] = {"attempts": 0, "successes": 0}
        CaptchaSolver._stats[name]["attempts"] += 1
        if success:
            CaptchaSolver._stats[name]["successes"] += 1

    @staticmethod
    def print_stats():
        stats = getattr(CaptchaSolver, '_stats', {})
        if not stats:
            return
        for name, s in stats.items():
            a, ok = s["attempts"], s["successes"]
            print(f"  [STATS] {name}: {ok}/{a} = {ok/a*100:.0f}%" if a else f"  [STATS] {name}: 0/0")


class ArithmeticCaptchaSolver(CaptchaSolver):
    """
     四则运算验证码求解器。
    页面结构: <img src="数字1.jpg"> X <img src="数字2.jpg"> =
    用 ddddocr 分别识别两张图片，计算结果后填入 #captcha。
    """
    name = "arithmetic"

    CHAR_MAP = {
        'o': '0', 'O': '0', 'q': '0',
        'l': '1', 'I': '1', 'i': '1',
        'z': '2', 'Z': '2',
        's': '5', 'S': '5',
        'b': '6', 'B': '8',
        'c': '6', 'C': '6',
        'g': '9', 'G': '6',
        't': '7',
    }

    def __init__(self, proxy=None):
        self._ocr = None
        self._proxy = proxy
        if HAS_DDDDOCR:
            try:
                self._ocr = _ddddocr_mod.DdddOcr(show_ad=False)
            except Exception:
                pass

    def is_available(self, browser=None) -> bool:
        if browser is None:
            return False
        try:
            has = browser.execute_script('''
                return !!(document.querySelector('.col-sm-3 img') &&
                           document.querySelectorAll('.col-sm-3 img').length >= 2);
            ''')
            return bool(has)
        except Exception:
            return False

    def _fix_digit(self, raw: str) -> Optional[int]:
        """修正 OCR 结果，返回数字或 None"""
        raw = raw.strip().replace(" ", "")
        if not raw:
            return None
        if raw.isdigit():
            return int(raw)
        if len(raw) == 1:
            fixed = self.CHAR_MAP.get(raw)
            if fixed is not None:
                return int(fixed)
        digits = re.findall(r'\d', raw)
        if digits:
            return int(digits[0])
        return None

    @staticmethod
    def _digit_from_url(src: str) -> Optional[int]:
        """从图片 URL 文件名直接解析答案数字。

        HAX 验证码图片命名格式: {md5}-{答案数字}{客户端IP}.jpg
        例: 7a90e147...267f-3104.28.196.79.jpg → 数字 3，IP 104.28.196.79
        这是服务端生成的，比 OCR 可靠得多。
        """
        if not src:
            return None
        m = re.search(r'-([0-9])([0-9]{1,3}(?:\.[0-9]{1,3}){3})\.jpg', src)
        if m:
            return int(m.group(1))
        return None

    @staticmethod
    def _upscale_image(data: bytes, scale: int = 4) -> Optional[bytes]:
        try:
            from PIL import Image, ImageEnhance, ImageFilter
            from io import BytesIO
            with Image.open(BytesIO(data)) as image:
                image = image.convert('RGB')
                image = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)
                image = ImageEnhance.Contrast(image).enhance(1.25)
                image = image.filter(ImageFilter.SHARPEN)
                out = BytesIO()
                image.save(out, format='PNG')
                return out.getvalue()
        except Exception as e:
            _dbg(f"图片放大失败: {type(e).__name__}: {e}")
            return None

    def _ocr_image_bytes(self, img_bytes: bytes) -> Optional[int]:
        """原图识别失败后放大增强，再用 ddddocr 识别。"""
        try:
            raw = self._ocr.classification(img_bytes)
            print(f"  [ARITHMETIC] OCR 原始结果: '{raw}', 长度={len(raw)}")
            result = self._fix_digit(raw)
            if result is not None:
                return result

            enlarged = self._upscale_image(img_bytes)
            if enlarged:
                enlarged_raw = self._ocr.classification(enlarged)
                print(f"  [ARITHMETIC] OCR 放大结果: '{enlarged_raw}', 长度={len(enlarged_raw)}")
                result = self._fix_digit(enlarged_raw)
                if result is not None:
                    return result
            return None
        except Exception as e:
            print(f"  [ARITHMETIC] ⚠️ OCR 异常: {type(e).__name__}: {e}")
            return None

    @staticmethod
    def _valid_captcha_image(data: bytes) -> bool:
        if not data or len(data) < 50:
            return False
        try:
            from PIL import Image
            from io import BytesIO
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                return width >= 10 and height >= 10 and width <= 1000 and height <= 1000
        except Exception:
            return False

    def _download_image(self, browser, src: str) -> Optional[bytes]:
        """通过 canvas 下载图片（自动带 cookie 和代理）"""
        if not src:
            return None
        if src.startswith("/"):
            src = HAX_BASE_URL + src
        elif not src.startswith("http"):
            src = HAX_BASE_URL + "/" + src
        _dbg(f"captcha img src: {src[:120]}")

        # 方式1: 同步 XHR 直接下载图片二进制数据
        try:
            safe_src = src.replace("\\", "\\\\").replace("'", "\\'")
            result = browser.execute_script(f'''
                try {{
                    var xhr = new XMLHttpRequest();
                    xhr.open("GET", "{safe_src}", false);
                    xhr.responseType = "arraybuffer";
                    xhr.send();
                    if (xhr.status !== 200) return null;
                    var bytes = new Uint8Array(xhr.response);
                    var binary = "";
                    for (var i = 0; i < bytes.length; i++) {{
                        binary += String.fromCharCode(bytes[i]);
                    }}
                    return btoa(binary);
                }} catch(e) {{
                    return null;
                }}
            ''')
            if result:
                import base64
                data = base64.b64decode(result)
                if self._valid_captcha_image(data):
                    _dbg(f"canvas 下载成功: {len(data)} bytes")
                    return data
                _dbg(f"canvas 图片无效或尺寸异常: {len(data)} bytes")
            else:
                _dbg("canvas 返回空（图片未加载）")
        except Exception as e:
            _dbg(f"canvas 异常: {e}")

        # 方式2: 从 DOM 拿已缓存的 <img> 元素转 canvas
        try:
            safe_src = src.replace("\\", "\\\\").replace("'", "\\'")
            result = browser.execute_script(f'''
                return (function() {{
                    var imgs = document.querySelectorAll('img');
                    for (var i = 0; i < imgs.length; i++) {{
                        var img = imgs[i];
                        if (img.src.indexOf("{safe_src.split("/")[-1]}") !== -1 && img.complete && img.naturalWidth > 0) {{
                            var c = document.createElement("canvas");
                            c.width = img.naturalWidth;
                            c.height = img.naturalHeight;
                            c.getContext("2d").drawImage(img, 0, 0);
                            return c.toDataURL("image/png").split(",")[1];
                        }}
                    }}
                    return null;
                }})()
            ''')
            if result:
                import base64
                data = base64.b64decode(result)
                if self._valid_captcha_image(data):
                    _dbg(f"DOM canvas 下载成功: {len(data)} bytes")
                    return data
        except Exception as e:
            _dbg(f"DOM canvas 异常: {e}")

        # 方式3: requests + proxy
        try:
            proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
            r = requests.get(src, proxies=proxies, timeout=15,
                             headers={"Referer": HAX_BASE_URL + "/",
                                      "User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and self._valid_captcha_image(r.content):
                _dbg(f"requests 下载成功: {len(r.content)} bytes")
                return r.content
        except Exception as e:
            _dbg(f"requests 异常: {e}")
        return None

    def solve(self, browser) -> Optional[str]:
        """对外入口：求解并集中记录统计（成功/失败都计入）。"""
        answer = self._solve(browser)
        self._register_stats(self.name, answer is not None)
        return answer

    OPS = {'+': lambda a, b: a + b, '-': lambda a, b: a - b,
           '*': lambda a, b: a * b, '/': lambda a, b: a // b if b else None}

    @classmethod
    def _calc(cls, num1: int, op: str, num2: int) -> Optional[int]:
        """按运算符计算结果；未知运算符或除零返回 None。"""
        fn = cls.OPS.get(op)
        return fn(num1, num2) if fn else None

    def _extract_expression(self, browser) -> Optional[Dict[str, Any]]:
        """第一步（取数）：从页面提取两张验证码图片 URL 和运算符。"""
        captcha_info = browser.execute_script('''
            return (function() {
                var div = document.querySelector('.col-sm-3');
                if (!div) return null;
                var imgs = div.querySelectorAll('img');
                if (imgs.length < 2) return null;
                var text = div.textContent || '';
                var op = '?';
                text = text.replace(/\\s/g, '');
                if (text.indexOf('X') !== -1 || text.indexOf('x') !== -1 || text.indexOf('\\u00d7') !== -1) op = '*';
                else if (text.indexOf('+') !== -1) op = '+';
                else if (text.indexOf('-') !== -1 || text.indexOf('\\u2212') !== -1) op = '-';
                else if (text.indexOf('\\u00f7') !== -1 || text.indexOf('/') !== -1) op = '/';
                return {
                    img1: imgs[0].src,
                    img2: imgs[1].src,
                    op: op
                };
            })()
        ''')
        if not captcha_info:
            print("  [ARITHMETIC] ⚠️ 验证码元素未找到")
            return None
        return captcha_info

    def _extract_operands(self, browser, img1_src: str, img2_src: str):
        """第二步（取数）：获取两个操作数，返回 (num1, num2)，失败的位为 None。

        优先级：文件名解析（服务端生成，最可靠）→ 下载图片 + OCR → 文件名兜底。
        """
        num1 = self._digit_from_url(img1_src)
        num2 = self._digit_from_url(img2_src)

        if num1 is not None and num2 is not None:
            f1 = img1_src.rsplit("/", 1)[-1]
            f2 = img2_src.rsplit("/", 1)[-1]
            print(f"  [ARITHMETIC] 文件名解析成功: img1={num1}, img2={num2}"
                  f"（{f1} / {f2}），跳过下载和 OCR")
            _dbg(f"[ARITHMETIC] img1 url: {img1_src}")
            _dbg(f"[ARITHMETIC] img2 url: {img2_src}")
            return num1, num2

        # 文件名解析不全，回退：下载图片 + OCR；OCR 仍失败的位用文件名数字兜底
        missing = ",".join(n for n, v in (("img1", num1), ("img2", num2)) if v is None)
        print(f"  [ARITHMETIC] 文件名解析不全（缺 {missing}），回退下载 + OCR...")

        if not self._ocr:
            print("  [ARITHMETIC] ⚠️ ddddocr 不可用，只能依赖文件名解析")
            return num1, num2

        img1_bytes = self._download_image(browser, img1_src)
        img2_bytes = self._download_image(browser, img2_src)

        if not img1_bytes or not img2_bytes:
            print("  [ARITHMETIC] ⚠️ 验证码图片下载失败")
            return num1, num2

        _dbg(f"图片大小: img1={len(img1_bytes)}B, img2={len(img2_bytes)}B")

        ocr1 = self._ocr_image_bytes(img1_bytes)
        ocr2 = self._ocr_image_bytes(img2_bytes)

        if ocr1 is None or ocr2 is None:
            print(f"  [ARITHMETIC] ⚠️ OCR 失败: img1={ocr1}, img2={ocr2}，重新获取验证码图片重试...")
            time.sleep(1)
            r1 = self._download_image(browser, img1_src)
            r2 = self._download_image(browser, img2_src)
            if r1 and r2:
                ocr1 = ocr1 if ocr1 is not None else self._ocr_image_bytes(r1)
                ocr2 = ocr2 if ocr2 is not None else self._ocr_image_bytes(r2)

        # OCR 成功的位用 OCR 结果，失败的位保留文件名数字
        num1 = ocr1 if ocr1 is not None else num1
        num2 = ocr2 if ocr2 is not None else num2
        parts = []
        for name, ocr_v, url_v in (("img1", ocr1, num1), ("img2", ocr2, num2)):
            if ocr_v is not None:
                parts.append(f"{name}=OCR:{ocr_v}")
            elif url_v is not None:
                parts.append(f"{name}=文件名:{url_v}")
        print(f"  [ARITHMETIC] 数字来源: {', '.join(parts)}")
        return num1, num2

    def _fill_answer(self, browser, answer: str) -> bool:
        """第三步（回填）：把计算结果写入 #captcha 输入框。"""
        try:
            browser.execute_script(f'''
                var input = document.getElementById('captcha') || document.querySelector('input[name="captcha"]');
                if (input) {{
                    input.value = '{answer}';
                    input.dispatchEvent(new Event('input', {{bubbles: true}}));
                    input.dispatchEvent(new Event('change', {{bubbles: true}}));
                }}
            ''')
            print(f"  [ARITHMETIC] 已填入: {answer}")
            return True
        except Exception as e:
            print(f"  [ARITHMETIC] ⚠️ 填入失败: {e}")
            return False

    def _solve(self, browser) -> Optional[str]:
        """编排：提取运算式 → 取操作数 → 计算 → 回填。返回答案字符串，失败返回 None。"""
        captcha_info = self._extract_expression(browser)
        if not captcha_info:
            return None

        img1_src = captcha_info.get("img1", "")
        img2_src = captcha_info.get("img2", "")
        op = captcha_info.get("op", "?")
        print(f"  [ARITHMETIC] 运算式结构: [img1] {op} [img2]")

        num1, num2 = self._extract_operands(browser, img1_src, img2_src)
        if num1 is None or num2 is None:
            print("  [ARITHMETIC] ❌ 数字获取失败（OCR 与文件名均未得到结果）")
            return None

        result = self._calc(num1, op, num2)
        if result is None:
            print(f"  [ARITHMETIC] ⚠️ 计算失败（未知运算符或除零）: {num1} {op} {num2}")
            return None

        answer = str(int(result))
        print(f"  [ARITHMETIC] 计算: {num1} {op} {num2} = {answer}")

        if not self._fill_answer(browser, answer):
            return None
        return answer


_arith_solver_singleton: Optional[ArithmeticCaptchaSolver] = None


def _get_arith_solver(proxy: str = None) -> ArithmeticCaptchaSolver:
    """复用同一个 DdddOcr 实例（模型加载较重），仅更新代理配置。"""
    global _arith_solver_singleton
    if _arith_solver_singleton is None:
        _arith_solver_singleton = ArithmeticCaptchaSolver(proxy=proxy)
    else:
        _arith_solver_singleton._proxy = proxy
    return _arith_solver_singleton


def solve_arithmetic_captcha(browser, proxy: str = None, scene: str = "") -> None:
    """四则运算验证码统一求解入口。

    页面没有算术验证码时静默返回；检测到但求解失败时抛出
    ArithmeticCaptchaUnavailable，由调用方决定重试策略。
    """
    tag = "[ARITHMETIC]"
    where = f"({scene})" if scene else ""
    if not HAS_DDDDOCR:
        # ddddocr 缺失不再直接跳过：文件名解析路径不需要 OCR
        print(f"  {tag} ddddocr 未安装，仅使用文件名解析")
    solver = _get_arith_solver(proxy)
    if not solver.is_available(browser):
        return
    print(f"\n  {tag} ========== {where} 检测到四则运算验证码 ==========")
    answer = solver.solve(browser)
    if answer is None:
        raise ArithmeticCaptchaUnavailable(f"{where} 算术验证码求解失败".strip())
    print(f"  {tag} ✅ {where} 求解完成".strip())


def mask(s: str, show: int = 1) -> str:
    if not s: return "***"
    if "@" in s:
        local, domain = s.split("@", 1)
        if len(local) <= 2:
            return s
        n = max(1, len(local) - 2 * show)
        return local[:show] + "*" * n + local[-show:] + "@" + domain
    if len(s) <= 2 * show:
        return s
    n = len(s) - 2 * show
    return s[:show] + "*" * n + s[-show:]

def mask_code(c: str) -> str:
    """验证码统一掩码：每隐藏一个字符对应一个 '*'（如 NzAw****...****ZWNj）。
    长码(>8位)保留前后各4位，短码保留前后各2位，中间一字符一星。"""
    if not c:
        return "****"
    show = 4 if len(c) > 8 else 2
    return mask(c, show=show)

def mask_phone(phone: str) -> str:
    """手机号掩码：前2位****后4位
    如 +506-63532545 → +506-63**2545, 63532545 → 63**2545"""
    p = phone.strip()
    if "-" in p:
        code, rest = p.split("-", 1)
        if len(rest) <= 6:
            return p
        return f"{code}-{rest[:2]}{'*' * (len(rest) - 6)}{rest[-4:]}"
    else:
        if len(p) <= 6:
            return p
        return f"{p[:2]}{'*' * (len(p) - 6)}{p[-4:]}"

def safe_sid_for_filename(sid: str) -> str:
    s = str(sid)
    if len(s) <= 3:
        return s
    return f"{s[0]}{'*' * (len(s) - 3)}{s[-2:]}"

def is_linux():
    return platform.system().lower() == "linux"

def setup_display():
    if is_linux() and not os.environ.get("DISPLAY"):
        try:
            from pyvirtualdisplay import Display
            d = Display(visible=False, size=(1920, 1080))
            d.start()
            os.environ["DISPLAY"] = d.new_display_var
            print("[INFO] 虚拟显示已启动")
            return d
        except Exception as e:
            print(f"[ERROR] 虚拟显示失败: {e}")
            sys.exit(1)
    return None

def shot(idx: int, name: str) -> str:
    return str(OUTPUT_DIR / f"acc{idx}-{cn_now().strftime('%H%M%S')}-{name}.png")

def safe_screenshot(browser, path: str):
    try:
        browser.save_screenshot(path)
        print(f"  [INFO] 截图 → {Path(path).name}")
    except Exception as e:
        print(f"  [WARN] 截图失败: {e}")


def _count_ads(browser):
    """统计页面上的广告元素数量"""
    try:
        return browser.execute_script('''
            (function() {
                var ads = document.querySelectorAll('[id*="google_ads"],[class*="google-ad"],[id*="gpt"],.adsbygoogle,[id*="google_vignette"],iframe[src*="doubleclick"],iframe[src*="googleads"],[id*="ad-container"],[class*="ad-slot"]');
                var scripts = document.querySelectorAll('script[src*="doubleclick"],script[src*="googleads"],script[src*="googlesyndication"],script[src*="adservice"]');
                var iframes = document.querySelectorAll('iframe[src*="doubleclick"],iframe[src*="googleads"]');
                return { el: ads.length, sc: scripts.length, ifr: iframes.length };
            })()
        ''') or {}
    except Exception:
        return {}

def block_ad_domains(page):
    """已停用：CDP 网络层拦截广告域名可能干扰页面脚本加载，
    导致最终续期按钮不可用。保留注释的原实现，需要时取消注释即可恢复。"""
    # try:
    #     page.run_cdp('Network.enable')
    #     page.run_cdp(
    #         'Network.setBlockedURLs',
    #         urls=['*://googleads.g.doubleclick.net/*'],
    #     )
    #     print('[INFO] 已启用广告域名拦截: googleads.g.doubleclick.net')
    #     return True
    # except Exception as e:
    #     print(f'[WARN] 广告域名拦截启用失败: {type(e).__name__}: {str(e)[:120]}')
    #     return False
    return False


def removeAds(browser):
    """清理已加载的广告元素，打印清理前后对比"""
    before = _count_ads(browser)
    b_total = before.get('el', 0) + before.get('sc', 0) + before.get('ifr', 0)
    _dbg(f"[去广告前] {before.get('el',0)} DOM, {before.get('sc',0)} script, {before.get('ifr',0)} iframe")

    print("  [INFO] 执行 removeAds 清理广告元素...")
    try:
        browser.execute_script('''
            (function() {
                // ── 删除元素 ──
                var removeSelectors = [
                    '.fc-monetization-dialog-container',
                    '[class*="fc-monetization"]',
                    '[class*="fc-dialog"]',
                    '[class*="fc-consent"]',
                    '[class*="fc-choice"]',
                    '[class*="fc-reward"]',
                    '#google-anno-sa',
                    '.sc-fIfZzT.kVmdCn',
                    'ins.adsbygoogle.adsbygoogle-noablate',
                    'div[data-google-ad-efd="true"]',
                    'div.google-aiuf',
                    'ins.adsbygoogle',
                ];
                removeSelectors.forEach(function(sel) {
                    document.querySelectorAll(sel).forEach(function(el) {
                        el.remove();
                    });
                });

                // ── 删除 AdBlock 检测提示 ──
                var adblockOverlay = document.querySelectorAll('[id*="adblock"],[class*="adblock"],[id*="AdBlock"],[class*="AdBlock"],[id*="adb"],.adBlock-detected,.blocker-notice');
                adblockOverlay.forEach(function(el) { el.remove(); });
                // 也移除包含 "disable AdBlock" 文本的弹窗（排除 reCAPTCHA 元素）
                var allEls = document.querySelectorAll('div, p, span, h1, h2, h3, h4');
                for (var i = 0; i < allEls.length; i++) {
                    var el = allEls[i];
                    // 跳过 reCAPTCHA 容器及其子元素
                    if (el.closest('.rc-anchor-container, .g-recaptcha, [data-sitekey], iframe[src*="recaptcha"]')) continue;
                    var txt = el.textContent || '';
                    if (txt.indexOf('disable AdBlock') !== -1 || (txt.indexOf('AdBlock') !== -1 && txt.indexOf('disable') !== -1)) {
                        var parent = el.closest('[class*="modal"],[class*="overlay"],[class*="dialog"],[class*="popup"],[role="dialog"]');
                        if (parent && !parent.closest('.rc-anchor-container, .g-recaptcha, [data-sitekey]')) {
                            parent.remove();
                        }
                        break;
                    }
                }

                // ── 隐藏元素 ──
                var hideSelectors = [
                    'ins.adsbygoogle.adsbygoogle-noablate[data-adsbygoogle-status="done"][data-anchor-status="displayed"]',
                    '.ns-epnsm-e-0.x-layout.GoogleActiveViewElement.web-on-show',
                ];
                var style = document.createElement('style');
                style.textContent = hideSelectors.map(function(s) {
                    return s + '{display:none!important;visibility:hidden!important;height:0!important;width:0!important;overflow:hidden!important;}';
                }).join('\\n');
                document.documentElement.appendChild(style);

                // ── MutationObserver 持续清理 ──
                new MutationObserver(function(ms) {
                    ms.forEach(function(m) {
                        m.addedNodes.forEach(function(n) {
                            if (n.nodeType !== 1) return;
                            var tag = n.tagName || '';
                            var id = n.id || '';
                            var cls = n.className || '';
                            // 删除
                            if (tag === 'INS' && cls.indexOf('adsbygoogle') !== -1) { n.remove(); return; }
                            if (id === 'google-anno-sa') { n.remove(); return; }
                            if (cls.indexOf('fc-monetization-dialog-container') !== -1) { n.remove(); return; }
                            if (/fc-(monetization|dialog|consent|choice|reward)/.test(cls)) { n.remove(); return; }
                            if (cls.indexOf('google-aiuf') !== -1) { n.remove(); return; }
                            if (n.getAttribute && n.getAttribute('data-google-ad-efd') === 'true') { n.remove(); return; }
                            // 隐藏
                            if (cls.indexOf('GoogleActiveViewElement') !== -1) { n.style.cssText = 'display:none!important'; return; }
                        });
                    });
                }).observe(document.documentElement, {childList: true, subtree: true});
            })()
        ''')
    except Exception as e:
        print(f"  [WARN] removeAds 异常: {e}")

    after = _count_ads(browser)
    a_total = after.get('el', 0) + after.get('sc', 0) + after.get('ifr', 0)
    _dbg(f"[去广告后] {after.get('el',0)} DOM, {after.get('sc',0)} script, {after.get('ifr',0)} iframe")
    print(f"  [INFO] 广告清理效果: {b_total} → {a_total} (减少 {b_total - a_total} 个)")

def _tg_header(masked_phone: str, tg_chat: str = None, ip_info: str = "") -> str:
    now_bj = datetime.now(CN_TZ)
    now_utc = datetime.now(timezone.utc)
    user_id = tg_chat if tg_chat else "0000"
    msg = f"🕐 运行时间: {now_bj.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)\n"
    msg += f"🕐 运行时间: {now_utc.strftime('%Y-%m-%d %H:%M:%S')} (UTC)\n"
    if ip_info:
        msg += f"🌐 IP 信息: {ip_info}\n"
    msg += f"👤 账号: <a href='tg://user?id={user_id}'>{masked_phone}</a>\n\n"
    return msg


def notify(result: dict, username: str, tg_token: str = None, tg_chat: str = None,
           ip_info: str = "", server_online: str = ""):
    """发送单个服务器的续期结果通知，先发 VPS 截图再发文字报告"""
    token = tg_token
    chat = tg_chat
    if not token or not chat: return
    try:
        masked_phone = result.get("phoneMask", mask(username))
        header = _tg_header(masked_phone, chat, ip_info)
        hostname = result.get("hostname", "")
        status = result.get("status", "")
        old_valid = result.get("old_valid_until", "")
        new_valid = result.get("new_valid_until", "")
        # 横幅文案提取的日期（不同于 info 页面实际到期日，用于「续期前后」对比之外的独立核对）
        banner_valid = result.get("banner_new_valid_until") or new_valid
        ipv6 = result.get("ipv6", "")
        location = result.get("location", "")
        msg = result.get("message", "")

        # 无论成功还是失败，最后发送最终 info 页面截图，并附带到期日对比。
        vps_shot = result.get("screenshot") or result.get("vps_info_screenshot", "")
        expiry_info = result.get("expiry_info", "")
        if vps_shot and Path(vps_shot).exists():
            caption = f"🖥️ 最终 VPS 信息截图 - {hostname}"
            if expiry_info:
                caption += f"\n📋 info 页面到期日对比: {expiry_info}"
            send_tg_photo(token, chat, vps_shot, caption)

        # 再发文字报告
        if result.get("success"):
            text = (
                f"<b>🎮 {HAX_TITLE} 续期报告</b>\n"
                f"{header}"
                f"✅ 续期成功\n"
                f"🖥️ 服务器: {hostname}\n"
                f"📍 位置: {location}\n"
                f"📊 状态: {status}\n"
                f"📋 info 页面到期日对比: {expiry_info or '未读取到'}\n"
            )
            if old_valid:
                text += f"📋 横幅提取到期日: {old_valid}"
                if banner_valid:
                    text += f" → {banner_valid}"
                text += "\n"
            if ipv6:
                text += f"🌐 IPv6: {ipv6}\n"
            swaps = result.get("warp_swaps", 0)
            retries = result.get("retry_count", 0)
            refresh = result.get("warp_refresh", 0)
            text += f"\n🔥 重试次数: {retries}\n🌐 ip更换次数: {swaps}\n⚡ warp刷新次数: {refresh}\n"
        else:
            text = (
                f"<b>🎮 {HAX_TITLE} 续期报告</b>\n"
                f"{header}"
                f"❌ 续期失败\n"
                f"🖥️ 服务器: {hostname}\n"
                f"📍 位置: {location}\n"
                f"📊 状态: {status}\n"
                f"📋 info 页面到期日对比: {expiry_info or '未读取到'}\n"
            )
            if old_valid:
                text += f"📋 横幅提取到期日: {old_valid}"
                if banner_valid:
                    text += f" → {banner_valid}"
                text += "\n"
            if ipv6:
                text += f"🌐 IPv6: {ipv6}\n"
            swaps = result.get("warp_swaps", 0)
            retries = result.get("retry_count", 0)
            refresh = result.get("warp_refresh", 0)
            text += f"\n🔥 重试次数: {retries}\n🌐 ip更换次数: {swaps}\n⚡ warp刷新次数: {refresh}\n"
            if msg:
                text += f"❗ 错误: {msg}\n"
            detail = result.get("detailMessage", "")
            if detail:
                text += f"📎 详情: {detail}\n"
        if server_online:
            text += f"🌐 {server_online}\n"
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=30)
        print("  [INFO] TG推送成功")
    except Exception as e:
        print(f"  [WARN] TG推送失败: {e}")


def notify_login_fail(username: str, img: str = None, tg_token: str = None, tg_chat: str = None,
                      ip_info: str = "", phone_mask: str = ""):
    token = tg_token
    chat = tg_chat
    if not token or not chat: return
    try:
        masked_phone = phone_mask or mask(username)
        header = _tg_header(masked_phone, chat, ip_info)
        text = (
                f"<b>🎮 {HAX_TITLE} 续期报告</b>\n"
                f"{header}"

            f"❌ 登录失败\n"
        )
        if img and Path(img).exists():
            with open(img, "rb") as f:
                requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat, "caption": text, "parse_mode": "HTML"}, files={"photo": f}, timeout=60)
        else:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=30)
    except: pass

def parse_accounts(s: str) -> List[Dict[str, str]]:
    accounts = []
    # 格式: phone,tg_bot_token,tg_chat_id[,tg_api_id,tg_api_hash,tg_session]
    # 多账号用分号分隔
    for line in re.split(r'[\n;]', s):
        line = line.strip()
        if not line:
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 1:
            print(f"[WARN] 格式错误，跳过: {line}")
            continue
        phone = parts[0]
        acc = {
            "phone": phone,
            "phoneMask": mask_phone(phone),
            "tg_token": parts[1] if len(parts) > 1 else None,
            "tg_chat": parts[2] if len(parts) > 2 else None,
        }
        # 可选：每个账号独立的 TG API 凭据
        if len(parts) > 3 and parts[3]:
            acc["tg_api_id"] = parts[3]
        if len(parts) > 4 and parts[4]:
            acc["tg_api_hash"] = parts[4]
        if len(parts) > 5 and parts[5]:
            acc["tg_session"] = parts[5]
        accounts.append(acc)
    return accounts

def dismiss_cookie_only(browser) -> bool:
    try:
        result = browser.execute_script('''
            (function() {
                var buttons = document.querySelectorAll('button');
                for (var i = 0; i < buttons.length; i++) {
                    var text = buttons[i].textContent.trim();
                    if (text === 'Consent' || text === 'Accept' || text === 'Accept All' ||
                        text === 'Do not consent' || text === 'Reject') {
                        buttons[i].click();
                        return text;
                    }
                }
                return '';
            })()
        ''')
        if result:
            print(f"  [INFO] 已关闭Cookie弹窗 ({result})")
            time.sleep(1)
            return True
    except: pass
    return False

def check_turnstile_done(browser) -> bool:
    try:
        return bool(browser.execute_script('''
            var cf = document.querySelector("input[name='cf-turnstile-response']");
            return cf && cf.value && cf.value.length > 20;
        '''))
    except:
        return False


def hax_handle_turnstile(browser, idx: int) -> bool:
    """处理 页面上的 Turnstile"""
    print("  [INFO] 处理 Turnstile...")
    time.sleep(2)

    dismiss_cookie_only(browser)

    if check_turnstile_done(browser):
        print("  [INFO] Turnstile 已完成")
        return True

    print("  [INFO] Turnstile 验证中...")
    for attempt in range(3):
        print(f"  [INFO] Turnstile 点击 ({attempt+1}/3)")
        clicked = browser.click_turnstile()
        time.sleep(3)

        if check_turnstile_done(browser):
            print(f"  [INFO] Turnstile 通过 ({attempt+1}/3)")
            return True

        if not clicked:
            time.sleep(5)

    print("  [INFO] 等待 Turnstile 完成...")
    start = time.time()
    while time.time() - start < 30:
        if check_turnstile_done(browser):
            print(f"  [INFO] Turnstile 已完成")
            return True
        time.sleep(2)

    print("  [WARN] Turnstile 超时")

    # ── reCAPTCHA 兜底 ──
    print("  [INFO] Turnstile 未通过，检查是否有 reCAPTCHA...")
    if handle_recaptcha_fallback(browser, idx):
        return True
    return False


def handle_recaptcha_fallback(browser, idx: int) -> bool:
    """如果页面上出现 reCAPTCHA，尝试用语音识别求解"""
    try:
        has_recaptcha = browser.execute_script('''
            var iframes = document.querySelectorAll('iframe');
            for (var i = 0; i < iframes.length; i++) {
                var src = iframes[i].getAttribute('src') || '';
                if (src.indexOf('recaptcha') !== -1 && src.indexOf('anchor') !== -1) {
                    return true;
                }
            }
            return false;
        ''')
        if not has_recaptcha:
            return False

        print("  [INFO] 检测到 reCAPTCHA，启动语音识别求解...")
        safe_screenshot(browser, shot(idx, "recaptcha_detect"))

        solver = RecaptchaAudioSolver(browser)
        solved = solver.solve()
        if solved:
            print("  [INFO] reCAPTCHA 语音验证通过")
        else:
            print("  [WARN] reCAPTCHA 语音验证失败")
        return solved
    except CaptchaBlocked as e:
        print(f"  [WARN] reCAPTCHA 封锁: {e}")
        return False
    except Exception as e:
        print(f"  [WARN] reCAPTCHA 处理异常: {e}")
        return False


def hax_click_accept(browser, idx: int) -> bool:
    """点击网页中的 Accept 按钮（confirmRequest）"""
    print("  [INFO] 点击 Accept...")
    time.sleep(3)
    try:
        clicked = browser.execute_script('''
            if (typeof confirmRequest === "function") {
                confirmRequest();
                return true;
            }
            var btn = document.querySelector('button[onclick*="confirmRequest"]');
            if (btn) { btn.click(); return true; }
            return false;
        ''')
        if not clicked:
            print("  [INFO] 使用 DrissionPage 原生按钮点击 Accept...")
            accept = browser.active.ele('css:button.button-item', timeout=5)
            if accept:
                accept.click()
        time.sleep(5)
        # 滚动到 "VPS Information"
        try:
            browser.execute_script('''
                (function() {
                    var els = document.querySelectorAll('h1, h2, h3, h4, div, span, p');
                    for (var i = 0; i < els.length; i++) {
                        if (els[i].textContent.trim() === 'VPS Information') {
                            els[i].scrollIntoView({block: "start", behavior: "instant"});
                            return;
                        }
                    }
                })()
            ''')
            time.sleep(0.5)
        except Exception:
            pass
        safe_screenshot(browser, shot(idx, "hax_login_after_accept"))
        return True
    except Exception as e:
        print(f"  [WARN] Accept 点击异常: {e}")
        return False


def hax_login_flow(browser, phone: str, idx: int, proxy: str = None) -> bool:
    """
    登录流程（Telegram Widget），支持重试。

    流程:
        1. 绕过 CF → 访问 /login
        2. 进入 Telegram Widget iframe → 点 "Log in with Telegram"
        3. 弹出 oauth.telegram.org 新窗口 → 填国家代码 + 手机号 → Continue
        4. Telethon 自动点击777000对话中的 Confirm
        5. 回到弹窗点 Accept
        6. 回到主页面验证登录成功

    重试机制:
        - 最多重试 MAX_LOGIN_RETRIES 次（默认 5 次，可通过环境变量覆盖）
        - 任一次成功立即返回 True，不再继续重试
        - 所有重试均失败则返回 False

    Args:
        browser: DrissionPage 浏览器实例
        phone: 手机号，格式如 "+503-72796921"
        idx: 账号序号，用于截图命名
        proxy: SOCKS5 代理地址

    Returns:
        True 登录成功，False 登录失败
    """
    # 解析手机号：必须用 - 分隔，如 +503-72796921
    raw = phone.strip()
    if "-" not in raw:
        print(f"  [ERROR] 手机号格式错误，需要包含 - 分隔国家代码和手机号，如 503-72796921")
        return False
    parts = raw.split("-", 1)
    country_code = parts[0].lstrip("+").strip()
    phone_num = parts[1].strip()

    for login_attempt in range(1, MAX_LOGIN_RETRIES + 1):
        if login_attempt > 1:
            print(f"\n{'─'*40}")
            print(f"  [INFO] 登录重试 {login_attempt}/{MAX_LOGIN_RETRIES}")
            print(f"{'─'*40}")

        print(f"\n{'─'*40}")
        print(f"  [INFO] hax 登录流程")
        print(f"{'─'*40}")
        print(f"  [INFO] 国家代码: +{country_code}, 手机号: {mask_phone(raw)}")

        ok = _hax_login_flow_inner(browser, country_code, phone_num, raw, idx, proxy)
        if ok:
            return True
        if login_attempt < MAX_LOGIN_RETRIES:
            print(f"  [INFO] 等待 5s 后重试...")
            time.sleep(5)
        else:
            print(f"  [ERROR] 登录重试已达上限 ({MAX_LOGIN_RETRIES}次)")

    return False


def _hax_login_flow_inner(browser, country_code: str, phone_num: str, raw: str,
                          idx: int, proxy: str = None) -> bool:
    """
    hax 登录流程内部实现（单次尝试，不含重试）。

    Args:
        browser: DrissionPage 浏览器实例
        country_code: 国家代码，如 "503"
        phone_num: 手机号（不含国家代码），如 "72796921"
        raw: 原始手机号（含国家代码和分隔符），用于日志
        idx: 账号序号，用于截图命名
        proxy: SOCKS5 代理地址

    Returns:
        True 登录成功，False 登录失败（由外层 hax_login_flow 决定是否重试）
    """
    # 1. 绕过 Cloudflare
    print("  [INFO] 访问 登录页...")
    browser.open(HAX_LOGIN_URL)
    time.sleep(5)

    cf_passed = False
    for attempt in range(6):
        src = browser.get_page_source()
        title = (browser.get_title() or "").lower()
        is_cf = any(i in src for i in ["Just a moment", "Verify you are human"]) or "just a moment" in title
        if not is_cf:
            cf_passed = True
            break
        print(f"  [INFO] Cloudflare 挑战，绕过中 ({attempt+1}/6)...")
        try:
            browser.click_turnstile()
        except Exception:
            pass
        time.sleep(6)
    if not cf_passed:
        return False

    time.sleep(2)

    removeAds(browser)

    # 滚动到 "Login to Hax.co.id"
    try:
        browser.execute_script('''
            (function() {
                var els = document.querySelectorAll('h1, h2, h3, h4, div, span, p');
                for (var i = 0; i < els.length; i++) {
                    if (els[i].textContent.indexOf('Login to Hax') !== -1) {
                        els[i].scrollIntoView({block: "start", behavior: "instant"});
                        return;
                    }
                }
            })()
        ''')
        time.sleep(0.5)
    except Exception:
        pass
    safe_screenshot(browser, shot(idx, "hax_login_page"))

    # 2. 等待 Telegram Widget iframe 加载（最多18秒）
    print("  [INFO] 查找 Telegram Widget iframe...")
    main_window = browser.driver.current_window_handle
    iframe_found = False
    for wait_round in range(6):
        iframe_found = browser.execute_script('''
            return !!document.querySelector('#telegram-login-LoginHaxBot');
        ''')
        if iframe_found:
            break
        # 也尝试宽松匹配
        iframe_found = browser.execute_script('''
            (function() {
                var iframes = document.querySelectorAll("iframe");
                for (var i = 0; i < iframes.length; i++) {
                    var src = (iframes[i].src || "").toLowerCase();
                    var id = (iframes[i].id || "").toLowerCase();
                    if (src.indexOf("telegram") !== -1 || id.indexOf("telegram") !== -1) return true;
                }
                return false;
            })()
        ''')
        if iframe_found:
            break
        print(f"  [INFO] 等待 Telegram Widget 加载... ({wait_round + 1}/6)")
        time.sleep(3)

    if not iframe_found:
        print("  [ERROR] 未找到 Telegram Widget iframe")
        safe_screenshot(browser, shot(idx, "hax_login_no_iframe"))
        return False

    print("  [INFO] 进入 Telegram Widget iframe...")
    try:
        tg_frame = browser.page.get_frame('css:iframe#telegram-login-LoginHaxBot', timeout=10)
    except Exception:
        tg_frame = None
    if not tg_frame:
        print("  [ERROR] Telegram Widget iframe 获取失败")
        safe_screenshot(browser, shot(idx, "hax_login_no_iframe"))
        return False

    print("  [INFO] 使用 DrissionPage 原生元素点击 Log in with Telegram...")
    popup_tab = None
    login_element = None
    before_tabs = set(browser.page.tab_ids)
    try:
        candidates = tg_frame.eles('tag:a', timeout=5) or []
        candidates += tg_frame.eles('tag:button', timeout=1) or []
        for element in candidates:
            text = (element.text or '').strip().lower()
            if 'log in' in text or 'login' in text or 'telegram' in text:
                login_element = element
                break
        if login_element:
            # 优先使用 DrissionPage 官方的 for_new_tab()。
            try:
                popup_tab = login_element.click.for_new_tab(timeout=15)
            except Exception as e:
                print(f"  [WARN] for_new_tab 失败，改用原生点击+轮询: {type(e).__name__}")
                # Telegram Widget 的 window.open() 偶尔晚于 for_new_tab 的等待窗口。
                login_element.click()
                deadline = time.time() + 15
                while time.time() < deadline:
                    new_tabs = [t for t in browser.page.tab_ids if t not in before_tabs]
                    if new_tabs:
                        popup_tab = browser.page.get_tab(new_tabs[-1])
                        break
                    time.sleep(0.2)
    except Exception as e:
        print(f"  [WARN] Telegram Widget 原生点击异常: {type(e).__name__}: {e}")
    browser.driver.switch_to.default_content()

    if not popup_tab:
        print("  [ERROR] Telegram 点击后没有创建 OAuth 标签页")
        safe_screenshot(browser, shot(idx, "hax_login_no_tg_btn"))
        return False

    popup_handle = popup_tab.tab_id
    browser.set_active(popup_handle)
    print(f"  [INFO] OAuth 标签页已捕获: {browser.get_current_url()}")
    time.sleep(2)
    # 滚动到 "Login to Hax.co.id"
    try:
        browser.execute_script('''
            (function() {
                var els = document.querySelectorAll('h1, h2, h3, h4, div, span, p');
                for (var i = 0; i < els.length; i++) {
                    if (els[i].textContent.indexOf('Login to Hax.co.id') !== -1) {
                        els[i].scrollIntoView({block: "start", behavior: "instant"});
                        return;
                    }
                }
            })()
        ''')
        time.sleep(0.5)
    except Exception:
        pass
    safe_screenshot(browser, shot(idx, "hax_login_after_iframe_click"))

    # 3. for_new_tab() 已捕获 OAuth 标签页；确认 URL 后继续填写手机号。
    popup_url = browser.get_current_url()
    if "oauth.telegram.org" not in popup_url:
        print(f"  [WARN] 新标签页 URL 非 Telegram OAuth: {popup_url[:120]}")
    else:
        print(f"  [INFO] OAuth 标签页已确认: {popup_url}")
    safe_screenshot(browser, shot(idx, "hax_login_popup"))

    # 4. OAuth 页面异步渲染完成后，用 DrissionPage 原生元素填写手机号
    print("  [INFO] 填写手机号...")
    phone_code_el = browser.active.ele('#login-phone-code', timeout=15)
    phone_el = browser.active.ele('#login-phone', timeout=15)
    if not phone_code_el or not phone_el:
        print("  [ERROR] OAuth 页面手机号输入框未加载")
        safe_screenshot(browser, shot(idx, "hax_login_no_phone_code"))
        return False

    target_code = f'+{country_code}'
    current_code = phone_code_el.property('value') or phone_code_el.attr('value') or ''
    print(f"  [INFO] OAuth 当前国家码: {current_code or '未知'}，目标: {target_code}")
    # Telegram 的 requestConfirmation() 直接读取 #login-phone-code.value；
    # 不依赖国家下拉菜单，使用 DrissionPage 原生输入同步真实 input 状态。
    print(f"  [INFO] 直接输入国家码: {target_code}")
    phone_code_el.input(target_code, clear=True, by_js=True)
    phone_code_el.run_js("this.dispatchEvent(new Event('input', {bubbles: true})); this.dispatchEvent(new Event('change', {bubbles: true}));")
    time.sleep(0.5)
    current_code = phone_code_el.property('value') or phone_code_el.attr('value') or ''
    if current_code != target_code:
        print(f"  [ERROR] 国家码输入未生效: 当前 {current_code or '未知'}，目标 {target_code}")
        safe_screenshot(browser, shot(idx, "hax_login_wrong_country"))
        return False

    print(f"  [INFO] 输入手机号: {mask_phone(phone_num)}")
    phone_el.input(phone_num, clear=True)
    time.sleep(1)
    safe_screenshot(browser, shot(idx, "hax_login_phone_filled"))

    # 5. 使用 DrissionPage 原生点击 Continue
    print("  [INFO] 点击 Continue...")
    continue_btn = browser.active.ele('css:button[type="submit"]', timeout=5)
    if not continue_btn:
        print("  [ERROR] OAuth Continue 按钮未加载")
        return False
    continue_btn.click()
    time.sleep(5)
    safe_screenshot(browser, shot(idx, "hax_login_after_continue"))

    # 检查是否出现 "We've just sent you a message"
    page_text = browser.execute_script('return document.body ? document.body.innerText : ""') or ""
    if "sent you a message" in page_text.lower() or "confirm" in page_text.lower():
        print("  [INFO] 已发送授权消息，等待确认...")
    else:
        print(f"  [WARN] 页面内容: {page_text[:200]}")

    # 6. Confirm 确认（Telethon 自动 或 等待手动）
    auth_ok = False
    if HAS_TELETHON:
        print("  [INFO] 通过 Telethon 点击 Confirm 按钮...")
        try:
            auth_ok = tg_confirm_auth_sync(str(TG_OFFICIAL_ID), timeout=60, proxy=proxy)
            if auth_ok:
                print("  [INFO] ✅ Confirm 已确认")
            else:
                print("  [WARN] Confirm 未找到或点击失败")
        except Exception as e:
            print(f"  [WARN] Telethon Confirm 确认失败: {e}")

    # 7. 回到弹窗，等待 Accept 按钮出现并点击
    print("  [INFO] 回到弹窗，等待 Accept 按钮...")

    accept_clicked = False
    popup_closed = False

    if popup_handle:
        try:
            browser.set_active(popup_handle)
        except Exception:
            popup_closed = True
            print("  [INFO] 弹窗已自动关闭（登录可能已成功）")

    if not popup_closed:
        max_wait = 90 if not auth_ok else 15
        if not auth_ok:
            max_wait = 15
            print(f"  [INFO] Telethon 未确认，等待 Accept 按钮（{max_wait}s）...")

        for wait in range(max_wait):
            time.sleep(2)
            try:
                has_accept = browser.execute_script('''
                    if (typeof confirmRequest === "function") return true;
                    var btn = document.querySelector('button[onclick*="confirmRequest"]');
                    if (btn) return true;
                    var btns = document.querySelectorAll('button.button-item');
                    for (var i = 0; i < btns.length; i++) {
                        if ((btns[i].textContent || "").trim().toLowerCase() === 'accept') return true;
                    }
                    return false;
                ''')
            except Exception:
                popup_closed = True
                print("  [INFO] 弹窗已关闭")
                break
            if has_accept:
                print(f"  [INFO] Accept 按钮已出现 ({wait*2}s)")
                hax_click_accept(browser, idx)
                accept_clicked = True
                break
            if wait % 5 == 0 and wait > 0:
                print(f"  [INFO] 等待中... ({wait*2}s/{max_wait}s)")

        if not accept_clicked and not popup_closed:
            print("  [WARN] Accept 按钮未出现")

    # 8. 切回主页面验证登录
    print("  [INFO] 切回主页面...")
    browser.set_active(main_window)
    time.sleep(5)
    # 滚动到 "VPS Information"
    try:
        browser.execute_script('''
            (function() {
                var els = document.querySelectorAll('h1, h2, h3, h4, div, span, p');
                for (var i = 0; i < els.length; i++) {
                    if (els[i].textContent.trim() === 'VPS Information') {
                        els[i].scrollIntoView({block: "start", behavior: "instant"});
                        return;
                    }
                }
            })()
        ''')
        time.sleep(0.5)
    except Exception:
        pass
    safe_screenshot(browser, shot(idx, "hax_login_after_accept"))

    # 验证登录成功
    print("  [INFO] 验证登录成功...")
    login_verified = False

    if HAS_TELETHON:
        try:
            codes = tg_extract_codes_sync(str(TG_OFFICIAL_ID), limit=3)
            for c in codes:
                msg = c.get("message", "")
                if "successfully logged in" in msg.lower() or "telegram widgets" in msg.lower():
                    login_verified = True
                    print(f"  [INFO] ✅ 登录成功确认: {msg[:80]}")
                    break
        except Exception as e:
            print(f"  [WARN] TG官方 消息检查: {e}")

    if not login_verified:
        current_url = browser.get_current_url()
        print(f"  [INFO] 当前 URL: {current_url}")
        if "/login" not in current_url.lower():
            login_verified = True
            print("  [INFO] ✅ 登录成功（URL 已跳转）")

    if login_verified:
        print("  [INFO] ✅ 登录成功")
        return True

    print("  [WARN] 登录状态不确定，请检查截图")
    safe_screenshot(browser, shot(idx, "hax_login_uncertain"))
    return False


def hax_get_vps_info(browser, idx: int) -> Tuple[List[Dict[str, str]], str, Optional[str]]:
    """访问 /vps-info/，解析服务器列表"""
    servers, seen = [], set()

    browser.open(HAX_VPS_INFO_URL)
    time.sleep(5)

    # Cloudflare 绕过
    for attempt in range(6):
        src = browser.get_page_source()
        title = (browser.get_title() or "").lower()
        is_cf = any(i in src for i in ["Just a moment", "Verify you are human"]) or "just a moment" in title
        if not is_cf:
            break
        print(f"  [INFO] CF 绕过 ({attempt+1}/6)...")
        try:
            browser.click_turnstile()
        except Exception:
            pass
        time.sleep(6)

    dismiss_cookie_only(browser)

    removeAds(browser)

    # 滚动到 Status 区域再截图，确保关键信息可见
    try:
        browser.execute_script('''
            var el = document.querySelector('label');
            var labels = document.querySelectorAll('label');
            for (var i = 0; i < labels.length; i++) {
                if (labels[i].textContent.indexOf('Status') !== -1) {
                    labels[i].scrollIntoView({block: "start", behavior: "instant"});
                    break;
                }
            }
        ''')
        time.sleep(1)
    except Exception:
        pass
    screenshot = shot(idx, "vps_info")
    safe_screenshot(browser, screenshot)

    src = browser.get_page_source()

    # 解析 label/div 对（VPS Information 用 Bootstrap form 布局）
    try:
        info = browser.execute_script("""
            var info = {};
            var rows = document.querySelectorAll('.row.mb-4, .row');
            for (var i = 0; i < rows.length; i++) {
                var label = rows[i].querySelector('label');
                var value = rows[i].querySelector('.col-sm-7, .col-md-7, div:last-child');
                if (label && value) {
                    var key = label.textContent.trim().replace(/\\s+/g, ' ');
                    var val = value.textContent.trim().replace(/\\s+/g, ' ');
                    if (key && val && key !== val) {
                        info[key] = val;
                    }
                }
            }
            return info;
        """)
        _dbg(f"解析到 {len(info)} 个字段")
        for k, v in (info or {}).items():
            _dbg(f"    {k}: {v}")
    except Exception as e:
        print(f"  [WARN] label/div 解析失败: {e}")
        info = {}

    # 提取 VPS 标识（hostname 中的数字部分作为 ID）
    hostname = (info or {}).get("Hostname", "")
    sid_match = re.search(r'(\d{5,})', hostname)
    sid = sid_match.group(1) if sid_match else hostname

    # 格式化 valid_until: "August 22, 2026" → "2026-08-22"
    raw_valid = (info or {}).get("Valid until", "")
    valid_until = raw_valid
    try:
        valid_until = datetime.strptime(raw_valid, "%B %d, %Y").strftime("%Y-%m-%d")
    except Exception:
        pass

    if sid:
        servers.append({
            "id": sid,
            "name": (info or {}).get("Location", "Unknown"),
            "status": (info or {}).get("Status", "").replace("check real time status here", "").strip(),
            "valid_until": valid_until,
            "hostname": hostname,
            "ipv6": (info or {}).get("IPv6", ""),
            "location": (info or {}).get("Location", ""),
            "disk": (info or {}).get("Total disk space", ""),
            "ram": (info or {}).get("Ram", ""),
            "bandwidth": (info or {}).get("Bandwidth Usage", ""),
            "vps_info_screenshot": screenshot,
        })
        _dbg(f"找到 VPS: {safe_sid_for_filename(sid)} ({(info or {}).get('Location', 'Unknown')})")

    if not servers:
        # 兜底: 正则
        page_text = browser.execute_script("return document.body ? document.body.innerText : ''") or ""
        if "no vps" in page_text.lower() or "no server" in page_text.lower() or "belum" in page_text.lower():
            return [], "", screenshot
        return [], "⚠️ 未找到服务器", screenshot

    return servers, "", screenshot


def _submit_code_page(browser, code: str, idx: int, sid_f: str, proxy: str = None,
                      max_retries: int = 5) -> None:
    """
    在 vps-renew-code 页面：解 reCAPTCHA → 输入验证码 → 提交最终按钮。

    顺序与 auto_hax_private 参考版一致：先解 reCAPTCHA 再输入验证码。
    CaptchaBlocked 时换 IP + 刷新页面重试，复用同一个 TG 验证码（约 3 分钟有效）。
    重试耗尽后向上抛出 CaptchaBlocked，由外层 renew 函数处理。

    Raises:
        CaptchaBlocked: 重试耗尽仍被封锁
    """
    for attempt in range(1, max_retries + 1):
        # 确保在主 frame
        try:
            browser.driver.switch_to.default_content()
        except Exception:
            pass

        # 解 reCAPTCHA（验证码输入之前解，与参考版一致）
        print("  [INFO] 检测 reCAPTCHA...")
        try:
            has_recaptcha = browser.execute_script('''
                return (function() {
                    if (document.querySelector(".rc-anchor-container, .g-recaptcha, [data-sitekey]")) return true;
                    var iframes = document.querySelectorAll("iframe");
                    for (var i = 0; i < iframes.length; i++) {
                        var src = iframes[i].src || "";
                        if (src.indexOf("recaptcha") !== -1 || src.indexOf("google.com/recaptcha") !== -1) return true;
                    }
                    return false;
                })()
            ''')
        except Exception as e:
            print(f"  [WARN] reCAPTCHA 检测脚本异常: {type(e).__name__}: {e}")
            has_recaptcha = None
        print(f"  [INFO] reCAPTCHA 检测结果: {has_recaptcha}")

        if has_recaptcha:
            if HAS_SPEECH:
                print("  [INFO] 检测到 Google reCAPTCHA，启动语音识别...")
                try:
                    recaptcha_solver = RecaptchaAudioSolver(browser)
                    if recaptcha_solver.solve():
                        print("  [INFO] ✅ reCAPTCHA 验证通过(使用的是语音识别)")
                    else:
                        print("  [WARN] reCAPTCHA 自动求解失败")
                except CaptchaBlocked as e:
                    print(f"  [WARN] reCAPTCHA 被封锁: {e}")
                    if attempt < max_retries:
                        print("  [INFO] 重启 WARP 换 IP，刷新验证码页面重试...")
                        if restart_warp(proxy) and handoff_browser_to_warp(browser):
                            time.sleep(5)
                        # 刷新 vps-renew-code 页面，复用 TG 验证码
                        browser.open(HAX_VPS_RENEW_CODE_URL)
                        time.sleep(5)
                        # Cloudflare 绕过
                        for cf_attempt in range(6):
                            src = browser.get_page_source()
                            title = (browser.get_title() or "").lower()
                            is_cf = any(i in src for i in ["Just a moment", "Verify you are human"]) or "just a moment" in title
                            if not is_cf:
                                break
                            try:
                                browser.click_turnstile()
                            except Exception:
                                pass
                            time.sleep(6)
                        dismiss_cookie_only(browser)
                        removeAds(browser)
                        # 刷新后是新算术验证码，必须重新求解，否则提交必被拒
                        try:
                            solve_arithmetic_captcha(browser, proxy=proxy, scene="刷新后")
                        except Exception as eA:
                            print(f"  [WARN] 刷新后算术验证码异常: {eA}")
                        safe_screenshot(browser, shot(idx, f"renew_{sid_f}_code_page_retry_{attempt}"))
                        continue  # 重试验证码页面操作
                    else:
                        print(f"  [ERROR] 验证码页面重试已达上限 ({max_retries}次)")
                        raise  # 被外层 CaptchaBlocked 捕获
                except Exception as e:
                    print(f"  [WARN] reCAPTCHA 求解异常: {e}")
                    if attempt < max_retries:
                        print("  [INFO] 重启 WARP 换 IP，刷新验证码页面重试...")
                        if restart_warp(proxy) and handoff_browser_to_warp(browser):
                            time.sleep(5)
                        browser.open(HAX_VPS_RENEW_CODE_URL)
                        time.sleep(5)
                        for cf_attempt in range(6):
                            src = browser.get_page_source()
                            title = (browser.get_title() or "").lower()
                            is_cf = any(i in src for i in ["Just a moment", "Verify you are human"]) or "just a moment" in title
                            if not is_cf:
                                break
                            try:
                                browser.click_turnstile()
                            except Exception:
                                pass
                            time.sleep(6)
                        dismiss_cookie_only(browser)
                        removeAds(browser)
                        # 刷新后是新算术验证码，必须重新求解，否则提交必被拒
                        try:
                            solve_arithmetic_captcha(browser, proxy=proxy, scene="刷新后")
                        except Exception as eA:
                            print(f"  [WARN] 刷新后算术验证码异常: {eA}")
                        safe_screenshot(browser, shot(idx, f"renew_{sid_f}_code_page_exception_{attempt}"))
                        continue
                    else:
                        print(f"  [ERROR] reCAPTCHA 求解重试已达上限 ({max_retries}次)")
            else:
                print("  [WARN] speech_recognition 未安装，跳过 reCAPTCHA")

        # 检查 浏览器会话是否仍然存活
        try:
            browser.driver.title
        except Exception as e:
            print(f"  [WARN] 浏览器会话已断开（{type(e).__name__}），跳过本次提交")
            if attempt < max_retries:
                time.sleep(3)
                continue
            else:
                print("  [ERROR] 浏览器驱动 已断开且重试耗尽")
                break

        # reCAPTCHA 解完后，输入续期验证码（TG bot 发来的）
        print("  [INFO] 输入续期验证码...")
        try:
            browser.execute_script(f'''
                var input = document.querySelector('input[name="code"], input[name="verification_code"], input[name="otp"]');
                if (input) {{
                    input.value = '{code}';
                    input.dispatchEvent(new Event('input', {{bubbles: true}}));
                    input.dispatchEvent(new Event('change', {{bubbles: true}}));
                }}
            ''')
            print("  [INFO] 验证码已输入")
        except Exception as e:
            print(f"  [ERROR] 验证码输入失败: {e}")
            raise

        time.sleep(1)

        # 滚动到 Renew VPS 按钮区域并截图
        try:
            browser.execute_script('''
                (function() {
                    var els = document.querySelectorAll('label, h2, h3, h4, div, span, p');
                    for (var i = 0; i < els.length; i++) {
                        if (els[i].textContent.trim() === 'Renew VPS') {
                            els[i].scrollIntoView({block: "start", behavior: "instant"});
                            return;
                        }
                    }
                    window.scrollTo(0, document.body.scrollHeight);
                })()
            ''')
            time.sleep(0.5)
        except Exception:
            pass
        safe_screenshot(browser, shot(idx, f"renew_{sid_f}_complete_code"))

        # 只用真实鼠标点击（用户实证：之前跑出进度条的成功轮次就是真实点击）。
        # 不做任何 JS click / XHR POST / form.submit 兜底——那些反而绕过了
        # 网站对「真人交互」的判定，导致 POST 被服务器无视。
        try:
            # 点击前：滚动按钮到视口中央 + 清理广告浮层（含 24h 看广告弹窗）
            removeAds(browser)
            browser.execute_script('''
                (function() {
                    var btn = document.querySelector('button[name="submit_button"], button[data-callback="onSubmit"]');
                    if (btn) btn.scrollIntoView({block: 'center', behavior: 'instant'});
                })();
            ''')
            time.sleep(1)
            clicked = False
            for locator in ('css:button[name="submit_button"]',
                            'css:button[data-callback="onSubmit"]',
                            'css:button.btn-primary',
                            'css:button[type="submit"]',
                            'css:input[type="submit"]'):
                try:
                    element = browser.page.ele(locator, timeout=2)
                    if element:
                        element.click()
                        clicked = True
                        print(f"  [INFO] 真实元素点击成功: {locator}")
                        break
                except Exception:
                    continue
            if not clicked:
                print("  [ERROR] 所有按钮定位器都未命中，未发生任何点击")
        except Exception as e:
            print(f"  [ERROR] 续期的最终按钮点击出错: {e}")

        # 点击后 3 秒截提交结果页，再由外层继续等待并跳转 info。
        time.sleep(3)
        safe_screenshot(browser, shot(idx, f"renew_{sid_f}_after_final_submit"))
        break  # 提交动作已完成，交给外层检查结果


def _renew_fill_form(browser, idx: int, sid_f: str) -> None:
    """打开 /vps-renew/ 并填写表单：web_address + agreement + Turnstile + 点击提交"""
    browser.open(HAX_VPS_RENEW_URL)
    time.sleep(5)

    # Cloudflare 绕过
    for attempt in range(6):
        src = browser.get_page_source()
        title = (browser.get_title() or "").lower()
        is_cf = any(i in src for i in ["Just a moment", "Verify you are human"]) or "just a moment" in title
        if not is_cf:
            break
        print(f"  [INFO] CF 绕过 ({attempt+1}/6)...")
        try:
            browser.click_turnstile()
        except Exception:
            pass
        time.sleep(6)

    dismiss_cookie_only(browser)
    removeAds(browser)

    # 填 web_address（模拟真实用户输入）
    web_addr = "hax.co.id"
    print(f"  [INFO] 填写 web_address: '{web_addr}'")
    try:
        browser.execute_script('''
            var input = document.getElementById('web_address') || document.querySelector('input[name="web_address"]');
            if (input) {
                input.value = '';
                input.focus();
                input.dispatchEvent(new Event('focus', {bubbles: true}));
            }
        ''')
        time.sleep(0.3)
        for ch in web_addr:
            browser.execute_script(f'''
                var input = document.getElementById('web_address') || document.querySelector('input[name="web_address"]');
                if (input) {{
                    input.value += '{ch}';
                    input.dispatchEvent(new Event('input', {{bubbles: true}}));
                    input.dispatchEvent(new Event('change', {{bubbles: true}}));
                }}
            ''')
            time.sleep(random.uniform(0.05, 0.15))
        print("  [INFO] web_address 已填写")
    except Exception as e:
        print(f"  [WARN] web_address 填写失败: {e}")

    # 勾 agreement
    print("  [INFO] 勾选 agreement...")
    try:
        browser.execute_script('''
            var cb = document.querySelector('input[name="agreement"]');
            if (cb && !cb.checked) {
                cb.click();
                cb.dispatchEvent(new Event('change', {bubbles: true}));
            }
        ''')
        time.sleep(0.5)
        checked = browser.execute_script('return document.querySelector(\'input[name="agreement"]\') ? document.querySelector(\'input[name="agreement"]\').checked : false;')
        print(f"  [INFO] agreement 状态: {'✅ 已勾选' if checked else '❌ 未勾选'}")
    except Exception as e:
        print(f"  [WARN] agreement 勾选失败: {e}")

    time.sleep(2)

    # 解 Turnstile
    hax_handle_turnstile(browser, idx)
    time.sleep(2)

    # 提交（点击 Renew VPS 按钮）
    safe_screenshot(browser, shot(idx, f"renew_{sid_f}_before_submit"))
    print("  [INFO] 点击 Renew VPS...")
    try:
        browser.execute_script('''
            var btn = document.querySelector('button[name="submit_button"]');
            if (btn) btn.click();
        ''')
        print("  [INFO] Renew VPS 已点击")
    except Exception as e:
        print(f"  [ERROR] 提交失败: {e}")
        raise

    time.sleep(5)
    safe_screenshot(browser, shot(idx, f"renew_{sid_f}_after_submit"))


def _renew_start_tg_listener(proxy: str = None, since: datetime = None):
    """启动 TG 后台监听线程，返回 (code_holder, thread)。
    since: 只接受此时间之后的验证码（过滤历史旧码）。"""
    code_holder = [None]
    tg_thread = None
    if HAS_TELETHON:
        def _tg_listen():
            try:
                # 先查历史记录（bot 可能在我们启动监听前就发了代码）
                try:
                    extractor = TGVerificationCodeExtractor(proxy=proxy)
                    history = asyncio.run(extractor.fetch_history(HAX_BOT, limit=20))
                    for h in history:
                        if not h.get("code"):
                            continue
                        # 时间过滤：只认本次运行开始之后的码，避免拿到上次运行的旧码
                        if since and h.get("time"):
                            try:
                                msg_time = datetime.fromisoformat(h["time"])
                                if msg_time.tzinfo is None:
                                    msg_time = msg_time.replace(tzinfo=timezone.utc)
                                if msg_time < since:
                                    print(f"  [INFO] 跳过历史旧码({h['time']}): {mask_code(h['code'])}")
                                    continue
                            except (ValueError, TypeError):
                                pass  # 时间解析失败则保守接受
                        code = h['code']
                        code_holder[0] = code
                        masked = mask_code(code)
                        print(f"  [INFO] TG 历史记录找到验证码: {masked}")
                        return
                except Exception as e:
                    _dbg(f"TG 历史查询: {e}")
                # 再监听新消息
                code_holder[0] = tg_wait_for_code_sync(HAX_BOT, timeout=150, proxy=proxy)
                if code_holder[0]:
                    print(f"  [INFO] TG 后台捕获验证码: {code_holder[0]}")
            except Exception as e:
                print(f"  [WARN] TG 后台监听异常: {e}")
        tg_thread = threading.Thread(target=_tg_listen, daemon=True)
        tg_thread.start()
        print("  [INFO] TG 后台监听已启动")
    return code_holder, tg_thread


def handoff_browser_to_warp(browser, target_url: Optional[str] = None) -> bool:
    """关闭带 sing-box 代理的 Chromium，重建无代理实例交给系统 WARP。"""
    old_page = browser.page
    try:
        cookies = old_page.cookies(all_domains=True, all_info=True)
    except Exception as e:
        print(f"  [WARN] 读取浏览器 cookies 失败: {type(e).__name__}: {e}")
        cookies = []

    # 只恢复 HAX 登录态，避免把第三方域 cookies 带入新实例。
    cookies = [
        c for c in (cookies or [])
        if 'hax.co.id' in str(c.get('domain', '')).lower()
        or 'hax.co.id' in str(c.get('url', '')).lower()
    ]
    try:
        old_page.quit()
    except Exception:
        pass

    try:
        co = ChromiumOptions().auto_port()
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-gpu')
        co.set_argument('--disable-dev-shm-usage')
        co.set_argument('--window-size=1920,1080')
        new_page = ChromiumPage(co)
        new_page.set.timeouts(page_load=30, script=30)
        block_ad_domains(new_page)
        new_page.get(HAX_BASE_URL)
        new_page.wait.doc_loaded()
        time.sleep(2)
        if cookies:
            new_page.set.cookies(cookies)
            # cookie 注入后重新导航，确保登录态和文档状态稳定。
            new_page.get(target_url or HAX_BASE_URL)
            new_page.wait.doc_loaded()
            time.sleep(3)
        browser.page = new_page
        browser.active = new_page
        browser.driver = _DPDriver(browser)
        print(f"  [INFO] 已关闭 sing-box 代理浏览器，重建 WARP 直连浏览器（恢复 {len(cookies)} 个 HAX cookies）")
        return True
    except Exception as e:
        print(f"  [ERROR] WARP 直连浏览器重建失败: {type(e).__name__}: {e}")
        return False


def _renew_navigate_to_code_page(browser, idx: int, sid_f: str, proxy: Optional[str] = None,
                                solve_arithmetic: bool = False) -> None:
    """等待 response，确认并点击 INPUT RENEW CODE，验证已进入验证码页。"""
    print("  [INFO] 等待验证码页面入口...")
    deadline = time.time() + 30
    link_info = None
    while time.time() < deadline:
        try:
            link_info = browser.execute_script('''
                return (function() {
                    var resp = document.getElementById('response');
                    if (!resp || resp.innerHTML.length <= 10) return null;
                    var link = resp.querySelector('a[href*="renew-code"]');
                    if (!link) return null;
                    return {href: link.href || '', text: (link.textContent || '').trim()};
                })()
            ''')
            if link_info:
                break
        except Exception:
            pass
        time.sleep(0.5)

    if not link_info:
        raise RuntimeError("30 秒内未找到 INPUT RENEW CODE 链接")

    before_url = browser.get_current_url()
    print(f"  [INFO] 找到 INPUT RENEW CODE: {link_info.get('text', '')!r}")
    clicked = browser.execute_script('''
        return (function() {
            var resp = document.getElementById('response');
            var link = resp && resp.querySelector('a[href*="renew-code"]');
            if (!link) return false;
            link.click();
            return true;
        })()
    ''')
    if not clicked:
        raise RuntimeError("INPUT RENEW CODE 链接点击失败")

    deadline = time.time() + 20
    entered = False
    while time.time() < deadline:
        try:
            url = browser.get_current_url()
            entered = bool(
                'renew-code' in url.lower()
                or browser.execute_script('''
                    return !!(
                        document.querySelector('.col-sm-3 img') &&
                        document.querySelectorAll('.col-sm-3 img').length >= 2
                    );
                ''')
            )
            if entered:
                print(f"  [INFO] INPUT RENEW CODE 点击成功，已进入验证码页: {url}")
                break
        except Exception:
            pass
        time.sleep(0.5)

    if not entered:
        raise RuntimeError(
            f"INPUT RENEW CODE 已点击，但 20 秒内未进入验证码页 "
            f"(before={before_url}, after={browser.get_current_url()})"
        )

    try:
        browser.execute_script('''
            (function() {
                var el = document.querySelector('input[name="code"]')
                      || document.querySelector('input[type="text"]')
                      || document.querySelector('.col-sm-3 img')
                      || document.querySelector('form');
                if (el) el.scrollIntoView({block: "center", behavior: "instant"});
            })()
        ''')
        time.sleep(0.5)
    except Exception:
        pass

    if solve_arithmetic:
        solve_arithmetic_captcha(browser, proxy=proxy, scene="验证码页进入后")


def _renew_get_code(code_holder, tg_thread) -> str:
    """等待并返回 TG 验证码，失败则抛异常"""
    code = code_holder[0]
    if not code and tg_thread:
        print("  [INFO] 等待 TG 后台验证码...")
        # TG 线程要连 Telethon + 查历史 + 监听新消息，30s 常常不够，放宽到 120s
        tg_thread.join(timeout=120)
        code = code_holder[0]

    if not code:
        print("  [ERROR] 未获取到续期验证码")
        raise Exception("未获取到续期验证码")

    print(f"  [INFO] 获取到验证码: {mask_code(code)}")
    return code


def _renew_refresh_code_page_after_captcha(browser, idx: int, sid_f: str,
                                           proxy: str = None) -> bool:
    """服务器拒绝 captcha 后换 WARP IP，并重新准备验证码页面。"""
    print("  [INFO] 服务器拒绝 captcha，立即切换 WARP IP...")
    if not restart_warp(proxy):
        print("  [ERROR] WARP IP 切换失败，停止本次 captcha 重试")
        return False
    if not handoff_browser_to_warp(browser):
        print("  [ERROR] WARP 直连浏览器重建失败，停止本次 captcha 重试")
        return False

    time.sleep(5)
    try:
        browser.open(HAX_VPS_RENEW_CODE_URL)
        time.sleep(5)
        for cf_attempt in range(6):
            src = browser.get_page_source()
            title = (browser.get_title() or "").lower()
            is_cf = any(i in src for i in ["Just a moment", "Verify you are human"]) or "just a moment" in title
            if not is_cf:
                break
            print(f"  [INFO] 换 IP 后处理 Cloudflare ({cf_attempt + 1}/6)...")
            browser.click_turnstile()
            time.sleep(6)
        dismiss_cookie_only(browser)
        removeAds(browser)
        solve_arithmetic_captcha(browser, proxy=proxy, scene="换IP后")
        safe_screenshot(browser, shot(idx, f"renew_{sid_f}_after_warp_rotate"))
        return True
    except Exception as e:
        print(f"  [ERROR] 换 IP 后刷新验证码页面失败: {type(e).__name__}: {e}")
        return False


def _renew_capture_final_info(browser, result: dict, old_valid_until: str) -> None:
    """无论续期成败，最后回到 VPS info 页面并以该页截图/到期日为准。"""
    try:
        servers, error, screenshot = hax_get_vps_info(browser, result.get("_idx", 0))
        if screenshot:
            result["screenshot"] = screenshot
            result["vps_info_screenshot"] = screenshot
        sid = str(result.get("server_id", ""))
        current = next((s for s in servers if str(s.get("id", "")) == sid), None)
        if current:
            new_valid = current.get("valid_until", "")
            if new_valid:
                result["new_valid_until"] = new_valid
                result["expiry_info"] = f"{old_valid_until} → {new_valid}" if old_valid_until else f"新到期日: {new_valid}"
                print(f"  [INFO] info 页面到期日对比: {old_valid_until} → {new_valid}")
                if not result.get("success") and old_valid_until and new_valid != old_valid_until:
                    result["success"] = True
                    result["message"] = "续期成功（info 页面到期时间已更新）"
            result["status"] = current.get("status", result.get("status", ""))
            result["location"] = current.get("location", result.get("location", ""))
            result["hostname"] = current.get("hostname", result.get("hostname", ""))
        elif error:
            print(f"  [WARN] 最终 info 页面未找到服务器: {error}")
    except Exception as e:
        print(f"  [WARN] 最终跳转 info 页面失败: {type(e).__name__}: {e}")


def _drain_alerts(browser) -> str:
    """等待并点掉页面上所有 JS alert，返回最后一条弹窗文本。

    hax 提交续期后用 alert 弹结果（如 "Your VPS has been renewed until ..."），
    不处理会阻塞 DrissionPage 所有后续操作（"存在未处理的提示框"）。
    """
    texts = []
    page = browser.page
    for _ in range(30):  # 最多等 ~15 秒
        try:
            t = page.handle_alert(accept=True, timeout=0.5)
            if isinstance(t, str):
                texts.append(t)
                print(f"  [INFO] 已处理弹窗: {t[:120]}")
                time.sleep(0.5)
                continue
        except Exception:
            pass
        if texts:
            break
        time.sleep(0.5)
    return texts[-1] if texts else ""


def _explain_failure(resp_text: str) -> str:
    """把服务端失败话术翻译成更明确的续期失败原因（用于回传 TG 通知）。"""
    t = (resp_text or "").lower()
    if "verification code is wrong" in t or "verification code" in t:
        return "续期失败: 验证码错误/已失效（验证码无效、已过期或被使用，需重新获取 TG 验证码）"
    if "captcha" in t:
        return "续期失败: 人机验证(captcha)被服务器拒绝，建议换 IP 后重试"
    if "expired" in t:
        return "续期失败: 会话/验证码已过期，请重新登录后续期"
    return f"续期失败: {resp_text}" if resp_text else "续期结果不确定"


def _renew_parse_result(browser, result: dict, old_valid_until: str) -> bool:
    """检查续期结果，更新 result 字典，返回是否成功"""
    # 最终提交后先等待约 5 秒检查成功提醒；没有提醒再回 info 页面兜底。
    # 先点掉提交触发的 JS alert（hax 用 alert 弹续期结果），否则后续操作全被阻塞。
    # alert 文本单独留存：成功话术常在原生 alert 里（不在页面 DOM，截图也抓不到），需用它兜底判定。
    alert_text = ""
    try:
        alert_text = _drain_alerts(browser)
    except Exception as e:
        print(f"  [WARN] 处理弹窗异常: {type(e).__name__}: {e}")
    time.sleep(2)

    # 滚动到 "Renew VPS" 区域再截图
    try:
        browser.execute_script('''
            (function() {
                var els = document.querySelectorAll('label, h2, h3, h4, div, span, p');
                for (var i = 0; i < els.length; i++) {
                    if (els[i].textContent.trim() === 'Renew VPS') {
                        els[i].scrollIntoView({block: "start", behavior: "instant"});
                        return;
                    }
                }
                window.scrollTo(0, document.body.scrollHeight);
            })()
        ''')
        time.sleep(0.5)
    except Exception:
        pass

    # 检查结果：直接对 page.html 做正则匹配（已验证 browser.page.html 可靠，
    # 而 execute_script 在本环境会返回空，不能依赖）。
    # 优先 #response .alert-success；其次 .alert-danger/.alert-warning；
    # 再兜底整页含 "renewed until" 话术。成功横幅可能延迟渲染：轮询最多 6 次 × 4 秒。
    def _scan_html():
        try:
            html = browser.page.html or ""
        except Exception as e:
            print(f"  [WARN] 读取 page.html 失败: {type(e).__name__}: {e}")
            return None
        m = re.search(
            r'id=["\']response["\'][^>]*>.*?class="[^"]*\balert-success\b[^"]*"[^>]*>(.*?)</div>',
            html, re.S | re.I)
        if m:
            # 成功他会走这里
            return {"success": True, "text": re.sub(r'\s+', ' ', m.group(1)).strip(), "dbg": "html"}
        m = re.search(
            r'id=["\']response["\'][^>]*>.*?class="[^"]*\balert-(?:danger|warning)\b[^"]*"[^>]*>(.*?)</div>',
            html, re.S | re.I)
        if m:
            return {"success": False, "text": re.sub(r'\s+', ' ', m.group(1)).strip(), "dbg": "html"}
        if re.search(r'renewed\s+until', html, re.I):
            mm = re.search(r'(Your VPS has been renewed until[^<]*)', html, re.I)
            return {"success": True, "text": (mm.group(1) if mm else "renewed until").strip(), "dbg": "html-body"}
        return None

    response_info = None
    for _attempt in range(6):
        response_info = _scan_html()
        if response_info:
            break
        if _attempt < 5:
            time.sleep(4)
    if response_info:
        print(f"  [DEBUG] 结果检测: success={response_info.get('success')} dbg={response_info.get('dbg','')} alert={str(alert_text)[:80]!r} text={str(response_info.get('text',''))[:150]!r}")

    # DOM 诊断：把 #response 结构、iframe 清单、body 文本打到日志（原始界面），
    # 兜底：原生 JS alert 弹出的续期结果（hax 常用 alert 报成功，且不在页面 DOM 内，
    # 截图也抓不到，但 _drain_alerts 已拿到其文本）。DOM/iframe 扫描都没命中时再用它判定。
    if not (response_info and response_info.get("success")) and alert_text:
        if re.search(r'renewed\s+until', alert_text, re.I):
            response_info = {"success": True, "text": alert_text.strip(), "dbg": "alert"}

    # 最终结果页截图 + 状态诊断（此时横幅已渲染，状态最完整；
    # 放在 _renew_capture_final_info 跳转之前）。均基于 page.html，不依赖 execute_script。
    sid_f = re.sub(r'[^\w]', '_', result.get("server_id", ""))
    idx = result.get("_idx", 0)
    safe_screenshot(browser, shot(idx, f"renew_{sid_f}_result"))
    result["screenshot"] = shot(idx, f"renew_{sid_f}_result")
    try:
        html = browser.page.html or ""
        resp_m = re.search(r'<div id="response".*?</div>\s*</div>', html, re.S | re.I)
        resp_html = resp_m.group(0)[:1500] if resp_m else "NO #response"
        iframes = re.findall(r'<iframe[^>]*\bid="([^"]*)"[^>]*\bsrc="([^"]*)"', html)
        body_m = re.search(r'<body[^>]*>(.*)</body>', html, re.S | re.I)
        body_text = re.sub(r'<[^>]+>', ' ', body_m.group(1)) if body_m else ''
        body_text = re.sub(r'\s+', ' ', body_text).strip()[:600]
        print("  [DEBUG] 最终状态诊断:")
        print(f"    #response: {resp_html}")
        print(f"    iframes({len(iframes)}): {[(i[0], i[1][:80]) for i in iframes]}")
        print(f"    bodyText: {body_text}")
    except Exception as e:
        print(f"  [WARN] 最终状态诊断失败: {e}")

    # 通过 #response .alert-success 判断续期是否成功（参照标准判定逻辑）
    if response_info and response_info.get("success"):
        resp_text = response_info.get("text", "")
        print(f"  [INFO] 续期成功提示: {resp_text}")

        # 从 "Your VPS has been renewed until August 25, 2026" 提取新到期日
        date_match = re.search(r'(\w+ \d{1,2}, \d{4})', resp_text)
        if date_match:
            new_valid_raw = date_match.group(1)
            print(f"  [INFO] 提取到原始日期: {new_valid_raw}")
            try:
                new_valid = datetime.strptime(new_valid_raw, "%B %d, %Y").strftime("%Y-%m-%d")
            except Exception:
                new_valid = new_valid_raw
            result["new_valid_until"] = new_valid
            result["banner_new_valid_until"] = new_valid
            result["expiry_info"] = f"{old_valid_until} → {new_valid}" if old_valid_until else f"新到期日: {new_valid}"
            print(f"  [INFO] 到期日(横幅): {old_valid_until} → {new_valid}")

        result["success"] = True
        result["message"] = "续期成功"
        _renew_capture_final_info(browser, result, old_valid_until)
        return True
    else:
        resp_text = response_info.get("text", "") if response_info else ""
        print(f"  [WARN] 续期失败提示: {resp_text}" if resp_text else "  [WARN] 未检测到续期结果")
        result["message"] = _explain_failure(resp_text)
        result["detailMessage"] = resp_text
        result["expiry_info"] = "续期失败"
        _renew_capture_final_info(browser, result, old_valid_until)
        return False


def renew(browser, server_info: Dict[str, str], idx: int, phone: str,
          tg_token: str = None, tg_chat: str = None, ip_info: str = "",
          proxy: str = None, phone_mask: str = "") -> Dict[str, Any]:
    """
     续期流程，支持重试。

    流程:
        1. 打开 /vps-renew/
        2. 填 web_address
        3. 勾 agreement
        4. 解 Turnstile
        5. 提交
        6. Telethon 从 tg 提取验证码
        7. 输入验证码 → 提交
        8. 检查 #response .alert-success 判断续期是否成功
        9. 从成功提示中提取新到期日

    重试机制:
        - 外层最多重试 MAX_RENEW_RETRIES_PER_URL 次（可通过环境变量覆盖）
        - 内层 CaptchaBlocked 时重启 WARP 换 IP，最多 MAX_WARP_RETRIES 次
        - 任一环节出现 CaptchaBlocked 异常，立即重启 WARP 换 IP
        - 任一环节出现其他异常，重试当前 URL（不换 IP）
        - 任一次成功立即返回 result，不再继续重试

    Args:
        browser: DrissionPage 浏览器实例
        server_info: 服务器信息字典（id, name, hostname, valid_until 等）
        idx: 账号序号，用于截图命名
        phone: 手机号
        tg_token: Telegram Bot Token（用于发送通知）
        tg_chat: Telegram Chat ID（用于发送通知）
        ip_info: 当前 IP 信息（用于通知）
        proxy: SOCKS5 代理地址
        phone_mask: 手机号掩码（用于通知）

    Returns:
        result 字典，包含 success, message, new_valid_until 等字段
    """
    sid = server_info.get("id", "")
    server_name = server_info.get("name", "Unknown")
    hostname = server_info.get("hostname", "")
    old_valid_until = server_info.get("valid_until", "")

    # 记录本次续期过程中的换 IP 次数、warp 刷新次数与外层重试次数
    get_warp_manager().current_tag = phone_mask or mask_phone(phone)
    warp_start = get_warp_manager().rotation_count
    warp_refresh_start = get_warp_manager().warp_refresh_count

    result = {
        "server_id": sid, "server_name": server_name, "hostname": hostname,
        "status": server_info.get("status", ""),
        "ipv6": server_info.get("ipv6", ""),
        "location": server_info.get("location", ""),
        "phoneMask": phone_mask or mask_phone(phone),
        "success": False, "message": "", "detailMessage": "", "screenshot": None,
        "warp_swaps": 0, "warp_refresh": 0, "retry_count": 0,
        "old_valid_until": old_valid_until, "new_valid_until": "",
        "vps_info_screenshot": server_info.get("vps_info_screenshot", ""),
        "_idx": idx,
    }

    sid_f = re.sub(r'[^\w]', '_', sid)

    print(f"\n{'─'*40}")
    print(f"  [INFO] 续期: {server_name} (id={safe_sid_for_filename(sid)})")
    print(f"{'─'*40}")

    # 算术验证码失败时额外保留一次换 IP 重试机会。
    max_attempts = MAX_RENEW_RETRIES_PER_URL + 1
    forced_arithmetic_retry_used = False
    for renew_attempt in range(1, max_attempts + 1):
        if renew_attempt > 1:
            print(f"  [INFO] 续期重试 {renew_attempt}/{MAX_RENEW_RETRIES_PER_URL}...")

        try:
            # 1. 填写表单并提交
            _renew_fill_form(browser, idx, sid_f)

            # 2. 启动 TG 监听（since=现在，只认之后到达的验证码）
            code_holder, tg_thread = _renew_start_tg_listener(proxy, since=datetime.now(timezone.utc) - timedelta(seconds=30))

            # 3. 进入验证码页面 + 解算术验证码
            _renew_navigate_to_code_page(browser, idx, sid_f, proxy)

            # 4. 最终续期页面强制脱离 sing-box，改由系统 WARP 直连。
            # 入口点击可通过代理完成，但验证码和最后提交必须使用同一 WARP 出口。
            print("  [INFO] 已进入最终续期页面，切换为 WARP 直连浏览器...")
            if not restart_warp(proxy):
                raise CaptchaBlocked("最终续期页面 WARP 切换失败")
            if not handoff_browser_to_warp(browser, HAX_VPS_RENEW_CODE_URL):
                raise CaptchaBlocked("最终续期页面 WARP 直连浏览器重建失败")

            # 新浏览器重新打开页面后，验证码图片和 Turnstile 都是新的，不能复用旧页面状态。
            for cf_attempt in range(6):
                src = browser.get_page_source()
                title = (browser.get_title() or "").lower()
                is_cf = any(i in src for i in ["Just a moment", "Verify you are human"]) or "just a moment" in title
                if not is_cf:
                    break
                print(f"  [INFO] WARP 直连页面处理 Cloudflare ({cf_attempt + 1}/6)...")
                try:
                    browser.click_turnstile()
                except Exception:
                    pass
                time.sleep(6)
            dismiss_cookie_only(browser)
            removeAds(browser)
            solve_arithmetic_captcha(browser, proxy=proxy, scene="WARP直连页")
            safe_screenshot(browser, shot(idx, f"renew_{sid_f}_warp_final_page_ready"))
            print("  [INFO] ✅ 最终续期页面已切换为 WARP 直连并重新完成算术验证码")

            # 5. 获取 TG 验证码
            code = _renew_get_code(code_holder, tg_thread)

            # 6. 提交验证码页面 + 检查结果（captcha 错误时内层重试）
            for code_attempt in range(1, MAX_WARP_RETRIES + 1):
                # 5a. 提交验证码页面（算术 + reCAPTCHA + 最终提交）
                _submit_code_page(browser, code, idx, sid_f, proxy)

                # 5b. 检查续期结果
                if _renew_parse_result(browser, result, old_valid_until):
                    break  # 成功，退出

                # captcha 错误时，刷新 vps-renew-code 页面重试
                resp_msg = result.get("message", "")
                if "captcha" in resp_msg.lower() and code_attempt < MAX_WARP_RETRIES:
                    print(f"  [INFO] captcha 错误，换 WARP IP 后重试 ({code_attempt}/{MAX_WARP_RETRIES})...")
                    if _renew_refresh_code_page_after_captcha(browser, idx, sid_f, proxy):
                        continue  # 新 IP + 新验证码页面后重试 _submit_code_page
                    result["message"] = "续期失败: captcha 被服务器拒绝，WARP 换 IP 失败"
                    result["detailMessage"] = "captcha 被服务器拒绝，WARP 换 IP 失败"
                    break

                break  # 非 captcha 错误或重试耗尽，退出内层循环

            if result["success"]:
                break  # 成功，退出外层重试

            if renew_attempt < MAX_RENEW_RETRIES_PER_URL:
                print(f"  [INFO] 等待 5s 后重试...")
                time.sleep(5)
                continue
            break

        except CaptchaBlocked as e:
            # reCAPTCHA 音频未下发、算术验证码无法识别等都说明当前出口不可用。
            # 算术验证码即使到达普通重试上限，也额外保留一次 WARP 重试。
            is_arithmetic_failure = isinstance(e, ArithmeticCaptchaUnavailable)
            can_retry = renew_attempt < MAX_RENEW_RETRIES_PER_URL
            if is_arithmetic_failure and not forced_arithmetic_retry_used:
                can_retry = True
                forced_arithmetic_retry_used = True
            if can_retry:
                print(f"  [WARN] {type(e).__name__}: {e}")
                print("  [INFO] 验证码失败，立即切换 WARP IP 后重试...")
                if restart_warp(proxy) and handoff_browser_to_warp(browser):
                    time.sleep(5)
                    continue
            result["message"] = f"验证码失败: {e}"
            break
        except Exception as e:
            print(f"  [ERROR] 续期异常: {e}")
            if renew_attempt < MAX_RENEW_RETRIES_PER_URL:
                print(f"  [INFO] 等待 5s 后重试...")
                time.sleep(5)
                continue
            result["message"] = f"续期异常: {e}"
            break

    result["warp_swaps"] = get_warp_manager().rotation_count - warp_start
    result["warp_refresh"] = get_warp_manager().warp_refresh_count - warp_refresh_start
    result["retry_count"] = max(renew_attempt - 1, 0)
    print(f"  [STATS][{result.get('phoneMask','')}] 续期重试={result['retry_count']} ip更换={result['warp_swaps']} warp刷新={result['warp_refresh']}")

    print(f"  {'[INFO]' if result['success'] else '[ERROR]'} {result['message']}")
    notify(result, phone, tg_token=tg_token, tg_chat=tg_chat, ip_info=ip_info)
    return result


def logout(browser):
    try:
        browser.open(f"{HAX_BASE_URL}/logout")
        time.sleep(3)
    except Exception:
        pass

def process(browser, phone: str, idx: int, tg_token: str = None, tg_chat: str = None,
            ip_info: str = "", proxy: str = None, phone_mask: str = "") -> Dict[str, Any]:
    result = {"username": phone, "phoneMask": phone_mask or mask_phone(phone),
              "success": False, "message": "", "servers": []}

    # 1. 登录（手机号 + TG 验证码）
    login_ok = hax_login_flow(browser, phone, idx, proxy=proxy)
    if not login_ok:
        result["message"] = "登录失败"
        notify_login_fail(phone, tg_token=tg_token, tg_chat=tg_chat, ip_info=ip_info,
                          phone_mask=result.get("phoneMask", ""))
        return result

    # 2. 查询 VPS 信息
    servers, error, dash_shot = hax_get_vps_info(browser, idx)
    if error and not servers:
        result["message"] = error
        notify_login_fail(phone, dash_shot, tg_token=tg_token, tg_chat=tg_chat, ip_info=ip_info,
                          phone_mask=result.get("phoneMask", ""))
        logout(browser)
        return result

    print(f"\n  [INFO] 找到 {len(servers)} 个服务器")
    for s in servers:
        print(f"    - ID: {safe_sid_for_filename(s['id'])}, Name: {s.get('name', 'Unknown')}")

    # 3. 对每个服务器续期
    for srv in servers:
        r = renew(browser, srv, idx, phone,
                  tg_token=tg_token, tg_chat=tg_chat, ip_info=ip_info,
                  proxy=proxy, phone_mask=result.get("phoneMask", ""))
        result["servers"].append(r)
        time.sleep(3)

    ok = sum(1 for s in result["servers"] if s.get("success"))
    result["success"] = ok > 0
    result["message"] = f"{ok}/{len(result['servers'])} 成功"

    logout(browser)
    return result

def mask_ip(ip: str) -> str:
    return ip.rsplit(".", 1)[0] + ".***"


def check_port(port: int, host: str = "127.0.0.1", timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def check_ip(proxy: str = None) -> str:
    mode = "✅ 代理" if proxy else "⚠️ 直连"
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = requests.get(
            "http://ip-api.com/json/?fields=status,query,countryCode",
            proxies=proxies, timeout=30
        ).json()
        if r.get("status") == "success":
            ip_str = f"{mask_ip(r['query'])} ({r['countryCode']})"
            return f"{ip_str} [{mode}]"
        print(f"[DEBUG] IP 查询返回异常状态: {r.get('status', 'unknown')}")
    except Exception as e:
        # 不输出代理 URL、认证信息或节点地址，只输出异常类型和安全文本。
        print(f"[DEBUG] IP 查询失败 ({mode}): {type(e).__name__}: {str(e)[:180]}")
    return f"未知 IP [{mode}]"


def restart_warp(proxy: str = None):
    """重启 WARP 以更换出口 IP（参考 demo/main.py）"""
    return get_warp_manager().rotate_ip(proxy=proxy,
                                        max_attempts=MAX_WARP_ROTATE_ATTEMPTS)


class WarpManager:
    """
    系统级 WARP VPN IP 轮换。
    _used_ips 记录本次运行已用过的 IP，重复时自动重试。
    """
    def __init__(self):
        self._used_ips: set = set()
        self.rotation_count: int = 0
        self.warp_refresh_count: int = 0
        self.current_tag: str = ""

    def _run(self, args: list, timeout: int = None) -> subprocess.CompletedProcess:
        if timeout is None:
            timeout = WARP_CMD_TIMEOUT
        cmd = ["sudo", "warp-cli", "--accept-tos"] + args
        print(f"  [WARP] 执行: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # 超时必须杀整个进程组：只杀 sudo 会留下孤儿 warp-cli 继续握着
            # stdout 管道，communicate() 会一直阻塞到它自然退出（曾卡 8 分钟）。
            import os as _os
            import signal as _signal
            try:
                _os.killpg(_os.getpgid(proc.pid), _signal.SIGKILL)
            except Exception:
                proc.kill()
            out, err = proc.communicate()
            print(f"  [WARP] ⚠️  命令超时({timeout}s)，已强杀进程组")
        result = subprocess.CompletedProcess(cmd, proc.returncode or 0,
                                             stdout=out or "", stderr=err or "")
        if result.stdout.strip():
            print(f"  [WARP] stdout: {result.stdout.strip()}")
        if result.stderr.strip():
            print(f"  [WARP] stderr: {result.stderr.strip()}")
        return result

    def _get_current_ip(self, proxy: str = None) -> str:
        # 优先通过 SOCKS5 代理查询，得到的是浏览器实际出口 IP（hax.co.id 看到的 IP），
        # 而不是系统 WARP IP。否则记录的初始 IP 与去重池都会用错。
        if proxy is not None:
            try:
                proxies = {"http": proxy, "https": proxy}
                r = requests.get("https://api.ipify.org", proxies=proxies, timeout=20)
                return r.text.strip()
            except Exception:
                pass
        try:
            r = subprocess.run(
                ["curl", "-s", "--max-time", "15", "https://api.ipify.org"],
                capture_output=True, text=True, timeout=20
            )
            return r.stdout.strip()
        except Exception:
            return ""

    def _wait_connected(self, max_wait: int = 60) -> bool:
        print(f"  [WARP] 等待 VPN 连接就绪（最多 {max_wait}s）...")
        start = time.time()
        while time.time() - start < max_wait:
            try:
                r = subprocess.run(
                    ["curl", "-s", "--max-time", "10",
                     "https://www.cloudflare.com/cdn-cgi/trace"],
                    capture_output=True, text=True, timeout=15
                )
                trace = r.stdout
                if "warp=on" in trace or "warp=plus" in trace:
                    ip_lines = [l for l in trace.splitlines() if l.startswith("ip=")]
                    ip = ip_lines[0].split("=")[1] if ip_lines else "unknown"
                    print(f"  [WARP] ✅ VPN 就绪，出口 IP: {ip}")
                    return True
            except Exception as e:
                print(f"  [WARP] 等待中... ({e})")
            time.sleep(3)
        print("  [WARP] ❌ 等待超时，warp 未激活")
        return False

    def _do_one_rotate(self, proxy: str = None) -> str:
        self.warp_refresh_count += 1
        print(f"  [WARP] [{self.current_tag}] warp刷新第 {self.warp_refresh_count} 次")
        self._run(["disconnect"])
        time.sleep(2)
        self._run(["registration", "delete"])
        time.sleep(2)
        result = self._run(["registration", "new"])
        if result.returncode != 0:
            print("  [WARP] ❌ 注册失败")
            return ""
        time.sleep(3)
        self._run(["connect"])
        time.sleep(5)
        if not self._wait_connected(max_wait=60):
            print("  [WARP] ❌ WARP 连接失败")
            return ""
        return self._get_current_ip(None)  # 直连查询，得到真实 WARP 出口

    def rotate_ip(self, attempt_idx: int = 0, max_attempts: int = 8, proxy: str = None) -> bool:
        print(f"  [WARP] ========== 第 {attempt_idx + 1} 次 IP 轮换 ==========")
        print(f"  [WARP] 已用 IP 池: {self._used_ips if self._used_ips else '(空)'}")

        old_ip = self._get_current_ip(proxy)
        print(f"  [WARP] 旧 IP(代理出口): {old_ip}")

        for i in range(1, max_attempts + 1):
            print(f"  [WARP] 轮换尝试 {i}/{max_attempts}")
            new_ip = self._do_one_rotate(proxy=None)  # 不传 proxy，查询真实 WARP 出口

            if not new_ip:
                print(f"  [WARP] ⚠️  第 {i} 次轮换失败，继续重试")
            elif new_ip in self._used_ips:
                print(f"  [WARP] ♻️  IP {new_ip} 已被本次运行使用过，继续尝试...")
            else:
                self._used_ips.add(new_ip)
                self.rotation_count += 1
                print(f"  [WARP] [{self.current_tag}] 不重复新IP第 {self.rotation_count} 个: {new_ip}")
                if new_ip != old_ip:
                    print(f"  [WARP] ✅ IP 已变化: {old_ip} → {new_ip}")
                else:
                    print(f"  [WARP] ⚠️  IP 与旧 IP 相同（{new_ip}），但未被本轮其他请求使用，接受")
                print(f"  [WARP] 已用 IP 池: {self._used_ips}")
                return True

            # 拿不到新 IP 不能放弃（带着被封 IP 去解验证码必失败），
            # 递增间隔再试：立即重注册 Cloudflare 往往返回同一 colo 的同一出口。
            if i < max_attempts:
                delay = min(5 * i, 20)
                print(f"  [WARP] 等待 {delay}s 后再次轮换...")
                time.sleep(delay)

        print(f"  [WARP] ❌ {max_attempts} 次尝试均未获得新 IP，当前出口: {self._get_current_ip()}")
        return False

    def record_initial_ip(self, proxy: str = None):
        ip = self._get_current_ip(proxy)
        if ip:
            self._used_ips.add(ip)
            print(f"  [WARP] 记录初始 IP(代理出口): {ip}，已用 IP 池: {self._used_ips}")


_warp_manager: WarpManager = None

def get_warp_manager() -> WarpManager:
    global _warp_manager
    if _warp_manager is None:
        _warp_manager = WarpManager()
    return _warp_manager


def send_tg_message(tg_token, tg_chat, message):
    for attempt in range(3):
        try:
            requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                data={"chat_id": tg_chat, "text": message, "parse_mode": "HTML"},
                proxies={"http": None, "https": None},
                timeout=30,
            )
            return True
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"[WARN] TG 消息发送失败: {e}")
    return False


def send_tg_photo(tg_token, tg_chat, photo_path, caption=""):
    if not os.path.isfile(photo_path):
        print(f"[WARN] 截图不存在: {photo_path}")
        return False
    for attempt in range(3):
        try:
            with open(photo_path, "rb") as f:
                resp = requests.post(
                    f"https://api.telegram.org/bot{tg_token}/sendPhoto",
                    data={"chat_id": tg_chat, "caption": caption, "parse_mode": "HTML"},
                    files={"photo": f},
                    proxies={"http": None, "https": None},
                    timeout=30,
                )
            return resp.status_code == 200
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"[WARN] TG 截图发送失败: {e}")
    return False


def _send_tg_report(results, masked_phone, tg_token, tg_chat, ip_info):
    all_servers = []
    for r in results:
        all_servers.extend(r.get("servers", []))

    total_servers = len(all_servers)
    success_count = sum(1 for s in all_servers if s["success"])
    failed_count = total_servers - success_count

    CaptchaSolver.print_stats()

    print(f"\n📊 续期统计 - {masked_phone}")
    print(f"   总服务器数: {total_servers}")
    print(f"   ✅ 续期成功: {success_count}")
    print(f"   ❌ 续期失败: {failed_count}")

    summary = f"\n" + "-" * 40 + "\n"
    summary += f"<b>📊 {HAX_TITLE} 续期统计</b>\n"
    summary += _tg_header(masked_phone, tg_chat, ip_info)
    summary += f"🖥️ 总服务器数: {total_servers}\n"
    summary += f"✅ 续期成功: {success_count}\n"
    summary += f"❌ 续期失败: {failed_count}\n"

    if send_tg_message(tg_token, tg_chat, summary):
        print(f"✅ TG 报告已发送 ({masked_phone})")
    else:
        print(f"⚠️ TG 报告发送失败 ({masked_phone})")


def main():
    # ── 正常续期流程 ─────────────────────────────────────────────────
    # 校验必填环境变量
    if not BATCH:
        print("[ERROR] 缺少必填环境变量: BATCH")
        sys.exit(1)

    acc_str = BATCH
    accounts = parse_accounts(acc_str)
    if not accounts:
        print("[ERROR] BATCH中无有效账号"); sys.exit(1)

    # 检查 TG 凭据：全局或每账号至少有一套
    has_global_tg = bool(TG_SESSION and TG_API_HASH)
    for acc in accounts:
        if not has_global_tg and not (acc.get("tg_api_hash") and acc.get("tg_session")):
            print(f"[ERROR] 账号 {acc['phone']} 缺少 TG 凭据（需全局 TG_SESSION/TG_API_HASH 或 BATCH 中包含）")
            sys.exit(1)

    print("=" * 40)
    print("  Hax Auto Renew")
    print("=" * 40)

    proxy = ""
    socks_port = os.environ.get("SOCKS_PORT", "10808").strip()

    # 通过 PROXY_CONTENT 生成 sing-box 配置
    if os.environ.get("PROXY_CONTENT", "").strip():
        proxy_content = os.environ.get("PROXY_CONTENT", "").strip()
        proxy_retry = int(os.environ.get("PROXY_RETRY_COUNT", "3").strip())
        ip_info = ""
        proxy_setup_ok = False
        for proxy_attempt in range(1, proxy_retry + 1):
            tag = f"[PROXY {proxy_attempt}/{proxy_retry}]"
            print(f"[INFO] {tag} 生成代理配置...")
            result = setup_proxy(proxy_content)
            if not result["success"]:
                err_msg = result.get("error", "未知错误")
                print(f"[ERROR] {tag} 代理配置生成失败: {err_msg}")
                if proxy_attempt < proxy_retry:
                    print(f"[INFO] {tag} 等待 3s 后重试...")
                    time.sleep(3)
                    continue

            # 先杀掉旧的 sing-box 进程
            subprocess.run(["pkill", "-f", "sing-box"], capture_output=True)
            time.sleep(1)

            # 启动 sing-box
            print(f"[INFO] {tag} 启动 sing-box...")
            subprocess.Popen(
                ["./sing-box", "run", "-c", "sing-box_config.json"],
                stdout=open("sing-box.log", "w"),
                stderr=subprocess.STDOUT
            )
            time.sleep(3)

            # 测试代理连接
            socks5_addr = f"socks5://127.0.0.1:{socks_port}"
            if check_port(int(socks_port)):
                ip_info = check_ip(socks5_addr)
                if "未知" not in ip_info:
                    proxy_setup_ok = True
                    proxy = socks5_addr
                    print(f"[INFO] {tag} ✅ 代理连接成功 ({ip_info})")
                    break
                else:
                    print(f"[WARN] {tag} 代理端口已监听但 SOCKS5 出口测试失败")
                    try:
                        with open("sing-box.log") as f:
                            log_content = f.read().strip()
                            if log_content:
                                print(f"[WARN] {tag} sing-box.log:\n{log_content}")
                    except:
                        pass
            else:
                print(f"[WARN] {tag} 代理端口未监听，sing-box 可能未正常启动")
                try:
                    with open("sing-box.log") as f:
                        print(f.read())
                except:
                    pass

            if proxy_attempt < proxy_retry:
                print(f"[INFO] {tag} 等待 5s 后重试...")
                time.sleep(5)

        if not proxy_setup_ok:
            print(f"[WARN] ❌ 代理配置 {proxy_retry} 次尝试均失败，回退到直连模式")
            proxy = None
            ip_info = check_ip()

    else:
        ip_info = check_ip()

    display = setup_display()
    results = []

    try:
        try:
            import nest_asyncio
            nest_asyncio.apply()
            print("[INFO] nest_asyncio 已应用")
        except ImportError:
            pass

        # DrissionPage ChromiumPage：使用 DrissionPage ChromiumPage。
        co=ChromiumOptions().auto_port(); co.set_argument('--no-sandbox'); co.set_argument('--disable-gpu'); co.set_argument('--disable-dev-shm-usage'); co.set_argument('--window-size=1920,1080')
        if proxy:
            # DrissionPage 对 SOCKS 代理会提示“不支持”，但 Chromium 原生支持。
            # 直接注入 Chromium 参数，并明确让本地 CDP/调试地址绕过代理。
            co.set_argument('--proxy-server', proxy)
            co.set_argument('--proxy-bypass-list', '<-loopback>')
            print('[INFO] 使用 Chromium 原生 SOCKS5 代理')
        page=ChromiumPage(co); page.set.timeouts(page_load=30,script=30); block_ad_domains(page); browser=DPBrowser(page)
        get_warp_manager().record_initial_ip(proxy)
        for i, acc in enumerate(accounts, 1):
            # 每个账号可独立设置 TG 凭据，未设置则沿用全局
            if acc.get("tg_api_id"):
                globals()["TG_API_ID"] = int(acc["tg_api_id"])
            if acc.get("tg_api_hash"):
                globals()["TG_API_HASH"] = acc["tg_api_hash"]
            if acc.get("tg_session"):
                globals()["TG_SESSION"] = acc["tg_session"]

            r=process(browser,acc["phone"],i,tg_token=acc.get("tg_token"),tg_chat=acc.get("tg_chat"),ip_info=ip_info,proxy=proxy,phone_mask=acc.get("phoneMask", ""))
            results.append(r)

    except Exception as e:
        print(f"[ERROR] 脚本异常: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    finally:
        try: browser.quit()
        except Exception: pass
        if display:
            display.stop()

    ok_acc = sum(1 for r in results if r.get("success"))
    total_srv = sum(len(r.get("servers", [])) for r in results)
    ok_srv = sum(sum(1 for s in r.get("servers", []) if s.get("success")) for r in results)

    print(f"\n{'='*40}")
    print(f"[INFO] 账号: {ok_acc}/{len(results)} | 服务器: {ok_srv}/{total_srv}")
    for r in results:
        status = "[INFO]" if r.get("success") else "[ERROR]"
        print(f"{status} {r.get('phoneMask', mask_phone(r['username']))}: {r.get('message', '')}")
        for s in r.get("servers", []):
            s_status = "[INFO]" if s.get("success") else "[ERROR]"
            print(f"  {s_status} {s.get('server_name', 'Unknown')}: {s.get('message', '')}")
    print(f"{'='*40}")

    # 发送 TG 汇总报告：仅当 BATCH 含多个账号时才发送；
    # 单账号由单台续期报告覆盖，这里只补打验证码求解统计到控制台。
    if len(accounts) > 1:
        acc_tg_map = {acc["phone"]: (acc.get("tg_token"), acc.get("tg_chat")) for acc in accounts}
        for r in results:
            tg_token, tg_chat = acc_tg_map.get(r["username"], (None, None))
            if tg_token and tg_chat:
                _send_tg_report([r], r.get('phoneMask', mask_phone(r["username"])), tg_token, tg_chat, ip_info)
    else:
        CaptchaSolver.print_stats()

    # sys.exit(0 if ok_srv > 0 else 1)

if __name__ == "__main__":
    main()
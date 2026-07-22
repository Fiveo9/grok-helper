from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
import argparse
import shutil
import tempfile
import datetime
import logging
import time
import os
import secrets
import sys
import threading
from faker import Faker

fake = Faker('en_US')

from email_register import get_email_and_token, get_oai_code


def setup_run_logger() -> logging.Logger:
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"run_{ts}.log")

    logger = logging.getLogger("grok_helper")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("日志文件: %s", log_path)
    return logger


run_logger: logging.Logger = None



def ensure_stable_python_runtime():
    # 优先自动切到更稳定的 3.12 / 3.13，避免 3.14 下 Mail.tm 偶发 TLS/兼容问题。
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}")
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    # 中文提示：避免把底层 TLS 兼容问题误判成脚本逻辑错误。
    if sys.version_info >= (3, 14):
        print("[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。")


ensure_stable_python_runtime()
warn_runtime_compatibility()

# 无头服务器自动启用 Xvfb 虚拟显示器
_virtual_display = None
if not os.environ.get("DISPLAY") or os.environ.get("USE_XVFB") == "1":
    try:
        from pyvirtualdisplay import Display
        _virtual_display = Display(visible=0, size=(1920, 1080))
        _virtual_display.start()
        print(f"[*] Xvfb 虚拟显示器已启动: {os.environ.get('DISPLAY')}")
    except Exception as e:
        print(f"[Warn] Xvfb 启动失败: {e}，将尝试直接运行")


def stop_virtual_display():
    global _virtual_display
    if _virtual_display is None:
        return
    try:
        _virtual_display.stop()
    except Exception:
        pass
    _virtual_display = None


co = ChromiumOptions()
co.auto_port()
co.set_argument("--no-sandbox")
co.set_argument("--disable-gpu")
co.set_argument("--disable-dev-shm-usage")
co.set_argument("--disable-software-rasterizer")
if not os.environ.get("DISPLAY"):
    co.set_argument("--headless=new")

# 从 config.json 读取代理配置给浏览器
_browser_proxy = ""
try:
    import json as _json_mod
    _cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.isfile(_cfg_path):
        with open(_cfg_path, "r") as _f:
            _cfg = _json_mod.load(_f)
        _browser_proxy = str(_cfg.get("browser_proxy", "") or _cfg.get("proxy", "") or "")
except Exception:
    pass
if _browser_proxy:
    co.set_proxy(_browser_proxy)
    print(f"[*] 浏览器代理: {_browser_proxy}")

# Linux 服务器自动检测 chromium 路径
import platform
import shutil
import glob as _glob_mod
_linux_browser_path = ""
if platform.system() == "Linux":
    # 优先用 playwright 装的 chromium（无 AppArmor 限制）
    _pw_chromes = _glob_mod.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    if _pw_chromes:
        _linux_browser_path = _pw_chromes[0]
        co.set_browser_path(_linux_browser_path)
    else:
        for _candidate in ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]:
            if os.path.isfile(_candidate):
                _linux_browser_path = _candidate
                co.set_browser_path(_linux_browser_path)
                break
    # user_data_path 在 start_browser() 每轮动态设置，此处不固定

co.set_timeouts(base=1)

# 加载修复 MouseEvent.screenX / screenY 的扩展。
EXTENSION_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "turnstilePatch"))
co.add_extension(EXTENSION_PATH)

_chrome_temp_dir: str = ""
browser = None
page = None

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

_sso_dir = os.path.join(os.path.dirname(__file__), "sso")
_sso_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
DEFAULT_SSO_FILE = os.path.join(_sso_dir, f"sso_{_sso_ts}.txt")


def start_browser():
    # 每轮从全新浏览器开始，使用独立临时 profile 目录避免 Cookie/Session 复用。
    global browser, page, _chrome_temp_dir
    if platform.system() == "Linux" and not _linux_browser_path:
        raise RuntimeError(
            "未找到 Chrome/Chromium。请先安装浏览器后再运行。"
            "宿主机至少需要安装以下依赖："
            "`pip install -r requirements.txt`、`apt install xvfb`、"
            "`apt install chromium-browser` 或 `apt install google-chrome-stable`。"
        )
    _chrome_temp_dir = tempfile.mkdtemp(prefix="chrome_run_")
    co.set_user_data_path(_chrome_temp_dir)
    browser = Chromium(co)
    tabs = browser.get_tabs()
    page = tabs[-1] if tabs else browser.new_tab()
    return browser, page


def stop_browser():
    # 完整关闭整个浏览器实例，并清理本轮临时 profile，供下一轮重新拉起。
    global browser, page, _chrome_temp_dir
    if browser is not None:
        try:
            browser.quit()
        except Exception:
            pass
    browser = None
    page = None
    if _chrome_temp_dir and os.path.isdir(_chrome_temp_dir):
        shutil.rmtree(_chrome_temp_dir, ignore_errors=True)
    _chrome_temp_dir = ""


def restart_browser():
    # 清除 cookie/storage 代替完整重启，节省 Chrome 冷启动时间。
    global browser, page
    if browser is None:
        start_browser()
        return
    try:
        tabs = browser.get_tabs()
        page = tabs[-1] if tabs else browser.new_tab()
        page.run_js("window.localStorage.clear(); window.sessionStorage.clear();")
        page.clear_cache(session_storage=True, cookies=True)
    except Exception:
        stop_browser()
        start_browser()


def refresh_active_page():
    # 验证码确认后页面会跳转，旧 page 句柄可能断开，这里统一重新获取当前活动标签页。
    global browser, page
    if browser is None:
        start_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
        else:
            page = browser.new_tab()
    except Exception:
        restart_browser()
    return page


def open_signup_page():
    # 每轮开始时打开注册页，并切到“使用邮箱注册”流程。
    global page
    refresh_active_page()
    try:
        page.get(SIGNUP_URL)
    except Exception:
        refresh_active_page()
        page = browser.new_tab(SIGNUP_URL)
    click_email_signup_button()


def close_current_page():
    # 兼容旧调用名，实际行为改为整轮重启浏览器。
    restart_browser()


def has_profile_form():
    # 最终注册页只要出现姓名和密码输入框，就认为已经成功进入资料填写阶段。
    refresh_active_page()
    try:
        return bool(page.run_js(
            """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
        ))
    except Exception:
        return False


def click_email_signup_button(timeout=10):
    # 页面打开后，自动点击“使用邮箱注册”按钮。
    deadline = time.time() + timeout
    while time.time() < deadline:
        clicked = page.run_js(r"""
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text.includes('使用邮箱注册') || text.includes('signupwithemail') || text.includes('signupemail') || text.includes('continuewith email') || text.includes('email');
});

if (!target) {
    return false;
}

target.click();
return true;
        """)

        if clicked:
            return True

        time.sleep(0.5)

    raise Exception('未找到“使用邮箱注册”按钮')


def fill_email_and_submit(timeout=15):
    # 复用 `email_register.py` 里的邮箱获取逻辑，保留邮箱与 token 供后续验证码步骤继续使用。
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")

    deadline = time.time() + timeout
    while time.time() < deadline:
        filled = page.run_js(
            """
const email = arguments[0];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input) {
    return 'not-ready';
}

input.focus();
input.click();

// 不能只写 `input.value = xxx`，否则 React / 受控表单可能没有同步内部状态。
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) {
    tracker.setValue('');
}
if (valueSetter) {
    valueSetter.call(input, email);
} else {
    input.value = email;
}

input.dispatchEvent(new InputEvent('beforeinput', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new Event('change', { bubbles: true }));

if ((input.value || '').trim() !== email || !input.checkValidity()) {
    return false;
}

input.blur();
return 'filled';
            """,
            email,
        )

        if filled == 'not-ready':
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(0.8)
            clicked = page.run_js(
                r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input || !input.checkValidity() || !(input.value || '').trim()) {
    return false;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '注册' || text.includes('注册') || t === 'signup' || t === 'sign up' || t.includes('sign up');
});

if (!submitButton || submitButton.disabled) {
    return false;
}

submitButton.click();
return true;
                """
            )

            if clicked:
                print(f"[*] 已填写邮箱并点击注册: {email}")
                return email, dev_token

        time.sleep(0.5)

    raise Exception("未找到邮箱输入框或注册按钮")



def fill_code_and_submit(email, dev_token, timeout=60):
    # 复用 `email_register.py` 里的验证码轮询逻辑，等待邮件到达后自动填写 OTP。
    # 等信超时由 config.json 的 otp_wait_timeout 控制（控制台设置项），缺省 90 秒。
    code = get_oai_code(dev_token, email, timeout=load_otp_wait_timeout())
    if not code:
        raise Exception("获取验证码失败")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            filled = page.run_js(
                """
const code = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(input, '');
        nativeInputValueSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
}

function dispatchInputEvents(input, value) {
    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const input = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || code.length || 6) > 1;
}) || null;

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }
    const maxLength = Number(node.maxLength || 0);
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || autocomplete === 'one-time-code';
});

if (!input && otpBoxes.length < code.length) {
    return 'not-ready';
}

if (input) {
    input.focus();
    input.click();
    setNativeValue(input, code);
    dispatchInputEvents(input, code);

    const normalizedValue = String(input.value || '').trim();
    const expectedLength = Number(input.maxLength || code.length || 6);
    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;

    if (normalizedValue !== code) {
        return 'aggregate-mismatch';
    }

    if (expectedLength > 0 && normalizedValue.length !== expectedLength) {
        return 'aggregate-length-mismatch';
    }

    if (slots.length && filledSlots && filledSlots !== normalizedValue.length) {
        return 'aggregate-slot-mismatch';
    }

    input.blur();
    return 'filled';
}

const orderedBoxes = otpBoxes.slice(0, code.length);
for (let i = 0; i < orderedBoxes.length; i += 1) {
    const box = orderedBoxes[i];
    const char = code[i] || '';
    box.focus();
    box.click();
    setNativeValue(box, char);
    dispatchInputEvents(box, char);
    box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: char }));
    box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: char }));
    box.blur();
}

const merged = orderedBoxes.map((node) => String(node.value || '').trim()).join('');
return merged === code ? 'filled' : 'box-mismatch';
                """,
                code,
            )
        except PageDisconnectedError:
            # 点击确认邮箱后如果刚好发生跳转，旧页面句柄会断开；此时切到新页继续判断即可。
            refresh_active_page()
            if has_profile_form():
                print("[*] 验证码提交后已跳转到最终注册页。")
                return code
            time.sleep(1)
            continue

        if filled == 'not-ready':
            if has_profile_form():
                print("[*] 已直接进入最终注册页，跳过验证码按钮确认。")
                return code
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 验证码输入框已出现，但写入失败: {filled}")
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(1.2)
            try:
                clicked = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const aggregateInput = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 0) > 1;
}) || null;

let value = '';
if (aggregateInput) {
    value = String(aggregateInput.value || '').trim();
    const expectedLength = Number(aggregateInput.maxLength || value.length || 6);
    if (!value || (expectedLength > 0 && value.length !== expectedLength)) {
        return false;
    }

    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    if (slots.length) {
        const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;
        if (filledSlots && filledSlots !== value.length) {
            return false;
        }
    }
} else {
    const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
        if (!isVisible(node) || node.disabled || node.readOnly) {
            return false;
        }
        const maxLength = Number(node.maxLength || 0);
        const autocomplete = String(node.autocomplete || '').toLowerCase();
        return maxLength === 1 || autocomplete === 'one-time-code';
    });
    value = otpBoxes.map((node) => String(node.value || '').trim()).join('');
    if (!value || value.length < 6) {
        return false;
    }
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const confirmButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '确认邮箱' || text.includes('确认邮箱') || text === '继续' || text.includes('继续') || text === '下一步' || text.includes('下一步') || t.includes('confirm') || t.includes('continue') || t.includes('next') || t.includes('verify');
});

if (!confirmButton) {
    return 'no-button';
}

confirmButton.focus();
confirmButton.click();
return 'clicked';
                    """
                )
            except PageDisconnectedError:
                refresh_active_page()
                if has_profile_form():
                    print("[*] 确认邮箱后页面跳转成功，已进入最终注册页。")
                    return code
                clicked = 'disconnected'

            if clicked == 'clicked':
                print(f"[*] 已填写验证码并点击确认邮箱: {code}")
                time.sleep(2)
                refresh_active_page()
                if has_profile_form():
                    print("[*] 验证码确认完成，最终注册页已就绪。")
                return code

            if clicked == 'no-button':
                current_url = page.url
                if 'sign-up' in current_url or 'signup' in current_url:
                    print(f"[*] 已填写验证码，页面已自动跳转到下一步: {current_url}")
                    return code

            if clicked == 'disconnected':
                time.sleep(1)
                continue

        time.sleep(0.5)

    debug_snapshot = page.run_js(
        r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible).map((node) => ({
    type: node.type || '',
    name: node.name || '',
    testid: node.getAttribute('data-testid') || '',
    autocomplete: node.autocomplete || '',
    maxLength: Number(node.maxLength || 0),
    value: String(node.value || ''),
}));

const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible).map((node) => ({
    text: String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim(),
    disabled: !!node.disabled,
    ariaDisabled: node.getAttribute('aria-disabled') || '',
}));

return { url: location.href, inputs, buttons };
        """
    )
    print(f"[Debug] 验证码页 DOM 摘要: {debug_snapshot}")
    raise Exception("未找到验证码输入框或确认邮箱按钮")


def getTurnstileToken():
    # 复用现有 turnstile 处理逻辑，在最终注册页需要时再触发。
    page.run_js("try { turnstile.reset() } catch(e) { }")

    turnstileResponse = None

    for i in range(0, 15):
        try:
            turnstileResponse = page.run_js("try { return turnstile.getResponse() } catch(e) { return null }")
            if turnstileResponse:
                return turnstileResponse

            challengeSolution = page.ele("@name=cf-turnstile-response")
            challengeWrapper = challengeSolution.parent()
            challengeIframe = challengeWrapper.shadow_root.ele("tag:iframe")

            challengeIframe.run_js("""
window.dtp = 1
function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}

// 旧方案在 4K 屏下不稳定，这里给出更自然的屏幕坐标。
let screenX = getRandomInt(800, 1200);
let screenY = getRandomInt(400, 600);

Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
                        """)

            challengeIframeBody = challengeIframe.ele("tag:body").shadow_root
            challengeButton = challengeIframeBody.ele("tag:input")
            challengeButton.click()
        except:
            pass
        time.sleep(1)
    raise Exception("failed to solve turnstile")


def build_profile():
    # 生成一组可重复使用的注册资料，密码至少包含大小写、数字和特殊字符。
    given_name = fake.first_name()  # 随机生成名
    family_name = fake.last_name()  # 随机生成姓
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout=30):
    # 在验证码通过后，直接锁定“可见且可写”的真实输入框，避免命中隐藏节点或 React 受控副本。
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    turnstile_token = ""

    while time.time() < deadline:
        filled = page.run_js(
            """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) {
        return false;
    }
    input.focus();
    input.click();

    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }

    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }

    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));

    return String(input.value || '') === String(value || '');
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return 'not-ready';
}

const givenOk = setInputValue(givenInput, givenName);
const familyOk = setInputValue(familyInput, familyName);
const passwordOk = setInputValue(passwordInput, password);

if (!givenOk || !familyOk || !passwordOk) {
    return 'filled-failed';
}

return [
    String(givenInput.value || '').trim() === String(givenName || '').trim(),
    String(familyInput.value || '').trim() === String(familyName || '').trim(),
    String(passwordInput.value || '') === String(password || ''),
].every(Boolean) ? 'filled' : 'verify-failed';
            """,
            given_name,
            family_name,
            password,
        )

        if filled == 'not-ready':
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 最终注册页输入框已出现，但姓名/密码写入失败: {filled}")
            time.sleep(0.5)
            continue

        values_ok = page.run_js(
            """
const expectedGiven = arguments[0];
const expectedFamily = arguments[1];
const expectedPassword = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return false;
}

return String(givenInput.value || '').trim() === String(expectedGiven || '').trim()
    && String(familyInput.value || '').trim() === String(expectedFamily || '').trim()
    && String(passwordInput.value || '') === String(expectedPassword || '');
            """,
            given_name,
            family_name,
            password,
        )
        if not values_ok:
            print("[Debug] 最终注册页字段值校验失败，继续重试填写。")
            time.sleep(0.5)
            continue

        turnstile_state = page.run_js(
            """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return 'not-found';
}
const value = String(challengeInput.value || '').trim();
return value ? 'ready' : 'pending';
            """
        )

        if turnstile_state == "pending" and not turnstile_token:
            print("[*] 检测到最终注册页存在 Turnstile，开始使用现有真人化点击逻辑。")
            turnstile_token = getTurnstileToken()
            if turnstile_token:
                synced = page.run_js(
                    """
const token = arguments[0];
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return false;
}
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) {
    nativeSetter.call(challengeInput, token);
} else {
    challengeInput.value = token;
}
challengeInput.dispatchEvent(new Event('input', { bubbles: true }));
challengeInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(challengeInput.value || '').trim() === String(token || '').trim();
                    """,
                    turnstile_token,
                )
                if synced:
                    print("[*] Turnstile 响应已同步到最终注册表单。")

        time.sleep(1.2)

        try:
            submit_button = page.ele('tag:button@@text()=完成注册') or page.ele('tag:button@@text():Create Account') or page.ele('tag:button@@text():Sign up')
        except Exception:
            submit_button = None

        if not submit_button:
            clicked = page.run_js(
                r"""
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (challengeInput && !String(challengeInput.value || '').trim()) {
    return false;
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '完成注册' || text.includes('完成注册') || t.includes('create account') || t.includes('sign up') || t.includes('complete');
});
if (!submitButton || submitButton.disabled || submitButton.getAttribute('aria-disabled') === 'true') {
    return false;
}
submitButton.focus();
submitButton.click();
return true;
                """
            )
        else:
            challenge_value = page.run_js(
                """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
return challengeInput ? String(challengeInput.value || '').trim() : 'not-found';
                """
            )
            if challenge_value not in ('not-found', ''):
                submit_button.click()
                clicked = True
            else:
                clicked = False

        if clicked:
            print(f"[*] 已填写注册资料并点击完成注册: {given_name} {family_name} / {password}")
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
            }

        time.sleep(0.5)

    raise Exception("未找到最终注册表单或完成注册按钮")


def extract_visible_numbers(timeout=60):
    # 登录/注册完成后，提取页面上可见的普通数字文本，不处理任何敏感 Cookie。
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = page.run_js(
            r"""
function isVisible(el) {
    if (!el) {
        return false;
    }
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const selector = [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'div', 'span', 'p', 'strong', 'b', 'small',
    '[data-testid]', '[class]', '[role="heading"]'
].join(',');

const seen = new Set();
const matches = [];
for (const node of document.querySelectorAll(selector)) {
    if (!isVisible(node)) {
        continue;
    }
    const text = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text) {
        continue;
    }
    const found = text.match(/\d+(?:\.\d+)?/g);
    if (!found) {
        continue;
    }
    for (const value of found) {
        const key = `${value}@@${text}`;
        if (seen.has(key)) {
            continue;
        }
        seen.add(key);
        matches.push({ value, text });
    }
}

return matches.slice(0, 30);
            """
        )

        if result:
            print("[*] 页面可见数字文本提取结果:")
            for item in result:
                try:
                    print(f"    - 数字: {item['value']} | 上下文: {item['text']}")
                except Exception:
                    pass
            return result

        time.sleep(1)

    raise Exception("登录后未提取到可见数字文本")


def wait_for_sso_cookie(timeout=30):
    # 必须在注册完成后再取 sso，优先抓取精确的 sso cookie。
    deadline = time.time() + timeout
    last_seen_names = set()

    while time.time() < deadline:
        try:
            refresh_active_page()
            if page is None:
                time.sleep(1)
                continue

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    print("[*] 注册完成后已获取到 sso cookie。")
                    return value

        except PageDisconnectedError:
            refresh_active_page()
        except Exception:
            pass

        time.sleep(1)

    raise Exception(f"注册完成后未获取到 sso cookie，当前已见 cookie: {sorted(last_seen_names)}")


def append_sso_to_txt(sso_value, output_path=DEFAULT_SSO_FILE):
    # 按用户要求，一行写一个 sso 值，持续追加。
    normalized = str(sso_value or "").strip()
    if not normalized:
        raise Exception("待写入的 sso 为空")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "a", encoding="utf-8") as file:
        file.write(normalized + "\n")

    print(f"[*] 已追加写入 sso 到文件: {output_path}")


def _load_config_request_proxy() -> str:
    # CPA token 换取与浏览器走同一出口，优先 proxy，其次 browser_proxy。
    try:
        import json as _json
        cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        return str(cfg.get("proxy") or cfg.get("browser_proxy") or "")
    except Exception:
        return ""


def _cpa_export_enabled() -> bool:
    # CPA 导出开关：优先读 config.json 的 cpa.enabled（控制台设置项），
    # 未配置该段时回退到环境变量 GROK_REGISTER_CPA_EXPORT_ENABLED（默认 true）。
    try:
        import json as _json
        cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        cpa_conf = cfg.get("cpa")
        if isinstance(cpa_conf, dict) and "enabled" in cpa_conf:
            return bool(cpa_conf.get("enabled"))
    except Exception:
        pass
    return os.getenv("GROK_REGISTER_CPA_EXPORT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _grok2api_build_push_enabled() -> bool:
    # grok2api 的 Grok Build 池是否需要推送：需要总开关 enabled 与 push_build 同时开启。
    # 与 grok2api_push.push_records 的判定保持一致——Build 池依赖 grok-cli 权限校验通过的
    # OAuth token，即使 CPA 导出关闭，只要 Build 开着就仍需换取 token 并检测权限。
    def _truthy(v) -> bool:
        if isinstance(v, bool):
            return v
        return str(v or "").strip().lower() in {"1", "true", "yes", "on"}

    try:
        import json as _json
        cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        g2a = cfg.get("grok2api") or {}
        return _truthy(g2a.get("enabled")) and _truthy(g2a.get("push_build"))
    except Exception:
        return False


def export_cpa_auth(sso_value, email, output_path):
    # 注册成功后，把 sso 换成 OAuth token，并按需导出 CPA 的 xai auth json。
    # 任何失败都不抛出，避免影响注册主流程与后续轮次。
    #
    # 换取到的 OAuth token dict 会被返回，供调用方复用推送到 grok2api 的 Grok Build 池，
    # 避免二次换取；无权限 / 失败时返回 None。
    #
    # 两个消费方共享同一次 token 换取 + grok-cli 权限检测：
    #   - CPA 导出（cpa.enabled）：写出 xai-*.json；
    #   - grok2api Grok Build 推送（grok2api.enabled && push_build）：复用返回的 token。
    # 只要其中任一开启，就执行换取与权限检测；两者都关时才整体跳过（不测 grok-cli 权限）。
    #
    # 开关优先级：config.json 的 cpa.enabled（控制台设置项）> 环境变量
    # GROK_REGISTER_CPA_EXPORT_ENABLED（默认 true）。
    want_cpa = _cpa_export_enabled()
    want_build = _grok2api_build_push_enabled()
    if not want_cpa and not want_build:
        return None

    try:
        import sso_to_cpa
    except Exception as e:
        print(f"  ❌ token 换取跳过：无法加载 sso_to_cpa: {e}")
        return None

    try:
        sso_to_cpa.configure(proxy=_load_config_request_proxy(), verify_tls=True)
        sso = sso_to_cpa.normalize_sso_cookie(sso_value)
        token = sso_to_cpa.sso_to_token(sso)
        if not token:
            print("  ❌ token 换取失败：未能换取 access_token")
            return None
        access_token = token.get("access_token") or token.get("key") or ""
        allowed, reason = sso_to_cpa.verify_cpa_access(access_token)
        if not allowed:
            print(f"  ⚠️ 账号无 grok-cli 访问权限，跳过 CPA 导出与 Grok Build 推送（{reason}）；sso 仍会推送到 grok2api Web/Console。")
            return None
        print(f"  ✅ grok-cli 访问权限校验通过（{reason}）")

        # CPA 导出关闭时只做换取 + 权限检测，token 仍返回供 Grok Build 复用，不写 json 文件。
        if want_cpa:
            uid, entry = sso_to_cpa.token_to_auth_entry(token, sso_cookie=sso, email=email)
            file_id = (
                sso_to_cpa.safe_filename_part(email)
                or sso_to_cpa.safe_filename_part(uid)
                or secrets.token_hex(4)
            )
            target_dirs = [os.path.join(os.path.dirname(output_path), "cpa_auths")]
            env_dir = os.getenv("GROK_REGISTER_CPA_AUTH_DIR", "").strip()
            if env_dir:
                target_dirs.append(env_dir)
            for target in target_dirs:
                try:
                    path = os.path.join(target, f"xai-{file_id}.json")
                    sso_to_cpa.write_auth_json(__import__("pathlib").Path(path), entry)
                    print(f"  💾 CPA auth 已导出: {path}")
                except Exception as e:
                    print(f"  ❌ CPA auth 写入失败 ({target}): {e}")
        return token
    except Exception as e:
        print(f"  ❌ token 换取异常: {e}")
        return None


def push_sso_to_api(new_tokens: list):
    # 推送 SSO token 到 grok2api 管理接口。
    # append=false：直接将本次 token 列表全量推送（覆盖）。
    # append=true（默认）：先 GET 查询线上现有 token，合并本次后全量推送。
    import json
    import urllib3
    import requests
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
    except Exception as e:
        print(f"[Warn] 读取 config.json 失败，跳过推送: {e}")
        return

    api_conf = conf.get("api", {})
    endpoint = str(api_conf.get("endpoint", "")).strip()
    api_token = str(api_conf.get("token", "")).strip()
    append_mode = api_conf.get("append", True)
    use_antiban_admin_api = endpoint.rstrip("/").endswith("/admin/api/tokens")

    if not endpoint or not api_token:
        return

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    tokens_to_push = [t for t in new_tokens if t]
    pool_name = "basic" if use_antiban_admin_api else "ssoBasic"

    if append_mode:
        try:
            get_resp = requests.get(endpoint, headers=headers, timeout=15, verify=False)
            if get_resp.status_code == 200:
                data = get_resp.json()
                # 兼容两种响应格式：
                # grok2 防封版: {"tokens": [{"token": "...", "pool": "basic"}]}
                # 旧版: {"tokens": {"ssoBasic": [...]}} 或 {"ssoBasic": [...]}
                if isinstance(data, dict) and isinstance(data.get("tokens"), list):
                    existing = [
                        item for item in data["tokens"]
                        if isinstance(item, dict) and str(item.get("pool", "basic") or "basic") == "basic"
                    ]
                elif isinstance(data, dict) and isinstance(data.get("tokens"), dict):
                    existing = data["tokens"].get("ssoBasic", [])
                else:
                    existing = data.get("ssoBasic", []) if isinstance(data, dict) else []
                existing_tokens = [
                    item["token"] if isinstance(item, dict) else str(item)
                    for item in existing if item
                ]
                seen = set()
                deduped = []
                for t in existing_tokens + tokens_to_push:
                    if t not in seen:
                        seen.add(t)
                        deduped.append(t)
                tokens_to_push = deduped
                print(f"[*] 查询到线上 {len(existing_tokens)} 个 token，合并本次 {len(new_tokens)} 个，共 {len(deduped)} 个")
            else:
                print(f"[Error] 查询线上 token 失败: HTTP {get_resp.status_code}，放弃推送以保护存量数据")
                return
        except Exception as e:
            print(f"[Error] 查询线上 token 异常: {e}，放弃推送以保护存量数据")
            return

    try:
        payload = {pool_name: tokens_to_push}
        resp = requests.post(
            endpoint,
            json=payload,
            headers=headers,
            timeout=60,
            verify=False,
        )
        if resp.status_code == 200:
            print(f"[*] SSO token 已推送到 API（共 {len(tokens_to_push)} 个）: {endpoint}")
        else:
            print(f"[Warn] 推送 API 返回异常: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[Warn] 推送 API 失败: {e}")


def push_to_grok2api(records: list):
    # 注册成功后，把账号推送到 grok2api（chenyme/grok2api，Go 版）的三个池：
    #   - Grok Build：OAuth 凭据（复用 CPA 换取到的 token，需要 grok-cli:access 权限）
    #   - Grok Web：SSO token
    #   - Grok Console：SSO token
    # 通过 config.json 的 grok2api 段配置（base_url + 三个池开关 + 管理员账号密码）。
    # 每个池独立开关，与旧版 /admin/api/tokens 推送互不影响。任何失败都吞掉，不影响主流程。
    import json

    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
    except Exception as e:
        print(f"[Warn] 读取 config.json 失败，跳过 grok2api 推送: {e}")
        return

    g2a_conf = conf.get("grok2api") or {}
    if not g2a_conf.get("enabled"):
        return

    try:
        import grok2api_push
    except Exception as e:
        print(f"[Warn] grok2api 推送跳过：无法加载 grok2api_push: {e}")
        return

    try:
        grok2api_push.push_records(
            records,
            config=g2a_conf,
            proxy=_load_config_request_proxy(),
        )
    except Exception as e:
        print(f"[Warn] grok2api 推送失败: {e}")


def run_single_registration(output_path=DEFAULT_SSO_FILE, extract_numbers=False):
    # 单轮流程：打开注册页 -> 完成注册 -> 获取 sso -> 写 txt。
    open_signup_page()
    email, dev_token = fill_email_and_submit()
    fill_code_and_submit(email, dev_token)
    profile = fill_profile_and_submit()
    sso_value = wait_for_sso_cookie()
    append_sso_to_txt(sso_value, output_path)
    cpa_token = export_cpa_auth(sso_value, email, output_path)

    if extract_numbers:
        extract_visible_numbers()

    result = {
        "email": email,
        "sso": sso_value,
        "cpa_token": cpa_token,
        **profile,
    }

    if run_logger:
        run_logger.info(
            "注册成功 | email=%s | password=%s | given=%s | family=%s",
            email,
            profile.get("password", ""),
            profile.get("given_name", ""),
            profile.get("family_name", ""),
        )

    print(f"[*] 本轮注册完成，邮箱: {email}")
    return result


def load_run_count() -> int:
    # 从 config.json 读取默认执行轮数，配置不存在时返回 10。
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
        v = conf.get("run", {}).get("count")
        if isinstance(v, int) and v >= 0:
            return v
    except Exception:
        pass
    return 10


def load_otp_wait_timeout() -> int:
    # 从 config.json 读取验证码等信超时（秒），限制在 30-600，配置缺失时返回 90。
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
        v = conf.get("otp_wait_timeout")
        if isinstance(v, int):
            return max(30, min(600, v))
    except Exception:
        pass
    return 90


def resolve_attempt_cap(target_count: int) -> int:
    # --count 现在表示“需要成功的数量”，注册失败会自动补一轮。为避免代理失效 / IP 被封时
    # 无限重试，这里给出一个总尝试次数上限：target * factor（向上取整），再加一个固定缓冲。
    # target_count <= 0（无限模式）时返回 0，表示不限制尝试次数。
    if target_count <= 0:
        return 0

    def _env_int(name: str, default: int, minimum: int) -> int:
        raw = os.environ.get(name, "").strip()
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    factor = _env_int("GROK_REGISTER_ATTEMPT_FACTOR", 3, 1)
    buffer = _env_int("GROK_REGISTER_ATTEMPT_BUFFER", 5, 0)
    return target_count * factor + buffer


def main():
    # 默认循环执行；每轮完成后关闭当前页，再自动进入下一轮。
    global run_logger
    run_logger = setup_run_logger()

    config_count = load_run_count()

    parser = argparse.ArgumentParser(description="xAI 自动注册并采集 sso")
    parser.add_argument(
        "--count",
        type=int,
        default=config_count,
        help=(
            "需要成功注册的账号数量，0 表示无限循环。注册失败会自动补一轮，"
            f"直到成功数量达到目标（默认读取 config.json run.count，当前 {config_count}）"
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=0,
        help="总尝试轮数上限，0 表示自动按 --count 推算（GROK_REGISTER_ATTEMPT_FACTOR / _BUFFER）",
    )
    parser.add_argument("--output", default=DEFAULT_SSO_FILE, help="sso 输出 txt 路径")
    parser.add_argument("--extract-numbers", action="store_true", help="注册完成后额外提取页面数字文本")
    args = parser.parse_args()

    attempt_cap = args.max_attempts if args.max_attempts > 0 else resolve_attempt_cap(args.count)
    if args.count > 0:
        cap_hint = f"，最多尝试 {attempt_cap} 轮" if attempt_cap > 0 else ""
        print(f"[*] 目标成功数量: {args.count}{cap_hint}（失败自动补轮）")

    attempt = 0
    success_count = 0
    collected_sso: list = []
    collected_records: list = []
    try:
        start_browser()
        while True:
            if args.count > 0 and success_count >= args.count:
                break
            if attempt_cap > 0 and attempt >= attempt_cap:
                print(
                    f"[Warn] 已达到最大尝试轮数 {attempt_cap}，"
                    f"成功 {success_count}/{args.count}，停止补轮。"
                )
                break

            attempt += 1
            if args.count > 0:
                print(f"\n[*] 开始第 {attempt} 轮注册（已成功 {success_count}/{args.count}）")
            else:
                print(f"\n[*] 开始第 {attempt} 轮注册（已成功 {success_count}）")

            try:
                result = run_single_registration(args.output, extract_numbers=args.extract_numbers)
                collected_sso.append(result["sso"])
                collected_records.append(result)
                success_count += 1
            except KeyboardInterrupt:
                print("\n[Info] 收到中断信号，停止后续轮次。")
                break
            except Exception as error:
                print(f"[Error] 第 {attempt} 轮失败: {error}")
            finally:
                restart_browser()

            still_pending = args.count == 0 or success_count < args.count
            has_attempts_left = attempt_cap == 0 or attempt < attempt_cap
            if still_pending and has_attempts_left:
                time.sleep(2)

    finally:
        if collected_sso:
            print(f"\n[*] 注册完成，推送 {len(collected_sso)} 个 token 到 API...")
            push_sso_to_api(collected_sso)
        if collected_records:
            push_to_grok2api(collected_records)

        stop_browser()
        stop_virtual_display()


if __name__ == "__main__":
    main()

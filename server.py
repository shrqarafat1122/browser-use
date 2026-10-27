import os
import hmac
import logging
import re
import glob
import shutil
import asyncio
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from fastapi import FastAPI, Header, HTTPException
from browser_use import Agent, BrowserProfile, BrowserSession, ChatOpenAI, Controller, ActionResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("browser_use_api")

app = FastAPI(
    title="Browser Use Universal API & Stream Sniffer (Local Stealth Edition)",
    description="100% Free local headless browser runner with Cloudflare Turnstile stealth and ad blocking",
    version="4.1.0"
)

controller = Controller()

# Anti-detection + Anti-ad + Replit Linux container flags
STEALTH_ARGS = [
    "--headless=new",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-blink-features=AutomationControlled",
    "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "--disable-popup-blocking=false",
    "--blink-settings=blockedPopup=true",
    "--disable-notifications",
    "--disable-extensions",
    "--host-rules=MAP breachbillboardpredestination.com 127.0.0.1, MAP *.predestination.com 127.0.0.1, MAP *.highperformancegate.com 127.0.0.1, MAP *.histats.com 127.0.0.1, MAP *.onclickmax.com 127.0.0.1"
]

JS_STEALTH_PATCH = """() => {
    try {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
    } catch (_) {}
}"""

JS_POLL_SNIFFER = """() => {
    try {
        const iframes = [...document.querySelectorAll('iframe')];
        const uembed = iframes.find(f => f.src && f.src.includes('uembed.php'));
        
        let iframeDoc = null;
        if (uembed && uembed.contentDocument) {
            iframeDoc = uembed.contentDocument;
        }

        const streams = [];

        // If uembed iframe is found, trigger play and inspect its performance resources
        if (iframeDoc) {
            const v = iframeDoc.querySelector('video');
            const playBtn = iframeDoc.querySelector('[aria-label=playstop], .unmute, button, video');
            if (playBtn) {
                try { playBtn.click(); } catch(_) {}
            }

            // Extract performance resources (Bhalocast M3U8)
            if (iframeDoc.defaultView && iframeDoc.defaultView.performance && iframeDoc.defaultView.performance.getEntriesByType) {
                iframeDoc.defaultView.performance.getEntriesByType('resource').forEach(item => {
                    if (item.name && (/\\.(m3u8|mpd|ts|m4s)(\\?|$)/i.test(item.name) || /bhalocast|hls|live/i.test(item.name))) {
                        streams.push(item.name);
                    }
                });
            }

            if (v && v.src && !v.src.startsWith('blob:')) streams.push(v.src);
            if (v && v.currentSrc && !v.currentSrc.startsWith('blob:')) streams.push(v.currentSrc);
        }

        // Top-level performance resources
        if (window.performance && performance.getEntriesByType) {
            performance.getEntriesByType('resource').forEach(item => {
                if (item.name && (/\\.(m3u8|mpd|ts|m4s)(\\?|$)/i.test(item.name) || /bhalocast|hls|live/i.test(item.name))) {
                    streams.push(item.name);
                }
            });
        }

        const discoveredIframes = iframes.map(f => f.src).filter(Boolean);

        return {
            has_uembed: !!iframeDoc,
            streams: Array.from(new Set(streams)),
            iframes: discoveredIframes
        };
    } catch (e) {
        return { error: String(e), streams: [], iframes: [] };
    }
}"""


def get_local_chromium_path() -> Optional[str]:
    """Auto-detects local Chromium binary in Replit."""
    custom = os.getenv("BROWSER_USE_EXECUTABLE_PATH")
    if custom and os.path.exists(custom):
        return custom

    for cmd in ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"]:
        p = shutil.which(cmd)
        if p:
            return p

    pw_cache = os.path.expanduser("~/.cache/ms-playwright")
    matches = glob.glob(f"{pw_cache}/chromium-*/chrome-linux/chrome")
    if matches:
        return matches[-1]

    for p in ["/repl/tools/bin/chromium", "/usr/bin/chromium", "/usr/bin/chromium-browser"]:
        if os.path.exists(p):
            return p

    return None


def create_local_profile() -> BrowserProfile:
    """Builds a local stealth profile."""
    chrome_path = get_local_chromium_path()
    profile_kwargs: Dict[str, Any] = {
        "use_cloud": False,
        "headless": True,
        "args": STEALTH_ARGS,
        "ignore_https_errors": True
    }
    if chrome_path:
        logger.info("Using local Chromium: %s", chrome_path)
        profile_kwargs["executable_path"] = chrome_path

    return BrowserProfile(**profile_kwargs)


@controller.action("Extract live M3U8 streaming manifest URLs from open page, player, and browser network entries")
async def extract_network_streams(browser_session: BrowserSession) -> ActionResult:
    try:
        page = await browser_session.get_current_page()
        if page:
            data = await page.evaluate(JS_POLL_SNIFFER)
            if isinstance(data, dict):
                streams = data.get("streams", [])
                m3u8s = [s for s in streams if ".m3u8" in s.lower()]
                if m3u8s:
                    return ActionResult(extracted_content=f"Found M3U8 manifest: {m3u8s[0]}")
                elif streams:
                    return ActionResult(extracted_content=f"Found streams: {streams[:3]}")
        return ActionResult(extracted_content="Waiting for stream to buffer, please retry in 3 seconds.")
    except Exception as e:
        return ActionResult(error=f"Error inspecting streams: {str(e)}")


class TaskRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=4000)
    max_steps: Optional[int] = Field(default=25, ge=1, le=100)
    use_vision: Optional[bool] = Field(default=True)


class SniffRequest(BaseModel):
    url: str = Field(..., min_length=5, description="Target webpage or stream player URL")
    timeout: Optional[int] = Field(default=35, ge=10, le=90)


@app.get("/")
def home():
    local_chrome = get_local_chromium_path()
    return {
        "status": "running",
        "service": "Browser Use Universal API",
        "version": "4.1.0",
        "mode": "100% Free Local Chromium (No Cloud Credits)",
        "detected_chromium": local_chrome,
        "stealth": "Enabled (AutomationControlled bypass + Real User-Agent)",
        "endpoints": {
            "agent_runner": "POST /run",
            "direct_stream_sniffer": "POST /sniff"
        }
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ==============================================================================
# 🎯 DEDICATED STREAM SNIFFER (/sniff)
# ==============================================================================
@app.post("/sniff")
async def sniff_stream(
    body: SniffRequest,
    x_api_key: str = Header(default="")
):
    secret = os.getenv("SERVICE_API_KEY", "")
    if not secret or not hmac.compare_digest(x_api_key, secret):
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid API key")

    target_url = body.url.strip()
    profile = create_local_profile()
    browser = BrowserSession(browser_profile=profile)
    captured_streams: List[str] = []
    page_title = ""

    try:
        # 1. Start browser
        logger.info("Starting local stealth browser...")
        await browser.start()

        # 2. Navigate
        logger.info("Navigating to: %s", target_url)
        await browser.navigate_to(target_url)

        # Apply stealth patch
        p = await browser.get_current_page()
        if p:
            try:
                await p.evaluate(JS_STEALTH_PATCH)
            except Exception:
                pass

        # 3. Handle Cloudflare "Just a moment..." challenge if present
        for cf_wait in range(6):
            await asyncio.sleep(2)
            try:
                title = await browser.get_current_page_title()
                if "just a moment" in title.lower():
                    logger.info("Waiting for Cloudflare verification... (%d/6)", cf_wait + 1)
                else:
                    page_title = title
                    break
            except Exception:
                pass

        # 4. If target is webcric.im, hop into the embedded player iframe (playsza.st)
        p = await browser.get_current_page()
        if p and "webcric" in target_url:
            data = await p.evaluate(JS_POLL_SNIFFER)
            if isinstance(data, dict):
                discovered_iframes = data.get("iframes", [])
                player_iframe = next((f for f in discovered_iframes if "playsza.st" in f or "player.php" in f), None)
                if player_iframe:
                    logger.info("Found playsza player iframe inside webcric: %s", player_iframe)
                    await browser.navigate_to(player_iframe)
                    await asyncio.sleep(3)
                    p = await browser.get_current_page()
                    if p:
                        try:
                            await p.evaluate(JS_STEALTH_PATCH)
                        except Exception:
                            pass

        # 5. Polling loop: Wait for plays.js countdown and uembed.php stream resources
        logger.info("Starting active polling for stream resources...")
        max_attempts = int(body.timeout // 2)
        for attempt in range(max_attempts):
            await asyncio.sleep(2)
            try:
                cur_p = await browser.get_current_page()
                if not cur_p:
                    continue

                eval_res = await cur_p.evaluate(JS_POLL_SNIFFER)
                if isinstance(eval_res, dict):
                    streams = eval_res.get("streams", [])
                    for s in streams:
                        if s not in captured_streams:
                            captured_streams.append(s)

                    m3u8s = [s for s in captured_streams if ".m3u8" in s.lower()]
                    if m3u8s:
                        logger.info("Stream manifest captured at attempt %d: %s", attempt, m3u8s[0])
                        break
            except Exception as e:
                logger.debug("Poll attempt %d note: %s", attempt, e)

    except Exception as exc:
        logger.exception("Sniffer execution error: %s", exc)
        return {
            "status": "error",
            "error_detail": str(exc)
        }
    finally:
        try:
            await browser.close()
        except Exception:
            pass

    clean_streams = list(dict.fromkeys(captured_streams))
    m3u8_list = [u for u in clean_streams if ".m3u8" in u.lower()]
    ts_list = [u for u in clean_streams if ".ts" in u.lower()]

    return {
        "status": "success" if m3u8_list else ("partial" if clean_streams else "not_found"),
        "target_url": target_url,
        "page_title": page_title,
        "manifest_found": len(m3u8_list) > 0,
        "m3u8_url": m3u8_list[0] if m3u8_list else None,
        "all_manifests": m3u8_list,
        "segments": ts_list[:5],
        "total_streams_found": len(clean_streams)
    }


# ==============================================================================
# 🤖 BROWSER USE AGENT RUNNER (/run)
# ==============================================================================
@app.post("/run")
async def run_task(
    body: TaskRequest,
    x_api_key: str = Header(default="")
):
    secret = os.getenv("SERVICE_API_KEY", "")
    if not secret or not hmac.compare_digest(x_api_key, secret):
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid API key")

    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        raise HTTPException(
            status_code=500,
            detail="OPENAI_API_KEY environment variable is missing"
        )

    profile = create_local_profile()

    llm = ChatOpenAI(
        model=os.getenv("OMNIROUTE_MODEL", "agy/claude-opus-4-6"),
        base_url=os.getenv("OMNIROUTE_BASE_URL", "https://shariqbabu-omniroute.hf.space/v1"),
        api_key=openai_key
    )

    captured_streams: List[str] = []
    stream_regex = re.compile(r"https?://[^\s\"']+\.(m3u8|mpd)(\?[^\s\"']*)?", re.IGNORECASE)

    try:
        browser = BrowserSession(browser_profile=profile)

        agent = Agent(
            task=body.task,
            llm=llm,
            browser=browser,
            controller=controller,
            use_vision=body.use_vision,
        )

        logger.info("Starting browser agent task: %s", body.task[:100])
        history = await agent.run(max_steps=body.max_steps)

        is_done = history.is_done()
        result_text = history.final_result() or "Task completed"

        # Final sniffer pass before closing
        try:
            p = await browser.get_current_page()
            if p:
                eval_res = await p.evaluate(JS_POLL_SNIFFER)
                if isinstance(eval_res, dict):
                    for s in eval_res.get("streams", []):
                        captured_streams.append(s)
        except Exception as e:
            logger.warning("Post-run browser sniffer note: %s", e)

        for match in stream_regex.finditer(str(result_text)):
            captured_streams.append(match.group(0))

        clean_streams = sorted(list(dict.fromkeys(captured_streams)))
        m3u8_found = [u for u in clean_streams if ".m3u8" in u.lower()]

        if m3u8_found and not any(m in result_text for m in m3u8_found):
            result_text += f"\n\n[Network Diagnostics]: Found {len(m3u8_found)} M3U8 stream manifest(s) in browser network logs:\n" + "\n".join(m3u8_found)

        actions = []
        try:
            actions = history.action_names()
        except Exception:
            pass

        return {
            "status": "success",
            "is_done": is_done,
            "result": result_text,
            "actions": actions,
            "captured_streams": clean_streams,
        }

    except Exception as exc:
        logger.exception("Agent execution error: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Agent execution failed: {str(exc)}"
        ) from exc


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)

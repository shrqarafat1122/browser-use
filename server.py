import os
import hmac
import logging
import re
import asyncio
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from fastapi import FastAPI, Header, HTTPException
from browser_use import Agent, BrowserProfile, BrowserSession, ChatOpenAI, Controller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("browser_use_api")

app = FastAPI(
    title="Browser Use Universal API & Stream Sniffer",
    description="Automated AI browser agent with dedicated cross-frame M3U8 stream sniffer",
    version="3.2.0"
)

controller = Controller()

JS_EXTRACTOR = """() => {
    const list = [];
    try {
        // 1. Video, Source, and Iframe tags
        document.querySelectorAll('video, source, iframe').forEach(el => {
            if (el.src) list.push(el.src);
        });

        // 2. Browser Network Performance Entries (XHR/Fetch/Media chunks)
        if (window.performance && performance.getEntriesByType) {
            performance.getEntriesByType('resource').forEach(item => {
                if (item.name && (/\\.(m3u8|mpd|ts)(\\?|$)/i.test(item.name) || /bhalocast|hls|stream|live/i.test(item.name))) {
                    list.push(item.name);
                }
            });
        }

        // 3. Scan DOM text for manifest URLs
        const html = document.documentElement.innerHTML;
        const matches = html.match(/https?:\\\/\\\/[^"'\\s<>]+\\.(m3u8|mpd)[^"'\\s<>]*/gi) || [];
        matches.forEach(m => list.push(m.replace(/\\\\\\//g, '/')));
    } catch (_) {}
    return Array.from(new Set(list));
}"""


@controller.action("Extract live M3U8, MPD streaming manifests from all open browser tabs and DOM")
async def extract_network_streams(browser: BrowserSession) -> str:
    """Checks all open browser tabs and network logs for stream URLs."""
    found = []
    try:
        pages = await browser.get_pages()
        for p in pages:
            try:
                res = await p.evaluate(JS_EXTRACTOR)
                if isinstance(res, list):
                    found.extend(res)
                elif isinstance(res, str) and res.startswith("["):
                    import json
                    found.extend(json.loads(res))
            except Exception:
                pass
        return f"Streaming URLs found: {list(set(found))}"
    except Exception as e:
        return f"Stream inspection note: {str(e)}"


class TaskRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=4000)
    max_steps: Optional[int] = Field(default=25, ge=1, le=100, description="Max agent reasoning steps")
    use_vision: Optional[bool] = Field(default=True, description="Enable visual analysis")


class SniffRequest(BaseModel):
    url: str = Field(..., description="Target webpage URL hosting the embedded player")
    timeout: Optional[int] = Field(default=20, ge=5, le=60, description="Seconds to wait for stream requests")
    referer: Optional[str] = Field(default="", description="Optional custom referer header")


@app.get("/")
def home():
    return {
        "status": "running",
        "service": "Browser Use Universal API",
        "version": "3.2.0",
        "endpoints": {
            "agent_runner": "POST /run",
            "direct_stream_sniffer": "POST /sniff"
        },
        "features": ["browser_session_started", "auto_iframe_hop", "cross_frame_m3u8", "omniroute_llm"]
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ==============================================================================
# 🎯 DEDICATED STREAM SNIFFER (/sniff) - Direct Cloud Browser & Auto Iframe Hop
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

    anti_ad_args = [
        "--block-new-web-contents",
        "--disable-popup-blocking=false",
        "--blink-settings=blockedPopup=true",
        "--disable-notifications",
        "--disable-extensions",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage"
    ]

    profile = BrowserProfile(
        use_cloud=(os.getenv("USE_CLOUD_BROWSER", "true").lower() == "true"),
        args=anti_ad_args,
        ignore_https_errors=True
    )

    browser = BrowserSession(browser_profile=profile)
    captured_streams: List[str] = []
    page_title = ""

    try:
        # 1. Start browser session properly
        logger.info("Sniffer starting browser session...")
        await browser.start()

        # 2. Navigate to target webpage
        logger.info("Sniffer navigating to: %s", target_url)
        await browser.navigate_to(target_url)
        await asyncio.sleep(5)

        try:
            page_title = await browser.get_current_page_title()
        except Exception:
            pass

        # 3. Click on video or container to trigger playback
        try:
            cur_page = await browser.get_current_page()
            if cur_page:
                await cur_page.evaluate("""() => {
                    const targets = document.querySelectorAll('video, .player, #player, iframe, button, div[class*="play"]');
                    targets.forEach(el => { try { el.click(); } catch(_) {} });
                }""")
                await asyncio.sleep(4)
        except Exception:
            pass

        # 4. Scan parent page and detect iframes
        pages = await browser.get_pages()
        found_iframes = []
        for p in pages:
            try:
                raw_res = await p.evaluate(JS_EXTRACTOR)
                urls = raw_res if isinstance(raw_res, list) else []
                for u in urls:
                    if re.search(r"\.(m3u8|mpd)(\?|$)", u, re.I):
                        captured_streams.append(u)
                    elif re.search(r"(\.ts|\.m4s|bhalocast)(\?|$)", u, re.I):
                        captured_streams.append(u)
                    elif "player.php" in u or "embed" in u:
                        found_iframes.append(u)
            except Exception:
                pass

        # 5. If no M3U8 found on parent, jump into the embedded iframe!
        if not captured_streams and found_iframes:
            iframe_target = found_iframes[0]
            logger.info("Parent scanned, jumping to embedded player iframe: %s", iframe_target)
            await browser.navigate_to(iframe_target)
            await asyncio.sleep(5)

            # Click play inside iframe
            try:
                iframe_page = await browser.get_current_page()
                if iframe_page:
                    await iframe_page.evaluate("""() => {
                        const v = document.querySelector('video') || document.querySelector('.player') || document.body;
                        if (v) v.click();
                    }""")
                    await asyncio.sleep(4)
            except Exception:
                pass

            # Re-scan inside iframe
            pages = await browser.get_pages()
            for p in pages:
                try:
                    raw_res = await p.evaluate(JS_EXTRACTOR)
                    urls = raw_res if isinstance(raw_res, list) else []
                    for u in urls:
                        if re.search(r"\.(m3u8|mpd)(\?|$)", u, re.I):
                            captured_streams.append(u)
                        elif re.search(r"(\.ts|\.m4s|bhalocast)(\?|$)", u, re.I):
                            captured_streams.append(u)
                except Exception:
                    pass

    except Exception as exc:
        logger.exception("Sniffer execution error: %s", exc)
    finally:
        try:
            await browser.close()
        except Exception:
            pass

    clean_streams = list(dict.fromkeys(captured_streams))
    m3u8_list = [u for u in clean_streams if ".m3u8" in u.lower()]
    ts_list = [u for u in clean_streams if ".ts" in u.lower()]

    return {
        "status": "success" if clean_streams else "partial",
        "target_url": target_url,
        "page_title": page_title,
        "manifest_found": len(m3u8_list) > 0,
        "m3u8_url": m3u8_list[0] if m3u8_list else None,
        "all_manifests": m3u8_list,
        "segments": ts_list[:5],
        "total_streams_found": len(clean_streams)
    }


# ==============================================================================
# 🤖 BROWSER USE AGENT RUNNER (/run) - Interactive LLM Tasks
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

    anti_ad_args = [
        "--disable-popup-blocking=false",
        "--blink-settings=blockedPopup=true",
        "--disable-notifications",
        "--disable-extensions",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage"
    ]

    profile = BrowserProfile(
        use_cloud=(os.getenv("USE_CLOUD_BROWSER", "true").lower() == "true"),
        args=anti_ad_args,
        ignore_https_errors=True
    )

    llm = ChatOpenAI(
        model=os.getenv("OMNIROUTE_MODEL", "agy/claude-opus-4-6"),
        base_url=os.getenv("OMNIROUTE_BASE_URL", "https://shariqbabu-omniroute.hf.space/v1"),
        api_key=openai_key
    )

    captured_streams: List[str] = []
    stream_regex = re.compile(r"https?://[^\s\"']+\.(m3u8|mpd)(\?[^\s\"']*)?", re.IGNORECASE)

    task_text = body.task
    if any(k in task_text.lower() for k in ["m3u8", "manifest", "stream", "hls"]):
        task_text += (
            "\n\n[SYSTEM DIRECTIVE]:"
            "\n1. Click the play button or video player container to start streaming."
            "\n2. Wait 8-10 seconds for video playback."
            "\n3. Use 'extract_network_streams' to scan all tabs and network resources."
        )

    try:
        browser = BrowserSession(browser_profile=profile)

        agent = Agent(
            task=task_text,
            llm=llm,
            browser=browser,
            controller=controller,
            use_vision=body.use_vision,
        )

        logger.info("Starting browser agent task: %s", body.task[:100])
        history = await agent.run(max_steps=body.max_steps)

        is_done = history.is_done()
        result_text = history.final_result() or "Task completed"

        # Scan all open pages before closing
        try:
            pages = await browser.get_pages()
            for p in pages:
                try:
                    extracted_raw = await p.evaluate(JS_EXTRACTOR)
                    for item in re.findall(r"https?://[^\s\"'\[\],]+\.(m3u8|mpd)(\?[^\s\"'\[\],]*)?", str(extracted_raw), re.I):
                        full_url = item[0] if isinstance(item, tuple) else item
                        captured_streams.append(full_url)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Post-run browser network scan note: %s", e)

        for match in stream_regex.finditer(str(result_text)):
            captured_streams.append(match.group(0))

        actions = []
        try:
            actions = history.action_names()
        except Exception:
            pass

        clean_streams = sorted(list(set(captured_streams)))

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

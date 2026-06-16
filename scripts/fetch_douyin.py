"""
抖音视频无水印下载脚本（Playwright 驱动）

Author: 阿宾 | GitHub: linlin131236/Nexus-abin
依赖：pip install playwright aiohttp && python -m playwright install chromium

用法：
  python fetch_douyin.py "https://www.douyin.com/video/7599980362898427178"
  python fetch_douyin.py 7599980362898427178

⚠️ 安全边界：
- 只下载用户明确指定的视频，不自动爬取
- 输出目录限制在传参范围内，不写系统目录
- 下载失败不重试，不卡死主流程
- 不用于批量无授权采集
"""

import asyncio
import os
import re
import logging
import time
import json
import argparse
from urllib.parse import unquote
from playwright.async_api import async_playwright
import aiohttp

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CHALLENGE_MAX_WAIT_SECONDS = 45
DETAIL_WAIT_MS = 8000


def _looks_like_waf_challenge(html):
    if not html:
        return True
    text = html.lower()
    markers = ["please wait", "waf-jschallenge", "_wafchallengeid", "argus-csp-token"]
    return any(m in text for m in markers)


def _first_http_url(urls):
    if not isinstance(urls, list):
        return None
    for url in urls:
        if isinstance(url, str) and url.startswith("http"):
            return url
    return None


def _extract_src_from_aweme_detail(detail_payload):
    if not isinstance(detail_payload, dict):
        return None
    aweme = detail_payload.get("aweme_detail")
    if not isinstance(aweme, dict):
        return None
    video = aweme.get("video")
    if not isinstance(video, dict):
        return None
    bit_rates = video.get("bit_rate")
    if isinstance(bit_rates, list):
        sortable = []
        for item in bit_rates:
            if not isinstance(item, dict):
                continue
            score = item.get("bit_rate", 0)
            play_addr = item.get("play_addr")
            urls = play_addr.get("url_list") if isinstance(play_addr, dict) else []
            src = _first_http_url(urls)
            if src:
                sortable.append((score, src))
        if sortable:
            sortable.sort(key=lambda x: x[0], reverse=True)
            return sortable[0][1]
    for key in ["play_addr_h264", "play_addr", "download_addr", "play_addr_265"]:
        addr = video.get(key)
        if isinstance(addr, dict):
            src = _first_http_url(addr.get("url_list"))
            if src:
                return src
    return None


async def download_video(video_url, output_path):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="zh-CN"
        )
        page = await context.new_page()
        try:
            logger.info(f"Processing: {video_url}")
            aweme_detail_payload = None
            media_candidates = []

            async def route_handler(route):
                if route.request.resource_type in ["image", "font", "stylesheet"]:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", route_handler)

            async def handle_response(response):
                nonlocal aweme_detail_payload
                try:
                    url = response.url
                    if response.status in [200, 206] and "douyinvod.com" in url and url.startswith("http"):
                        media_candidates.append(url)
                    if response.status == 200 and "/aweme/v1/web/aweme/detail/" in url and aweme_detail_payload is None:
                        aweme_detail_payload = await response.json()
                except Exception:
                    return

            def on_response(response):
                asyncio.create_task(handle_response(response))

            page.on("response", on_response)

            try:
                await page.goto(video_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning(f"Page load timeout: {e}")
                return False

            # Check for 404
            try:
                u = (page.url or "").lower()
                if "web_video_404_link" in u or "item_non_existent" in u:
                    logger.warning("Video not found")
                    return False
            except Exception:
                pass

            # Wait for WAF challenge
            deadline = time.monotonic() + CHALLENGE_MAX_WAIT_SECONDS
            ready = False
            while time.monotonic() < deadline:
                try:
                    html = await page.content()
                except Exception:
                    await page.wait_for_timeout(2000)
                    continue
                if not _looks_like_waf_challenge(html):
                    ready = True
                    break
                await page.wait_for_timeout(2000)

            if not ready:
                logger.warning("WAF challenge not resolved")
                return False

            await page.wait_for_timeout(DETAIL_WAIT_MS)
            await asyncio.sleep(1)

            src = None
            if aweme_detail_payload:
                logger.info("Got aweme_detail payload")
                src = _extract_src_from_aweme_detail(aweme_detail_payload)

            if not src and media_candidates:
                logger.info("Using intercepted media URL")
                src = media_candidates[0]

            if not src:
                logger.warning("No video source found")
                return False

            if not src.startswith("http"):
                logger.warning(f"Invalid source: {src}")
                return False

            logger.info(f"Downloading from {src}")
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Referer": "https://www.douyin.com/"
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(src, headers=headers, timeout=120) as resp:
                    if resp.status in [200, 206]:
                        with open(output_path, 'wb') as f:
                            while True:
                                chunk = await resp.content.read(1024 * 1024)
                                if not chunk:
                                    break
                                f.write(chunk)
                        logger.info(f"Saved: {output_path}")
                        return True
                    else:
                        logger.warning(f"Download failed: HTTP {resp.status}")
                        return False
        except Exception as e:
            logger.error(f"Error: {e}")
            return False
        finally:
            await page.close()
            await browser.close()


def normalize_input_to_url(item: str) -> str:
    item = (item or "").strip()
    if not item:
        return ""
    if item.startswith("http://") or item.startswith("https://"):
        return item
    if item.isdigit() and 8 <= len(item) <= 25:
        return f"https://www.douyin.com/video/{item}"
    return item


def main():
    parser = argparse.ArgumentParser(description="Fetch Douyin videos (URL or video_id)")
    parser.add_argument("items", nargs="*", help="Douyin URL(s) or video_id(s)")
    parser.add_argument("--file", help="Input file, one URL/video_id per line")
    parser.add_argument("--output-dir", default="downloads", help="Output directory")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    items = []
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            for line in f:
                t = line.strip()
                if t and not t.startswith("#"):
                    items.append(t)
    items.extend(args.items or [])
    items = list(dict.fromkeys([x.strip() for x in items if x.strip()]))

    if not items:
        print("No input items")
        return

    try:
        os.makedirs(args.output_dir, exist_ok=True)
    except Exception as e:
        print(f"[FAIL] 无法创建输出目录 {args.output_dir}: {e}")
        return
    results = []
    for raw in items:
        try:
            url = normalize_input_to_url(raw)
            vid_match = re.search(r"/video/(\d{8,25})", url)
            vid = vid_match.group(1) if vid_match else str(int(time.time() * 1000))
            output_path = os.path.join(args.output_dir, f"{vid}.mp4")
            ok = asyncio.run(download_video(url, output_path))
            results.append({"input": raw, "url": url, "video_id": vid, "ok": ok, "output": output_path if ok else ""})
        except Exception as e:
            logger.error(f"Batch item failed: {raw} — {e}")
            results.append({"input": raw, "url": raw, "video_id": "", "ok": False, "output": ""})

    ok = sum(1 for r in results if r["ok"])
    fail = len(results) - ok
    if args.json:
        print(json.dumps({"total": len(results), "ok": ok, "failed": fail, "items": results}, ensure_ascii=False, indent=2))
    else:
        print(f"total={len(results)} ok={ok} failed={fail}")
        for r in results:
            status = "OK" if r["ok"] else "FAIL"
            print(f"[{status}] {r['input']} -> {r['output'] or r['url']}")


if __name__ == "__main__":
    main()

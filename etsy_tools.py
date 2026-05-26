import re
import json
import time
from urllib.parse import urlparse, urlunparse, urlencode, parse_qs

import requests
from bs4 import BeautifulSoup


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
}


def clean_url(url: str) -> str:
    if not url:
        return ""

    url = str(url).strip()
    url = url.replace("\\u002F", "/")
    url = url.replace("\\/", "/")
    url = url.replace("&amp;", "&")

    if url.startswith("//"):
        url = "https:" + url

    return url


def is_etsy_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return "etsy.com" in host
    except Exception:
        return False


def strip_listing_url(url: str) -> str:
    url = clean_url(url)

    try:
        parsed = urlparse(url)
        path = parsed.path

        match = re.search(r"(/listing/\d+/[^/?#]+)", path)
        if match:
            return f"https://www.etsy.com{match.group(1)}"

        match = re.search(r"(/listing/\d+)", path)
        if match:
            return f"https://www.etsy.com{match.group(1)}"

    except Exception:
        pass

    return url.split("?")[0].split("#")[0]


def make_shop_page_url(shop_url: str, page: int) -> str:
    """
    Etsy 店铺翻页链接，一般通过 page 参数控制。
    """
    shop_url = clean_url(shop_url)
    parsed = urlparse(shop_url)

    query = parse_qs(parsed.query)
    query["page"] = [str(page)]

    new_query = urlencode(query, doseq=True)

    return urlunparse((
        parsed.scheme or "https",
        parsed.netloc or "www.etsy.com",
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment,
    ))


def fetch_html(url: str, timeout: int = 30) -> str:
    if not is_etsy_url(url):
        raise ValueError("不是 Etsy 链接")

    response = requests.get(
        url,
        headers=DEFAULT_HEADERS,
        timeout=timeout,
        allow_redirects=True,
    )

    if response.status_code != 200:
        raise RuntimeError(f"请求失败，HTTP 状态码：{response.status_code}")

    html = response.text
    lower_html = html.lower()

    if "captcha" in lower_html or "are you a human" in lower_html:
        raise RuntimeError("疑似触发 Etsy 验证页面，请降低频率，或手动粘贴商品链接。")

    return html


def extract_listing_urls_from_html(html: str) -> list:
    urls = set()

    soup = BeautifulSoup(html, "lxml")

    for a in soup.find_all("a", href=True):
        href = clean_url(a.get("href", ""))

        if href.startswith("/listing/"):
            href = "https://www.etsy.com" + href

        if "etsy.com" in href and "/listing/" in href:
            urls.add(strip_listing_url(href))

    patterns = [
        r'https://www\.etsy\.com/listing/\d+/[^"\'\s<>]+',
        r'https:\\/\\/www\.etsy\.com\\/listing\\/\d+\\/[^"\'\s<>]+',
        r'"/listing/\d+/[^"]+',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, html):
            match = clean_url(match.strip('"'))

            if match.startswith("/listing/"):
                match = "https://www.etsy.com" + match

            if "etsy.com" in match and "/listing/" in match:
                urls.add(strip_listing_url(match))

    return sorted(urls)


def extract_shop_listing_urls(
    shop_url: str,
    max_pages: int = 5,
    max_items: int = 100,
    delay_seconds: float = 2.0,
) -> list:
    """
    从 Etsy 店铺页提取商品链接。
    按 page=1,2,3... 温和翻页。
    """
    all_urls = []
    seen = set()

    for page in range(1, max_pages + 1):
        page_url = make_shop_page_url(shop_url, page)

        html = fetch_html(page_url)
        urls = extract_listing_urls_from_html(html)

        new_count = 0

        for url in urls:
            if url not in seen:
                seen.add(url)
                all_urls.append(url)
                new_count += 1

                if len(all_urls) >= max_items:
                    return all_urls

        if new_count == 0 and page > 1:
            break

        time.sleep(delay_seconds)

    return all_urls


def extract_json_ld_objects(soup: BeautifulSoup) -> list:
    objects = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text() or ""

        if not text.strip():
            continue

        try:
            data = json.loads(text)
        except Exception:
            continue

        if isinstance(data, list):
            objects.extend(data)
        elif isinstance(data, dict):
            objects.append(data)

    return objects


def extract_title(soup: BeautifulSoup, json_ld_objects: list) -> str:
    for obj in json_ld_objects:
        if isinstance(obj, dict) and obj.get("name"):
            return str(obj.get("name")).strip()

    meta = soup.find("meta", property="og:title")
    if meta and meta.get("content"):
        return meta["content"].strip()

    if soup.title and soup.title.string:
        return soup.title.string.strip()

    return ""


def add_image_url(images: list, url: str):
    url = clean_url(url)

    if not url:
        return

    if not url.startswith("http"):
        return

    if "etsystatic.com" not in url and "etsy.com" not in url:
        return

    # 去掉一些缩略图参数，但保留原始 URL 可用性
    if url not in images:
        images.append(url)


def extract_images(soup: BeautifulSoup, html: str, json_ld_objects: list) -> list:
    images = []

    for obj in json_ld_objects:
        if not isinstance(obj, dict):
            continue

        image_data = obj.get("image")

        if isinstance(image_data, str):
            add_image_url(images, image_data)

        elif isinstance(image_data, list):
            for item in image_data:
                if isinstance(item, str):
                    add_image_url(images, item)
                elif isinstance(item, dict):
                    add_image_url(images, item.get("url", ""))

        elif isinstance(image_data, dict):
            add_image_url(images, image_data.get("url", ""))

    for meta in soup.find_all("meta"):
        prop = meta.get("property") or meta.get("name") or ""
        content = meta.get("content") or ""

        if prop in ["og:image", "twitter:image"]:
            add_image_url(images, content)

    for img in soup.find_all("img"):
        for attr in ["src", "data-src", "srcset", "data-srcset"]:
            value = img.get(attr)

            if not value:
                continue

            parts = str(value).split(",")

            for part in parts:
                candidate = part.strip().split(" ")[0]
                add_image_url(images, candidate)

    patterns = [
        r'https://i\.etsystatic\.com/[^"\'\s<>\\]+',
        r'https:\\/\\/i\.etsystatic\.com\\/[^"\'\s<>]+',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, html):
            add_image_url(images, match)

    return images


def add_video_url(videos: list, url: str):
    url = clean_url(url)

    if not url:
        return

    if not url.startswith("http"):
        return

    if (
        "etsystatic.com" not in url
        and "etsy.com" not in url
        and ".mp4" not in url
    ):
        return

    if url not in videos:
        videos.append(url)


def extract_videos(soup: BeautifulSoup, html: str) -> list:
    videos = []

    for tag in soup.find_all(["video", "source"]):
        for attr in ["src", "data-src"]:
            add_video_url(videos, tag.get(attr, ""))

    for meta in soup.find_all("meta"):
        prop = meta.get("property") or meta.get("name") or ""
        content = meta.get("content") or ""

        if "video" in prop.lower():
            add_video_url(videos, content)

    patterns = [
        r'https://v\.etsystatic\.com/[^"\'\s<>\\]+',
        r'https://video\.etsystatic\.com/[^"\'\s<>\\]+',
        r'https:\\/\\/v\.etsystatic\.com\\/[^"\'\s<>]+',
        r'https:\\/\\/video\.etsystatic\.com\\/[^"\'\s<>]+',
        r'https://[^"\'\s<>\\]+\.mp4[^"\'\s<>\\]*',
        r'https:\\/\\/[^"\'\s<>]+\.mp4[^"\'\s<>]*',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, html):
            add_video_url(videos, match)

    return videos


def extract_listing_assets(listing_url: str) -> dict:
    listing_url = strip_listing_url(listing_url)

    html = fetch_html(listing_url)
    soup = BeautifulSoup(html, "lxml")
    json_ld_objects = extract_json_ld_objects(soup)

    title = extract_title(soup, json_ld_objects)
    images = extract_images(soup, html, json_ld_objects)
    videos = extract_videos(soup, html)

    return {
        "title": title,
        "listing_url": listing_url,
        "main_image": images[0] if images else "",
        "images": images,
        "videos": videos,
        "status": "成功",
        "error": "",
    }


def collect_etsy_shop_assets(
    shop_url: str,
    max_pages: int = 5,
    max_items: int = 100,
    images_per_product: int = 10,
    delay_seconds: float = 2.5,
) -> list:
    listing_urls = extract_shop_listing_urls(
        shop_url=shop_url,
        max_pages=max_pages,
        max_items=max_items,
        delay_seconds=delay_seconds,
    )

    results = []

    for index, listing_url in enumerate(listing_urls, start=1):
        try:
            item = extract_listing_assets(listing_url)
            item["index"] = index

            if images_per_product and images_per_product > 0:
                item["images"] = item["images"][:images_per_product]
                item["main_image"] = item["images"][0] if item["images"] else ""

            results.append(item)

        except Exception as e:
            results.append({
                "index": index,
                "title": "",
                "listing_url": listing_url,
                "main_image": "",
                "images": [],
                "videos": [],
                "status": "失败",
                "error": str(e),
            })

        time.sleep(delay_seconds)

    return results


def build_boss_view_rows(results: list) -> list:
    """
    老板查看表：每个商品之间插入空行。
    """
    rows = []

    for item in results:
        idx = item.get("index", "")
        title = item.get("title", "")
        listing_url = item.get("listing_url", "")
        images = item.get("images", [])
        videos = item.get("videos", [])

        rows.append({
            "编号": f"{idx:03d}" if isinstance(idx, int) else idx,
            "商品名称": title,
            "类型": "商品链接",
            "序号": "",
            "链接": listing_url,
            "状态": item.get("status", ""),
            "错误信息": item.get("error", ""),
        })

        for image_index, image_url in enumerate(images, start=1):
            rows.append({
                "编号": "",
                "商品名称": "",
                "类型": "图片",
                "序号": image_index,
                "链接": image_url,
                "状态": "",
                "错误信息": "",
            })

        for video_index, video_url in enumerate(videos, start=1):
            rows.append({
                "编号": "",
                "商品名称": "",
                "类型": "视频",
                "序号": video_index,
                "链接": video_url,
                "状态": "",
                "错误信息": "",
            })

        # 空行隔开不同商品
        rows.append({
            "编号": "",
            "商品名称": "",
            "类型": "",
            "序号": "",
            "链接": "",
            "状态": "",
            "错误信息": "",
        })

    return rows


def build_ai_import_rows(results: list) -> list:
    """
    AI 生图导入表：一行一个商品。
    参考图链接里放该商品全部图片链接，用英文逗号分隔。
    """
    rows = []

    for item in results:
        idx = item.get("index", 0)
        images = item.get("images", [])

        rows.append({
            "编号": f"{idx:03d}",
            "产品名称": item.get("title", ""),
            "场景": "根据参考图生成适合电商展示的产品场景",
            "风格": "高级真实产品摄影",
            "比例": "4:3",
            "分辨率": "1k",
            "生成数量": 1,
            "重点要求": "保留产品主体特征，画面干净，真实质感，适合电商展示",
            "禁止出现": "文字、水印、logo、人物、畸形结构、低清晰度",
            "参考图链接": ", ".join(images),
            "Etsy商品链接": item.get("listing_url", ""),
            "Etsy视频链接": ", ".join(item.get("videos", [])),
            "采集状态": item.get("status", ""),
            "错误信息": item.get("error", ""),
        })

    return rows
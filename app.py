
import os
import re
import io
import json
import time
import zipfile
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import streamlit as st
from dotenv import load_dotenv


# =========================
# 基础配置
# =========================

load_dotenv()

def get_secret_value(key, default=""):
    """
    本地运行：读取 .env
    Streamlit Cloud：优先读取 Secrets
    """
    try:
        value = st.secrets.get(key, None)
        if value is not None:
            return str(value)
    except Exception:
        pass
    return os.getenv(key, default)


APIMART_API_KEY = get_secret_value("APIMART_API_KEY", "").strip()
APIMART_BASE_URL = get_secret_value("APIMART_BASE_URL", "https://api.apimart.ai").strip().rstrip("/")
APP_PASSWORD = get_secret_value("APP_PASSWORD", "").strip()

GENERATE_ENDPOINT = f"{APIMART_BASE_URL}/v1/images/generations"
TASK_ENDPOINT = f"{APIMART_BASE_URL}/v1/tasks"

DEFAULT_MODEL = "gpt-image-2"


# =========================
# 通用工具函数
# =========================

def safe_filename(name: str) -> str:
    name = str(name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.replace("\n", "_").replace("\r", "_").strip()
    return name[:120] if len(name) > 120 else name


def normalize_empty(value, default=""):
    if pd.isna(value):
        return default
    value = str(value).strip()
    if value.lower() in ["nan", "none"]:
        return default
    return value


def parse_reference_images(value: str):
    value = normalize_empty(value, "")
    if not value:
        return []

    urls = []
    # 支持英文逗号、中文逗号、换行、分号
    parts = re.split(r"[,，;\n；]+", value)
    for item in parts:
        item = item.strip()
        if item and item.startswith("http"):
            urls.append(item)

    # 去重但保持顺序
    return list(dict.fromkeys(urls))


def get_headers() -> dict:
    if not APIMART_API_KEY:
        raise ValueError("没有检测到 APIMART_API_KEY，请先在 Streamlit Secrets 或 .env 中配置。")

    return {
        "Authorization": f"Bearer {APIMART_API_KEY}",
        "Content-Type": "application/json"
    }


# =========================
# 密码保护
# =========================

def check_password():
    if not APP_PASSWORD:
        return True

    if st.session_state.get("password_ok", False):
        return True

    st.title("访问验证")
    password = st.text_input("请输入访问密码", type="password")

    if password == APP_PASSWORD:
        st.session_state["password_ok"] = True
        st.rerun()

    if password:
        st.error("密码错误")

    return False


# =========================
# Etsy 浏览器采集脚本
# =========================

def build_etsy_browser_collector_js(
    max_pages=5,
    max_items=80,
    images_per_product=12,
    delay_ms=2500
):
    """
    生成可在 Etsy 店铺页面 Console 中运行的浏览器采集脚本。
    注意：脚本在用户浏览器里运行，而不是 Streamlit 云服务器抓 Etsy。
    """
    return f"""
(async function () {{
  const config = {{
    maxPages: {int(max_pages)},
    maxItems: {int(max_items)},
    imagesPerProduct: {int(images_per_product)},
    delayMs: {int(delay_ms)}
  }};

  function sleep(ms) {{
    return new Promise(resolve => setTimeout(resolve, ms));
  }}

  function cleanUrl(url) {{
    if (!url) return "";
    url = String(url).trim();
    url = url.replaceAll("\\\\u002F", "/");
    url = url.replaceAll("\\\\/", "/");
    url = url.replaceAll("&amp;", "&");

    if (url.startsWith("//")) {{
      url = "https:" + url;
    }}

    if (url.startsWith("/listing/")) {{
      url = "https://www.etsy.com" + url;
    }}

    return url;
  }}

  function stripListingUrl(url) {{
    url = cleanUrl(url);

    try {{
      const u = new URL(url);
      const matchWithTitle = u.pathname.match(/\\/listing\\/\\d+\\/[^/?#]+/);
      const matchIdOnly = u.pathname.match(/\\/listing\\/\\d+/);

      if (matchWithTitle) {{
        return "https://www.etsy.com" + matchWithTitle[0];
      }}

      if (matchIdOnly) {{
        return "https://www.etsy.com" + matchIdOnly[0];
      }}
    }} catch (e) {{}}

    return url.split("?")[0].split("#")[0];
  }}

  function makeShopPageUrl(shopUrl, page) {{
    const u = new URL(shopUrl);
    u.searchParams.set("page", String(page));
    return u.toString();
  }}

  async function fetchHtml(url) {{
    const res = await fetch(url, {{
      method: "GET",
      credentials: "include",
      headers: {{
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
      }}
    }});

    if (!res.ok) {{
      throw new Error(`请求失败：HTTP ${{res.status}}，URL：${{url}}`);
    }}

    const html = await res.text();

    const lower = html.toLowerCase();
    if (lower.includes("captcha") || lower.includes("are you a human")) {{
      throw new Error("疑似触发 Etsy 验证页面，请停止采集，降低频率后再试。");
    }}

    return html;
  }}

  function unique(arr) {{
    return Array.from(new Set(arr.filter(Boolean)));
  }}

  function extractListingUrlsFromHtml(html) {{
    const urls = [];

    const patterns = [
      /https:\\/\\/www\\.etsy\\.com\\/listing\\/\\d+\\/[^"'\\\\\\s<>]+/g,
      /https:\\\\\\/\\\\\\/www\\.etsy\\.com\\\\\\/listing\\\\\\/\\d+\\\\\\/[^"'\\\\\\s<>]+/g,
      /href="(\\/listing\\/\\d+\\/[^"]+)"/g
    ];

    for (const pattern of patterns) {{
      let match;

      while ((match = pattern.exec(html)) !== null) {{
        let raw = match[1] || match[0];

        raw = cleanUrl(raw);

        if (raw.startsWith("/listing/")) {{
          raw = "https://www.etsy.com" + raw;
        }}

        if (raw.includes("/listing/")) {{
          urls.push(stripListingUrl(raw));
        }}
      }}
    }}

    return unique(urls);
  }}

  function decodeHtml(text) {{
    const textarea = document.createElement("textarea");
    textarea.innerHTML = text || "";
    return textarea.value;
  }}

  function extractTitle(html) {{
    let match;

    match = html.match(/<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)["']/i);
    if (match) return decodeHtml(match[1]).replace(/\\s*\\|\\s*Etsy\\s*$/i, "").trim();

    match = html.match(/<title[^>]*>([^<]+)<\\/title>/i);
    if (match) return decodeHtml(match[1]).replace(/\\s*\\|\\s*Etsy\\s*$/i, "").trim();

    match = html.match(/"title"\\s*:\\s*"([^"]+)"/i);
    if (match) return decodeHtml(match[1]);

    return "";
  }}

  function extractImages(html) {{
    const images = [];

    const patterns = [
      /https:\\/\\/i\\.etsystatic\\.com\\/[^"'\\\\\\s<>]+/g,
      /https:\\\\\\/\\\\\\/i\\.etsystatic\\.com\\\\\\/[^"'\\\\\\s<>]+/g
    ];

    for (const pattern of patterns) {{
      let match;

      while ((match = pattern.exec(html)) !== null) {{
        let url = cleanUrl(match[0]);

        if (url.includes("i.etsystatic.com")) {{
          images.push(url);
        }}
      }}
    }}

    return unique(images);
  }}

  function extractVideos(html) {{
    const videos = [];

    const patterns = [
      /https:\\/\\/v\\.etsystatic\\.com\\/[^"'\\\\\\s<>]+/g,
      /https:\\/\\/video\\.etsystatic\\.com\\/[^"'\\\\\\s<>]+/g,
      /https:\\\\\\/\\\\\\/v\\.etsystatic\\.com\\\\\\/[^"'\\\\\\s<>]+/g,
      /https:\\\\\\/\\\\\\/video\\.etsystatic\\.com\\\\\\/[^"'\\\\\\s<>]+/g,
      /https:\\/\\/[^"'\\\\\\s<>]+\\.mp4[^"'\\\\\\s<>]*/g,
      /https:\\\\\\/\\\\\\/[^"'\\\\\\s<>]+\\.mp4[^"'\\\\\\s<>]*/g
    ];

    for (const pattern of patterns) {{
      let match;

      while ((match = pattern.exec(html)) !== null) {{
        let url = cleanUrl(match[0]);

        if (
          url.includes("etsystatic.com") ||
          url.includes(".mp4")
        ) {{
          videos.push(url);
        }}
      }}
    }}

    return unique(videos);
  }}

  async function collectShopListingUrls(shopUrl) {{
    const allUrls = [];
    const seen = new Set();

    for (let page = 1; page <= config.maxPages; page++) {{
      const pageUrl = makeShopPageUrl(shopUrl, page);
      console.log(`正在读取店铺第 ${{page}} 页：`, pageUrl);

      const html = await fetchHtml(pageUrl);
      const urls = extractListingUrlsFromHtml(html);

      let newCount = 0;

      for (const url of urls) {{
        if (!seen.has(url)) {{
          seen.add(url);
          allUrls.push(url);
          newCount++;

          if (allUrls.length >= config.maxItems) {{
            return allUrls;
          }}
        }}
      }}

      console.log(`第 ${{page}} 页找到 ${{urls.length}} 个链接，新增 ${{newCount}} 个。`);

      if (newCount === 0 && page > 1) {{
        break;
      }}

      await sleep(config.delayMs);
    }}

    return allUrls;
  }}

  async function collectListingAssets(listingUrl, index) {{
    console.log(`正在采集商品 ${{index}}：`, listingUrl);

    const html = await fetchHtml(listingUrl);

    const title = extractTitle(html);
    let images = extractImages(html);
    let videos = extractVideos(html);

    if (config.imagesPerProduct > 0) {{
      images = images.slice(0, config.imagesPerProduct);
    }}

    return {{
      index,
      title,
      listing_url: listingUrl,
      main_image: images[0] || "",
      images,
      videos,
      status: "成功",
      error: ""
    }};
  }}

  function downloadJson(data, filename) {{
    const blob = new Blob(
      [JSON.stringify(data, null, 2)],
      {{ type: "application/json;charset=utf-8" }}
    );

    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");

    a.href = url;
    a.download = filename;

    document.body.appendChild(a);
    a.click();

    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }}

  try {{
    let shopUrl = window.location.href;

    if (!shopUrl.includes("etsy.com/shop/")) {{
      shopUrl = prompt("请输入 Etsy 店铺链接：", "https://www.etsy.com/shop/HappyLaceCo");
    }}

    if (!shopUrl) {{
      alert("没有输入店铺链接，已取消。");
      return;
    }}

    console.log("开始采集 Etsy 店铺：", shopUrl);

    const listingUrls = await collectShopListingUrls(shopUrl);

    console.log(`共找到 ${{listingUrls.length}} 个商品链接。`);

    const results = [];

    for (let i = 0; i < listingUrls.length; i++) {{
      const listingUrl = listingUrls[i];

      try {{
        const item = await collectListingAssets(listingUrl, i + 1);
        results.push(item);
      }} catch (e) {{
        results.push({{
          index: i + 1,
          title: "",
          listing_url: listingUrl,
          main_image: "",
          images: [],
          videos: [],
          status: "失败",
          error: String(e.message || e)
        }});
      }}

      await sleep(config.delayMs);
    }}

    const output = {{
      source: "etsy_browser_collector",
      shop_url: shopUrl,
      collected_at: new Date().toISOString(),
      config,
      total: results.length,
      results
    }};

    console.log("采集完成：", output);

    downloadJson(output, "etsy_shop_assets.json");

    alert(`采集完成，共 ${{results.length}} 个商品。JSON 文件已下载。`);
  }} catch (e) {{
    console.error(e);
    alert("采集失败：" + String(e.message || e));
  }}
}})();
"""


def build_etsy_boss_rows_from_json(data: dict):
    results = data.get("results", [])
    rows = []

    for item in results:
        idx = item.get("index", "")
        title = item.get("title", "")
        listing_url = item.get("listing_url", "")
        images = item.get("images", []) or []
        videos = item.get("videos", []) or []

        rows.append({
            "编号": f"{int(idx):03d}" if str(idx).isdigit() else idx,
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


def build_etsy_ai_rows_from_json(data: dict):
    results = data.get("results", [])
    rows = []

    for item in results:
        idx = item.get("index", 0)
        images = item.get("images", []) or []
        videos = item.get("videos", []) or []

        rows.append({
            "编号": f"{int(idx):03d}" if str(idx).isdigit() else idx,
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
            "Etsy视频链接": ", ".join(videos),
            "采集状态": item.get("status", ""),
            "错误信息": item.get("error", ""),
        })

    return rows


# =========================
# APImart 生图函数
# =========================

def build_prompt(row, global_style: str) -> str:
    product_name = normalize_empty(row.get("产品名称"), "产品")
    scene = normalize_empty(row.get("场景"), "自然真实的产品展示场景")
    style = normalize_empty(row.get("风格"), "高级真实产品摄影")
    focus = normalize_empty(row.get("重点要求"), "主体清晰，构图高级，真实质感")
    negative = normalize_empty(row.get("禁止出现"), "文字、水印、logo、人物、畸形结构")

    prompt = f"""
请生成一张商业展示图片。

【主体】
{product_name}

【使用场景】
{scene}

【图片风格】
{style}

【重点要求】
{focus}

【统一风格】
{global_style}

【禁止出现】
{negative}

【画面要求】
1. 主体必须清晰突出。
2. 构图高级，画面干净。
3. 光线自然，材质真实。
4. 适合电商展示、详情页、社媒宣传。
5. 不要出现任何文字、水印、logo。
""".strip()

    return prompt


def normalize_size(value, default_size="4:3"):
    """
    防止 Excel 把 1:1 读成 1:01:00 这类时间格式。
    """
    value = normalize_empty(value, default_size)

    replacements = {
        "1:01:00": "1:1",
        "4:03:00": "4:3",
        "3:04:00": "3:4",
        "16:09:00": "16:9",
        "9:16:00": "9:16",
    }

    return replacements.get(value, value)


def submit_generation_task(
    prompt: str,
    size: str,
    resolution: str,
    image_urls=None,
    model: str = DEFAULT_MODEL
) -> str:
    if image_urls is None:
        image_urls = []

    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size,
        "resolution": resolution
    }

    if image_urls:
        payload["image_urls"] = image_urls

    response = requests.post(
        GENERATE_ENDPOINT,
        headers=get_headers(),
        json=payload,
        timeout=60
    )

    try:
        data = response.json()
    except Exception:
        raise RuntimeError(f"提交任务失败，HTTP {response.status_code}，响应内容：{response.text}")

    if response.status_code != 200:
        raise RuntimeError(f"提交任务失败，HTTP {response.status_code}，响应内容：{data}")

    if data.get("code") != 200:
        raise RuntimeError(f"提交任务失败，返回内容：{data}")

    task_data = data.get("data")

    if isinstance(task_data, list) and len(task_data) > 0:
        task_id = task_data[0].get("task_id")
    elif isinstance(task_data, dict):
        task_id = task_data.get("task_id")
    else:
        task_id = None

    if not task_id:
        raise RuntimeError(f"没有拿到 task_id，返回内容：{data}")

    return task_id


def poll_task_result(
    task_id: str,
    max_wait_seconds: int = 240,
    first_delay_seconds: int = 10,
    interval_seconds: int = 5,
    max_query_retries: int = 5
) -> list:
    time.sleep(first_delay_seconds)
    start_time = time.time()
    consecutive_errors = 0

    while True:
        if time.time() - start_time > max_wait_seconds:
            raise TimeoutError(f"任务超时：{task_id}")

        url = f"{TASK_ENDPOINT}/{task_id}"

        try:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {APIMART_API_KEY}"},
                params={"language": "zh"},
                timeout=60
            )

            try:
                data = response.json()
            except Exception:
                raise RuntimeError(f"查询任务失败，HTTP {response.status_code}，响应内容：{response.text}")

            if response.status_code != 200:
                raise RuntimeError(f"查询任务失败，HTTP {response.status_code}，响应内容：{data}")

            consecutive_errors = 0

            task_info = data.get("data", {})
            status = task_info.get("status")

            if status == "completed":
                result = task_info.get("result", {})
                images = result.get("images", [])

                image_url_list = []

                for image_item in images:
                    urls = image_item.get("url", [])
                    if isinstance(urls, list):
                        image_url_list.extend(urls)
                    elif isinstance(urls, str):
                        image_url_list.append(urls)

                if not image_url_list:
                    raise RuntimeError(f"任务完成但没有图片 URL，返回内容：{data}")

                return image_url_list

            if status in ["failed", "cancelled"]:
                error_info = task_info.get("error", {})
                raise RuntimeError(f"任务失败：{task_id}，错误：{error_info}")

            time.sleep(interval_seconds)

        except (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as e:
            consecutive_errors += 1

            if consecutive_errors > max_query_retries:
                raise RuntimeError(
                    f"查询任务网络错误多次失败，已超过最大重试次数。task_id：{task_id}，错误：{e}"
                )

            time.sleep(interval_seconds * consecutive_errors)


def download_image(image_url: str) -> bytes:
    response = requests.get(image_url, timeout=120)
    if response.status_code != 200:
        raise RuntimeError(f"下载图片失败，HTTP {response.status_code}，URL：{image_url}")
    return response.content


def validate_dataframe(df: pd.DataFrame):
    required_columns = [
        "编号",
        "产品名称",
        "场景",
        "风格",
        "比例",
        "分辨率",
        "生成数量",
        "重点要求",
        "禁止出现",
        "参考图链接"
    ]
    missing_columns = [col for col in required_columns if col not in df.columns]
    return missing_columns


def read_ai_excel(uploaded_file):
    """
    兼容普通 AI 模板，也兼容 Etsy 采集结果 Excel 里的 AI生图导入表。
    """
    excel_data = pd.read_excel(uploaded_file, sheet_name=None)
    if "AI生图导入表" in excel_data:
        return excel_data["AI生图导入表"]
    if "AI 生图导入表" in excel_data:
        return excel_data["AI 生图导入表"]
    first_sheet_name = list(excel_data.keys())[0]
    return excel_data[first_sheet_name]


def build_jobs(df, global_style, default_size, default_resolution):
    jobs = []

    for row_index, row in df.iterrows():
        item_id = normalize_empty(row.get("编号"), str(row_index + 1))
        product_name = normalize_empty(row.get("产品名称"), "产品")
        row_size = normalize_size(row.get("比例"), default_size)
        row_resolution = normalize_empty(row.get("分辨率"), default_resolution)

        try:
            row_count = int(float(row.get("生成数量", 1)))
            if row_count < 1:
                row_count = 1
        except Exception:
            row_count = 1

        reference_images = parse_reference_images(row.get("参考图链接", ""))
        prompt = build_prompt(row, global_style)

        for image_index in range(row_count):
            display_name = f"{item_id}_{product_name}_{image_index + 1}"

            jobs.append({
                "item_id": item_id,
                "product_name": product_name,
                "display_name": display_name,
                "prompt": prompt,
                "size": row_size,
                "resolution": row_resolution,
                "reference_images": reference_images
            })

    return jobs


def process_single_job(job, model, max_wait_seconds, first_delay_seconds, interval_seconds):
    task_id = submit_generation_task(
        prompt=job["prompt"],
        size=job["size"],
        resolution=job["resolution"],
        image_urls=job["reference_images"],
        model=model
    )

    image_urls = poll_task_result(
        task_id=task_id,
        max_wait_seconds=max_wait_seconds,
        first_delay_seconds=first_delay_seconds,
        interval_seconds=interval_seconds
    )

    images = []
    for idx, image_url in enumerate(image_urls):
        image_bytes = download_image(image_url)
        filename = safe_filename(f"{job['display_name']}_{idx + 1}.png")
        images.append({
            "filename": filename,
            "image_url": image_url,
            "image_bytes": image_bytes
        })

    return {
        "status": "成功",
        "task_id": task_id,
        "job": job,
        "images": images,
        "error": ""
    }


# =========================
# Streamlit 页面
# =========================

st.set_page_config(
    page_title="Etsy 素材采集 + APImart 批量生图工具",
    page_icon="🎨",
    layout="wide"
)

if not check_password():
    st.stop()

st.title("🎨 Etsy 素材采集 + APImart 批量生图工具")

with st.sidebar:
    page = st.radio(
        "选择功能",
        ["AI 批量生图", "Etsy 素材采集"],
        index=0
    )

    st.divider()
    st.write("API 状态：")
    if APIMART_API_KEY:
        st.success("已检测到 APIMART_API_KEY")
    else:
        st.error("未检测到 APIMART_API_KEY")


# =========================
# Etsy 素材采集页面
# =========================

if page == "Etsy 素材采集":
    st.subheader("🛒 Etsy 店铺图片 / 视频素材采集")

    st.info(
        "这是浏览器采集方案：脚本在你自己的浏览器 Etsy 页面里运行，"
        "不会让 Streamlit 云服务器直接请求 Etsy，因此可以避开服务器请求 Etsy 时的 403。"
    )

    st.warning(
        "请仅用于自有店铺或已授权店铺。不要高频请求，不要绕过验证码。"
    )

    st.markdown("### 第一步：生成并下载采集脚本")

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        max_pages = st.number_input("最多翻页数", min_value=1, max_value=30, value=5, step=1)

    with col2:
        max_items = st.number_input("最多商品数", min_value=1, max_value=500, value=80, step=1)

    with col3:
        images_per_product = st.number_input("每个商品最多图片数", min_value=1, max_value=30, value=12, step=1)

    with col4:
        delay_ms = st.number_input("请求间隔毫秒", min_value=500, max_value=20000, value=2500, step=500)

    collector_js = build_etsy_browser_collector_js(
        max_pages=max_pages,
        max_items=max_items,
        images_per_product=images_per_product,
        delay_ms=delay_ms
    )

    st.download_button(
        label="下载 Etsy 浏览器采集脚本",
        data=collector_js,
        file_name="etsy_browser_collector.js",
        mime="text/javascript"
    )

    with st.expander("查看脚本使用方法"):
        st.markdown(
            """
1. 打开 Etsy 店铺页面，比如 `https://www.etsy.com/shop/HappyLaceCo`
2. 按 `F12` 打开开发者工具
3. 切换到 `Console`
4. 如果浏览器不允许粘贴，先输入 `allow pasting` 并回车
5. 打开下载的 `etsy_browser_collector.js`，复制全部代码
6. 粘贴到 Console，回车运行
7. 运行完成后会自动下载 `etsy_shop_assets.json`
8. 回到本页面上传这个 JSON，生成 Excel
            """
        )

    st.code(collector_js[:3000] + "\n\n// 代码较长，建议直接下载脚本文件使用。", language="javascript")

    st.divider()
    st.markdown("### 第二步：上传采集结果 JSON，生成 Excel")

    uploaded_json = st.file_uploader(
        "上传 etsy_shop_assets.json",
        type=["json"]
    )

    if uploaded_json:
        try:
            data = json.load(uploaded_json)
        except Exception as e:
            st.error(f"JSON 读取失败：{e}")
            st.stop()

        boss_rows = build_etsy_boss_rows_from_json(data)
        ai_rows = build_etsy_ai_rows_from_json(data)

        boss_df = pd.DataFrame(boss_rows)
        ai_df = pd.DataFrame(ai_rows)

        total = len(data.get("results", []))
        success_count = sum(1 for x in data.get("results", []) if x.get("status") == "成功")
        fail_count = total - success_count

        st.success(f"解析完成：共 {total} 个商品，成功 {success_count} 个，失败 {fail_count} 个。")

        st.subheader("老板查看表：商品之间已自动空一行")
        st.dataframe(boss_df, use_container_width=True)

        st.subheader("AI 生图导入表：可直接上传到 AI 批量生图")
        st.dataframe(ai_df, use_container_width=True)

        excel_buffer = io.BytesIO()

        with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
            boss_df.to_excel(writer, index=False, sheet_name="老板查看表")
            ai_df.to_excel(writer, index=False, sheet_name="AI生图导入表")

            workbook = writer.book

            for sheet_name in ["老板查看表", "AI生图导入表"]:
                worksheet = workbook[sheet_name]
                worksheet.freeze_panes = "A2"

                for column_cells in worksheet.columns:
                    max_length = 0
                    column_letter = column_cells[0].column_letter

                    for cell in column_cells:
                        value = cell.value
                        if value is None:
                            continue
                        max_length = max(max_length, len(str(value)))

                    worksheet.column_dimensions[column_letter].width = min(max_length + 2, 80)

        excel_buffer.seek(0)

        st.download_button(
            label="下载 Etsy 店铺素材 Excel",
            data=excel_buffer,
            file_name="etsy店铺素材采集结果.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    st.stop()


# =========================
# AI 批量生图页面
# =========================

st.subheader("🎨 AI 批量生图")
st.caption("上传 Excel → 并发提交任务 → 并发轮询 → 下载图片 → 打包 ZIP")

with st.sidebar:
    st.divider()
    st.header("AI 生图设置")

    model = st.text_input("模型名称", value=DEFAULT_MODEL)

    default_size = st.selectbox(
        "默认比例 size",
        ["1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "5:4", "4:5", "2:1", "1:2", "21:9", "9:21", "auto"],
        index=1
    )

    default_resolution = st.selectbox(
        "默认分辨率 resolution",
        ["1k", "2k", "4k"],
        index=0
    )

    parallel_workers = st.number_input(
        "并发数",
        min_value=1,
        max_value=20,
        value=6,
        step=1
    )

    max_wait_seconds = st.number_input(
        "单张最大等待秒数",
        min_value=60,
        max_value=1200,
        value=240,
        step=30
    )

    first_delay_seconds = st.number_input(
        "提交后首次查询等待秒数",
        min_value=3,
        max_value=30,
        value=8,
        step=1
    )

    interval_seconds = st.number_input(
        "轮询间隔秒数",
        min_value=2,
        max_value=20,
        value=4,
        step=1
    )

    st.divider()

    global_style = st.text_area(
        "统一风格要求",
        value="真实高级产品摄影，画面干净，主体突出，自然光线，真实材质，高级商业展示感。",
        height=120
    )


uploaded_file = st.file_uploader("上传 Excel 文件", type=["xlsx", "xls"])

if uploaded_file is None:
    st.info("请先上传 Excel。Etsy 采集导出的 Excel 可以直接上传，系统会自动读取 `AI生图导入表`。")
    st.stop()

try:
    df = read_ai_excel(uploaded_file)
except Exception as e:
    st.error(f"读取 Excel 失败：{e}")
    st.stop()

st.subheader("Excel 预览")
st.dataframe(df, use_container_width=True)

missing_columns = validate_dataframe(df)
if missing_columns:
    st.error(f"Excel 缺少这些列：{missing_columns}")
    st.stop()

jobs = build_jobs(df, global_style, default_size, default_resolution)
total_images = len(jobs)

st.subheader("生成设置确认")

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.metric("总行数", len(df))
with col2:
    st.metric("预计图片数", total_images)
with col3:
    st.metric("默认比例", default_size)
with col4:
    st.metric("并发数", parallel_workers)

with st.expander("查看第一行 Prompt 预览"):
    if len(df) > 0:
        st.code(build_prompt(df.iloc[0], global_style), language="text")

start_button = st.button("🚀 开始并行批量生成", type="primary")

if not start_button:
    st.stop()

if not APIMART_API_KEY:
    st.error("请先在 Streamlit Secrets 或 .env 中配置 APIMART_API_KEY。")
    st.stop()

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
output_folder = os.path.join("outputs", f"batch_{timestamp}")
os.makedirs(output_folder, exist_ok=True)

progress_bar = st.progress(0)
status_text = st.empty()
result_area = st.container()

generated_files = []
log_rows = []
finished_count = 0

status_text.info(f"开始并行处理，共 {total_images} 个任务，并发数：{parallel_workers}")

with ThreadPoolExecutor(max_workers=int(parallel_workers)) as executor:
    future_map = {
        executor.submit(
            process_single_job,
            job,
            model,
            int(max_wait_seconds),
            int(first_delay_seconds),
            int(interval_seconds)
        ): job
        for job in jobs
    }

    for future in as_completed(future_map):
        job = future_map[future]
        display_name = job["display_name"]

        try:
            result = future.result()

            for img in result["images"]:
                local_path = os.path.join(output_folder, img["filename"])
                with open(local_path, "wb") as f:
                    f.write(img["image_bytes"])

                generated_files.append({
                    "filename": img["filename"],
                    "bytes": img["image_bytes"]
                })

                with result_area:
                    st.image(img["image_bytes"], caption=img["filename"], width=320)

                log_rows.append({
                    "编号": job["item_id"],
                    "产品名称": job["product_name"],
                    "状态": "成功",
                    "task_id": result["task_id"],
                    "文件名": img["filename"],
                    "图片URL": img["image_url"],
                    "错误信息": ""
                })

            status_text.success(f"完成：{display_name}")

        except Exception as e:
            error_message = str(e)

            log_rows.append({
                "编号": job["item_id"],
                "产品名称": job["product_name"],
                "状态": "失败",
                "task_id": "",
                "文件名": "",
                "图片URL": "",
                "错误信息": error_message
            })

            with result_area:
                st.error(f"{display_name} 失败：{error_message}")

        finished_count += 1
        progress_bar.progress(min(finished_count / total_images, 1.0))

zip_buffer = io.BytesIO()
with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
    for item in generated_files:
        zip_file.writestr(item["filename"], item["bytes"])

    log_df = pd.DataFrame(log_rows)
    log_excel_buffer = io.BytesIO()
    log_df.to_excel(log_excel_buffer, index=False)
    log_excel_buffer.seek(0)
    zip_file.writestr("生成日志.xlsx", log_excel_buffer.read())

zip_buffer.seek(0)

st.success("全部处理完成！")

st.download_button(
    label="📦 下载全部图片 ZIP",
    data=zip_buffer,
    file_name=f"批量生成图片结果_{timestamp}.zip",
    mime="application/zip"
)

if log_rows:
    st.subheader("生成日志")
    st.dataframe(pd.DataFrame(log_rows), use_container_width=True)

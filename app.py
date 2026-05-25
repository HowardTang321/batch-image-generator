import os
import re
import io
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

APIMART_API_KEY = os.getenv("APIMART_API_KEY", "").strip()
APIMART_BASE_URL = os.getenv("APIMART_BASE_URL", "https://api.apimart.ai").strip().rstrip("/")

GENERATE_ENDPOINT = f"{APIMART_BASE_URL}/v1/images/generations"
TASK_ENDPOINT = f"{APIMART_BASE_URL}/v1/tasks"

DEFAULT_MODEL = "gpt-image-2"


# =========================
# 工具函数
# =========================

def safe_filename(name: str) -> str:
    name = str(name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.replace("\n", "_").replace("\r", "_").strip()
    return name[:120] if len(name) > 120 else name


def get_headers() -> dict:
    if not APIMART_API_KEY:
        raise ValueError("没有检测到 APIMART_API_KEY，请先在 .env 文件中配置。")

    return {
        "Authorization": f"Bearer {APIMART_API_KEY}",
        "Content-Type": "application/json"
    }


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
    for item in value.split(","):
        item = item.strip()
        if item:
            urls.append(item)
    return urls


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
    interval_seconds: int = 5
) -> list:
    time.sleep(first_delay_seconds)
    start_time = time.time()

    while True:
        if time.time() - start_time > max_wait_seconds:
            raise TimeoutError(f"任务超时：{task_id}")

        url = f"{TASK_ENDPOINT}/{task_id}"
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


def build_jobs(df, global_style, default_size, default_resolution):
    jobs = []

    for row_index, row in df.iterrows():
        item_id = normalize_empty(row.get("编号"), str(row_index + 1))
        product_name = normalize_empty(row.get("产品名称"), "产品")
        row_size = normalize_empty(row.get("比例"), default_size)
        row_resolution = normalize_empty(row.get("分辨率"), default_resolution)

        try:
            row_count = int(row.get("生成数量", 1))
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
    """
    单个任务的完整流程：
    提交 -> 轮询 -> 下载
    这个函数会被线程池并发执行
    """
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
    page_title="APImart 并行批量图片生成工具",
    page_icon="🎨",
    layout="wide"
)

st.title("🎨 APImart 并行批量图片生成工具")
st.caption("上传 Excel → 并发提交任务 → 并发轮询 → 下载图片 → 打包 ZIP")

with st.sidebar:
    st.header("全局设置")

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

    st.divider()

    st.write("API 状态：")
    if APIMART_API_KEY:
        st.success("已检测到 APIMART_API_KEY")
    else:
        st.error("未检测到 APIMART_API_KEY")


uploaded_file = st.file_uploader("上传 Excel 文件", type=["xlsx", "xls"])

if uploaded_file is None:
    st.info("请先上传 Excel。")
    st.stop()

try:
    df = pd.read_excel(uploaded_file)
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
    st.error("请先在 .env 文件中配置 APIMART_API_KEY。")
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

# 并行执行
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

            # 保存图片
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

# 打包 ZIP
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
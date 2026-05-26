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

try:
    import oss2
except Exception:
    oss2 = None


# =========================
# 基础配置 / Secrets
# =========================

load_dotenv()


def get_secret_value(key: str, default: str = "") -> str:
    """优先读取 Streamlit Secrets；本地运行时读取 .env。"""
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

# eHunt，可在 Secrets 里预填，也可页面手动输入
EHUNT_API_BASE_URL = get_secret_value("EHUNT_API_BASE_URL", "").strip().rstrip("/")
EHUNT_API_KEY = get_secret_value("EHUNT_API_KEY", "").strip()

# 阿里云 OSS，可选。不配置也不影响生成图片和 ZIP 下载。
ALIYUN_OSS_ACCESS_KEY_ID = get_secret_value("ALIYUN_OSS_ACCESS_KEY_ID", "").strip()
ALIYUN_OSS_ACCESS_KEY_SECRET = get_secret_value("ALIYUN_OSS_ACCESS_KEY_SECRET", "").strip()
ALIYUN_OSS_ENDPOINT = get_secret_value("ALIYUN_OSS_ENDPOINT", "").strip()
ALIYUN_OSS_BUCKET_NAME = get_secret_value("ALIYUN_OSS_BUCKET_NAME", "").strip()
ALIYUN_OSS_PUBLIC_BASE_URL = get_secret_value("ALIYUN_OSS_PUBLIC_BASE_URL", "").strip().rstrip("/")

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
    if value.lower() in ["nan", "none", "nat"]:
        return default
    return value


def normalize_ratio(value, default="4:3") -> str:
    """处理 Excel 把 1:1 识别成 1:01:00 的问题。"""
    value = normalize_empty(value, default)
    value = value.replace("：", ":").strip()

    # Excel 可能把 1:1 变成 1:01:00 / 01:01:00
    m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})$", value)
    if m:
        a = int(m.group(1))
        b = int(m.group(2))
        c = int(m.group(3))
        if c == 0:
            return f"{a}:{b}"

    # 有些 Excel 读出来是 1900-01-01 01:01:00
    m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", value)
    if m and "-" in value:
        a = int(m.group(1))
        b = int(m.group(2))
        c = int(m.group(3))
        if c == 0:
            return f"{a}:{b}"

    return value


def parse_reference_images(value: str):
    value = normalize_empty(value, "")
    if not value:
        return []

    urls = []
    # 支持逗号、中文逗号、换行、分号
    for item in re.split(r"[,，;；\n]+", value):
        item = item.strip()
        if item and item.startswith("http"):
            urls.append(item)
    return list(dict.fromkeys(urls))


def split_links(value):
    if value is None or pd.isna(value):
        return []
    text = str(value).strip()
    if not text:
        return []
    urls = re.findall(r"https?://[^\s,;，；]+", text)
    if urls:
        return list(dict.fromkeys(urls))
    links = []
    for part in re.split(r"[\n,，;；]+", text):
        part = part.strip()
        if part.startswith("http"):
            links.append(part)
    return list(dict.fromkeys(links))


# =========================
# 密码验证
# =========================

st.set_page_config(
    page_title="电商素材采集 + AI 批量生图工具",
    page_icon="🎨",
    layout="wide"
)


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


if not check_password():
    st.stop()


# =========================
# APImart 生图函数
# =========================

def get_headers() -> dict:
    if not APIMART_API_KEY:
        raise ValueError("没有检测到 APIMART_API_KEY，请先在 .env 或 Streamlit Secrets 中配置。")
    return {
        "Authorization": f"Bearer {APIMART_API_KEY}",
        "Content-Type": "application/json"
    }


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


def submit_generation_task(prompt: str, size: str, resolution: str, image_urls=None, model: str = DEFAULT_MODEL) -> str:
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

    response = requests.post(GENERATE_ENDPOINT, headers=get_headers(), json=payload, timeout=60)
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


def poll_task_result(task_id: str, max_wait_seconds: int = 300, first_delay_seconds: int = 12,
                     interval_seconds: int = 6, max_query_retries: int = 5) -> list:
    """轮询任务状态；遇到 SSL/网络抖动会重试。"""
    time.sleep(first_delay_seconds)
    start_time = time.time()
    consecutive_errors = 0

    while True:
        if time.time() - start_time > max_wait_seconds:
            raise TimeoutError(f"任务超时：{task_id}")

        try:
            response = requests.get(
                f"{TASK_ENDPOINT}/{task_id}",
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

        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            consecutive_errors += 1
            if consecutive_errors > max_query_retries:
                raise RuntimeError(f"查询任务多次网络失败，task_id：{task_id}，错误：{e}")
            time.sleep(interval_seconds * consecutive_errors)


def download_image(image_url: str) -> bytes:
    response = requests.get(image_url, timeout=120)
    if response.status_code != 200:
        raise RuntimeError(f"下载图片失败，HTTP {response.status_code}，URL：{image_url}")
    return response.content


def validate_dataframe(df: pd.DataFrame):
    required_columns = ["编号", "产品名称", "场景", "风格", "比例", "分辨率", "生成数量", "重点要求", "禁止出现", "参考图链接"]
    return [col for col in required_columns if col not in df.columns]


def build_jobs(df, global_style, default_size, default_resolution):
    jobs = []
    for row_index, row in df.iterrows():
        item_id = normalize_empty(row.get("编号"), str(row_index + 1))
        product_name = normalize_empty(row.get("产品名称"), "产品")
        row_size = normalize_ratio(row.get("比例"), default_size)
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
        images.append({"filename": filename, "image_url": image_url, "image_bytes": image_bytes})
    return {"status": "成功", "task_id": task_id, "job": job, "images": images, "error": ""}


# =========================
# 阿里云 OSS，可选
# =========================

def get_aliyun_oss_bucket():
    if oss2 is None:
        return None
    if not all([ALIYUN_OSS_ACCESS_KEY_ID, ALIYUN_OSS_ACCESS_KEY_SECRET, ALIYUN_OSS_ENDPOINT, ALIYUN_OSS_BUCKET_NAME]):
        return None
    auth = oss2.Auth(ALIYUN_OSS_ACCESS_KEY_ID, ALIYUN_OSS_ACCESS_KEY_SECRET)
    return oss2.Bucket(auth, ALIYUN_OSS_ENDPOINT, ALIYUN_OSS_BUCKET_NAME)


def make_oss_object_key(timestamp: str, filename: str):
    date_part = datetime.now().strftime("%Y-%m-%d")
    return f"generated-images/{date_part}/batch_{timestamp}/{safe_filename(filename)}"


def upload_bytes_to_aliyun_oss(file_bytes: bytes, object_key: str, content_type: str = "image/png"):
    bucket = get_aliyun_oss_bucket()
    if bucket is None:
        return ""
    try:
        result = bucket.put_object(object_key, file_bytes, headers={"Content-Type": content_type})
        if result.status == 200:
            if ALIYUN_OSS_PUBLIC_BASE_URL:
                return f"{ALIYUN_OSS_PUBLIC_BASE_URL}/{object_key}"
            return f"oss://{ALIYUN_OSS_BUCKET_NAME}/{object_key}"
        return f"上传 OSS 失败，状态码：{result.status}"
    except Exception as e:
        return f"上传 OSS 失败：{e}"


# =========================
# eHunt 店铺查询 API：POST /api/v1/stores
# =========================

def call_ehunt_stores_api(api_base_url: str, api_key: str, search_key: str, country: str = "US", category: str = "",
                          page_num: int = 1, page_size: int = 20, sort_by: int = 8, desc: int = 1):
    api_base_url = api_base_url.strip().rstrip("/")
    url = f"{api_base_url}/api/v1/stores"
    headers = {"Content-Type": "application/json", "X-VIP-TOKEN": api_key.strip()}
    payload = {
        "search_key": search_key,
        "status": 1,
        "country": country,
        "category": category,
        "sort_by": sort_by,
        "desc": desc,
        "page_num": page_num,
        "page_size": page_size,
    }
    response = requests.post(url, headers=headers, json=payload, timeout=60)
    try:
        data = response.json()
    except Exception:
        raise RuntimeError(f"E Hunt API 返回不是 JSON。HTTP {response.status_code}，内容：{response.text[:500]}")
    if response.status_code != 200:
        raise RuntimeError(f"E Hunt API 请求失败。HTTP {response.status_code}，返回：{data}")
    if data.get("code") not in [0, 200, "0", "200"]:
        raise RuntimeError(f"E Hunt API 返回错误：{data}")
    return data


def parse_ehunt_store_response(data: dict):
    result = data.get("data", {})
    store_list = result.get("list", [])
    rows = []
    for item in store_list:
        rows.append({
            "店铺ID": item.get("store_id", ""),
            "店铺名称": item.get("store_name", ""),
            "店铺链接": item.get("store_url", ""),
            "店铺网站": item.get("shop_website", ""),
            "Logo链接": item.get("logo_url", ""),
            "状态": item.get("status", ""),
            "商品数": item.get("products", ""),
            "开店时间": item.get("start_at", ""),
            "周销量": item.get("sales_weekly", ""),
            "总销量": item.get("sales_total", ""),
            "评论数": item.get("reviews", ""),
            "周评论数": item.get("reviews_weekly", ""),
            "收藏数": item.get("favorites", ""),
            "周收藏数": item.get("favorites_weekly", ""),
            "评分": item.get("rating", ""),
            "国家": ", ".join(item.get("country", [])) if isinstance(item.get("country"), list) else item.get("country", ""),
            "类目": ", ".join(item.get("category", [])) if isinstance(item.get("category"), list) else item.get("category", ""),
        })
    return rows


def build_store_seed_rows(store_rows):
    rows = []
    for index, item in enumerate(store_rows, start=1):
        rows.append({
            "编号": f"{index:03d}",
            "店铺名称": item.get("店铺名称", ""),
            "店铺链接": item.get("店铺链接", ""),
            "商品数": item.get("商品数", ""),
            "总销量": item.get("总销量", ""),
            "收藏数": item.get("收藏数", ""),
            "备注": "当前为店铺数据；商品图片/视频需要继续接商品查询接口或导入 eHunt 导出文件。",
        })
    return rows


# =========================
# Excel 输出工具
# =========================

def make_excel_download(sheets: dict) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
            worksheet = writer.book[sheet_name[:31]]
            worksheet.freeze_panes = "A2"
            for column_cells in worksheet.columns:
                max_length = 0
                column_letter = column_cells[0].column_letter
                for cell in column_cells:
                    if cell.value is not None:
                        max_length = max(max_length, len(str(cell.value)))
                worksheet.column_dimensions[column_letter].width = min(max_length + 2, 80)
    buffer.seek(0)
    return buffer.read()


# =========================
# 页面布局
# =========================

st.title("🎨 eHunt 店铺查询 + AI 批量生图工具")

with st.sidebar:
    page = st.radio(
        "选择功能",
        ["AI 批量生图", "eHunt 店铺查询"],
        index=0,
    )
    st.divider()


# =========================
# eHunt 店铺查询页
# =========================

if page == "eHunt 店铺查询":
    st.subheader("🔎 eHunt 店铺查询")
    st.info("当前接入 eHunt 店铺查询接口：POST /api/v1/stores。它返回店铺信息；商品图片/视频通常需要商品查询接口或 eHunt 导出文件。")

    api_base_url = st.text_input("E Hunt API Base URL", value=EHUNT_API_BASE_URL, placeholder="例如：https://api.xxx.com")
    api_key = st.text_input("E Hunt API Key", value=EHUNT_API_KEY, type="password", placeholder="填写 vip_xxx API Key")
    search_key = st.text_input("搜索关键词 / 店铺名", value="jewelry", placeholder="例如：HappyLaceCo 或 jewelry")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        country = st.text_input("国家", value="US")
    with col2:
        category = st.text_input("类目，可空", value="")
    with col3:
        page_num = st.number_input("页码", min_value=1, max_value=100, value=1, step=1)
    with col4:
        page_size = st.number_input("每页数量", min_value=1, max_value=100, value=20, step=1)

    col5, col6 = st.columns(2)
    with col5:
        sort_by = st.number_input("排序字段 sort_by", min_value=0, max_value=20, value=8, step=1)
    with col6:
        desc = st.selectbox("排序方式 desc", options=[1, 0], index=0)

    if st.button("开始查询 eHunt 店铺", type="primary"):
        if not api_base_url.strip():
            st.error("请填写 E Hunt API Base URL。")
            st.stop()
        if not api_key.strip():
            st.error("请填写 E Hunt API Key。")
            st.stop()
        if not search_key.strip():
            st.error("请填写搜索关键词或店铺名。")
            st.stop()

        with st.spinner("正在调用 eHunt 店铺查询接口..."):
            try:
                data = call_ehunt_stores_api(
                    api_base_url=api_base_url,
                    api_key=api_key,
                    search_key=search_key,
                    country=country,
                    category=category,
                    page_num=int(page_num),
                    page_size=int(page_size),
                    sort_by=int(sort_by),
                    desc=int(desc),
                )
            except Exception as e:
                st.error(f"调用失败：{e}")
                st.stop()

        store_rows = parse_ehunt_store_response(data)
        store_df = pd.DataFrame(store_rows)
        if store_df.empty:
            st.warning("没有查询到店铺数据。")
            st.stop()

        seed_df = pd.DataFrame(build_store_seed_rows(store_rows))
        st.success(f"查询成功，共返回 {len(store_df)} 条店铺数据。")
        st.subheader("店铺查询结果")
        st.dataframe(store_df, use_container_width=True)
        st.subheader("后续商品采集预备表")
        st.dataframe(seed_df, use_container_width=True)

        excel_bytes = make_excel_download({"店铺查询结果": store_df, "后续商品采集预备表": seed_df})
        st.download_button(
            label="下载 eHunt 店铺查询结果 Excel",
            data=excel_bytes,
            file_name="ehunt店铺查询结果.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    st.stop()


# =========================
# AI 批量生图页
# =========================

st.caption("上传 Excel → 并发提交任务 → 并发轮询 → 下载图片 → 打包 ZIP")

with st.sidebar:
    st.header("AI 生图设置")

    model = st.text_input("模型名称", value=DEFAULT_MODEL)
    default_size = st.selectbox(
        "默认比例 size",
        ["1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "5:4", "4:5", "2:1", "1:2", "21:9", "9:21", "auto"],
        index=1,
    )
    default_resolution = st.selectbox("默认分辨率 resolution", ["1k", "2k", "4k"], index=0)
    parallel_workers = st.number_input("并发数", min_value=1, max_value=20, value=6, step=1)
    max_wait_seconds = st.number_input("单张最大等待秒数", min_value=60, max_value=1200, value=300, step=30)
    first_delay_seconds = st.number_input("提交后首次查询等待秒数", min_value=3, max_value=30, value=12, step=1)
    interval_seconds = st.number_input("轮询间隔秒数", min_value=2, max_value=20, value=6, step=1)
    st.divider()
    global_style = st.text_area("统一风格要求", value="真实高级产品摄影，画面干净，主体突出，自然光线，真实材质，高级商业展示感。", height=120)
    st.divider()
    st.write("API 状态：")
    if APIMART_API_KEY:
        st.success("已检测到 APIMART_API_KEY")
    else:
        st.error("未检测到 APIMART_API_KEY")
    if get_aliyun_oss_bucket() is not None:
        st.success("已检测到阿里云 OSS 配置，图片会自动保存到 OSS")

uploaded_file = st.file_uploader("上传 Excel 文件", type=["xlsx", "xls"])
if uploaded_file is None:
    st.info("请先上传 Excel。若上传的是 eHunt 转换结果，请选择 `AI生图导入表` 这个 Sheet。")
    st.stop()

try:
    excel_file = pd.ExcelFile(uploaded_file)
    default_sheet = "AI生图导入表" if "AI生图导入表" in excel_file.sheet_names else excel_file.sheet_names[0]
    sheet_name = st.selectbox("选择工作表", excel_file.sheet_names, index=excel_file.sheet_names.index(default_sheet))
    df = pd.read_excel(excel_file, sheet_name=sheet_name)
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
    st.error("请先在 .env 或 Streamlit Secrets 中配置 APIMART_API_KEY。")
    st.stop()

if total_images <= 0:
    st.error("没有可生成的任务。")
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
            int(interval_seconds),
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

                object_key = make_oss_object_key(timestamp, img["filename"])
                oss_url = upload_bytes_to_aliyun_oss(img["image_bytes"], object_key, content_type="image/png")

                generated_files.append({"filename": img["filename"], "bytes": img["image_bytes"], "oss_url": oss_url})
                with result_area:
                    st.image(img["image_bytes"], caption=img["filename"], width=320)

                log_rows.append({
                    "编号": job["item_id"],
                    "产品名称": job["product_name"],
                    "状态": "成功",
                    "task_id": result["task_id"],
                    "文件名": img["filename"],
                    "APImart图片URL": img["image_url"],
                    "阿里云OSS保存地址": oss_url,
                    "错误信息": "",
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
                "APImart图片URL": "",
                "阿里云OSS保存地址": "",
                "错误信息": error_message,
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
    log_excel_bytes = log_excel_buffer.read()
    zip_file.writestr("生成日志.xlsx", log_excel_bytes)

    log_object_key = make_oss_object_key(timestamp, "生成日志.xlsx")
    upload_bytes_to_aliyun_oss(
        log_excel_bytes,
        log_object_key,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

zip_buffer.seek(0)
st.success("全部处理完成！")
st.download_button(
    label="📦 下载全部图片 ZIP",
    data=zip_buffer,
    file_name=f"批量生成图片结果_{timestamp}.zip",
    mime="application/zip",
)

if log_rows:
    st.subheader("生成日志")
    st.dataframe(pd.DataFrame(log_rows), use_container_width=True)

import requests
import pandas as pd


def call_ehunt_stores_api(
    api_base_url: str,
    api_key: str,
    search_key: str,
    country: str = "US",
    category: str = "",
    page_num: int = 1,
    page_size: int = 20,
    sort_by: int = 8,
    desc: int = 1,
):
    """
    调用 E Hunt 店铺查询接口。
    文档信息：
    POST /api/v1/stores
    Header: X-VIP-TOKEN
    """

    api_base_url = api_base_url.strip().rstrip("/")

    url = f"{api_base_url}/api/v1/stores"

    headers = {
        "Content-Type": "application/json",
        "X-VIP-TOKEN": api_key.strip(),
    }

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

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=60,
    )

    try:
        data = response.json()
    except Exception:
        raise RuntimeError(
            f"E Hunt API 返回不是 JSON。HTTP {response.status_code}，内容：{response.text[:500]}"
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"E Hunt API 请求失败。HTTP {response.status_code}，返回：{data}"
        )

    if data.get("code") not in [0, 200, "0", "200"]:
        raise RuntimeError(f"E Hunt API 返回错误：{data}")

    return data


def parse_ehunt_store_response(data: dict):
    """
    解析店铺查询接口返回。
    """
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


def build_store_ai_seed_rows(store_rows):
    """
    这个不是最终生图表，只是为了后续接商品查询接口预留。
    当前店铺接口没有商品图片，所以无法直接作为参考图。
    """
    rows = []

    for index, item in enumerate(store_rows, start=1):
        rows.append({
            "编号": f"{index:03d}",
            "店铺名称": item.get("店铺名称", ""),
            "店铺链接": item.get("店铺链接", ""),
            "商品数": item.get("商品数", ""),
            "总销量": item.get("总销量", ""),
            "收藏数": item.get("收藏数", ""),
            "备注": "当前为店铺数据，商品图片/视频需要继续接商品查询接口",
        })

    return rows
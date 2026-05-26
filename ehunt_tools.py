import re
import pandas as pd


def normalize_column_name(col):
    return str(col).strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def find_column(df, candidates):
    """
    根据候选关键词自动匹配列名。
    兼容中文、英文、大小写、空格差异。
    """
    normalized_map = {
        normalize_column_name(col): col
        for col in df.columns
    }

    for candidate in candidates:
        candidate_norm = normalize_column_name(candidate)

        for norm_col, original_col in normalized_map.items():
            if candidate_norm == norm_col:
                return original_col

    for candidate in candidates:
        candidate_norm = normalize_column_name(candidate)

        for norm_col, original_col in normalized_map.items():
            if candidate_norm in norm_col or norm_col in candidate_norm:
                return original_col

    return None


def split_links(value):
    """
    把一个单元格里的多个链接拆开。
    支持逗号、换行、分号、空格分隔。
    """
    if pd.isna(value):
        return []

    text = str(value).strip()
    if not text:
        return []

    # 从文本里直接提取 http 链接
    urls = re.findall(r"https?://[^\s,;，；]+", text)

    if urls:
        return list(dict.fromkeys(urls))

    # 如果没有正则匹配，就按常见分隔符切
    parts = re.split(r"[\n,，;；]+", text)
    links = []

    for part in parts:
        part = part.strip()
        if part.startswith("http"):
            links.append(part)

    return list(dict.fromkeys(links))


def read_ehunt_file(uploaded_file):
    """
    读取 eHunt 导出的 CSV / Excel。
    """
    filename = uploaded_file.name.lower()

    if filename.endswith(".csv"):
        try:
            return pd.read_csv(uploaded_file)
        except UnicodeDecodeError:
            uploaded_file.seek(0)
            return pd.read_csv(uploaded_file, encoding="utf-8-sig")
        except Exception:
            uploaded_file.seek(0)
            return pd.read_csv(uploaded_file, encoding="gbk")

    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        return pd.read_excel(uploaded_file)

    raise ValueError("只支持 CSV / XLSX / XLS 文件。")


def convert_ehunt_df(df, images_per_product=10):
    """
    把 eHunt 导出的表转换成标准 results 结构。
    """
    title_col = find_column(df, [
        "产品名称", "商品名称", "标题", "Title", "Product Title",
        "Product Name", "Listing Title", "Name"
    ])

    listing_url_col = find_column(df, [
        "商品链接", "产品链接", "Listing URL", "Listing Link",
        "Product URL", "Product Link", "URL", "Link"
    ])

    main_image_col = find_column(df, [
        "主图", "主图链接", "Main Image", "Main Image URL",
        "Image", "Image URL", "Thumbnail", "Photo"
    ])

    images_col = find_column(df, [
        "全部图片", "图片链接", "图片", "Images", "Image URLs",
        "All Images", "Product Images", "Photos"
    ])

    video_col = find_column(df, [
        "视频", "视频链接", "Videos", "Video", "Video URL",
        "Video URLs", "Product Videos"
    ])

    shop_col = find_column(df, [
        "店铺", "店铺名称", "Shop", "Shop Name", "Store", "Store Name"
    ])

    price_col = find_column(df, [
        "价格", "Price", "Sale Price"
    ])

    sales_col = find_column(df, [
        "销量", "总销量", "Total Sales", "Sales", "Sale Count"
    ])

    favorites_col = find_column(df, [
        "收藏", "收藏数", "Favorites", "Favorite Count", "Favourites"
    ])

    results = []

    for index, row in df.iterrows():
        title = str(row.get(title_col, "")).strip() if title_col else ""
        listing_url = str(row.get(listing_url_col, "")).strip() if listing_url_col else ""

        images = []

        if images_col:
            images.extend(split_links(row.get(images_col, "")))

        if main_image_col:
            main_images = split_links(row.get(main_image_col, ""))
            for img in main_images:
                if img not in images:
                    images.insert(0, img)

        images = [x for x in images if x.startswith("http")]
        images = list(dict.fromkeys(images))

        if images_per_product and images_per_product > 0:
            images = images[:images_per_product]

        videos = []
        if video_col:
            videos = split_links(row.get(video_col, ""))
            videos = [x for x in videos if x.startswith("http")]
            videos = list(dict.fromkeys(videos))

        shop_name = str(row.get(shop_col, "")).strip() if shop_col else ""
        price = str(row.get(price_col, "")).strip() if price_col else ""
        sales = str(row.get(sales_col, "")).strip() if sales_col else ""
        favorites = str(row.get(favorites_col, "")).strip() if favorites_col else ""

        # 没有图片和链接的空行跳过
        if not title and not listing_url and not images:
            continue

        results.append({
            "index": len(results) + 1,
            "title": title,
            "listing_url": listing_url,
            "main_image": images[0] if images else "",
            "images": images,
            "videos": videos,
            "shop_name": shop_name,
            "price": price,
            "sales": sales,
            "favorites": favorites,
            "status": "成功" if images or listing_url else "缺少图片或链接",
            "error": "",
        })

    return results


def build_ehunt_boss_rows(results):
    """
    老板查看表：一个商品下面列图片/视频，商品之间空一行。
    """
    rows = []

    for item in results:
        idx = item.get("index", "")
        title = item.get("title", "")
        listing_url = item.get("listing_url", "")

        rows.append({
            "编号": f"{int(idx):03d}" if str(idx).isdigit() else idx,
            "商品名称": title,
            "店铺名称": item.get("shop_name", ""),
            "价格": item.get("price", ""),
            "销量": item.get("sales", ""),
            "收藏": item.get("favorites", ""),
            "类型": "商品链接",
            "序号": "",
            "链接": listing_url,
            "状态": item.get("status", ""),
            "错误信息": item.get("error", ""),
        })

        for image_index, image_url in enumerate(item.get("images", []), start=1):
            rows.append({
                "编号": "",
                "商品名称": "",
                "店铺名称": "",
                "价格": "",
                "销量": "",
                "收藏": "",
                "类型": "图片",
                "序号": image_index,
                "链接": image_url,
                "状态": "",
                "错误信息": "",
            })

        for video_index, video_url in enumerate(item.get("videos", []), start=1):
            rows.append({
                "编号": "",
                "商品名称": "",
                "店铺名称": "",
                "价格": "",
                "销量": "",
                "收藏": "",
                "类型": "视频",
                "序号": video_index,
                "链接": video_url,
                "状态": "",
                "错误信息": "",
            })

        rows.append({
            "编号": "",
            "商品名称": "",
            "店铺名称": "",
            "价格": "",
            "销量": "",
            "收藏": "",
            "类型": "",
            "序号": "",
            "链接": "",
            "状态": "",
            "错误信息": "",
        })

    return rows


def build_ehunt_ai_rows(results):
    """
    AI 生图导入表：一行一个商品。
    """
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
            "视频链接": ", ".join(videos),
            "店铺名称": item.get("shop_name", ""),
            "价格": item.get("price", ""),
            "销量": item.get("sales", ""),
            "收藏": item.get("favorites", ""),
            "采集状态": item.get("status", ""),
            "错误信息": item.get("error", ""),
        })

    return rows
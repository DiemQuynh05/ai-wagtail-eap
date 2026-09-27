"""
Gemini AI - Chuyên gia Phân tích Kinh doanh Điện thoại.

Tính năng:
  1. analyze_sales_report()   - Báo cáo doanh thu, lợi nhuận dự kiến, dòng máy/thương hiệu bán chạy.
  2. analyze_inventory_alert() - Cảnh báo tồn kho: mẫu sắp hết cần nhập gấp, mẫu tồn lâu đọng vốn, kế hoạch nhập hàng.
  3. ask_business_assistant()  - Hỏi đáp tự do về tình hình kinh doanh dựa trên dữ liệu thật.

Số liệu được tính trước ở analytics.py; Gemini chỉ diễn giải và đưa khuyến nghị.
Khi chưa có GEMINI_API_KEY hoặc Gemini lỗi, hệ thống trả về phân tích theo luật (source="fallback")
để Dashboard vẫn hoạt động.
"""
import hashlib
import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from pydantic import BaseModel, Field

from .analytics import build_inventory_snapshot, build_sales_snapshot

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "Bạn là Chuyên gia Phân tích Kinh doanh của một cửa hàng/đại lý điện thoại di động tại Việt Nam. "
    "Luôn trả lời bằng tiếng Việt, ngắn gọn, rõ ràng, thực tế. "
    "CHỈ sử dụng số liệu có trong dữ liệu JSON được cung cấp, không tự bịa thêm số liệu. "
    "Tiền tệ là VNĐ, viết số có dấu chấm phân cách hàng nghìn (ví dụ 32.990.000 đ). "
    "Nếu dữ liệu quá ít để kết luận, hãy nói rõ điều đó."
)


# ---------- Cấu trúc JSON mà Gemini phải trả về ----------

class BrandInsight(BaseModel):
    brand: str
    revenue_share_pct: float
    comment: str


class ProductInsight(BaseModel):
    name: str
    units: int
    comment: str


class SalesAnalysis(BaseModel):
    headline: str = Field(description="Một câu tóm tắt quan trọng nhất")
    summary: str = Field(description="Đoạn tổng quan 3-5 câu về doanh thu, lợi nhuận, tỷ suất")
    hot_brands: list[BrandInsight]
    hot_products: list[ProductInsight]
    insights: list[str] = Field(description="Các nhận định đáng chú ý")
    risks: list[str] = Field(description="Rủi ro kinh doanh cần lưu ý")
    recommendations: list[str] = Field(description="Hành động cụ thể nên làm")


class RestockItem(BaseModel):
    name: str
    sku: str
    current_stock: int
    suggested_qty: int
    priority: str = Field(description="CAO, TRUNG_BINH hoặc THAP")
    reason: str


class OverstockItem(BaseModel):
    name: str
    sku: str
    current_stock: int
    reason: str
    suggestion: str


class InventoryAnalysis(BaseModel):
    headline: str
    summary: str
    urgent_restock: list[RestockItem]
    overstock: list[OverstockItem]
    budget_comment: str = Field(description="Nhận xét về tổng ngân sách nhập hàng và vốn đang đọng")
    recommendations: list[str]


# ---------- Gọi Gemini ----------

class AIUnavailable(Exception):
    pass


def is_ai_configured():
    return bool(getattr(settings, "GEMINI_API_KEY", ""))


def gemini_model():
    return getattr(settings, "GEMINI_MODEL", "gemini-3.8-flash")


def _call_gemini(prompt, schema=None):
    """Gọi Gemini. Có schema => trả về dict theo schema, không có => trả về văn bản."""
    if not is_ai_configured():
        raise AIUnavailable("Chưa cấu hình GEMINI_API_KEY trong file .env")
    try:
        from google import genai
        from google.genai import errors, types
    except ImportError as exc:
        raise AIUnavailable("Chưa cài thư viện google-genai (pip install google-genai)") from exc

    config = {
        "system_instruction": SYSTEM_INSTRUCTION,
        "temperature": 0.4,
        "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
    }
    if schema:
        config.update(response_mime_type="application/json", response_schema=schema)
    config = types.GenerateContentConfig(**config)

    try:
        client = genai.Client(
            api_key=settings.GEMINI_API_KEY,
            http_options=types.HttpOptions(
                timeout=getattr(settings, "GEMINI_TIMEOUT_MS", 60000),
                # Tự thử lại khi Gemini quá tải (503) hoặc vượt giới hạn tạm thời (429)
                retry_options=types.HttpRetryOptions(
                    attempts=3, initial_delay=2, max_delay=10, http_status_codes=[429, 500, 503]
                ),
            ),
        )
        response = client.models.generate_content(model=gemini_model(), contents=prompt, config=config)
    except errors.APIError as exc:
        logger.warning("Gemini API error %s: %s", exc.code, exc.message)
        raise AIUnavailable(f"Lỗi khi gọi Gemini ({exc.code} {exc.status}): {exc.message}") from exc
    except Exception as exc:
        logger.warning("Gemini request failed: %s", exc)
        raise AIUnavailable(f"Không kết nối được Gemini: {exc}") from exc

    if not schema:
        if not response.text:
            raise AIUnavailable("Gemini không trả về nội dung")
        return response.text.strip()
    if isinstance(response.parsed, BaseModel):
        return response.parsed.model_dump()
    try:
        return schema.model_validate_json(response.text).model_dump()
    except Exception as exc:
        raise AIUnavailable("Gemini trả về JSON không hợp lệ") from exc


def _cache_key(feature, payload):
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return f"phone_erp:ai:{feature}:{gemini_model()}:{digest}"


def _run(feature, metrics, prompt, schema, fallback, refresh=False):
    """Khung chung: cache -> Gemini -> fallback nếu lỗi."""
    key = _cache_key(feature, [metrics, prompt])
    result = {
        "feature": feature,
        "generated_at": timezone.now().isoformat(),
        "model": gemini_model(),
        "cached": False,
        "warning": None,
        "metrics": metrics,
    }

    if not refresh and (cached := cache.get(key)):
        return {**result, **cached, "cached": True}

    try:
        analysis = _call_gemini(prompt, schema)
    except AIUnavailable as exc:
        return {**result, "source": "fallback", "model": None, "warning": str(exc), "analysis": fallback(metrics)}

    stored = {"source": "gemini", "generated_at": result["generated_at"], "analysis": analysis}
    cache.set(key, stored, getattr(settings, "AI_CACHE_SECONDS", 600))
    return {**result, **stored}


def _as_json(data):
    return json.dumps(data, ensure_ascii=False, indent=1)


def _vnd(amount):
    return f"{amount:,.0f}".replace(",", ".") + " đ"


# ---------- 1. Báo cáo doanh thu & dòng máy bán chạy ----------

def _sales_prompt(metrics):
    period = f"{metrics['period_days']} ngày gần nhất" if metrics["period_days"] else "toàn bộ thời gian"
    return (
        f"Phân tích tình hình bán điện thoại trong {period}.\n"
        "Yêu cầu:\n"
        "- Tổng kết doanh thu, lợi nhuận dự kiến (lợi nhuận tính theo giá nhập hiện tại), tỷ suất lợi nhuận.\n"
        "- Đánh giá thương hiệu nào đang 'hot' nhất, chiếm bao nhiêu % doanh số (ví dụ: Apple chiếm 60% doanh thu).\n"
        "- Chỉ ra 3-5 mẫu máy bán chạy nhất và nhận xét ngắn.\n"
        "- Nêu rủi ro (phụ thuộc 1 thương hiệu, nhiều đơn hủy, biên lợi nhuận thấp...) và khuyến nghị hành động.\n\n"
        f"DỮ LIỆU:\n{_as_json(metrics)}"
    )


def _sales_fallback(m):
    s = m["summary"]
    brands, products = m["by_brand"], m["top_products"]
    if not s["completed_orders"]:
        return {
            "headline": "Chưa có đơn hàng hoàn thành để phân tích.",
            "summary": "Hãy nhập đơn hàng trong Wagtail Admin để có số liệu phân tích.",
            "hot_brands": [], "hot_products": [], "insights": [], "risks": [], "recommendations": [],
        }

    top_brand = brands[0]
    insights = [f"Giá trị trung bình mỗi đơn: {_vnd(s['avg_order_value'])}."]
    if s["cancelled_orders"]:
        insights.append(f"Có {s['cancelled_orders']}/{s['total_orders']} đơn bị hủy.")
    risks = []
    if top_brand["revenue_share_pct"] >= 60:
        risks.append(f"Doanh thu phụ thuộc lớn vào {top_brand['brand']} ({top_brand['revenue_share_pct']}%).")
    if s["profit_margin_pct"] < 10:
        risks.append(f"Tỷ suất lợi nhuận thấp ({s['profit_margin_pct']}%).")

    return {
        "headline": f"{top_brand['brand']} dẫn đầu với {top_brand['revenue_share_pct']}% doanh thu.",
        "summary": (
            f"Đã bán {s['units_sold']} máy qua {s['completed_orders']} đơn hoàn thành, doanh thu {_vnd(s['revenue'])}, "
            f"lợi nhuận dự kiến {_vnd(s['estimated_profit'])} (tỷ suất {s['profit_margin_pct']}%)."
        ),
        "hot_brands": [
            {"brand": b["brand"], "revenue_share_pct": b["revenue_share_pct"],
             "comment": f"{b['units']} máy, doanh thu {_vnd(b['revenue'])}"}
            for b in brands[:5]
        ],
        "hot_products": [
            {"name": p["name"], "units": p["units"], "comment": f"Doanh thu {_vnd(p['revenue'])}"}
            for p in products[:5]
        ],
        "insights": insights,
        "risks": risks,
        "recommendations": [
            f"Ưu tiên đảm bảo nguồn hàng cho {products[0]['name']} - mẫu bán chạy nhất.",
            "Đây là phân tích tự động theo luật; phân tích chi tiết từ AI chưa khả dụng (xem warning).",
        ],
    }


def analyze_sales_report(days=None, refresh=False):
    metrics = build_sales_snapshot(days)
    return _run("sales_report", metrics, _sales_prompt(metrics), SalesAnalysis, _sales_fallback, refresh)


# ---------- 2. Cảnh báo tồn kho & dự báo nhập hàng ----------

def _inventory_prompt(metrics):
    return (
        f"Đánh giá kho điện thoại dựa trên tốc độ bán trong {metrics['analysis_window_days']} ngày gần nhất.\n"
        "Giải thích các trường: status = OUT_OF_STOCK (hết hàng), LOW_STOCK (sắp hết), "
        "SLOW_MOVING (tồn lâu/bán chậm), OK; days_of_cover = số ngày bán được với tồn kho hiện tại "
        "(null = không bán được máy nào); suggested_reorder_qty = số lượng hệ thống đề xuất nhập.\n"
        "Yêu cầu:\n"
        "- urgent_restock: các mẫu hết/sắp hết hàng cần nhập gấp để tránh mất khách, "
        "dùng suggested_reorder_qty làm số lượng đề xuất (có thể điều chỉnh nếu có lý do), xếp theo mức ưu tiên.\n"
        "- overstock: các mẫu tồn kho quá lâu gây đọng vốn, kèm gợi ý xử lý (khuyến mãi, combo, bán cho đại lý...).\n"
        "- Nhận xét ngân sách nhập hàng (total_reorder_cost) và vốn đang đọng (slow_moving_capital).\n\n"
        f"DỮ LIỆU:\n{_as_json(metrics)}"
    )


def _inventory_fallback(m):
    s, products = m["summary"], m["products"]
    priority = {"OUT_OF_STOCK": "CAO", "LOW_STOCK": "TRUNG_BINH"}
    restock = sorted(
        (p for p in products if p["status"] in priority),
        key=lambda p: (p["status"] != "OUT_OF_STOCK", p["stock"]),
    )
    slow = sorted((p for p in products if p["status"] == "SLOW_MOVING"), key=lambda p: -p["stock_value_at_cost"])

    return {
        "headline": (
            f"{s['out_of_stock_count']} mẫu hết hàng, {s['low_stock_count']} mẫu sắp hết, "
            f"{s['slow_moving_count']} mẫu tồn lâu."
        ),
        "summary": (
            f"Kho có {s['total_models']} mẫu, {s['total_units_in_stock']} máy, "
            f"giá trị theo giá nhập {_vnd(s['stock_value_at_cost'])}."
        ),
        "urgent_restock": [
            {"name": p["name"], "sku": p["sku"], "current_stock": p["stock"],
             "suggested_qty": p["suggested_reorder_qty"], "priority": priority[p["status"]],
             "reason": "Đã hết hàng" if p["status"] == "OUT_OF_STOCK"
             else f"Tồn {p['stock']} máy, ngưỡng cảnh báo {p['min_limit']}"}
            for p in restock
        ],
        "overstock": [
            {"name": p["name"], "sku": p["sku"], "current_stock": p["stock"],
             "reason": f"Bán {p['units_sold_window']} máy trong {m['analysis_window_days']} ngày",
             "suggestion": "Cân nhắc khuyến mãi hoặc bán sỉ cho đại lý"}
            for p in slow
        ],
        "budget_comment": (
            f"Cần khoảng {_vnd(s['total_reorder_cost'])} để nhập {s['total_reorder_units']} máy; "
            f"vốn đang đọng ở hàng tồn lâu: {_vnd(s['slow_moving_capital'])}."
        ),
        "recommendations": ["Đây là phân tích tự động theo luật; phân tích chi tiết từ AI chưa khả dụng (xem warning)."],
    }


def analyze_inventory_alert(days=30, refresh=False):
    metrics = build_inventory_snapshot(days)
    return _run("inventory_alert", metrics, _inventory_prompt(metrics), InventoryAnalysis, _inventory_fallback, refresh)


# ---------- 3. Trợ lý hỏi đáp kinh doanh ----------

MAX_QUESTION_LENGTH = 500


def ask_business_assistant(question, days=30):
    question = (question or "").strip()[:MAX_QUESTION_LENGTH]
    metrics = {"sales": build_sales_snapshot(days), "inventory": build_inventory_snapshot(days)}
    prompt = (
        "Trả lời câu hỏi của chủ cửa hàng dựa trên dữ liệu kinh doanh bên dưới. "
        "Trả lời tối đa khoảng 200 từ, có thể dùng gạch đầu dòng. "
        "Nếu câu hỏi không liên quan đến kinh doanh cửa hàng điện thoại, hãy từ chối lịch sự.\n\n"
        f"DỮ LIỆU:\n{_as_json(metrics)}\n\nCÂU HỎI: {question}"
    )

    def fallback(m):
        s = m["sales"]["summary"]
        return (
            "Trợ lý AI chưa sẵn sàng nên chưa trả lời được câu hỏi này. Số liệu nhanh: "
            f"doanh thu {_vnd(s['revenue'])}, đã bán {s['units_sold']} máy, "
            f"{m['inventory']['summary']['low_stock_count'] + m['inventory']['summary']['out_of_stock_count']} "
            "mẫu cần nhập thêm."
        )

    result = _run("ask", metrics, prompt, None, fallback)
    result["question"] = question
    return result

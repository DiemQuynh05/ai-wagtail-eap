"""
Tầng tính toán số liệu (không dùng AI).

Mọi con số (doanh thu, lợi nhuận, tồn kho, số ngày bán...) đều được tính chính xác
từ CSDL tại đây, sau đó mới gửi cho Gemini để diễn giải. Nhờ vậy AI không tự "bịa" số.
"""
import math
from datetime import timedelta

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import Order, PhoneProduct

# Số ngày dự kiến từ lúc đặt hàng đến lúc hàng về kho
RESTOCK_LEAD_TIME_DAYS = 7
# Lượng hàng mục tiêu sau khi nhập: đủ bán trong bao nhiêu ngày
TARGET_COVER_DAYS = 30
# Tồn kho đủ bán quá số ngày này => coi là tồn lâu, đọng vốn
SLOW_MOVING_COVER_DAYS = 90

COST_EXPR = ExpressionWrapper(
    F("quantity") * F("product__import_price"),
    output_field=DecimalField(max_digits=16, decimal_places=0),
)


def _int(value):
    return int(value or 0)


def _pct(part, total):
    return round(part * 100 / total, 1) if total else 0.0


def _orders_in_window(days):
    qs = Order.objects.all()
    if days:
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=days))
    return qs


def build_sales_snapshot(days=None):
    """Tổng hợp doanh thu, lợi nhuận dự kiến, thương hiệu và mẫu máy bán chạy."""
    orders = _orders_in_window(days)
    completed = orders.filter(status="COMPLETED")

    status_counts = {row["status"]: row["n"] for row in orders.values("status").annotate(n=Count("id"))}
    totals = completed.aggregate(revenue=Sum("total_price"), cost=Sum(COST_EXPR), units=Sum("quantity"))
    revenue, cost, units = _int(totals["revenue"]), _int(totals["cost"]), _int(totals["units"])
    completed_orders = status_counts.get("COMPLETED", 0)

    by_brand = [
        {
            "brand": row["product__brand"],
            "units": _int(row["units"]),
            "revenue": _int(row["revenue"]),
            "profit": _int(row["revenue"]) - _int(row["cost"]),
            "revenue_share_pct": _pct(_int(row["revenue"]), revenue),
        }
        for row in completed.values("product__brand")
        .annotate(units=Sum("quantity"), revenue=Sum("total_price"), cost=Sum(COST_EXPR))
        .order_by("-revenue")
    ]

    top_products = [
        {
            "name": row["product__name"],
            "brand": row["product__brand"],
            "sku": row["product__sku"],
            "units": _int(row["units"]),
            "revenue": _int(row["revenue"]),
            "profit": _int(row["revenue"]) - _int(row["cost"]),
            "revenue_share_pct": _pct(_int(row["revenue"]), revenue),
        }
        for row in completed.values("product__name", "product__brand", "product__sku")
        .annotate(units=Sum("quantity"), revenue=Sum("total_price"), cost=Sum(COST_EXPR))
        .order_by("-units", "-revenue")[:10]
    ]

    top_customers = [
        {"name": row["customer__full_name"], "orders": row["n"], "revenue": _int(row["revenue"])}
        for row in completed.values("customer__full_name")
        .annotate(n=Count("id"), revenue=Sum("total_price"))
        .order_by("-revenue")[:5]
    ]

    daily_revenue = [
        {"date": row["day"].isoformat(), "revenue": _int(row["revenue"]), "units": _int(row["units"])}
        for row in completed.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(revenue=Sum("total_price"), units=Sum("quantity"))
        .order_by("day")
    ]

    return {
        "period_days": days,
        "summary": {
            "total_orders": sum(status_counts.values()),
            "completed_orders": completed_orders,
            "pending_orders": status_counts.get("PENDING", 0),
            "cancelled_orders": status_counts.get("CANCELLED", 0),
            "units_sold": units,
            "revenue": revenue,
            "estimated_cost": cost,
            "estimated_profit": revenue - cost,
            "profit_margin_pct": _pct(revenue - cost, revenue),
            "avg_order_value": round(revenue / completed_orders) if completed_orders else 0,
        },
        "by_brand": by_brand,
        "top_products": top_products,
        "top_customers": top_customers,
        "daily_revenue": daily_revenue,
    }


def _classify_product(stock, min_limit, units_sold, days_of_cover):
    if stock <= 0:
        return "OUT_OF_STOCK"
    if stock <= min_limit or (days_of_cover is not None and days_of_cover < RESTOCK_LEAD_TIME_DAYS):
        return "LOW_STOCK"
    if units_sold == 0 or (days_of_cover is not None and days_of_cover > SLOW_MOVING_COVER_DAYS):
        return "SLOW_MOVING"
    return "OK"


def build_inventory_snapshot(days=30):
    """Đánh giá từng mẫu máy: sắp hết hàng, hết hàng, tồn lâu, số lượng nên nhập."""
    days = days or 30
    sold = {
        row["product_id"]: _int(row["units"])
        for row in _orders_in_window(days)
        .filter(status="COMPLETED")
        .values("product_id")
        .annotate(units=Sum("quantity"))
    }

    products = []
    for p in PhoneProduct.objects.order_by("brand", "name"):
        units_sold = sold.get(p.pk, 0)
        avg_daily = units_sold / days
        days_of_cover = round(p.stock_quantity / avg_daily, 1) if avg_daily else None
        status = _classify_product(p.stock_quantity, p.min_warning_limit, units_sold, days_of_cover)

        reorder_qty = 0
        if status in ("OUT_OF_STOCK", "LOW_STOCK"):
            target = max(p.min_warning_limit * 2, math.ceil(avg_daily * TARGET_COVER_DAYS))
            reorder_qty = max(target - p.stock_quantity, 0)

        products.append({
            "id": p.pk,
            "name": p.name,
            "brand": p.brand,
            "sku": p.sku,
            "stock": p.stock_quantity,
            "min_limit": p.min_warning_limit,
            "import_price": _int(p.import_price),
            "selling_price": _int(p.selling_price),
            "units_sold_window": units_sold,
            "avg_daily_sales": round(avg_daily, 2),
            "days_of_cover": days_of_cover,
            "status": status,
            "suggested_reorder_qty": reorder_qty,
            "reorder_cost": reorder_qty * _int(p.import_price),
            "stock_value_at_cost": p.stock_quantity * _int(p.import_price),
        })

    def count(status):
        return sum(1 for p in products if p["status"] == status)

    slow = [p for p in products if p["status"] == "SLOW_MOVING"]
    return {
        "analysis_window_days": days,
        "rules": {
            "restock_lead_time_days": RESTOCK_LEAD_TIME_DAYS,
            "target_cover_days": TARGET_COVER_DAYS,
            "slow_moving_cover_days": SLOW_MOVING_COVER_DAYS,
        },
        "summary": {
            "total_models": len(products),
            "total_units_in_stock": sum(p["stock"] for p in products),
            "stock_value_at_cost": sum(p["stock_value_at_cost"] for p in products),
            "stock_value_at_retail": sum(p["stock"] * p["selling_price"] for p in products),
            "out_of_stock_count": count("OUT_OF_STOCK"),
            "low_stock_count": count("LOW_STOCK"),
            "slow_moving_count": len(slow),
            "slow_moving_capital": sum(p["stock_value_at_cost"] for p in slow),
            "total_reorder_units": sum(p["suggested_reorder_qty"] for p in products),
            "total_reorder_cost": sum(p["reorder_cost"] for p in products),
        },
        "products": products,
    }


def build_dashboard_stats():
    """Số liệu cho các Card thống kê trên Dashboard (không gọi AI)."""
    sales = build_sales_snapshot()["summary"]
    low_stock = [p for p in PhoneProduct.objects.all() if p.stock_quantity <= p.min_warning_limit]
    return {
        "total_revenue": sales["revenue"],
        "estimated_profit": sales["estimated_profit"],
        "units_sold": sales["units_sold"],
        "completed_orders": sales["completed_orders"],
        "pending_orders": sales["pending_orders"],
        "total_models": PhoneProduct.objects.count(),
        "total_units_in_stock": _int(PhoneProduct.objects.aggregate(s=Sum("stock_quantity"))["s"]),
        "low_stock_count": len(low_stock),
        "low_stock_products": [
            {"id": p.pk, "name": p.name, "brand": p.brand, "stock": p.stock_quantity, "min_limit": p.min_warning_limit}
            for p in low_stock
        ],
    }

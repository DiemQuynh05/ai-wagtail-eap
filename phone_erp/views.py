"""
API JSON cho Frontend Dashboard.

AI (Gemini):
  GET  /api/ai/status/            - Kiểm tra AI đã cấu hình chưa
  GET  /api/ai/sales-report/      - AI Báo cáo doanh thu & dòng máy bán chạy  (?days=30&refresh=1)
  GET  /api/ai/inventory-alert/   - AI Cảnh báo tồn kho & dự báo nhập hàng     (?days=30&refresh=1)
  POST /api/ai/ask/               - Hỏi đáp tự do {"question": "..."}

Dữ liệu Dashboard (không gọi AI):
  GET  /api/dashboard/stats/      - Số liệu cho Card thống kê
  GET  /api/products/             - Danh sách tồn kho (?brand=Apple&group_by=brand)
  GET  /api/customers/            - Danh sách khách hàng (cho form tạo đơn)
  GET  /api/orders/               - Đơn hàng gần nhất (?limit=20)
  POST /api/orders/               - Tạo đơn hàng {"customer_id", "product_id", "quantity", "status"}
"""
import json
from functools import wraps

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import ai_services
from .analytics import build_dashboard_stats, build_inventory_snapshot
from .models import Customer, Order, PhoneProduct

MAX_DAYS = 3650
MAX_ORDERS_LIMIT = 200


class BadRequest(Exception):
    pass


def api_view(func):
    """Chuyển BadRequest thành HTTP 400 và bọc kết quả trong JsonResponse."""
    @wraps(func)
    def wrapper(request, *args, **kwargs):
        try:
            data, status = func(request, *args, **kwargs), 200
            if isinstance(data, tuple):
                data, status = data
        except BadRequest as exc:
            data, status = {"error": str(exc)}, 400
        return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})
    return wrapper


def _int_param(value, name, default=None, minimum=1, maximum=MAX_DAYS):
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"Tham số '{name}' phải là số nguyên")
    if not minimum <= number <= maximum:
        raise BadRequest(f"Tham số '{name}' phải nằm trong khoảng {minimum}-{maximum}")
    return number


def _refresh(request):
    return request.GET.get("refresh") in ("1", "true", "yes")


def _json_body(request):
    try:
        body = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise BadRequest("Body phải là JSON hợp lệ")
    if not isinstance(body, dict):
        raise BadRequest("Body phải là một JSON object")
    return body


# ---------- AI ----------

@require_GET
@api_view
def ai_status(request):
    return {"configured": ai_services.is_ai_configured(), "model": ai_services.gemini_model()}


@require_GET
@api_view
def ai_sales_report(request):
    days = _int_param(request.GET.get("days"), "days")
    return ai_services.analyze_sales_report(days=days, refresh=_refresh(request))


@require_GET
@api_view
def ai_inventory_alert(request):
    days = _int_param(request.GET.get("days"), "days", default=30)
    return ai_services.analyze_inventory_alert(days=days, refresh=_refresh(request))


@require_POST
@api_view
def ai_ask(request):
    body = _json_body(request)
    question = str(body.get("question", "")).strip()
    if not question:
        raise BadRequest("Vui lòng nhập câu hỏi")
    if len(question) > ai_services.MAX_QUESTION_LENGTH:
        raise BadRequest(f"Câu hỏi tối đa {ai_services.MAX_QUESTION_LENGTH} ký tự")
    days = _int_param(body.get("days"), "days", default=30)
    return ai_services.ask_business_assistant(question, days=days)


# ---------- Dữ liệu Dashboard ----------

@require_GET
def dashboard_page(request):
    return render(request, "phone_erp/dashboard.html")

@require_GET
@api_view
def dashboard_stats(request):
    return build_dashboard_stats()


@require_GET
@api_view
def product_list(request):
    snapshot = build_inventory_snapshot(_int_param(request.GET.get("days"), "days", default=30))
    products = snapshot["products"]
    brand = request.GET.get("brand", "").strip()
    if brand:
        products = [p for p in products if p["brand"].lower() == brand.lower()]

    data = {
        "count": len(products),
        "brands": sorted({p["brand"] for p in snapshot["products"]}),
        "products": products,
    }
    if request.GET.get("group_by") == "brand":
        groups = {}
        for p in products:
            groups.setdefault(p["brand"], []).append(p)
        data["groups"] = [
            {"brand": b, "total_stock": sum(p["stock"] for p in items), "products": items}
            for b, items in groups.items()
        ]
    return data


@require_GET
@api_view
def customer_list(request):
    customers = Customer.objects.order_by("full_name").values("id", "full_name", "phone", "email")
    return {"count": len(customers), "customers": list(customers)}


def _serialize_order(order):
    return {
        "id": order.pk,
        "customer": {"id": order.customer_id, "name": order.customer.full_name},
        "product": {"id": order.product_id, "name": order.product.name, "brand": order.product.brand},
        "quantity": order.quantity,
        "total_price": int(order.total_price),
        "status": order.status,
        "status_display": order.get_status_display(),
        "created_at": order.created_at.isoformat(),
    }


def _create_order(request):
    body = _json_body(request)
    customer_id = _int_param(body.get("customer_id"), "customer_id", maximum=2**31)
    product_id = _int_param(body.get("product_id"), "product_id", maximum=2**31)
    quantity = _int_param(body.get("quantity"), "quantity", default=1, maximum=10_000)
    status = body.get("status") or "COMPLETED"
    if not customer_id or not product_id:
        raise BadRequest("Thiếu customer_id hoặc product_id")
    if status not in dict(Order.STATUS_CHOICES):
        raise BadRequest(f"Trạng thái không hợp lệ: {status}")

    try:
        customer = Customer.objects.get(pk=customer_id)
    except Customer.DoesNotExist:
        raise BadRequest("Không tìm thấy khách hàng")

    try:
        with transaction.atomic():
            product = PhoneProduct.objects.select_for_update().get(pk=product_id)
            order = Order(customer=customer, product=product, quantity=quantity, status=status)
            order.save()  # Order.save() tự tính tổng tiền và trừ tồn kho
    except PhoneProduct.DoesNotExist:
        raise BadRequest("Không tìm thấy sản phẩm")
    except ValidationError as exc:
        raise BadRequest(" ".join(exc.messages))

    return {
        "message": f"Đã tạo đơn #{order.pk}",
        "order": _serialize_order(order),
        "remaining_stock": product.stock_quantity,
    }, 201


@require_http_methods(["GET", "POST"])
@api_view
def orders(request):
    if request.method == "POST":
        return _create_order(request)
    limit = _int_param(request.GET.get("limit"), "limit", default=20, maximum=MAX_ORDERS_LIMIT)
    qs = Order.objects.select_related("customer", "product").order_by("-created_at")[:limit]
    return {"orders": [_serialize_order(o) for o in qs]}

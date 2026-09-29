from django.urls import path
from . import views

app_name = "phone_erp"

urlpatterns = [
    # AI Gemini
    path("ai/status/", views.ai_status, name="ai_status"),
    path("ai/sales-report/", views.ai_sales_report, name="ai_sales_report"),
    path("ai/inventory-alert/", views.ai_inventory_alert, name="ai_inventory_alert"),
    path("ai/ask/", views.ai_ask, name="ai_ask"),
    # Dữ liệu Dashboard
    path("dashboard/", views.dashboard_page, name="dashboard_page"), path("dashboard/stats/", views.dashboard_stats, name="dashboard_stats"),
    path("products/", views.product_list, name="product_list"),
    path("customers/", views.customer_list, name="customer_list"),
    path("orders/", views.orders, name="orders"),
]

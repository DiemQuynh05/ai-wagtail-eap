import json
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from . import ai_services
from .analytics import build_inventory_snapshot, build_sales_snapshot
from .models import Customer, Order, PhoneProduct


class PhoneErpTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.iphone = PhoneProduct.objects.create(
            name="iPhone 15 128GB", brand="Apple", sku="IP15-128",
            import_price=18_000_000, selling_price=20_000_000, stock_quantity=10, min_warning_limit=3,
        )
        self.samsung = PhoneProduct.objects.create(
            name="Galaxy S24 Ultra", brand="Samsung", sku="S24U",
            import_price=25_000_000, selling_price=30_000_000, stock_quantity=4, min_warning_limit=3,
        )
        self.xiaomi = PhoneProduct.objects.create(
            name="Xiaomi 14", brand="Xiaomi", sku="XM14",
            import_price=12_000_000, selling_price=15_000_000, stock_quantity=20, min_warning_limit=3,
        )
        self.customer = Customer.objects.create(full_name="Nguyễn Văn A", phone="0900000000")
        Order.objects.create(customer=self.customer, product=self.iphone, quantity=5)
        Order.objects.create(customer=self.customer, product=self.samsung, quantity=1)
        Order.objects.create(customer=self.customer, product=self.iphone, quantity=1, status="PENDING")


class AnalyticsTests(PhoneErpTestBase):
    def test_sales_snapshot(self):
        s = build_sales_snapshot()
        self.assertEqual(s["summary"]["revenue"], 130_000_000)
        self.assertEqual(s["summary"]["estimated_profit"], 130_000_000 - 115_000_000)
        self.assertEqual(s["summary"]["units_sold"], 6)
        self.assertEqual(s["summary"]["pending_orders"], 1)
        self.assertEqual(s["by_brand"][0]["brand"], "Apple")
        self.assertEqual(s["by_brand"][0]["revenue_share_pct"], 76.9)

    def test_inventory_snapshot_classification(self):
        products = {p["sku"]: p for p in build_inventory_snapshot(30)["products"]}
        self.assertEqual(products["S24U"]["stock"], 3)
        self.assertEqual(products["S24U"]["status"], "LOW_STOCK")
        self.assertEqual(products["S24U"]["suggested_reorder_qty"], 3)
        self.assertEqual(products["XM14"]["status"], "SLOW_MOVING")
        self.assertEqual(products["IP15-128"]["status"], "OK")


@override_settings(GEMINI_API_KEY="")
class FallbackTests(PhoneErpTestBase):
    def test_sales_report_fallback_without_key(self):
        result = ai_services.analyze_sales_report()
        self.assertEqual(result["source"], "fallback")
        self.assertIn("GEMINI_API_KEY", result["warning"])
        self.assertIn("Apple", result["analysis"]["headline"])

    def test_inventory_fallback(self):
        analysis = ai_services.analyze_inventory_alert()["analysis"]
        self.assertEqual([i["sku"] for i in analysis["urgent_restock"]], ["S24U"])
        self.assertEqual([i["sku"] for i in analysis["overstock"]], ["XM14"])


@override_settings(GEMINI_API_KEY="test-key")
class GeminiTests(PhoneErpTestBase):
    FAKE_SALES = {
        "headline": "Apple dẫn đầu", "summary": "ok", "hot_brands": [], "hot_products": [],
        "insights": [], "risks": [], "recommendations": [],
    }

    @mock.patch("phone_erp.ai_services._call_gemini")
    def test_result_is_cached(self, call):
        call.return_value = (self.FAKE_SALES, "gemini-test")
        first = ai_services.analyze_sales_report()
        second = ai_services.analyze_sales_report()
        self.assertEqual(first["source"], "gemini")
        self.assertTrue(second["cached"])
        self.assertEqual(call.call_count, 1)

        ai_services.analyze_sales_report(refresh=True)
        self.assertEqual(call.call_count, 2)

    @mock.patch("phone_erp.ai_services._call_gemini")
    def test_gemini_error_falls_back(self, call):
        call.side_effect = ai_services.AIUnavailable("quota exceeded")
        result = ai_services.analyze_sales_report()
        self.assertEqual(result["source"], "fallback")
        self.assertEqual(result["warning"], "quota exceeded")

    @staticmethod
    def _quota_error():
        from google.genai import errors
        return errors.APIError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})

    @override_settings(GEMINI_MODEL="model-a", GEMINI_FALLBACK_MODELS=["model-b"])
    @mock.patch("google.genai.Client")
    def test_switches_model_when_quota_exceeded(self, client_cls):
        response = mock.Mock(parsed=None, text=json.dumps(self.FAKE_SALES))
        generate = client_cls.return_value.models.generate_content
        generate.side_effect = [self._quota_error(), response]

        result = ai_services.analyze_sales_report()
        self.assertEqual(result["source"], "gemini")
        self.assertEqual(result["model"], "model-b")
        self.assertEqual([c.kwargs["model"] for c in generate.call_args_list], ["model-a", "model-b"])

    @override_settings(GEMINI_MODEL="model-a", GEMINI_FALLBACK_MODELS=["model-b"])
    @mock.patch("google.genai.Client")
    def test_all_models_exhausted(self, client_cls):
        client_cls.return_value.models.generate_content.side_effect = [self._quota_error(), self._quota_error()]
        result = ai_services.analyze_sales_report()
        self.assertEqual(result["source"], "fallback")
        self.assertIn("model-a: hết lượt gọi miễn phí", result["warning"])
        self.assertIn("model-b: hết lượt gọi miễn phí", result["warning"])


@override_settings(GEMINI_API_KEY="")
class ApiTests(PhoneErpTestBase):
    def test_ai_endpoints(self):
        for name in ("ai_status", "ai_sales_report", "ai_inventory_alert"):
            response = self.client.get(reverse(f"phone_erp:{name}"))
            self.assertEqual(response.status_code, 200, name)

    def test_invalid_days(self):
        response = self.client.get(reverse("phone_erp:ai_sales_report"), {"days": "abc"})
        self.assertEqual(response.status_code, 400)

    def test_ask_requires_question(self):
        url = reverse("phone_erp:ai_ask")
        self.assertEqual(self.client.post(url, {}, content_type="application/json").status_code, 400)
        response = self.client.post(url, {"question": "Doanh thu bao nhiêu?"}, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json()["analysis"], str)

    def test_dashboard_stats(self):
        data = self.client.get(reverse("phone_erp:dashboard_stats")).json()
        self.assertEqual(data["total_revenue"], 130_000_000)
        self.assertEqual(data["units_sold"], 6)
        self.assertEqual(data["low_stock_count"], 1)

    def test_products_grouped_by_brand(self):
        data = self.client.get(reverse("phone_erp:product_list"), {"group_by": "brand"}).json()
        self.assertEqual(data["brands"], ["Apple", "Samsung", "Xiaomi"])
        self.assertEqual(len(data["groups"]), 3)
        apple = self.client.get(reverse("phone_erp:product_list"), {"brand": "apple"}).json()
        self.assertEqual(apple["count"], 1)

    def test_create_order_deducts_stock(self):
        url = reverse("phone_erp:orders")
        payload = {"customer_id": self.customer.pk, "product_id": self.iphone.pk, "quantity": 2}
        response = self.client.post(url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["order"]["total_price"], 40_000_000)
        self.iphone.refresh_from_db()
        self.assertEqual(self.iphone.stock_quantity, 3)

    def test_create_order_insufficient_stock(self):
        url = reverse("phone_erp:orders")
        payload = {"customer_id": self.customer.pk, "product_id": self.samsung.pk, "quantity": 99}
        response = self.client.post(url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("tồn kho", response.json()["error"])
        self.samsung.refresh_from_db()
        self.assertEqual(self.samsung.stock_quantity, 3)


class SeedDemoDataTests(TestCase):
    def test_seed_creates_data_and_refuses_twice(self):
        from django.core.management import CommandError, call_command
        call_command("seed_demo_data", verbosity=0)
        self.assertEqual(PhoneProduct.objects.count(), 10)
        self.assertEqual(Customer.objects.count(), 5)
        self.assertEqual(Order.objects.count(), 21)
        self.assertEqual(PhoneProduct.objects.get(sku="S24U-256").stock_quantity, 0)
        with self.assertRaises(CommandError):
            call_command("seed_demo_data", verbosity=0)
        call_command("seed_demo_data", reset=True, verbosity=0)
        self.assertEqual(Order.objects.count(), 21)

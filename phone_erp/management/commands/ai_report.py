import json

from django.core.management.base import BaseCommand

from phone_erp import ai_services


class Command(BaseCommand):
    help = "Chạy thử AI Gemini: python manage.py ai_report sales|inventory|ask [--question ...] [--days N]"

    def add_arguments(self, parser):
        parser.add_argument("feature", choices=["sales", "inventory", "ask"])
        parser.add_argument("--days", type=int, default=None, help="Số ngày gần nhất cần phân tích")
        parser.add_argument("--question", default="", help="Câu hỏi cho tính năng ask")
        parser.add_argument("--refresh", action="store_true", help="Bỏ qua cache, gọi lại Gemini")
        parser.add_argument("--full", action="store_true", help="In cả số liệu (metrics)")

    def handle(self, feature, days, question, refresh, full, **options):
        if feature == "sales":
            result = ai_services.analyze_sales_report(days=days, refresh=refresh)
        elif feature == "inventory":
            result = ai_services.analyze_inventory_alert(days=days or 30, refresh=refresh)
        else:
            if not question:
                self.stderr.write("Cần truyền --question \"...\"")
                return
            result = ai_services.ask_business_assistant(question, days=days or 30)

        if not full:
            result.pop("metrics", None)
        if result.get("warning"):
            self.stderr.write(self.style.WARNING(f"Cảnh báo: {result['warning']}"))
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))

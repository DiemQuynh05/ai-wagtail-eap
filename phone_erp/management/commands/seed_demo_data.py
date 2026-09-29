from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from phone_erp.models import Customer, Order, PhoneProduct

# (tên, thương hiệu, SKU, giá nhập, giá bán, tồn kho ban đầu, ngưỡng cảnh báo)
PRODUCTS = [
    ("iPhone 15 Pro Max 256GB", "Apple", "IP15PM-256", 29_500_000, 32_990_000, 12, 3),
    ("iPhone 15 128GB", "Apple", "IP15-128", 18_500_000, 21_490_000, 14, 3),
    ("iPhone 13 128GB", "Apple", "IP13-128", 11_000_000, 13_290_000, 9, 3),
    ("Samsung Galaxy S24 Ultra 256GB", "Samsung", "S24U-256", 25_000_000, 28_990_000, 8, 2),
    ("Samsung Galaxy A55 5G 128GB", "Samsung", "A55-128", 7_600_000, 9_290_000, 25, 5),
    ("Samsung Galaxy Z Flip6 256GB", "Samsung", "ZFLIP6-256", 22_000_000, 25_990_000, 10, 2),
    ("Xiaomi 14 256GB", "Xiaomi", "XM14-256", 15_500_000, 18_490_000, 8, 2),
    ("Xiaomi Redmi Note 13 Pro 256GB", "Xiaomi", "RN13P-256", 6_200_000, 7_490_000, 20, 5),
    ("OPPO Reno11 F 5G", "OPPO", "RENO11F-256", 7_000_000, 8_490_000, 18, 4),
    ("Google Pixel 8 128GB", "Google", "PIXEL8-128", 13_000_000, 15_990_000, 6, 2),
]

CUSTOMERS = [
    ("Đại lý Minh Phát", "0901234567", "minhphat@example.com"),
    ("Đại lý Hoàng Long Mobile", "0912345678", "hoanglong@example.com"),
    ("Nguyễn Văn An", "0987654321", "an.nguyen@example.com"),
    ("Trần Thị Bình", "0978123456", None),
    ("Lê Minh Châu", "0965432109", "chau.le@example.com"),
]

# (khách hàng, SKU, số lượng, cách đây bao nhiêu ngày, trạng thái)
ORDERS = [
    (0, "IP15PM-256", 3, 28, "COMPLETED"),
    (1, "IP15PM-256", 2, 21, "COMPLETED"),
    (2, "IP15PM-256", 2, 14, "COMPLETED"),
    (3, "IP15PM-256", 1, 6, "COMPLETED"),
    (4, "IP15PM-256", 1, 2, "COMPLETED"),
    (0, "IP15-128", 4, 25, "COMPLETED"),
    (1, "IP15-128", 3, 17, "COMPLETED"),
    (2, "IP15-128", 2, 9, "COMPLETED"),
    (3, "IP15-128", 1, 3, "COMPLETED"),
    (4, "IP13-128", 3, 12, "COMPLETED"),
    (0, "S24U-256", 3, 26, "COMPLETED"),
    (1, "S24U-256", 2, 19, "COMPLETED"),
    (0, "S24U-256", 2, 8, "COMPLETED"),
    (2, "S24U-256", 1, 1, "COMPLETED"),
    (3, "A55-128", 1, 15, "COMPLETED"),
    (1, "XM14-256", 2, 20, "COMPLETED"),
    (4, "XM14-256", 1, 5, "COMPLETED"),
    (0, "RN13P-256", 5, 11, "COMPLETED"),
    (2, "RENO11F-256", 1, 7, "COMPLETED"),
    (3, "ZFLIP6-256", 1, 1, "PENDING"),
    (4, "PIXEL8-128", 1, 4, "CANCELLED"),
]


class Command(BaseCommand):
    help = "Nạp dữ liệu mẫu: 10 mẫu điện thoại, 5 khách hàng, 21 đơn hàng trong 30 ngày gần nhất"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Xóa toàn bộ sản phẩm/khách hàng/đơn hàng trước khi nạp")

    @transaction.atomic
    def handle(self, reset, **options):
        if reset:
            Order.objects.all().delete()
            Customer.objects.all().delete()
            PhoneProduct.objects.all().delete()
        elif PhoneProduct.objects.exists() or Customer.objects.exists() or Order.objects.exists():
            raise CommandError("CSDL đã có dữ liệu. Dùng --reset để xóa và nạp lại dữ liệu mẫu.")

        products = {}
        for name, brand, sku, import_price, selling_price, stock, min_limit in PRODUCTS:
            products[sku] = PhoneProduct.objects.create(
                name=name, brand=brand, sku=sku, import_price=import_price,
                selling_price=selling_price, stock_quantity=stock, min_warning_limit=min_limit,
            )
        customers = [Customer.objects.create(full_name=n, phone=p, email=e) for n, p, e in CUSTOMERS]

        now = timezone.now()
        for customer_idx, sku, quantity, days_ago, status in ORDERS:
            # Order.save() tự tính tổng tiền và trừ tồn kho như khi bán thật
            order = Order.objects.create(
                customer=customers[customer_idx], product=products[sku], quantity=quantity, status=status,
            )
            Order.objects.filter(pk=order.pk).update(created_at=now - timedelta(days=days_ago, hours=customer_idx))

        self.stdout.write(self.style.SUCCESS(
            f"Đã nạp {len(PRODUCTS)} sản phẩm, {len(CUSTOMERS)} khách hàng, {len(ORDERS)} đơn hàng."
        ))

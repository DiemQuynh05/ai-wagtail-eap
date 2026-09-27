from django.db import models
from django.core.exceptions import ValidationError
from wagtail.snippets.models import register_snippet


# 1. BẢNG SẢN PHẨM ĐIỆN THOẠI
@register_snippet
class PhoneProduct(models.Model):
    name = models.CharField(max_length=255, verbose_name="Tên mẫu điện thoại")
    brand = models.CharField(max_length=100, verbose_name="Thương hiệu")
    sku = models.CharField(max_length=50, unique=True, verbose_name="Mã SKU / IMEI")
    import_price = models.DecimalField(max_digits=12, decimal_places=0, verbose_name="Giá nhập (VNĐ)")
    selling_price = models.DecimalField(max_digits=12, decimal_places=0, verbose_name="Giá bán (VNĐ)")
    stock_quantity = models.IntegerField(default=0, verbose_name="Số lượng tồn kho")
    min_warning_limit = models.IntegerField(default=3, verbose_name="Ngưỡng báo động tồn kho")

    class Meta:
        verbose_name = "Sản phẩm Điện thoại"
        verbose_name_plural = "Kho Điện thoại"

    def __str__(self):
        return f"{self.name} ({self.brand}) - Giá: {self.selling_price:,.0f} VNĐ - Tồn: {self.stock_quantity}"


# 2. BẢNG KHÁCH HÀNG / ĐẠI LÝ
@register_snippet
class Customer(models.Model):
    full_name = models.CharField(max_length=255, verbose_name="Tên khách hàng")
    phone = models.CharField(max_length=20, verbose_name="Số điện thoại")
    email = models.EmailField(blank=True, null=True, verbose_name="Email")

    class Meta:
        verbose_name = "Khách hàng"
        verbose_name_plural = "Danh sách Khách hàng"

    def __str__(self):
        return f"{self.full_name} - {self.phone}"


# 3. BẢNG ĐƠN HÀNG
@register_snippet
class Order(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Chờ xử lý'),
        ('COMPLETED', 'Đã hoàn thành'),
        ('CANCELLED', 'Đã hủy'),
    ]

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, verbose_name="Khách hàng")
    product = models.ForeignKey(PhoneProduct, on_delete=models.CASCADE, verbose_name="Sản phẩm điện thoại")
    quantity = models.PositiveIntegerField(default=1, verbose_name="Số lượng mua")
    
    # blank=True cho phép người dùng để trống ô này trên giao diện
    total_price = models.DecimalField(
        max_digits=12, 
        decimal_places=0, 
        blank=True, 
        default=0, 
        verbose_name="Tổng tiền (VNĐ)"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='COMPLETED', verbose_name="Trạng thái")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ngày tạo đơn")

    class Meta:
        verbose_name = "Đơn hàng"
        verbose_name_plural = "Danh sách Đơn hàng"

    def save(self, *args, **kwargs):
        # Tự động lấy Đơn giá x Số lượng để tính Tổng tiền trước khi ghi vào CSDL
        if self.product:
            self.total_price = self.product.selling_price * self.quantity

        # Kiểm tra và trừ số lượng tồn kho
        if not self.pk and self.status == 'COMPLETED':
            if self.product.stock_quantity < self.quantity:
                raise ValidationError(f"Số lượng tồn kho không đủ! Chỉ còn {self.product.stock_quantity} máy.")
            self.product.stock_quantity -= self.quantity
            self.product.save()

        elif self.pk:
            old_order = Order.objects.get(pk=self.pk)
            if old_order.status != 'COMPLETED' and self.status == 'COMPLETED':
                if self.product.stock_quantity < self.quantity:
                    raise ValidationError(f"Số lượng tồn kho không đủ! Chỉ còn {self.product.stock_quantity} máy.")
                self.product.stock_quantity -= self.quantity
                self.product.save()
            elif old_order.status == 'COMPLETED' and self.status == 'CANCELLED':
                self.product.stock_quantity += self.quantity
                self.product.save()

        super().save(*args, **kwargs)

    def __str__(self):
        return f"Đơn #{self.id} - {self.customer.full_name} mua {self.product.name}"
# API Phone ERP & Gemini AI

Tài liệu bàn giao cho Thành viên 3 (Frontend). Tất cả endpoint nằm dưới tiền tố `/api/` và trả về JSON.

## 1. Cài đặt

```bash
pip install -r requirements.txt
cp .env.example .env        # rồi điền GEMINI_API_KEY (lấy tại https://aistudio.google.com/apikey)
python manage.py migrate
python manage.py runserver
```

Chưa có `GEMINI_API_KEY` hoặc Gemini bị lỗi/hết quota thì API **vẫn trả về 200**, kèm `"source": "fallback"` (phân tích theo luật) và lý do ở `"warning"`. Frontend nên hiện `warning` nếu khác `null`.

Mỗi model Gemini có quota miễn phí riêng. Khi `GEMINI_MODEL` hết quota, quá tải hoặc quá thời gian chờ, hệ thống tự thử lần lượt các model trong `GEMINI_FALLBACK_MODELS`. Trường `"model"` trong phản hồi cho biết model đã dùng. **Không nên gửi `refresh=1` mỗi lần bấm nút**, vì mỗi lần như vậy tốn một lượt quota. Dữ liệu thay đổi thì AI vẫn tự phân tích lại.

Test AI bằng dòng lệnh, không cần Frontend:

```bash
python manage.py ai_report sales --days 30
python manage.py ai_report inventory
python manage.py ai_report ask --question "Nên nhập thêm mẫu nào tuần này?"
```

## 2. Danh sách endpoint

| Method | URL | Mô tả | Tham số |
|---|---|---|---|
| GET | `/api/ai/status/` | AI đã được cấu hình chưa | - |
| GET | `/api/ai/sales-report/` | **AI Phân tích Thị trường & Doanh thu** | `days` (mặc định: toàn bộ), `refresh=1` |
| GET | `/api/ai/inventory-alert/` | **AI Dự báo Nhập hàng / Cảnh báo tồn kho** | `days` (mặc định 30), `refresh=1` |
| POST | `/api/ai/ask/` | Hỏi đáp tự do với trợ lý AI | body `{"question": "...", "days": 30}` |
| GET | `/api/dashboard/stats/` | Số liệu cho Card thống kê | - |
| GET | `/api/products/` | Tồn kho điện thoại | `brand=Apple`, `group_by=brand` |
| GET | `/api/customers/` | Khách hàng (cho dropdown form) | - |
| GET | `/api/orders/` | Đơn hàng gần nhất | `limit` (mặc định 20) |
| POST | `/api/orders/` | Tạo đơn hàng mới (tự trừ kho) | body `{"customer_id", "product_id", "quantity", "status"}` |

Lỗi đầu vào trả về HTTP 400: `{"error": "..."}`.

Kết quả AI được cache 10 phút khi dữ liệu không đổi (`"cached": true`). Có đơn hàng mới thì dữ liệu thay đổi và AI tự phân tích lại. Muốn ép gọi lại thì thêm `refresh=1`.

## 3. Cấu trúc phản hồi AI

Mọi endpoint AI đều trả về cùng một khung:

```json
{
  "feature": "sales_report",
  "source": "gemini",
  "model": "gemini-3.8-flash",
  "cached": false,
  "generated_at": "2026-09-27T08:00:00+00:00",
  "warning": null,
  "metrics": { "...": "số liệu tính từ CSDL, dùng để vẽ biểu đồ/bảng" },
  "analysis": { "...": "nội dung do AI viết" }
}
```

Số liệu trong `metrics` được tính chính xác từ CSDL. Chỉ phần `analysis` do AI viết.

### `analysis` của `/api/ai/sales-report/`

```json
{
  "headline": "Apple dẫn đầu với 76,9% doanh thu",
  "summary": "Đoạn tổng quan...",
  "hot_brands": [{"brand": "Apple", "revenue_share_pct": 76.9, "comment": "..."}],
  "hot_products": [{"name": "iPhone 15 128GB", "units": 5, "comment": "..."}],
  "insights": ["..."],
  "risks": ["..."],
  "recommendations": ["..."]
}
```

`metrics` gồm: `summary` (revenue, estimated_profit, profit_margin_pct, units_sold, completed/pending/cancelled_orders, avg_order_value), `by_brand`, `top_products`, `top_customers`, `daily_revenue`.

### `analysis` của `/api/ai/inventory-alert/`

```json
{
  "headline": "1 mẫu hết hàng, 2 mẫu sắp hết, 1 mẫu tồn lâu",
  "summary": "...",
  "urgent_restock": [
    {"name": "Galaxy S24 Ultra", "sku": "S24U", "current_stock": 3, "suggested_qty": 3, "priority": "CAO", "reason": "..."}
  ],
  "overstock": [
    {"name": "Xiaomi 14", "sku": "XM14", "current_stock": 20, "reason": "...", "suggestion": "..."}
  ],
  "budget_comment": "Cần khoảng 75.000.000 đ để nhập 3 máy...",
  "recommendations": ["..."]
}
```

`priority` nhận giá trị `CAO` / `TRUNG_BINH` / `THAP`. Mỗi phần tử `metrics.products[]` có `status`: `OUT_OF_STOCK`, `LOW_STOCK`, `SLOW_MOVING`, `OK`.

### `analysis` của `/api/ai/ask/`

Là một chuỗi văn bản (có thể chứa gạch đầu dòng Markdown). Phản hồi có thêm trường `question`.

## 4. Ví dụ JavaScript (Fetch API)

```javascript
// Lấy CSRF token (bắt buộc với POST). Trong template Django thêm {% csrf_token %}
// hoặc đọc từ cookie "csrftoken".
function getCookie(name) {
  return document.cookie.split("; ").find(c => c.startsWith(name + "="))?.split("=")[1];
}

async function aiSalesReport() {
  const res = await fetch("/api/ai/sales-report/?days=30");
  const data = await res.json();
  if (data.warning) console.warn(data.warning);
  return data;   // data.analysis.headline, data.analysis.hot_brands, ...
}

async function createOrder(customerId, productId, quantity) {
  const res = await fetch("/api/orders/", {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken")},
    body: JSON.stringify({customer_id: customerId, product_id: productId, quantity}),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error);   // ví dụ: "Số lượng tồn kho không đủ! Chỉ còn 3 máy."
  return data;   // data.order, data.remaining_stock
}

async function askAI(question) {
  const res = await fetch("/api/ai/ask/", {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken")},
    body: JSON.stringify({question}),
  });
  return (await res.json()).analysis;
}
```

Gọi Gemini mất khoảng 5–20 giây, nên hiện spinner/disable nút trong lúc chờ.

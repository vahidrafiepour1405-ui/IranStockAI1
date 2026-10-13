# IranStockAI — شکارچی فرصت بورس ایران

اپلیکیشن وب فارسی و راست‌چین برای نمایش اطلاعات بازار ایران با اولویت صحت داده و جلوگیری از سیگنال کاذب. این پروژه مستقل است.

## وضعیت فعلی و محدودیت‌های مهم

- داده بازار از چند endpoint هدف TSETMC دریافت و ساختار پاسخ پیش از پذیرش بررسی می‌شود.
- منبع و زمان آخرین دریافت در API نمایش داده می‌شود؛ پس از شکست دریافت، داده ذخیره‌شده با وضعیت STALE و هشدار برمی‌گردد.
- اگر منبع داده موجود نباشد، API بازار با HTTP 503 و بدون داده نمونه پاسخ می‌دهد.
- تاریخچه معتبر و اتصال تأییدشده کدال هنوز پیاده‌سازی/تأیید نشده‌اند؛ endpointهای تاریخچه و بنیادی صریحاً INSUFFICIENT DATA برمی‌گردانند.
- موتور سیگنال خرید/فروش تأییدشده وجود ندارد؛ endpoint سیگنال تا دسترسی به داده تاریخی و اعتبارسنجی‌شده، INSUFFICIENT DATA می‌دهد.
- این پروژه سفارش معامله ثبت نمی‌کند و سود یا موفقیت را تضمین نمی‌کند.
- نام منبع و timestamp به‌تنهایی تضمین صحت اقتصادی داده نیست؛ قبل از استفاده معاملاتی، داده واقعی سرور باید اعتبارسنجی شود.

## اجرای محلی

Python 3.12 توصیه می‌شود.

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate
pip install -r requirements.txt
pip install pytest
uvicorn main:app --host 0.0.0.0 --port 8000
```

سپس `http://127.0.0.1:8000` را باز کنید.

## تست‌ها

```bash
python -m compileall -q main.py tests
pytest -q
```

تست‌ها منطق نرمال‌سازی، تقسیم بر صفر، Parser قالب قدیمی، رفتار OFFLINE و سیاست عدم جعل سیگنال را پوشش می‌دهند. این تست‌ها به‌تنهایی دسترسی زنده TSETMC یا سلامت Render را اثبات نمی‌کنند.

## Docker

```bash
docker build -t iranstockai .
docker run --rm -p 8000:8000 -e PORT=8000 iranstockai
```

## استقرار Render

- مخزن: `vahidrafiepour1405-ui/IranStockAI1`
- Runtime: Docker
- Dockerfile: `./Dockerfile`
- Health check: `/health`
- متغیر اختیاری `MAX_DATA_AGE_SECONDS` (پیش‌فرض 180)
- متغیر اختیاری `MARKET_CACHE_SECONDS` (پیش‌فرض 20)
- متغیر اختیاری `UPSTREAM_TIMEOUT_SECONDS` (پیش‌فرض 8)
- متغیر اختیاری `TSETMC_MARKETWATCH_URL` برای endpoint جایگزین

پس از انتشار، این مسیرها را از مرورگر بررسی کنید: `/health`, `/api/status`, `/api/diagnostics`, `/api/market?limit=200`. وضعیت منبع باید با پاسخ واقعی سرور تطبیق داده شود.

## API

| مسیر | کارکرد |
|---|---|
| `GET /` | داشبورد |
| `GET /health` | سلامت فرایند |
| `GET /api/status` | وضعیت و سن داده ذخیره‌شده |
| `GET /api/diagnostics` | بررسی endpointهای منبع |
| `GET /api/market?q=...&limit=200` | جست‌وجو و فهرست نمادها |
| `GET /api/scan` | غربال تغییرات منفی، نه سیگنال خرید |
| `GET /api/quote/{symbol}` | مظنه نماد |
| `GET /api/history/{symbol}` | فعلاً داده ناکافی |
| `GET /api/fundamentals/{symbol}` | فعلاً داده ناکافی |
| `GET /api/signals/{symbol}` | سیگنال محافظه‌کارانه؛ تا زمان تأیید داده، داده ناکافی |

## داده و امنیت

- هیچ قیمت، تراکنش یا سیگنال تصادفی تولید نمی‌شود.
- پیام خطا برای کاربر به کد عمومی و غیرحساس محدود شده است؛ تشخیص منبع نام میزبان را نشان می‌دهد نه URL کامل.
- کلیدها و Secretها نباید در Git ثبت شوند.
- Health endpoint فقط زنده بودن برنامه را نشان می‌دهد، نه سلامت منبع بازار.
- Service Worker پاسخ‌های `/api/` را کش نمی‌کند.

## بازگشت به نسخه قبلی

در GitHub، از تاریخچه Commit شاخه اصلی نسخه سالم قبلی را پیدا کنید و از گزینه Revert استفاده کنید؛ سپس وضعیت Deploy را در Render کنترل کنید. شاخه اصلاحات تا زمان بازبینی و پذیرش نباید بدون بررسی به main ادغام شود.

# راهنمای فارسی هلیوس

هلیوس ChatGPT یا Codex را به رهبر یک تیم چندمدلی تبدیل می‌کند. برای پروژه‌های بزرگ، GPT ابتدا پروژه را به تسک‌های کوچک و قابل‌آزمایش می‌شکند، وابستگی‌ها و معیار پذیرش را مشخص می‌کند، برای هر تسک از رجیستری بنچمارک یک مدل متخصص می‌گیرد و فقط خروجی‌های تأییدشده را در نتیجه نهایی ادغام می‌کند.

## وضعیت فعلی

این قابلیت‌ها همین حالا کار می‌کنند:

- خردکردن پروژه و ساخت task graph در همان گفت‌وگوی ChatGPT/Codex؛
- انتخاب مدل جداگانه برای تحقیق، کدنویسی، فرانت‌اند، استدلال، OCR، Vision و Generation؛
- اجرای هم‌زمان حداکثر چهار تسک مستقل؛
- بازبینی با یک خانواده مدل دیگر و یک نوبت اصلاح؛
- ثبت نام واقعی مدل استفاده‌شده و منبع انتخاب.

وضعیت پروژه فعلاً session-scoped است. بازیابی بعد از restart، pause/resume دائمی، بودجه اجباری و event log در برنامه V2 قرار دارند؛ endpointهای `/v2/projects` هنوز وجود ندارند.

## رجیستری بنچمارک

هلیوس هفته‌ای یک بار فقط با وب‌سرچ، فهرست مدل‌ها را به‌روز می‌کند و تست خصوصی اجرا نمی‌کند. منبع باید leaderboard رسمی، سایت خود بنچمارک یا مقاله اصلی باشد و حداقل سه مدل قابل‌مقایسه داشته باشد. جدول‌های display-only، aggregator و امتیازهای تخمینی پذیرفته نمی‌شوند.

تازگی هر دسته جداگانه ذخیره می‌شود. اگر داده قدیمی یا ناکافی باشد، هلیوس محدودیت را اعلام می‌کند و ادعای «قوی‌ترین مدل» نمی‌کند.

## نصب روی macOS

پیش‌نیازها: Python 3.10+، Node.js 22+، npm و کلید OpenRouter.

```bash
git clone https://github.com/Erfouni/helios-llm-orchestrator.git
cd helios-llm-orchestrator
./scripts/install-macos.sh
```

نصاب نسخه ابزارها را بررسی می‌کند، تست و اسکن امنیتی را اجرا می‌کند، کلید را با ورودی مخفی در Keychain می‌گذارد، سرویس و زمان‌بندی دوشنبه ساعت 03:00 را نصب می‌کند و یک refresh اولیه انجام می‌دهد.

## نصب روی Windows 10/11

PowerShell را با همان کاربری که قرار است هلیوس را اجرا کند باز کنید:

```powershell
git clone https://github.com/Erfouni/helios-llm-orchestrator.git
Set-Location helios-llm-orchestrator
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

کلید با Windows DPAPI برای همان کاربر رمز می‌شود. نصاب یک Scheduled Task برای اجرای هلیوس هنگام login و یک Task هفتگی برای دوشنبه ساعت 03:00 می‌سازد. راهنمای کامل در `docs/WINDOWS.md` است.

```bash
curl http://127.0.0.1:3188/health
```

## ابزارهای MCP

- `openrouter_list_models`: جست‌وجوی مدل‌های OpenRouter
- `openrouter_run_model`: اجرای یک تسک با مدل مشخص
- `openrouter_compare_models`: مقایسه دو تا چهار مدل
- `helios_get_benchmark_registry`: دیدن منبع و تازگی رجیستری
- `helios_select_benchmark_model`: انتخاب متخصص یک دسته
- `helios_refresh_benchmarks`: refresh صریح رجیستری

برای تنظیم MCP، فایل `mcp/client-config.example.json` را کپی و مسیر مطلق پروژه را جایگزین کنید.

## امنیت

- سرویس فقط روی loopback مک در دسترس است.
- کلید از Keychain مک، فایل رمز‌شده با DPAPI ویندوز، یا environment خوانده می‌شود و در پاسخ برنمی‌گردد.
- ورودی پیام‌ها، اندازه درخواست، پارامترهای عددی و هم‌زمانی محدود هستند.
- خروجی مدل خارجی داده‌ی غیرقابل‌اعتماد است و مجوز مستقلی برای فایل، GitHub یا انتشار اجتماعی ندارد.
- LinkedIn و Instagram خارج از executor پروژه‌اند و هر انتشار عمومی تأیید دقیق خودش را می‌خواهد.

برای تغییر کلید:

```bash
./scripts/configure-key.sh
launchctl kickstart -k "gui/$(id -u)/com.local.helios-multimodel-router"
```

# راهنمای استفاده فارسی

## هلیوس چطور کار می‌کند؟

هلیوس بین ChatGPT/Codex و OpenRouter قرار می‌گیرد. درخواست مدل از طریق MCP به برنامه محلی روی مک می‌رسد؛ برنامه فقط روی `127.0.0.1:3188` گوش می‌دهد و همان درخواست را با کلید شما به OpenRouter می‌فرستد. پاسخ همراه با نام مدل واقعی استفاده‌شده برمی‌گردد.

برای استفاده محلی به دامنه نیازی نیست. دامنه یا تونل HTTPS فقط زمانی لازم است که یک سرویس خارج از مک بخواهد مستقیماً از اینترنت به MCP وصل شود.

## نصب

پیش‌نیازها:

- macOS
- Python 3.10 یا جدیدتر
- Node.js 22 یا جدیدتر
- npm
- کلید OpenRouter

```bash
git clone git@github.com:mesutfd/helios-multimodel-router.git
cd helios-multimodel-router
chmod +x scripts/*.sh
./scripts/install-macos.sh
```

نصاب کلید را با ورودی مخفی می‌گیرد و در Keychain ذخیره می‌کند. مقدار کلید داخل `.env` یا Git نوشته نمی‌شود.

## بررسی سلامت

```bash
curl http://127.0.0.1:3188/health
```

اگر `configured: true` باشد، کلید از Keychain یا Environment قابل‌دسترسی است.

## ابزارهای MCP

- `openrouter_list_models`: جست‌وجوی مدل‌های زنده OpenRouter
- `openrouter_run_model`: اجرای یک کار با یک مدل مشخص
- `openrouter_compare_models`: مقایسه هم‌زمان پاسخ دو تا چهار مدل

برای نمونه تنظیم MCP، فایل `mcp/client-config.example.json` را کپی کنید و مسیر مطلق پروژه روی مک خودتان را جایگزین کنید.

## Skill هلیوس

پوشه `skill/helios` شامل Skill آماده است. این Skill:

- درخواست‌های صریح به GLM، Gemini، Claude، DeepSeek، Qwen و مدل‌های دیگر را به هلیوس می‌فرستد.
- اطلاعات مخفی، Secret و متن‌های سیستمی را به مدل خارجی ارسال نمی‌کند.
- در صورت نصب LinkedIn Agent جداگانه روی پورت `3190`، قابلیت‌های مجاز لینکدین را هم می‌شناسد.
- برای انتشار پست یا کامنت لینکدین، تأیید صریح همان متن را الزامی می‌داند.

راهنمای نصب Skill در `docs/SKILL_INSTALL.md` است.

## تغییر کلید OpenRouter

```bash
./scripts/configure-key.sh
```

پس از تغییر کلید، سرویس را ری‌استارت کنید:

```bash
launchctl kickstart -k "gui/$(id -u)/com.local.helios-multimodel-router"
```

## امنیت

- فایل `.env` و Logها وارد Git نمی‌شوند.
- کلید داخل Keychain نگهداری می‌شود.
- برنامه اتصال شبکه‌ای ورودی خارج از مک را قبول نمی‌کند.
- پاسخ مدل خارجی پیشنهاد است؛ عملیات فایل یا انتشار عمومی باید جداگانه و با مجوز انجام شود.

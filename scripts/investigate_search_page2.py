"""Исследование 2: механика региона, submit, URL, размер страницы (read-only + apply фильтров)."""
import asyncio
import json
import re
from pathlib import Path

from src.utils.browser_utils import create_playwright_browser, save_browser_session

OUT = Path("data_folder/output/investigate2")
OUT.mkdir(parents=True, exist_ok=True)


def clean(tag: str) -> str:
    return re.sub(r">\s+<", "><", str(tag or ""))


async def main():
    browser, context, page = await create_playwright_browser()
    try:
        await page.goto("https://hh.ru/search/vacancy?text=data", timeout=90000)
        await page.wait_for_timeout(6000)
        print("URL0:", page.url)

        # Открыть панель
        await page.get_by_text("Фильтры", exact=True).first.click()
        await page.wait_for_timeout(3000)
        await page.wait_for_selector("[data-qa='search-drawer-filters-submit']", timeout=15000)
        print("drawer open")

        # Блок региона в панели
        area_chip = page.locator("[data-qa='search-filter-area-chip']")
        (OUT / "01_area_chip.html").write_text(clean(await area_chip.first.evaluate("el => el.outerHTML")))

        # Рядом с чипом — подписи панели
        drawer = page.locator("[data-qa='search-filters']")
        if await drawer.count():
            (OUT / "02_search_filters_block.html").write_text(clean((await drawer.first.evaluate("el => el.outerHTML"))[:60000]))

        # Кликнуть чип региона -> должно появиться поле "Поиск региона"
        await area_chip.first.click()
        await page.wait_for_timeout(1500)
        region_inputs = page.locator('[data-qa="chips-trigger-input"]')
        n = await region_inputs.count()
        print("chips inputs after chip click:", n)

        async def pick_region(name: str, tag: str):
            # находим input с контекстом "Поиск региона"
            chosen = None
            for i in range(await region_inputs.count()):
                el = region_inputs.nth(i)
                ctx = await el.evaluate(
                    "el => { let c = el; for (let i=0;i<8&&c.parentElement;i++) c=c.parentElement; return (c.innerText||'').trim().slice(0,300); }"
                )
                if "регион" in (ctx or "").lower() or "поиск региона" in (ctx or "").lower():
                    chosen = i
                    break
            if chosen is None:
                # иначе берём первый пустой
                chosen = 0
            print(f"{tag}: using input {chosen}")
            el = region_inputs.nth(chosen)
            await el.click(timeout=5000)
            await page.wait_for_timeout(300)
            await el.fill("")
            await el.type(name, delay=30)
            await page.wait_for_timeout(3000)
            # варианты
            opts = page.locator("[data-qa^='magritte-select-option-']")
            m = await opts.count()
            texts = []
            for j in range(min(m, 10)):
                t = await opts.nth(j).text_content()
                texts.append(re.sub(r"\s+", " ", t or "").strip())
            (OUT / f"03_{tag}_options.json").write_text(json.dumps(texts, ensure_ascii=False, indent=1))
            print(f"{tag}: options={texts[:5]}")
            if m == 0:
                return False
            # лучший вариант: точное/ближайшее совпадение
            best = 0
            for j, t in enumerate(texts):
                if t.lower().startswith(name.lower()):
                    best = j
                    break
            await opts.nth(best).click()
            await page.wait_for_timeout(1500)
            return True

        r1 = await pick_region("Астана", "astana")
        r2 = await pick_region("Алматы", "almaty")
        print("picked:", r1, r2)

        # выбранные чипы региона (ищем в блоке area)
        area_block = page.locator("[data-qa='filter-select-area'], [data-qa*='area']")
        chips = page.locator("[data-qa='search-filter-area-chip'], [data-qa*='chip']")
        (OUT / "04_chips_after_pick.html").write_text(
            "\n---\n".join(clean(await chips.nth(i).evaluate("el => el.outerHTML")) for i in range(min(await chips.count(), 12)))
        )

        # Состояние чекбоксов search_field до/после кликов
        def checkbox_states():
            return page.evaluate(
                """
                () => {
                    const out = {};
                    document.querySelectorAll('input[type="checkbox"]').forEach(el => {
                        const p = el.closest('[data-qa]');
                        const qa = p ? p.getAttribute('data-qa') : (el.name || el.value || '?');
                        out[qa] = el.checked;
                    });
                    return out;
                }
                """
            )

        before = await checkbox_states()
        (OUT / "05_checkbox_before.json").write_text(json.dumps(before, ensure_ascii=False, indent=1))

        # search_field: выставить все true (сначала прочитать состояние, затем довести до нужного)
        for key in ("name", "company_name", "description"):
            el = page.locator(f"[data-qa='search-filter-search_field-value-{key}']")
            if await el.count():
                checked = await el.first.evaluate("el => { const i = el.closest('label') ? el.closest('label').querySelector('input[type=checkbox]') : null; return i ? i.checked : null; }")
                print(f"search_field {key} before click: {checked}")
                await el.first.click()
                await page.wait_for_timeout(500)

        after = await checkbox_states()
        (OUT / "06_checkbox_after.json").write_text(json.dumps(after, ensure_ascii=False, indent=1))

        # with_salary
        ws = page.locator("[data-qa='search-filter-value-with_salary']")
        if await ws.count():
            ws_checked = await ws.first.evaluate(
                "el => { const i = el.closest('label') ? el.closest('label').querySelector('input[type=checkbox]') : null; return i ? i.checked : null; }"
            )
            print("with_salary before:", ws_checked)

        # Apply
        await page.locator("[data-qa='search-drawer-filters-submit']").first.click()
        await page.wait_for_timeout(8000)
        print("URL_AFTER_APPLY:", page.url)
        (OUT / "07_url_after_apply.txt").write_text(page.url)

        # drawer закрылась?
        print("submit still visible:", await page.locator("[data-qa='search-drawer-filters-submit']").count())

        # Размер страницы
        menu = page.locator("[data-qa='items_on_page-menu']")
        if await menu.count():
            (OUT / "08_items_on_page.html").write_text(clean((await menu.first.evaluate("el => el.outerHTML"))[:20000]))
            print("items_on_page html saved")
            # текст вокруг
            print("menu text:", re.sub(r"\s+", " ", (await menu.first.text_content() or ""))[:200])

        # Период
        per = page.locator("[data-qa='search-period-menu']")
        if await per.count():
            (OUT / "09_period_menu.html").write_text(clean((await per.first.evaluate("el => el.outerHTML"))[:20000]))

        # Пагинация: url страницы 2
        next_link = page.locator("[data-qa='pager-next']")
        if await next_link.count():
            href = await next_link.first.get_attribute("href")
            (OUT / "10_pager_next_href.txt").write_text(href or "")
            print("pager-next href:", href)
            await next_link.first.click()
            await page.wait_for_timeout(6000)
            print("URL_PAGE2:", page.url)
            cards = await page.locator('[data-qa="vacancy-serp__vacancy"]').count()
            print("cards on page2:", cards)

        # Обратно на страницу 1 и проверяем URL-параметры
        await page.goto("https://hh.ru/search/vacancy?text=data", timeout=90000)
        await page.wait_for_timeout(5000)
        print("URL_RESET:", page.url)
    finally:
        await save_browser_session(context)
        await context.close()
        await browser.close()
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())

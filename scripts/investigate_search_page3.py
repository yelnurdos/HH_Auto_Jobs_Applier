"""Исследование 3: полный флоу с robust-кликами: регион -> submit -> URL -> размер страницы."""
import asyncio
import json
import re
from pathlib import Path

from src.utils.browser_utils import create_playwright_browser, save_browser_session

OUT = Path("data_folder/output/investigate3")
OUT.mkdir(parents=True, exist_ok=True)


def clean(tag: str) -> str:
    return re.sub(r">\s+<", "><", str(tag or ""))


async def robust_click(page, locator, what: str) -> bool:
    """Клик с fallback на force и JS."""
    try:
        await locator.first.click(timeout=5000)
        print(f"click ok (normal): {what}")
        return True
    except Exception as e1:
        print(f"click normal failed ({what}): {str(e1)[:100]}")
        try:
            await locator.first.click(timeout=5000, force=True)
            print(f"click ok (force): {what}")
            return True
        except Exception as e2:
            print(f"click force failed ({what}): {str(e2)[:100]}")
            try:
                await locator.first.evaluate("el => el.click()")
                print(f"click ok (js): {what}")
                return True
            except Exception as e3:
                print(f"click js failed ({what}): {str(e3)[:100]}")
                return False


async def main():
    browser, context, page = await create_playwright_browser()
    try:
        await page.goto("https://hh.ru/search/vacancy?text=data", timeout=90000)
        await page.wait_for_timeout(6000)

        await robust_click(page, page.get_by_text("Фильтры", exact=True), "Фильтры")
        await page.wait_for_selector("[data-qa='search-drawer-filters-submit']", timeout=15000)
        print("drawer open")

        area = page.locator("[data-qa='filter-select-area']")
        print("area block count:", await area.count())

        async def pick_region(name: str, tag: str):
            inp = area.locator("[data-qa='chips-trigger-input']")
            print(f"{tag}: region inputs in area block:", await inp.count())
            if await inp.count() == 0:
                return False
            # фокус + ввод
            try:
                await inp.first.click(timeout=3000)
            except Exception:
                await inp.first.evaluate("el => el.focus()")
            await page.wait_for_timeout(300)
            await inp.first.fill("")
            await inp.first.type(name, delay=30)
            await page.wait_for_timeout(3000)
            # подсказки: видимые варианты, текст похож на запрос
            opts = page.locator("[data-qa^='magritte-select-option-']")
            m = await opts.count()
            texts = []
            for j in range(min(m, 15)):
                vis = await opts.nth(j).is_visible()
                t = re.sub(r"\s+", " ", (await opts.nth(j).text_content() or "")).strip()
                texts.append({"t": t, "vis": vis})
            (OUT / f"03_{tag}_options.json").write_text(json.dumps(texts, ensure_ascii=False, indent=1))
            print(f"{tag}: options:", [x["t"] for x in texts][:8])
            # лучший вариант
            best_idx = None
            for j, x in enumerate(texts):
                if x["vis"] and x["t"].lower().startswith(name.lower()):
                    best_idx = j
                    break
            if best_idx is None:
                for j, x in enumerate(texts):
                    if x["vis"]:
                        best_idx = j
                        break
            if best_idx is None:
                return False
            # дамп одного варианта
            try:
                (OUT / f"04_{tag}_option_html.html").write_text(clean(await opts.nth(best_idx).evaluate("el => el.outerHTML"))[:4000])
            except Exception:
                pass
            ok = await robust_click(page, opts.nth(best_idx), f"option {tag}")
            await page.wait_for_timeout(1500)
            # состояние area-блока
            block_txt = re.sub(r"\s+", " ", (await area.first.text_content() or "")).strip()
            (OUT / f"05_{tag}_area_block_text.txt").write_text(block_txt[:500])
            print(f"{tag}: area block text:", block_txt[:300])
            return ok and name.lower() in block_txt.lower()

        r1 = await pick_region("Астана", "astana")
        r2 = await pick_region("Алматы", "almaty")
        print("picked:", r1, r2)

        # search_field state
        states = await page.evaluate(
            """
            () => {
                const out = {};
                document.querySelectorAll('input[type="checkbox"]').forEach(el => {
                    const qa = el.getAttribute('data-qa') || '';
                    if (qa) out[qa] = el.checked;
                });
                return out;
            }
            """
        )
        (OUT / "06_checkbox_states.json").write_text(json.dumps(states, ensure_ascii=False, indent=1))
        sf = {k: v for k, v in states.items() if k.startswith("search-filter-search_field")}
        print("search_field states:", sf)

        # submit
        await robust_click(page, page.locator("[data-qa='search-drawer-filters-submit']"), "submit")
        await page.wait_for_timeout(8000)
        print("URL_AFTER_APPLY:", page.url)
        (OUT / "07_url_after_apply.txt").write_text(page.url)
        print("drawer closed:", await page.locator("[data-qa='search-drawer-filters-submit']").count() == 0)
        cards = await page.locator('[data-qa="vacancy-serp__vacancy"]').count()
        print("cards after apply:", cards)

        # items_on_page
        menu = page.locator("[data-qa='items_on_page-menu']")
        if await menu.count():
            (OUT / "08_items_on_page.html").write_text(clean((await menu.first.evaluate("el => el.outerHTML"))[:30000]))
            print("items_on_page text:", re.sub(r"\s+", " ", (await menu.first.text_content() or ""))[:200])

        # pager
        nxt = page.locator("[data-qa='pager-next']")
        if await nxt.count():
            href = await nxt.first.get_attribute("href")
            print("pager-next href:", href)
            (OUT / "09_pager_next_href.txt").write_text(href or "")
            if href:
                await robust_click(page, nxt, "pager-next")
                await page.wait_for_timeout(6000)
                print("URL_PAGE2:", page.url)
                print("cards page2:", await page.locator('[data-qa="vacancy-serp__vacancy"]').count())

    finally:
        await save_browser_session(context)
        await context.close()
        await browser.close()
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())

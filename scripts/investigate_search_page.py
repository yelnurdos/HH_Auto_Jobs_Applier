"""Исследование структуры новой формы поиска hh.ru (read-only).

Открывает обычную выдачу, панель «Фильтры»,-region block, размер страницы,
пустую выдачу. Никаких отправок/сообщений не делает.
"""
import asyncio
import json
import re
from pathlib import Path

from src.utils.browser_utils import create_playwright_browser, save_browser_session

OUT = Path("data_folder/output/investigate")
OUT.mkdir(parents=True, exist_ok=True)


def clean(tag: str) -> str:
    """Сжимаем HTML до читаемого вида."""
    tag = re.sub(r">\s+<", "><", tag)
    return tag


async def main():
    browser, context, page = await create_playwright_browser()
    try:
        # 1) Обычная выдача
        await page.goto("https://hh.ru/search/vacancy?text=data", timeout=90000)
        await page.wait_for_timeout(5000)
        (OUT / "01_serp_url.txt").write_text(page.url)
        (OUT / "01_serp.html").write_text(clean(await page.content()))

        # Верхний input поиска
        inp = page.locator("[data-qa='search-input']")
        inp_html = await inp.first.evaluate("el => el.outerHTML") if await inp.count() else "N/A"
        (OUT / "02_top_input.txt").write_text(f"count={await inp.count()}\nhtml={inp_html}")

        # 2) Кнопка Фильтры
        filters_btn = page.get_by_text("Фильтры", exact=True)
        btn_htmls = []
        for i in range(min(await filters_btn.count(), 3)):
            try:
                btn_htmls.append(clean(str(await filters_btn.nth(i).evaluate("el => el.outerHTML"))))
            except Exception as e:
                btn_htmls.append(f"err: {e}")
        (OUT / "03_filters_btn.txt").write_text(f"count={await filters_btn.count()}\n" + "\n---\n".join(btn_htmls))
        if await filters_btn.count() == 0:
            print("FILTERS BUTTON NOT FOUND")
            return
        await filters_btn.first.click()
        await page.wait_for_timeout(4000)

        # 3) Структура панели: все элементы с data-qa
        qas = await page.evaluate(
            """
            () => {
                const out = [];
                document.querySelectorAll('[data-qa]').forEach(el => {
                    let t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                    if (t.length > 120) t = t.slice(0, 120);
                    out.push({qa: el.getAttribute('data-qa'), tag: el.tagName.toLowerCase(), text: t});
                });
                return out;
            }
            """
        )
        (OUT / "04_drawer_data_qa.json").write_text(
            json.dumps(qas, ensure_ascii=False, indent=1)
        )

        # 4) Регион: найти блок по подписи, дамп его HTML
        region_block = await page.evaluate(
            """
            () => {
                const els = Array.from(document.querySelectorAll('*'));
                for (const el of els) {
                    const direct = Array.from(el.childNodes).filter(n => n.nodeType === 3);
                    const txt = direct.map(n => n.textContent.trim()).join(' ');
                    if (/^Регион( компании)?$/.test(txt) || txt === 'Регионы') {
                        let block = el;
                        for (let i = 0; i < 4; i++) {
                            if (block.parentElement) block = block.parentElement;
                        }
                        return block.outerHTML.slice(0, 20000);
                    }
                }
                return null;
            }
            """
        )
        if region_block:
            (OUT / "05_region_block.html").write_text(clean(region_block))
            print("REGION BLOCK FOUND, len:", len(region_block))
        else:
            print("REGION BLOCK NOT FOUND")

        # все chips-trigger-input с контекстом
        chips = await page.evaluate(
            """
            () => {
                const out = [];
                document.querySelectorAll('[data-qa="chips-trigger-input"], [data-qa*="chips-trigger"]').forEach((el, i) => {
                    let ctx = el;
                    for (let i2 = 0; i2 < 6 && ctx.parentElement; i2++) ctx = ctx.parentElement;
                    out.push({
                        i,
                        html: el.outerHTML.slice(0, 500),
                        ctx_text: (ctx.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 200),
                    });
                });
                return out;
            }
            """
        )
        (OUT / "06_chips_inputs.json").write_text(json.dumps(chips, ensure_ascii=False, indent=1))

        # 5) Ввод региона: найти input внутри region-блока и ввести "Астана"
        # пробуем кликнуть на input с подсказкой-плейсхолдером о городе/регионе
        region_input = page.locator('[data-qa="chips-trigger-input"]')
        n = await region_input.count()
        print("chips-trigger-input count:", n)
        if n:
            # вводим в каждый и смотрим, где появится подсказка с "Астана"
            for i in range(min(n, 4)):
                try:
                    await region_input.nth(i).click(timeout=3000)
                    await page.wait_for_timeout(300)
                    await page.keyboard.type("Астана", delay=30)
                    await page.wait_for_timeout(3500)
                    sugg = await page.evaluate(
                        """
                        () => {
                            const out = [];
                            document.querySelectorAll('[role="option"], [data-qa*="suggest"], [data-qa*="chip-suggest"], [class*="suggest"] li, [class*="suggest"] [class*="item"]').forEach(el => {
                                const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                                if (t) out.push({qa: el.getAttribute('data-qa') || '', cls: el.className && el.className.toString().slice(0, 120), text: t.slice(0, 150)});
                            });
                            return out.slice(0, 30);
                        }
                        """
                    )
                    (OUT / f"07_suggestions_input_{i}.json").write_text(
                        json.dumps(sugg, ensure_ascii=False, indent=1)
                    )
                    print(f"input {i}: suggestions={len(sugg)}")
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(500)
                    # очистим
                    try:
                        await region_input.nth(i).fill("")
                    except Exception:
                        pass
                    await page.wait_for_timeout(300)
                    if sugg:
                        break
                except Exception as e:
                    print(f"input {i} error: {e}")

        # 6) Кнопки панели
        for qa in ("search-drawer-filters-submit", "search-filters-close", "search-drawer-filters-reset"):
            el = page.locator(f"[data-qa='{qa}']")
            vis = await el.first.is_visible() if await el.count() else None
            print(qa, "count=", await el.count(), "visible=", vis)
        (OUT / "08_drawer_buttons.json").write_text(
            json.dumps(
                {
                    qa: {"count": await page.locator(f"[data-qa='{qa}']").count()}
                    for qa in (
                        "search-drawer-filters-submit",
                        "search-filters-close",
                        "search-drawer-filters-reset",
                        "vacancy-search-text-input",
                        "search-filter-search_field-value-name",
                        "search-filter-search_field-value-company_name",
                        "search-filter-search_field-value-description",
                        "search-filter-value-with_salary",
                    )
                },
                ensure_ascii=False,
                indent=1,
            )
        )

        # 7) Состояние чекбоксов search_field по умолчанию
        states = await page.evaluate(
            """
            () => {
                const out = {};
                document.querySelectorAll('input[type="checkbox"]').forEach(el => {
                    const qa = el.closest('[data-qa]');
                    if (qa) out[qa.getAttribute('data-qa')] = el.checked;
                });
                return out;
            }
            """
        )
        (OUT / "09_checkbox_states.json").write_text(json.dumps(states, ensure_ascii=False, indent=1))

        # 8) Закрыть панель
        close = page.locator("[data-qa='search-filters-close']")
        if await close.count():
            await close.first.click()
            await page.wait_for_timeout(1500)

        # 9) Размер страницы на SERP
        page_size = await page.evaluate(
            """
            () => {
                const out = [];
                document.querySelectorAll('[data-qa]').forEach(el => {
                    const qa = el.getAttribute('data-qa') || '';
                    const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                    if (/страниц|запис|показ/.test(qa) || (/^\\d+$/.test(t) && /20|50|100/.test(t))) {
                        out.push({qa, tag: el.tagName.toLowerCase(), text: t.slice(0, 100), html: el.outerHTML.slice(0, 400)});
                    }
                });
                // ищем select
                document.querySelectorAll('select').forEach(el => {
                    out.push({sel: 1, name: el.name, html: el.outerHTML.slice(0, 600)});
                });
                return out.slice(0, 40);
            }
            """
        )
        (OUT / "10_page_size.json").write_text(json.dumps(page_size, ensure_ascii=False, indent=1))

        # 10) Пустая выдача
        await page.goto("https://hh.ru/search/vacancy?text=zzzzqqqqxxx12345", timeout=90000)
        await page.wait_for_timeout(5000)
        empty = await page.evaluate(
            """
            () => {
                const body = document.body.innerText;
                const m = body.match(/Ничего не найдено[\\s\\S]{0,400}/);
                const nores = Array.from(document.querySelectorAll('[data-qa]')).filter(el =>
                    /no-result|nothing|empty/i.test(el.getAttribute('data-qa') || '')
                ).map(el => el.outerHTML.slice(0, 500));
                return {found_text: m ? m[0] : null, nores: nores.slice(0, 5)};
            }
            """
        )
        (OUT / "11_empty_state.json").write_text(json.dumps(empty, ensure_ascii=False, indent=1))
        print("EMPTY STATE:", json.dumps(empty, ensure_ascii=False)[:300])

    finally:
        await save_browser_session(context)
        await context.close()
        await browser.close()
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())

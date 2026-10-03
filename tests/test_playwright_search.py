"""
Регрессионные тесты нового поиска hh.ru (задача 001).

Форма проверяется на локальной HTML-странице, имитирующей новую выдачу hh.ru
(tests/fixtures/hh_search.html): перехват адресов hh.ru через page.route,
реальный браузер Playwright. Сетевые ответы (502/пустая выдача) подменяются.
Реальные сообщения никуда не отправляются.
"""
import json
import urllib.parse
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from src.job_manager.playwright_manager import (
    PlaywrightJobManager,
    SearchPageError,
    SearchSetupError,
)
from src.job_manager.search_probe import SearchProbe
from src.telegram.telegram_error_handler import AsyncTelegramSink

FIXTURE = Path(__file__).parent / "fixtures" / "hh_search.html"

ERROR_PAGE_HTML = (
    "<html><head><meta charset='utf-8'></head><body>"
    "<h1>Страница временно недоступна</h1><p>Ошибка 502</p>"
    "</body></html>"
)
EMPTY_PAGE_HTML = (
    "<html><head><meta charset='utf-8'></head><body>"
    "<div data-qa='empty-vacancy-search-block'>Ничего не найдено</div>"
    "</body></html>"
)
UNKNOWN_PAGE_HTML = (
    "<html><head><meta charset='utf-8'></head><body>"
    "<div>Неизвестная страница</div>"
    "</body></html>"
)


def make_serp_html(
    regions=None,
    page_size_default: str = "50",
    has_search_input: bool = True,
    suggestion_delay_ms: int = 0,
    pick_page_size_disabled: bool = False,
    apply_drop_text: bool = False,
    apply_drop_area_ids=None,
    apply_uncheck_flags=None,
    apply_drop_flags=None,
    clear_suggestions_on_pick: bool = False,
    enter_navigates: bool = False,
    has_search_field_checkboxes: bool = True,
) -> str:
    """Локальная выдача hh.ru с настраиваемым списком регионов.

    Тест-патч потери условий при отправке панели (R1):
    - apply_drop_text — applyFilters убирает text из адреса;
    - apply_drop_area_ids — убирает указанные id регионов;
    - apply_uncheck_flags — снимает чекбоксы области поиска до сборки адреса;
    - apply_drop_flags — флажки не попадают в адрес, чекбоксы остаются включёнными
      (наблюдаемое состояние панели — источник истины).

    Прочие режимы:
    - clear_suggestions_on_pick (R1a) — после выбора подсказки удаляется из DOM;
    - enter_navigates (R3a) — Enter делает полноценную навигацию (сеть, possible 502);
    - has_search_field_checkboxes — наличие чекбоксов области поиска в панели.
    """
    template = FIXTURE.read_text(encoding="utf-8")
    if regions is None:
        regions = [
            {"name": "Астана", "id": 159},
            {"name": "Алматы", "id": 160},
            {"name": "Астана область", "id": 161},
        ]
    if apply_drop_area_ids is None:
        apply_drop_area_ids = []
    if apply_uncheck_flags is None:
        apply_uncheck_flags = []
    if apply_drop_flags is None:
        apply_drop_flags = []
    template = template.replace("/*__REGIONS__*/null", json.dumps(regions, ensure_ascii=False))
    template = template.replace("/*__PAGE_SIZE_DEFAULT__*/", page_size_default)
    template = template.replace("/*__SUGGESTION_DELAY__*/0", str(int(suggestion_delay_ms)))
    template = template.replace(
        "/*__PICK_DISABLED__*/false", "true" if pick_page_size_disabled else "false"
    )
    template = template.replace(
        "/*__APPLY_DROP_TEXT__*/false", "true" if apply_drop_text else "false"
    )
    template = template.replace(
        "/*__APPLY_DROP_AREAS__*/[]", json.dumps(list(apply_drop_area_ids))
    )
    template = template.replace(
        "/*__APPLY_UNCHECK_FLAGS__*/[]", json.dumps(list(apply_uncheck_flags))
    )
    template = template.replace(
        "/*__APPLY_DROP_FLAGS__*/[]", json.dumps(list(apply_drop_flags))
    )
    template = template.replace(
        "/*__CLEAR_SUGGESTIONS__*/false", "true" if clear_suggestions_on_pick else "false"
    )
    template = template.replace(
        "/*__ENTER_NAVIGATES__*/false", "true" if enter_navigates else "false"
    )
    if not has_search_input:
        template = template.replace(
            '  <input data-qa="search-input" name="text" value="" aria-label="Поиск по вакансиям">\n',
            "",
        )
    if not has_search_field_checkboxes:
        for key, label in (
            ("name", " в названии вакансии"),
            ("company_name", " в названии компании"),
            ("description", " в описании вакансии"),
        ):
            template = template.replace(
                f'    <label><input data-qa="search-filter-search_field-value-{key}" '
                f'type="checkbox" name="search_field" value="{key}" checked>{label}</label>\n',
                "",
            )
    return template


@pytest_asyncio.fixture
async def page():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context()
    page = await context.new_page()
    yield page
    await context.close()
    await browser.close()
    await pw.stop()


def make_manager(page) -> PlaywrightJobManager:
    """Менеджер с вшитой страницей и без реальных пауз."""
    manager = PlaywrightJobManager(secrets={})
    manager.page = page
    manager.SEARCH_PAGE_RETRY_BASE_DELAY = 0.01
    manager.SEARCH_RESULTS_WAIT_MS = 1500
    manager.PAGE_SIZE_VERIFY_POLLS = 2

    async def _no_pause(*args, **kwargs):
        return None

    manager.pause_async = _no_pause
    return manager


SEARCH_PARAMS = {
    "keywords": "data",
    "area": "Астана, Алматы",
    "search_field": {"name": True, "company_name": True, "description": True},
    "only_with_salary": False,
    "show": {"show_20": True, "show_50": False, "show_100": False},
}


@pytest.mark.asyncio
async def test_new_panel_applies_keywords_areas_and_flags(page):
    """1. Старый /advanced не используется; панель получает data, оба региона,
    области поиска и правильное состояние флажков; повторное применение
    не снимает флажки."""
    serp = make_serp_html()
    await page.route("**/search/vacancy**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=serp
    ))
    manager = make_manager(page)

    await manager.set_advanced_search_params(dict(SEARCH_PARAMS), "resume123")

    parsed = urllib.parse.urlparse(page.url)
    assert "/search/vacancy/advanced" not in page.url
    query = urllib.parse.parse_qs(parsed.query)
    assert query.get("text") == ["data"]
    assert sorted(query.get("area", [])) == ["159", "160"]
    assert query.get("search_field") == ["name", "company_name", "description"]
    assert "with_salary" not in query  # только с зарплатой выключено
    assert manager.search_page_url == page.url

    # Панель получила ключевые слова
    drawer_text = await page.locator("[data-qa='vacancy-search-text-input']").first.input_value()
    assert drawer_text == "data"
    # Размер выдачи переключился на 20
    size_label = await page.locator("#page-size-label").text_content()
    assert size_label.strip() == "20"

    # Повторное применение не снимает флажки
    await manager.set_advanced_search_params(dict(SEARCH_PARAMS), "resume123")
    for key in ("name", "company_name", "description"):
        assert await page.locator(f"[data-qa='search-filter-search_field-value-{key}']").first.is_checked()
    assert not await page.locator("[data-qa='search-filter-value-with_salary']").first.is_checked()
    query2 = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query)
    assert query2.get("text") == ["data"]
    assert sorted(query2.get("area", [])) == ["159", "160"]


@pytest.mark.asyncio
async def test_stale_suggestions_do_not_break_second_region(page):
    """2в. Поведение живого hh.ru: подсказки приходят асинхронно, до их обновления
    в списке остаются опции предыдущего запроса — второй регион всё равно применяется."""
    serp = make_serp_html(suggestion_delay_ms=1500)
    await page.route("**/search/vacancy**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=serp
    ))
    manager = make_manager(page)

    await manager.set_advanced_search_params(dict(SEARCH_PARAMS), "resume123")

    query = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query)
    assert sorted(query.get("area", [])) == ["159", "160"]


@pytest.mark.asyncio
async def test_missing_region_is_error_before_submit(page):
    """2. Невозможно применить заданный регион — ошибка до отправки поиска."""
    serp = make_serp_html(regions=[])  # подсказки регионов отсутствуют
    await page.route("**/search/vacancy**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=serp
    ))
    manager = make_manager(page)

    with pytest.raises(SearchSetupError, match="Астана"):
        await manager.set_advanced_search_params(dict(SEARCH_PARAMS), "resume123")

    # Поиск не отправлялся: адреса без регионов, панель осталась открытой
    query = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query)
    assert "area" not in query
    assert await page.locator("[data-qa='search-drawer-filters-submit']").count() > 0


@pytest.mark.asyncio
async def test_missing_keywords_input_is_error(page):
    """2б. Невозможно применить ключевые слова — ошибка до отправки поиска."""
    serp = make_serp_html(has_search_input=False)  # верхнего поля поиска нет
    await page.route("**/search/vacancy**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=serp
    ))
    manager = make_manager(page)

    with pytest.raises(SearchSetupError, match="ключевые слова|search-input"):
        await manager.set_advanced_search_params(dict(SEARCH_PARAMS), "resume123")


@pytest.mark.asyncio
async def test_lost_keywords_and_areas_raise_before_processing(page):
    """R1. Отправка панели потеряла ключевые слова и оба региона — явная
    ошибка до обработки вакансий, а не «успешный» поиск без условий."""
    serp = make_serp_html(apply_drop_text=True, apply_drop_area_ids=[159, 160])
    await page.route("**/search/vacancy**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=serp
    ))
    manager = make_manager(page)
    params = dict(SEARCH_PARAMS)
    params["show"] = None  # изолируем отправку панели

    with pytest.raises(SearchSetupError, match="не применены"):
        await manager.set_advanced_search_params(params, "resume123")

    # Адрес не сохранён как рабочий
    assert manager.search_page_url == ""


@pytest.mark.asyncio
async def test_lost_single_area_raises(page):
    """R1. Из двух заданных регионов в адресе остался один — явная ошибка."""
    serp = make_serp_html(apply_drop_area_ids=[160])
    await page.route("**/search/vacancy**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=serp
    ))
    manager = make_manager(page)
    params = dict(SEARCH_PARAMS)
    params["show"] = None

    with pytest.raises(SearchSetupError, match="Алматы"):
        await manager.set_advanced_search_params(params, "resume123")


@pytest.mark.asyncio
async def test_lost_search_field_flag_raises(page):
    """R1. Заданный флажок области поиска «сброшен» при отправке — явная
    ошибка: параметра нет в адресе, и чекбокс в панели снят (наблюдаемое состояние)."""
    serp = make_serp_html(apply_uncheck_flags=["name"])
    await page.route("**/search/vacancy**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=serp
    ))
    manager = make_manager(page)
    params = dict(SEARCH_PARAMS)
    params["show"] = None

    with pytest.raises(SearchSetupError, match="name"):
        await manager.set_advanced_search_params(params, "resume123")


@pytest.mark.asyncio
async def test_validation_catches_lost_conditions_after_page_size(page):
    """R1. Потеря условий после смены размера выдачи — явная ошибка."""
    serp = make_serp_html()
    await page.route("**/*", lambda route: route.fulfill(
        status=200, content_type="text/html", body=serp
    ))
    manager = make_manager(page)
    await manager.set_advanced_search_params(dict(SEARCH_PARAMS), "resume123")

    # Имитация: смена размера привела к перерисовке без параметров поиска
    await page.goto("https://hh.ru/search/vacancy?items_on_page=20")
    with pytest.raises(SearchSetupError, match="не применены"):
        await manager._validate_search_applied()


@pytest.mark.asyncio
async def test_region_id_read_before_suggestions_cleared(page):
    """R1a. Живое поведение hh.ru: после выбора подсказка удаляется из DOM,
    и индекс в глобальном списке magritte-select-option-* указывает на другой
    элемент (опцию размера страницы). Id региона читается до клика, поиск
    проходит проверку с реальными id."""
    serp = make_serp_html(clear_suggestions_on_pick=True)
    await page.route("**/search/vacancy**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=serp
    ))
    manager = make_manager(page)

    await manager.set_advanced_search_params(dict(SEARCH_PARAMS), "resume123")

    assert manager._applied_area_ids == {"Астана": 159, "Алматы": 160}
    query = urllib.parse.parse_qs(urllib.parse.urlparse(manager.search_page_url).query)
    assert sorted(query.get("area", [])) == ["159", "160"]


@pytest.mark.asyncio
async def test_region_without_numeric_id_is_error(page):
    """R1a. У выбранного региона нет читаемого числового id — явная ошибка:
    неполное подтверждение условий не даёт успешного поиска."""
    serp = make_serp_html(
        regions=[
            {"name": "Астана", "id": "abc"},
            {"name": "Алматы", "id": "def"},
        ]
    )
    await page.route("**/search/vacancy**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=serp
    ))
    manager = make_manager(page)

    with pytest.raises(SearchSetupError, match="id"):
        await manager.set_advanced_search_params(dict(SEARCH_PARAMS), "resume123")


@pytest.mark.asyncio
async def test_flags_missing_from_url_but_checked_in_panel_pass(page):
    """R1b. Параметры области поиска отсутствуют в адресе, но чекбоксы панели
    включены (наблюдаемое состояние) — поиск не отклоняется."""
    serp = make_serp_html(apply_drop_flags=["name", "company_name", "description"])
    await page.route("**/search/vacancy**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=serp
    ))
    manager = make_manager(page)
    params = dict(SEARCH_PARAMS)
    params["show"] = None

    await manager.set_advanced_search_params(params, "resume123")

    query = urllib.parse.parse_qs(urllib.parse.urlparse(manager.search_page_url).query)
    assert "search_field" not in query
    assert query.get("text") == ["data"]
    assert sorted(query.get("area", [])) == ["159", "160"]


@pytest.mark.asyncio
async def test_missing_panel_checkbox_is_error(page):
    """R1b. Параметра нет в адресе, а чекбокса — в панели: сбой чтения не
    выдаётся за «выключено» — явная ошибка."""
    serp = make_serp_html(has_search_field_checkboxes=False)
    await page.route("**/search/vacancy**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=serp
    ))
    manager = make_manager(page)
    await page.goto("https://hh.ru/search/vacancy?text=data")
    manager.search_params = dict(SEARCH_PARAMS)
    manager._applied_area_ids = {}

    with pytest.raises(SearchSetupError, match="чекбокс"):
        await manager._read_panel_filter_states()


@pytest.mark.asyncio
async def test_502_then_working_results(page):
    """3. 502 -> рабочая выдача: ограниченный повтор, фильтры сохранены, карточки получены."""
    serp = make_serp_html()
    state = {"calls": 0}

    async def handler(route):
        if "/search/vacancy" in route.request.url:
            state["calls"] += 1
            if state["calls"] <= 2:
                await route.fulfill(status=502, content_type="text/html", body=ERROR_PAGE_HTML)
                return
        await route.fulfill(status=200, content_type="text/html", body=serp)

    await page.route("**/*", handler)
    manager = make_manager(page)

    await manager.set_advanced_search_params(dict(SEARCH_PARAMS), "resume123")
    vacancies = await manager.get_vacancies_from_page(0)

    assert len(vacancies) == 3
    assert state["calls"] <= 2 + manager.SEARCH_PAGE_MAX_ATTEMPTS
    query = urllib.parse.parse_qs(urllib.parse.urlparse(manager.search_page_url).query)
    assert query.get("text") == ["data"]
    assert sorted(query.get("area", [])) == ["159", "160"]
    assert vacancies[0]["id"] == "1000"
    assert vacancies[0]["employer"]["name"] == "TestCo"


@pytest.mark.asyncio
async def test_post_submit_502_retried_then_success_keeps_conditions(page):
    """R3. Первая 502 возникает именно после submit: повтор того же запроса
    с уже выбранными условиями успешен, фильтры сохранены."""
    serp = make_serp_html()
    state = {"urls": []}

    async def handler(route):
        state["urls"].append(route.request.url)
        if len(state["urls"]) == 1:
            await route.fulfill(status=502, content_type="text/html", body=ERROR_PAGE_HTML)
        else:
            await route.fulfill(status=200, content_type="text/html", body=serp)

    await page.route("**/*", handler)
    manager = make_manager(page)
    # Исходный переход с условиями (как после submit панели)
    await page.goto("https://hh.ru/search/vacancy?text=data&area=159&area=160&search_field=name")

    await manager._wait_for_search_results()

    # Исходная + один повтор, не больше
    assert len(state["urls"]) == 2
    retry_query = urllib.parse.parse_qs(urllib.parse.urlparse(state["urls"][1]).query)
    assert retry_query.get("text") == ["data"]
    assert sorted(retry_query.get("area", [])) == ["159", "160"]
    assert retry_query.get("search_field") == ["name"]


@pytest.mark.asyncio
async def test_post_submit_persistent_error_hits_limit(page):
    """R3. Постоянная 502 после submit: явная ошибка по лимиту попыток,
    не больше SEARCH_PAGE_MAX_ATTEMPTS запросов, включая исходный переход."""
    state = {"calls": 0}

    async def handler(route):
        state["calls"] += 1
        await route.fulfill(status=502, content_type="text/html", body=ERROR_PAGE_HTML)

    await page.route("**/*", handler)
    manager = make_manager(page)
    await page.goto("https://hh.ru/search/vacancy?text=data&area=159&area=160")

    with pytest.raises(SearchPageError):
        await manager._wait_for_search_results()

    assert state["calls"] == manager.SEARCH_PAGE_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_enter_502_with_correct_text_is_retried(page):
    """R3a. 502 после Enter приходит по адресу с правильным text: адрес не
    доказывает успех — выполняется повтор того же запроса, выдача рабочая."""
    serp = make_serp_html(enter_navigates=True)
    state = {"text_calls": 0}

    async def handler(route):
        if "text=data" in route.request.url:
            state["text_calls"] += 1
            if state["text_calls"] == 1:
                await route.fulfill(status=502, content_type="text/html", body=ERROR_PAGE_HTML)
                return
        await route.fulfill(status=200, content_type="text/html", body=serp)

    await page.route("**/*", handler)
    manager = make_manager(page)
    manager.search_params = dict(SEARCH_PARAMS)
    await page.goto("https://hh.ru/search/vacancy")

    await manager._set_keywords()

    # Исходный переход + один повтор
    assert state["text_calls"] == 2
    assert "text=data" in page.url
    assert await page.locator('[data-qa="vacancy-serp__vacancy"]').count() == 3


@pytest.mark.asyncio
async def test_enter_persistent_502_raises(page):
    """R3a. Постоянный 502 после Enter (адрес с правильным text): явная ошибка
    по лимиту, запросов ровно SEARCH_PAGE_MAX_ATTEMPTS, включая исходный."""
    state = {"text_calls": 0}
    serp = make_serp_html(enter_navigates=True)

    async def handler(route):
        if "text=data" in route.request.url:
            state["text_calls"] += 1
            await route.fulfill(status=502, content_type="text/html", body=ERROR_PAGE_HTML)
            return
        await route.fulfill(status=200, content_type="text/html", body=serp)

    await page.route("**/*", handler)
    manager = make_manager(page)
    manager.search_params = dict(SEARCH_PARAMS)
    await page.goto("https://hh.ru/search/vacancy")

    with pytest.raises(SearchPageError):
        await manager._set_keywords()

    assert state["text_calls"] == manager.SEARCH_PAGE_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_enter_error_page_with_http_200_raises(page):
    """R3a. Страница ошибки с HTTP 200 и правильным text после Enter: явная
    ошибка по лимиту, а не успех."""
    state = {"text_calls": 0}
    serp = make_serp_html(enter_navigates=True)

    async def handler(route):
        if "text=data" in route.request.url:
            state["text_calls"] += 1
            await route.fulfill(status=200, content_type="text/html", body=ERROR_PAGE_HTML)
            return
        await route.fulfill(status=200, content_type="text/html", body=serp)

    await page.route("**/*", handler)
    manager = make_manager(page)
    manager.search_params = dict(SEARCH_PARAMS)
    await page.goto("https://hh.ru/search/vacancy")

    with pytest.raises(SearchPageError):
        await manager._set_keywords()

    assert state["text_calls"] == manager.SEARCH_PAGE_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_persistent_502_raises_not_empty(page):
    """4а. Постоянный 502: явная ошибка после лимита попыток, а не []."""
    state = {"calls": 0}

    async def handler(route):
        if "/search/vacancy" in route.request.url:
            state["calls"] += 1
        await route.fulfill(status=502, content_type="text/html", body=ERROR_PAGE_HTML)

    await page.route("**/*", handler)
    manager = make_manager(page)

    with pytest.raises(SearchPageError):
        await manager.set_advanced_search_params(dict(SEARCH_PARAMS), "resume123")

    assert state["calls"] == manager.SEARCH_PAGE_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_error_page_with_http_200_raises(page):
    """4б. Страница ошибки при HTTP 200: явная ошибка после лимита, а не []."""
    state = {"calls": 0}

    async def handler(route):
        if "/search/vacancy" in route.request.url:
            state["calls"] += 1
        await route.fulfill(status=200, content_type="text/html", body=ERROR_PAGE_HTML)

    await page.route("**/*", handler)
    manager = make_manager(page)
    manager.search_page_url = "https://hh.ru/search/vacancy?text=data&area=159&area=160"

    with pytest.raises(SearchPageError):
        await manager.get_vacancies_from_page(0)

    assert state["calls"] == manager.SEARCH_PAGE_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_confirmed_empty_results_return_empty_list(page):
    """5а. Подтверждённая пустая выдача возвращает []."""
    await page.route("**/*", lambda route: route.fulfill(
        status=200, content_type="text/html", body=EMPTY_PAGE_HTML
    ))
    manager = make_manager(page)
    manager.search_page_url = "https://hh.ru/search/vacancy?text=zzz"

    assert await manager.get_vacancies_from_page(0) == []


@pytest.mark.asyncio
async def test_unknown_markup_raises(page):
    """5б. Нет ни карточек, ни явного признака пустой выдачи — ошибка."""
    await page.route("**/*", lambda route: route.fulfill(
        status=200, content_type="text/html", body=UNKNOWN_PAGE_HTML
    ))
    manager = make_manager(page)
    manager.search_page_url = "https://hh.ru/search/vacancy?text=data"

    with pytest.raises(SearchPageError):
        await manager.get_vacancies_from_page(0)


@pytest.mark.asyncio
async def test_pagination_preserves_search_params(page):
    """6. Следующая страница после просмотра вакансии сохраняет параметры поиска."""
    serp = make_serp_html()
    requested_urls = []

    async def handler(route):
        requested_urls.append(route.request.url)
        await route.fulfill(status=200, content_type="text/html", body=serp)

    await page.route("**/*", handler)
    manager = make_manager(page)
    manager.search_page_url = "https://hh.ru/search/vacancy?text=data&area=159&area=160&search_field=name"

    # «Посещаем» вакансию
    await page.goto("https://hh.ru/vacancy/1001")

    vacancies = await manager.get_vacancies_from_page(1)
    assert len(vacancies) == 3

    last = urllib.parse.urlparse(requested_urls[-1])
    query = urllib.parse.parse_qs(last.query)
    assert last.path == "/search/vacancy"
    assert query.get("text") == ["data"]
    assert sorted(query.get("area", [])) == ["159", "160"]
    assert query.get("search_field") == ["name"]
    assert query.get("page") == ["1"]


@pytest.mark.asyncio
async def test_page_size_fallback_via_url_when_ui_click_ignored(page):
    """6б. Если клик по размеру в меню не применяется (перерисовка страницы),
    размер всё равно применяется через URL-параметр items_on_page и проверяется."""
    serp = make_serp_html(pick_page_size_disabled=True)
    await page.route("**/search/vacancy**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=serp
    ))
    manager = make_manager(page)

    await manager.set_advanced_search_params(dict(SEARCH_PARAMS), "resume123")

    query = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query)
    assert query.get("items_on_page") == ["20"]
    assert query.get("text") == ["data"]
    assert sorted(query.get("area", [])) == ["159", "160"]
    size_label = await page.locator("#page-size-label").text_content()
    assert size_label.strip() == "20"
    assert manager.search_page_url == page.url


def _make_probe_mocks(
    adapter_side_effect=None,
    chain_after_invoke: Exception = None,
    reply_metadata=None,
):
    """Мок-компоненты для проверки поведенческих ограничений SearchProbe.

    job_is_interesting имитирует реальный метод: ошибка вызова/разбора
    цепочки глотается и возвращается как словарь с score 0 (не исключение).

    - adapter_side_effect — поведение ai_adapter.invoke (исключение/список);
    - chain_after_invoke — исключение после успешного adapter.invoke
      (сломанный структурированный ответ);
    - reply_metadata — response_metadata ответа LLM.
    """
    vacancies = [
        {
            "name": f"Vacancy {i}",
            "id": str(100 + i),
            "alternate_url": f"https://hh.ru/vacancy/{100 + i}",
            "employer": {"id": "55", "name": "TestCo"},
        }
        for i in range(5)
    ]
    jobs = [
        {
            "job_title": f"Vacancy {i}",
            "vacancy_id": str(100 + i),
            "company_id": "55",
            "company_name": "TestCo",
        }
        for i in range(5)
    ]

    manager = MagicMock()
    manager.search_page_url = "https://hh.ru/search/vacancy?text=data&area=159&area=160"
    manager._applied_area_ids = {"Астана": 159, "Алматы": 160}
    manager.get_vacancies_from_page = AsyncMock(return_value=vacancies)
    manager.apply_to_vacancy = AsyncMock()
    orig_raise_resume = AsyncMock(name="orig_raise_resume")
    manager.raise_resume = orig_raise_resume

    resume_component = MagicMock()
    resume_component.get_resume_info = AsyncMock(return_value=({}, "readable"))

    search_component = MagicMock()
    search_component.search_params = dict(SEARCH_PARAMS)
    search_component.start_search = AsyncMock()

    apply_component = MagicMock()
    apply_component.scrape_vacancy = AsyncMock(side_effect=jobs)
    apply_component.send_repsonse = AsyncMock()
    apply_component.apply_job = AsyncMock()

    reply = MagicMock()
    reply.response_metadata = (
        reply_metadata if reply_metadata is not None else {"model_name": "openai/gpt-6-luna"}
    )

    gpt = MagicMock()
    gpt.ai_adapter.model.fallback_models = ["google/gemini-2.5-flash"]
    gpt.ai_adapter.model.model_name = "openai/gpt-6-luna"
    gpt.ai_adapter.invoke = MagicMock(
        return_value=reply if adapter_side_effect is None else None,
        side_effect=adapter_side_effect,
    )

    # Реальная цепочка вызовов: job_is_interesting -> chain.invoke -> ai_adapter.invoke
    def _chain_invoke(prompt):
        gpt.ai_adapter.invoke(prompt)
        if chain_after_invoke is not None:
            raise chain_after_invoke
        return {"score": 80, "reasoning": "ok"}

    chain = MagicMock()
    chain.invoke = _chain_invoke
    parser = MagicMock()
    parser.get_format_instructions.return_value = "fmt"
    gpt.chains = {"job_is_interesting": (chain, parser)}

    def _job_is_interesting():
        # Как реальный метод: цепочка ищется в self.chains при каждом вызове
        # (проба подменяет запись на прокси), ошибка глотается и возвращается
        # как словарь — не как исключение
        chain_entry, parser_entry = gpt.chains["job_is_interesting"]
        try:
            return chain_entry.invoke(
                {
                    "resume": "resume",
                    "job_description": "job",
                    "search_parameters": {},
                    "format_instructions": parser_entry.get_format_instructions(),
                }
            )
        except Exception:
            return {"score": 0, "reasoning": "Ошибка при вызове LLM"}

    gpt.job_is_interesting = MagicMock(side_effect=_job_is_interesting)
    gpt.set_job = MagicMock()

    probe = SearchProbe(manager, resume_component, search_component, apply_component, gpt)
    return probe, manager, apply_component, gpt, orig_raise_resume


@pytest.mark.asyncio
async def test_probe_no_external_side_effects_and_stops_after_three():
    """7. Пробный запуск: без отправки отклика, поднятия резюме и Telegram;
    останавливается после трёх вакансий; фактическая модель сохраняется.
    R2: run() сам зону не снимает — блокировки активны до явного restore."""
    probe, manager, apply_component, gpt, orig_raise_resume = _make_probe_mocks()
    orig_sink_call = AsyncTelegramSink.__call__

    probe._block_external_side_effects()
    probe._capture_llm_model()

    # Во время проверки блокировки активны
    assert manager.raise_resume is not orig_raise_resume
    assert AsyncTelegramSink.__call__ is not orig_sink_call
    assert gpt.ai_adapter.model.fallback_models == []

    try:
        report = await probe.run()

        assert report["status"] == "ok"
        assert report["responses_sent"] == 0
        assert len(report["vacancies_checked"]) == SearchProbe.MAX_VACANCIES == 3
        assert apply_component.scrape_vacancy.await_count == 3
        apply_component.send_repsonse.assert_not_awaited()
        apply_component.apply_job.assert_not_awaited()
        manager.apply_to_vacancy.assert_not_awaited()

        # Подъём резюме ни разу не выполнялся
        orig_raise_resume.assert_not_awaited()
        assert report["resumes_raised"] == 0
        assert report["telegram_messages_sent"] == 0

        # Отчёт различает запрошенные и подтверждённые фильтры
        assert report["requested_filters"]["keywords"] == "data"
        assert report["requested_filters"]["areas"] == ["Астана", "Алматы"]
        assert report["confirmed_filters"]["keywords"] == "data"
        assert report["confirmed_filters"]["areas"] == ["Астана", "Алматы"]
        assert report["expected_model"] == "openai/gpt-6-luna"

        # R2: после run зона НЕ снята — снимает вызывающий
        assert AsyncTelegramSink.__call__ is not orig_sink_call
        assert manager.raise_resume is not orig_raise_resume
    finally:
        probe._restore_external_side_effects()

    # После снятия зоны всё восстановлено
    assert AsyncTelegramSink.__call__ is orig_sink_call
    assert manager.raise_resume is orig_raise_resume

    # Резервные модели отключались в памяти и восстановлены; модель — из ответа
    assert gpt.ai_adapter.model.fallback_models == ["google/gemini-2.5-flash"]
    assert report["actual_model"] == "openai/gpt-6-luna"
    assert gpt.ai_adapter.invoke.call_count == 3


@pytest.mark.asyncio
async def test_probe_llm_failure_on_first_vacancy_is_error():
    """R4. Отказ модели на первой вакансии: штатная оценка возвращает
    {'score': 0, ...} вместо исключения — статус не ok, модель не подтверждена."""
    probe, manager, apply_component, gpt, _ = _make_probe_mocks(
        adapter_side_effect=RuntimeError("LLM недоступна")
    )
    orig_sink_call = AsyncTelegramSink.__call__

    probe._block_external_side_effects()
    probe._capture_llm_model()
    try:
        report = await probe.run()
    finally:
        probe._restore_external_side_effects()

    assert report["status"] == "error"
    assert "LLM" in report["error"]
    assert report["vacancies_checked"] == []
    assert report["actual_model"] is None
    assert AsyncTelegramSink.__call__ is orig_sink_call


@pytest.mark.asyncio
async def test_probe_llm_failure_on_last_vacancy_keeps_partials():
    """R4. Отказ модели на последней (третьей) вакансии: статус не ok,
    частичные результаты сохранены."""
    reply = MagicMock()
    reply.response_metadata = {"model_name": "openai/gpt-6-luna"}
    probe, manager, apply_component, gpt, _ = _make_probe_mocks(
        adapter_side_effect=[reply, reply, RuntimeError("LLM недоступна")]
    )
    probe._block_external_side_effects()
    probe._capture_llm_model()
    try:
        report = await probe.run()
    finally:
        probe._restore_external_side_effects()

    assert report["status"] == "error"
    assert len(report["vacancies_checked"]) == 2
    assert [v["name"] for v in report["vacancies_checked"]] == ["Vacancy 0", "Vacancy 1"]
    assert report["actual_model"] == "openai/gpt-6-luna"


@pytest.mark.asyncio
async def test_probe_broken_structured_response_is_error():
    """R4. Сломанный структурированный ответ: LLM ответил, но разбор цепочки
    упал — статус не ok (явный сигнал, не сравнение текста сообщения)."""
    probe, manager, apply_component, gpt, _ = _make_probe_mocks(
        chain_after_invoke=ValueError("Failed to parse LLM output")
    )
    probe._block_external_side_effects()
    probe._capture_llm_model()
    try:
        report = await probe.run()
    finally:
        probe._restore_external_side_effects()

    assert report["status"] == "error"
    assert "LLM" in report["error"]
    assert report["vacancies_checked"] == []


@pytest.mark.asyncio
async def test_probe_missing_model_metadata_is_error():
    """R4. Ответ LLM без метаданных модели — не подтверждает проверку Luna."""
    probe, manager, apply_component, gpt, _ = _make_probe_mocks(reply_metadata={})
    probe._block_external_side_effects()
    probe._capture_llm_model()
    try:
        report = await probe.run()
    finally:
        probe._restore_external_side_effects()

    assert report["status"] == "error"
    assert "модель" in report["error"]
    assert report["actual_model"] is None


@pytest.mark.asyncio
async def test_probe_model_substitution_is_error():
    """R4. Подмена модели: ответ пришёл от другой модели — статус не ok."""
    probe, manager, apply_component, gpt, _ = _make_probe_mocks(
        reply_metadata={"model_name": "openai/gpt-5-legacy"}
    )
    probe._block_external_side_effects()
    probe._capture_llm_model()
    try:
        report = await probe.run()
    finally:
        probe._restore_external_side_effects()

    assert report["status"] == "error"
    assert "Подмена модели" in report["error"]
    assert report["vacancies_checked"] == []


@pytest.mark.asyncio
async def test_probe_resume_call_does_not_confirm_model():
    """R4. Успешный разбор контактов резюме (до проверки) не используется как
    модель оценки: при неуспешных оценках модель не подтверждена, при успешных —
    модель именно оценочных ответов."""
    # Сценарий 1: контакты разобраны (чужая модель), все оценки упали
    legacy_reply = MagicMock()
    legacy_reply.response_metadata = {"model_name": "legacy/contact-model"}
    probe, manager, apply_component, gpt, _ = _make_probe_mocks(
        adapter_side_effect=[
            legacy_reply,
            RuntimeError("LLM недоступна"),
            RuntimeError("LLM недоступна"),
            RuntimeError("LLM недоступна"),
        ]
    )
    probe._block_external_side_effects()
    probe._capture_llm_model()
    gpt.ai_adapter.invoke("resume contacts")  # успешный разбор контактов
    try:
        report = await probe.run()
    finally:
        probe._restore_external_side_effects()

    assert report["status"] == "error"
    assert report["actual_model"] is None

    # Сценарий 2: контакты разобраны (чужая модель), оценки успешные
    reply = MagicMock()
    reply.response_metadata = {"model_name": "openai/gpt-6-luna"}
    probe2, manager2, apply2, gpt2, _ = _make_probe_mocks(
        adapter_side_effect=[legacy_reply, reply, reply, reply]
    )
    probe2._block_external_side_effects()
    probe2._capture_llm_model()
    gpt2.ai_adapter.invoke("resume contacts")
    try:
        report2 = await probe2.run()
    finally:
        probe2._restore_external_side_effects()

    assert report2["status"] == "ok"
    assert report2["actual_model"] == "openai/gpt-6-luna"
    assert len(report2["vacancies_checked"]) == 3


@pytest.mark.asyncio
async def test_verify_main_block_zone_covers_full_lifecycle(page, monkeypatch):
    """R2. Полный verify_search.main() с подменённой сетью: ошибка поиска,
    ошибка резюме и ошибка закрытия — ни одного вызова настоящего отправителя,
    состояние восстановлено после выхода."""
    from scripts import verify_search
    from src.job_manager.resume_scraper import ResumeScraper

    events = []
    recorder = lambda self, *args, **kwargs: events.append(args)

    # Сеть: только локальная страница ошибки (никаких реальных запросов)
    await page.route(
        "**/*",
        lambda route: route.fulfill(status=502, content_type="text/html", body=ERROR_PAGE_HTML),
    )

    closed = []

    async def fake_initialize(self):
        self.page = page

    async def fake_close(self):
        closed.append(True)

    async def fake_resume_params(self):
        return "resume123", ["AI/ML"]

    async def fake_resume_info(self):
        return {}, "readable"

    async def no_pause(self, *args, **kwargs):
        return None

    # Отчёты теста не должны попадать в реальный data_folder/output
    saved_reports = []
    monkeypatch.setattr(verify_search, "save_yaml_file", lambda path, data: saved_reports.append((str(path), data)))

    monkeypatch.setattr(PlaywrightJobManager, "initialize", fake_initialize)
    monkeypatch.setattr(PlaywrightJobManager, "pause_async", no_pause)
    monkeypatch.setattr(PlaywrightJobManager, "SEARCH_PAGE_RETRY_BASE_DELAY", 0.01)
    monkeypatch.setattr(PlaywrightJobManager, "SEARCH_RESULTS_WAIT_MS", 300)
    monkeypatch.setattr(PlaywrightJobManager, "close", fake_close)
    monkeypatch.setattr(ResumeScraper, "get_resume_parameters", fake_resume_params)
    monkeypatch.setattr(ResumeScraper, "get_resume_info", fake_resume_info)
    monkeypatch.setattr(AsyncTelegramSink, "__call__", recorder)

    try:
        # Сценарий 1: ошибка поиска — итоговый logger.error остаётся внутри зоны
        code = await verify_search.main()
        assert code == 1
        assert closed == [True]
        assert events == []
        assert AsyncTelegramSink.__call__ is recorder
        closed.clear()

        # Сценарий 2: ошибка чтения резюме (после установки блокировки) —
        # общий отчёт и восстановление, без сообщений в Telegram
        async def resume_error(self):
            raise RuntimeError("сбой чтения резюме")

        monkeypatch.setattr(ResumeScraper, "get_resume_info", resume_error)
        code = await verify_search.main()
        assert code == 1
        assert closed == [True]
        assert events == []
        assert AsyncTelegramSink.__call__ is recorder
        closed.clear()

        # Сценарий 3: ошибка закрытия браузера — восстановление всё равно выполнено
        monkeypatch.setattr(ResumeScraper, "get_resume_info", fake_resume_info)

        async def close_error(self):
            raise RuntimeError("сбой закрытия браузера")

        monkeypatch.setattr(PlaywrightJobManager, "close", close_error)
        with pytest.raises(RuntimeError, match="сбой закрытия"):
            await verify_search.main()
        assert events == []
        assert AsyncTelegramSink.__call__ is recorder

        # В каждом сценарии отчёт сохранён (через stub) и имеет статус error
        assert len(saved_reports) == 3
        assert all(report["status"] == "error" for _, report in saved_reports)
        assert any("сбой чтения резюме" in (report["error"] or "") for _, report in saved_reports)
    finally:
        monkeypatch.undo()

import asyncio
import json
import os
import random
import re
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from Levenshtein import distance
from playwright.async_api import Browser, BrowserContext, Page, Locator

from src.logger_config import logger
from src.telegram.telegram_manager import process_captcha
from src.utils.browser_utils import (
    create_playwright_browser,
    save_browser_session,
    safe_click,
    safe_fill,
    get_clean_text,
)
from src.utils.utils import sanitize_text
from src.views.resume import Resume


class SearchPageError(Exception):
    """Страница выдачи hh.ru недоступна: ошибка hh.ru, сбой сети или неизвестная разметка."""


class SearchSetupError(Exception):
    """Не удалось применить заданные в search_config.yaml настройки до отправки поиска."""


class PlaywrightJobManager:
    """
    Управляет экземпляром браузера Playwright, аутентификацией и высокоуровневыми взаимодействиями.
    """

    def __init__(self, secrets: dict):
        self.secrets = secrets
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.login = secrets.get("hh_login")
        self.password = secrets.get("hh_password")
        self.search_page_url = ""

    async def initialize(self):
        """Инициализирует браузер, контекст и страницу."""
        if not self.browser:
            self.browser, self.context, self.page = await create_playwright_browser()

    async def close(self):
        """Закрывает ресурсы браузера."""
        if self.context:
            await save_browser_session(self.context)
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        self.page = None

    async def ensure_logged_in(self) -> bool:
        """Проверяет авторизацию, если нет - выполняет вход."""
        if not self.page:
            await self.initialize()
        logger.info("Checking login status...")
        await self.pause_async(2, 3)
        if not await self._is_logged_in():
            return await self._perform_login()
        return True

    async def _perform_login(self) -> bool:
        """Выполняет процесс входа."""
        # Click login button
        if not await safe_click(self.page, "[data-qa*='login']"):
            logger.error("Could not find login button")
            return False

        # Some flows show an account-type chooser first (employer vs applicant).
        # We always want applicant/employee ("Я ищу работу") flow.
        await self.pause_async(1, 2)
        logger.info("Handling account type chooser")
        await self._handle_account_type_chooser_if_present()

        # HH may default credential type to PHONE; switch to EMAIL if the toggle exists.
        logger.info("Selecting email credential type")
        await self._select_email_credential_type_if_present()

        # Fill login (email) FIRST (HH can require it before switching to password form)
        await self.pause_async(1, 2)
        logger.info("Filling login")
        await safe_fill(
            self.page,
            "//*[@data-qa='applicant-login-input-email']",
            self.login,
            wait_for_timeout=2000,
        )

        # Then open password form (button text: "Войти с паролем")
        logger.info("Opening password form")
        await safe_click(self.page, "//*[starts-with(@data-qa, 'expand-login-by')]")
        await self.pause_async(1, 2)

        # Fill password
        logger.info("Filling password")
        await safe_fill(
            self.page,
            "//*[@data-qa='login-input-password' or @data-qa='applicant-login-input-password']",
            self.password,
            wait_for_timeout=10000,
        )
        await self.pause_async(2, 3)

        # Click submit (button text: "Войти"). Avoid clicking generic submit too early ("Дальше")
        await safe_click(self.page, "//*[@data-qa='submit-button']", timeout=10000)
        await self.pause_async(2, 3)

        # Check for errors
        error_msg = self.page.locator("//*[@data-qa='account-login-error']")
        if await error_msg.count() > 0:
            text = await get_clean_text(error_msg.first)
            logger.error(f"Login error: {text}")
            return False

        # Verify login success
        if await self._is_logged_in():
            logger.info("Login successful.")
            # Save browser session after successful login
            await save_browser_session(self.context)
            return True
        else:
            logger.warning("Login verification failed.")
            return False

    async def _is_logged_in(self) -> bool:
        """Проверяет, выполнен ли вход."""
        logger.info("Navigating to login page...")
        try:
            await self.page.goto("https://hh.ru/employer")
            logger.info("Переход на страницу: https://hh.ru/employer")
        except Exception as e:
            logger.warning(f"Failed to navigate to login page: {e}")
            logger.info("Trying to continue...")

        try:
            resume_menu = self.page.locator('[data-qa="mainmenu_profileAndResumes"]')
            create_resume_button = self.page.locator('[data-qa="mainmenu_createResume"]')

            if await resume_menu.count() > 0 or await create_resume_button.count() > 0:
                logger.info("User is already logged in.")
                return True
        except Exception as e:
            logger.warning(f"Error checking login status: {e}")
        return False

    async def _handle_account_type_chooser_if_present(self) -> None:
        """
        Обрабатывает выбор типа аккаунта (работодатель/соискатель), если он появляется.
        Если появляется, выбирает соискателя ("Я ищу работу") и нажимает "Войти".
        """
        if not self.page:
            return

        chooser_container = self.page.locator("//*[@data-qa='account-type-cards']")
        applicant_card = self.page.locator(
            "xpath=//*[contains(@data-qa, 'account-type-card-APPLICANT')]/ancestor::label[1]"
        )
        submit_btn = self.page.locator("//*[@data-qa='submit-button']")

        try:
            has_container = (await chooser_container.count()) > 0
            has_applicant = (await applicant_card.count()) > 0
            has_submit = (await submit_btn.count()) > 0
        except Exception as e:
            logger.warning(f"Error checking account type chooser: {e}")
            return

        if not (has_container or (has_applicant and has_submit)):
            return

        logger.info("Account type chooser detected. Selecting applicant account...")

        # Click applicant card (stable by data-qa); fallback to text match.
        clicked = await safe_click(
            self.page,
            "//*[contains(@data-qa,'account-type-card-APPLICANT')]/ancestor::label[1]",
            timeout=10000,
        )
        if not clicked:
            await safe_click(
                self.page,
                "//*[.//span[@data-qa='cell-text-content' and contains(., 'Я') and contains(., 'ищу работу')]]",
                timeout=10000,
            )

        await safe_click(self.page, "//*[@data-qa='submit-button']", timeout=10000)
        await self.pause_async(1, 2)

    async def _select_email_credential_type_if_present(self) -> None:
        """
        Переключает тип входа на Email, если выбран телефон.
        Вход для соискателя HH может показывать переключатель типа учетных данных (ТЕЛЕФОН vs EMAIL).
        Если присутствует и выбран ТЕЛЕФОН, переключается на EMAIL ("Почта").
        """
        if not self.page:
            return

        switcher = self.page.locator("//*[@data-qa='credential-type-switch']")
        if (await switcher.count()) == 0:
            return

        # In HH markup, selected state can appear as data-qa="credential-type-PHONE checked"
        phone_checked = self.page.locator("[data-qa*='credential-type-PHONE'][data-qa*='checked']")
        if (await phone_checked.count()) == 0:
            return

        logger.info("Credential type switch detected. Switching to EMAIL...")
        clicked = await safe_click(
            self.page,
            "//*[@data-qa='credential-type-EMAIL']/ancestor::label[1]",
            timeout=10000,
        )
        if not clicked:
            await safe_click(
                self.page,
                "//*[self::label or self::div][.//*[contains(., 'Почта')]]",
                timeout=10000,
            )
        await self.pause_async(0.5, 1)

    async def _handle_captcha(self, submit_selector: str):
        """Обрабатывает капчу, если она появляется."""
        captcha_img = self.page.locator("//*[@data-qa='account-captcha-picture']")

        start_time = datetime.now()

        while await captcha_img.count() > 0:
            if (datetime.now() - start_time).total_seconds() > 3600:
                logger.error("Captcha not solved in 1 hour.")
                break

            logger.info("Captcha detected.")

            img_path = "captcha_image.png"
            message_id = str(int(datetime.now().timestamp() * 10**6))

            # Send captcha if we haven't already (or just always send fresh screenshot)
            try:
                await captcha_img.first.screenshot(path=img_path)
            except Exception as e:
                logger.error(f"Failed to save captcha image: {e}")
                break

            tg_token = self.secrets["tg_token"]
            tg_api_id = self.secrets.get("tg_api_id")
            tg_api_hash = self.secrets.get("tg_api_hash")
            tg_chat_id = self.secrets["tg_chat_id"]
            tg_topic_id = self.secrets["tg_captcha_topic_id"]

            # Send image
            await process_captcha(
                tg_token,
                tg_api_id,
                tg_api_hash,
                tg_chat_id,
                tg_topic_id,
                img_path,
                message_id,
                listen=False,
            )

            # Wait for answer
            answer = await process_captcha(
                tg_token,
                tg_api_id,
                tg_api_hash,
                tg_chat_id,
                tg_topic_id,
                img_path,
                message_id,
                listen=True,
            )

            if answer:
                logger.info(f"Received captcha answer: {answer}")
                await safe_fill(self.page, "//*[@data-qa='account-captcha-input']", answer)
                await safe_click(self.page, submit_selector)

                # Wait for reload/check
                await self.pause_async(5, 6)
                if os.path.exists(img_path):
                    os.remove(img_path)
            else:
                await self.pause_async(5, 6)

    async def pause_async(self, low=0.5, high=1.0):
        """Асинхронная пауза. Время паузы выбирается случайно в пределах между low и high секунд."""
        await asyncio.sleep(random.uniform(low, high))

    async def start_search(self, resume_id: str) -> None:
        """
        Устаревший вход: страница резюме и рекомендованные вакансии.
        Текущий поиск не использует (см. `set_advanced_search_params`),
        метод сохранён для совместимости.
        """
        url = f"https://hh.ru/resume/{resume_id}"
        await self.page.goto(url)
        logger.info(f"Переход на страницу: {url}")
        await safe_click(self.page, "xpath=//*[contains(text(), 'Подобрали для вас')]")

    # Максимум попыток открытия страницы выдачи при временных сбоях hh.ru
    SEARCH_PAGE_MAX_ATTEMPTS = 3
    # Задержка (сек) перед первой повторной попыткой, растёт с каждой попыткой
    SEARCH_PAGE_RETRY_BASE_DELAY = 3.0
    # Сколько миллисекунд ждать отрисовки выдачи (карточки или маркер пустой выдачи)
    SEARCH_RESULTS_WAIT_MS = 20000
    # Сколько проверок (по 0.5 с) делать после выбора размера страницы
    PAGE_SIZE_VERIFY_POLLS = 12
    # Маркеры страницы ошибки hh.ru (ошибка может приходить при HTTP 200)
    ERROR_PAGE_MARKERS = (
        "Страница временно недоступна",
        "Ошибка 502",
        "Ошибка 503",
        "Ошибка 504",
    )

    async def set_advanced_search_params(
        self, search_params: Dict[str, Any], resume_id: str
    ) -> None:
        """
        Открывает поиск вакансий hh.ru и применяет настройки из `search_config.yaml`
        через панель «Фильтры» на обычной странице выдачи.

        Контракт сохранён: (search_params, resume_id); `resume_id` не используется —
        поиск больше не идёт через страницу резюме и старый `/advanced`.

        `search_params` ожидается в "сыром" виде (как в YAML / `SearchConfig.model_dump()`).

        Поднятые в настройках фильтры применяются явно: если заданный фильтр не
        поддерживается или не удалось применить, выбрасывается SearchSetupError —
        расширенный поиск с молча отброшенными условиями не запускается.
        """
        self.search_params = search_params or {}
        self.search_page_url = ""
        self._applied_area_ids: Dict[str, int] = {}
        logger.info("Задаю параметры поиска в панели «Фильтры» hh.ru")

        # 1) Обычная страница выдачи (без /advanced и без страницы резюме)
        await self._goto_search_url("https://hh.ru/search/vacancy")
        await self._handle_interfering_messages()

        # 2) Ключевые слова в верхнем поле поиска
        await self._set_keywords()

        # 3) Открыть панель «Фильтры»
        if not await self._open_filters_panel():
            raise SearchSetupError("Не удалось открыть панель «Фильтры» на странице выдачи")

        # 4) Применить настройки (остановка поиска при невозможности применить заданный фильтр)
        await self._set_search_field()
        await self._set_area()
        await self._set_words_to_exclude()
        await self._set_professional_role()
        await self._set_industry()
        await self._set_districts()
        await self._set_salary_and_currency()
        await self._set_only_with_salary()
        await self._set_education()
        await self._set_experience()
        await self._set_employment()
        await self._set_job_format()
        await self._set_vacancy_label()
        await self._set_order_by()
        await self._set_period()

        # 5) Подтвердить панель и дождаться обновления выдачи
        if not await self._apply_filters_panel():
            raise SearchSetupError("Не найдена кнопка подтверждения панели «Фильтры»")
        await self._wait_for_search_results()
        # Проверить фактически применённые условия (потеря ключевых слов/регионов/
        # флажков — явная ошибка до обработки вакансий); адрес сохраняется только здесь
        await self._validate_search_applied()

        # 6) Размер выдачи (элемент вне панели)
        await self._set_show()

    # -----------------------------
    # Advanced search helpers (UI)
    # -----------------------------

    @staticmethod
    def _split_multi(value: Any) -> List[str]:
        """Разделяет строку с несколькими значениями (через запятую или точку с запятой)."""
        if not value:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if not isinstance(value, str):
            return [str(value).strip()] if str(value).strip() else []
        # Accept both comma and semicolon separated input
        raw = value.replace(";", ",")
        return [v.strip() for v in raw.split(",") if v.strip()]

    @staticmethod
    def _true_keys(value: Any) -> List[str]:
        """Возвращает список ключей словаря, значения которых True."""
        if not isinstance(value, dict):
            return []
        return [k for k, v in value.items() if v is True]

    @staticmethod
    def _first_true_key(value: Any) -> Optional[str]:
        """Возвращает первый ключ словаря с значением True."""
        keys = PlaywrightJobManager._true_keys(value)
        return keys[0] if keys else None

    async def _click_best_suggestion(self, desired: str, suggestion_xpath: str) -> bool:
        """Кликает по наилучшему предложению из выпадающего списка (на основе расстояния Левенштейна)."""
        desired_norm = (desired or "").strip().lower()
        if not desired_norm:
            return False
        suggestions = self.page.locator(suggestion_xpath)
        try:
            await suggestions.first.wait_for(state="visible", timeout=10000)
        except Exception:
            return False

        items = await suggestions.all()
        if not items:
            return False

        texts: List[str] = []
        for item in items:
            t = (await item.text_content()) or ""
            t = re.sub(r"\s+", " ", t).strip()
            texts.append(t)

        distances = [
            (idx, distance(desired_norm, (texts[idx] or "").lower())) for idx in range(len(texts))
        ]
        best_idx = min(distances, key=lambda x: x[1])[0]
        try:
            await safe_click(self.page, suggestion_xpath, element_number=best_idx)
            await self.pause_async(0.5, 1)
            return True
        except Exception:
            return False

    async def _goto_search_url(self, url: str, max_attempts: Optional[int] = None) -> None:
        """
        Переходит на адрес страницы выдачи с ограниченным числом повторных попыток.

        Повторяется при временных сбоях: статусы 5xx, страница ошибки hh.ru
        (может приходить при HTTP 200), сетевые/навигационные сбои.
        Бесконечных повторов нет; после исчерпания попыток — SearchPageError.
        """
        if not self.page:
            raise SearchPageError("Браузер не инициализирован")
        attempts = max_attempts or self.SEARCH_PAGE_MAX_ATTEMPTS
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self.page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                last_error = SearchPageError(f"Сбой сети/навигации при открытии {url}: {e}")
                logger.warning(f"Не удалось открыть страницу выдачи (попытка {attempt}/{attempts}): {e}")
                await asyncio.sleep(self.SEARCH_PAGE_RETRY_BASE_DELAY * attempt)
                continue

            status = response.status if response is not None else None
            # Даём странице время отрисоваться: ошибка hh.ru может приходить при HTTP 200
            await self.pause_async(1.5, 2.5)
            marker = await self._page_error_marker()

            if status is not None and status >= 500:
                last_error = SearchPageError(f"hh.ru вернул статус {status} для {url}")
                logger.warning(f"hh.ru вернул статус {status} (попытка {attempt}/{attempts})")
                await asyncio.sleep(self.SEARCH_PAGE_RETRY_BASE_DELAY * attempt)
                continue
            if marker:
                last_error = SearchPageError(
                    f"hh.ru показал страницу ошибки (status={status}, маркер «{marker}»): {url}"
                )
                logger.warning(
                    f"Страница ошибки hh.ru: «{marker}» (попытка {attempt}/{attempts})"
                )
                await asyncio.sleep(self.SEARCH_PAGE_RETRY_BASE_DELAY * attempt)
                continue
            return
        raise last_error if last_error else SearchPageError(f"Не удалось открыть страницу выдачи: {url}")

    async def _page_error_marker(self) -> Optional[str]:
        """Возвращает маркер, если текущая страница — страница ошибки hh.ru, иначе None."""
        try:
            body_text = await self.page.evaluate(
                "() => (document.body && document.body.innerText) || ''"
            )
        except Exception:
            return None
        text = (body_text or "").lower()
        for marker in self.ERROR_PAGE_MARKERS:
            if marker.lower() in text:
                return marker
        return None

    def _search_url_for_page(self, page_num: int) -> str:
        """
        Строит адрес страницы выдачи с номером `page_num` из сохранённого адреса поиска.
        Все параметры поиска (ключевые слова, регионы и пр.) сохраняются.
        """
        if not self.search_page_url:
            raise SearchPageError(
                "Адрес поиска не задан — сначала выполните set_advanced_search_params"
            )
        parsed = urllib.parse.urlparse(self.search_page_url)
        if "hh.ru" not in parsed.netloc or "/search/vacancy" not in parsed.path:
            raise SearchPageError(
                f"Сохранённый адрес не является поиском вакансий: {self.search_page_url}"
            )
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        query["page"] = [str(page_num)]
        new_query = urllib.parse.urlencode(query, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))

    async def _robust_click(self, locator: Locator, what: str = "", timeout: int = 5000) -> bool:
        """
        Клик по первому элементу locator. Fallback: force-клик, затем JS-клик
        (панели hh.ru — overlay, обычный клик может перехватываться оверлеем).
        """
        try:
            await locator.first.click(timeout=timeout)
            return True
        except Exception:
            pass
        try:
            await locator.first.click(timeout=timeout, force=True)
            return True
        except Exception:
            pass
        try:
            await locator.first.evaluate("el => el.click()")
            return True
        except Exception as e:
            logger.warning(f"Не удалось кликнуть {what or 'элемент'}: {e}")
            return False

    async def _open_filters_panel(self) -> bool:
        """
        Открывает панель «Фильтры» на странице выдачи и ждёт её появления.

        Важно: на живом hh.ru data-qa="catalog-search-extra-filters" может стоять
        на обёртке блока «Быстрые фильтры», а не на кнопке — клик по div не бросает
        исключения, но панель не открывается. Поэтому ищем реальный <button> с
        подписью «Фильтры», эскалируем способ клика (обычный → force → JS) и после
        каждого клика проверяем, что панель действительно открылась.
        """
        # Дожимаем отрисовку выдачи перед открытием панели (страница может
        # перерисовываться после запуска поиска)
        try:
            await self.page.wait_for_selector(
                '[data-qa="vacancy-serp__vacancy"], [data-qa="empty-vacancy-search-block"]',
                timeout=8000,
            )
        except Exception:
            logger.warning("Выдача не отрисовалась до открытия панели «Фильтры» — продолжаю")

        # Реальная кнопка «Фильтры»: <button> с вложенной подписью, иначе span с текстом
        button = self.page.locator(
            "xpath=//button[.//*[normalize-space(text())='Фильтры']]"
        ).first
        if await button.count() == 0:
            button = self.page.get_by_text("Фильтры", exact=True).first
        if await button.count() == 0:
            logger.error("Кнопка «Фильтры» не найдена")
            return False

        for mode in ("normal", "force", "js"):
            try:
                if mode == "normal":
                    await button.click(timeout=6000)
                elif mode == "force":
                    await button.click(timeout=6000, force=True)
                else:
                    await button.evaluate("el => el.click()")
            except Exception as e:
                logger.debug(f"Клик {mode} по «Фильтры» не удался: {str(e)[:120]}")
                continue
            try:
                await self.page.wait_for_selector(
                    "[data-qa='search-drawer-filters-submit']", timeout=8000
                )
                return True
            except Exception:
                logger.debug(f"Панель не открылась после клика {mode}, пробую следующий способ")
                continue
        logger.error("Панель «Фильтры» не открылась (не появилось поле подтверждения)")
        return False

    async def _apply_filters_panel(self) -> bool:
        """Нажимает кнопку подтверждения панели «Фильтры»."""
        submit = self.page.locator("[data-qa='search-drawer-filters-submit']")
        if await submit.count() == 0:
            return False
        return await self._robust_click(submit, what="подтверждение фильтров", timeout=8000)

    async def _wait_for_search_results(self) -> None:
        """
        Ждёт закрытия панели и обновления выдачи.

        Если после применения фильтров hh.ru показал страницу ошибки (5xx или
        маркер ошибки при HTTP 200), повторяет тот же запрос с уже выбранными
        условиями (не повторяет клики по флажкам, не возвращается на поиск без
        параметров): всего до SEARCH_PAGE_MAX_ATTEMPTS попыток, включая исходный
        переход.

        Финальный адрес сохраняет `_validate_search_applied` — только после
        проверки применённых условий.
        """
        # Ждём закрытия панели (до ~15 сек)
        for _ in range(30):
            submit = self.page.locator("[data-qa='search-drawer-filters-submit']")
            try:
                if await submit.count() == 0 or not await submit.first.is_visible():
                    break
            except Exception:
                break
            await asyncio.sleep(0.5)

        # Ждём полного закрытия модалки: анимация закрытия может перехватывать клики
        try:
            await self.page.wait_for_selector(
                "[data-qa='search-filters']", state="hidden", timeout=10000
            )
        except Exception:
            pass
        await self.pause_async(0.5, 1)

        # Повторяемый переход: адрес уже содержит выбранные условия
        url = self.page.url
        attempts = self.SEARCH_PAGE_MAX_ATTEMPTS
        marker: Optional[str] = None
        for attempt in range(1, attempts + 1):
            # Ждём появления карточек или явного маркера пустой выдачи
            try:
                await self.page.wait_for_selector(
                    '[data-qa="vacancy-serp__vacancy"], [data-qa="empty-vacancy-search-block"]',
                    timeout=self.SEARCH_RESULTS_WAIT_MS,
                )
            except Exception:
                logger.warning("Выдача не отрисовалась после применения фильтров")
            await self.pause_async(0.5, 1)
            marker = await self._page_error_marker()
            if not marker:
                break
            logger.warning(
                f"hh.ru показал страницу ошибки после применения фильтров "
                f"(попытка {attempt}/{attempts})"
            )
            if attempt >= attempts:
                break
            await asyncio.sleep(self.SEARCH_PAGE_RETRY_BASE_DELAY * attempt)
            try:
                response = await self.page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                logger.warning(
                    f"Сбой повторного перехода на адрес выдачи "
                    f"(попытка {attempt + 1}/{attempts}): {e}"
                )
                continue
            if response is not None and response.status >= 500:
                logger.warning(
                    f"hh.ru вернул статус {response.status} при повторе "
                    f"(попытка {attempt + 1}/{attempts})"
                )
        if marker:
            raise SearchPageError(
                f"hh.ru вернул страницу ошибки после применения фильтров: «{marker}»"
            )
        logger.info(f"Поиск применён, адрес после панели: {self.page.url}")

    async def _read_panel_filter_states(self) -> Dict[str, bool]:
        """
        Переоткрывает панель «Фильтры» и читает наблюдаемое состояние чекбоксов
        (области поиска, «только с зарплатой»). Используется для параметров,
        которых нет в адресе выдачи: источник истины — состояние интерфейса,
        не предположение.
        """
        if not await self._open_filters_panel():
            raise SearchSetupError(
                "Не удалось открыть панель «Фильтры» для проверки поисковых условий"
            )
        states: Dict[str, bool] = {}
        for key in ("name", "company_name", "description"):
            locator = self.page.locator(f"[data-qa='search-filter-search_field-value-{key}']")
            if await locator.count() == 0:
                raise SearchSetupError(
                    f"Не найден чекбокс области поиска '{key}' в панели «Фильтры»"
                )
            try:
                states[key] = await locator.first.is_checked()
            except Exception as e:
                # Сбой чтения не выдаём за прочитанное «выключено»
                raise SearchSetupError(f"Не удалось прочитать чекбокс '{key}' в панели: {e}")
        salary = self.page.locator("[data-qa='search-filter-value-with_salary']")
        if await salary.count() == 0:
            raise SearchSetupError("Не найден чекбокс «только с зарплатой» в панели «Фильтры»")
        try:
            states["with_salary"] = await salary.first.is_checked()
        except Exception as e:
            raise SearchSetupError(f"Не удалось прочитать чекбокс «только с зарплатой»: {e}")
        # Закрываем панель без подтверждения
        close = self.page.locator("[data-qa='search-filters-close']")
        if await close.count():
            await self._robust_click(close, what="закрытие панели «Фильтры»", timeout=5000)
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            await self.page.wait_for_selector(
                "[data-qa='search-drawer-filters-submit']", state="hidden", timeout=10000
            )
        except Exception:
            logger.warning("Панель «Фильтры» не закрылась после проверки условий")
        return states

    async def _validate_search_applied(self) -> None:
        """
        После отправки панели (и после смены размера выдачи) сверяет фактически
        применённые условия: адрес выдачи, а для параметров, отсутствующих в
        адресе — наблюдаемое состояние чекбоксов панели.

        Потеря ключевых слов, регионов или заданных флажков — SearchSetupError
        до обработки вакансий. Адрес сохраняется как рабочий только после
        успешной проверки.
        """
        url = self.page.url
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        problems: List[str] = []

        # Ключевые слова: применённый запрос — параметр text в адресе выдачи.
        # Значение в верхнем поле не доказывает применение: поле может
        # сохранять введённый текст при пустом/чужом запросе.
        keywords = str(
            self.search_params.get("keywords") or self.search_params.get("text") or ""
        ).strip()
        if keywords:
            url_text = (query.get("text") or [""])[0]
            if url_text != keywords:
                problems.append(
                    f"ключевые слова '{keywords}' не применены: в адресе '{url_text}'"
                )

        # Регионы: каждый запрошенный регион должен иметь подтверждённый id,
        # и этот id должен быть в адресе (неполный словарь — не успех)
        url_areas = query.get("area", [])
        for region in self._split_multi(self.search_params.get("area")):
            if not region:
                continue
            area_id = (getattr(self, "_applied_area_ids", {}) or {}).get(region)
            if area_id is None:
                problems.append(f"регион '{region}': нет подтверждённого id, проверить нельзя")
            elif str(area_id) not in url_areas:
                problems.append(f"регион '{region}' (id {area_id}) потерялся из адреса")

        # Область поиска: заданные true значения — в адресе или в панели
        search_field = self.search_params.get("search_field")
        panel_states: Optional[Dict[str, bool]] = None
        if isinstance(search_field, dict):
            url_sf = query.get("search_field", [])
            missing = [
                key
                for key in ("name", "company_name", "description")
                if search_field.get(key) is True and key not in url_sf
            ]
            if missing:
                panel_states = await self._read_panel_filter_states()
                for key in missing:
                    if not panel_states.get(key):
                        problems.append(f"область поиска '{key}' не применена")

        # «Только с зарплатой»: включено — должно быть в адресе (или в панели),
        # выключено — не должно быть в адресе
        only_with_salary = self.search_params.get("only_with_salary")
        url_salary = (query.get("with_salary") or [""])[0].lower() in ("true", "1")
        if only_with_salary is True:
            if not url_salary:
                if panel_states is None:
                    panel_states = await self._read_panel_filter_states()
                if not panel_states.get("with_salary"):
                    problems.append("«только с зарплатой» включено в настройках, но не применено")
        elif only_with_salary is False and url_salary:
            problems.append("only_with_salary=False, но в адресе with_salary=true")

        if problems:
            raise SearchSetupError("Поисковые условия не применены полностью: " + "; ".join(problems))
        self.search_page_url = url

    async def _set_keywords(self) -> None:
        """Вводит ключевые слова в верхнее поле поиска и проверяет фактическое значение."""
        logger.debug("Вводим ключевые слова")
        keywords = str(self.search_params.get("keywords") or self.search_params.get("text") or "").strip()
        if not keywords:
            return
        selector = "[data-qa='search-input']"
        if await self.page.locator(selector).count() == 0:
            raise SearchSetupError("Не найдено верхнее поле поиска вакансий (search-input)")
        await self._robust_click(self.page.locator(selector), what="поле поиска", timeout=8000)
        await self.pause_async(0.5, 1)
        if not await safe_fill(self.page, selector, keywords, timeout=10000):
            raise SearchSetupError(f"Не удалось ввести ключевые слова: '{keywords}'")
        actual = await self.page.locator(selector).first.input_value()
        if (actual or "").strip() != keywords:
            raise SearchSetupError(
                f"Ключевые слова не применились: ожидалось '{keywords}', в поле '{(actual or '').strip()}'"
            )
        # Запускаем поиск и ждём обновления адреса. Обновление адреса НЕ доказывает
        # успех: hh.ru может вернуть страницу ошибки (5xx или маркер при HTTP 200)
        # по тому же адресу с правильным text. Маркер ошибки проверяется до
        # признания перехода успешным; при ошибке — повтор того же запроса
        # (прямой адрес с ключевыми словами), всего до SEARCH_PAGE_MAX_ATTEMPTS
        # попыток, включая исходный переход.
        await self.page.keyboard.press("Enter")
        attempts = self.SEARCH_PAGE_MAX_ATTEMPTS
        url_ok = False
        for attempt in range(1, attempts + 1):
            try:
                await self.page.wait_for_function(
                    "expected => new URLSearchParams(location.search).get('text') === expected",
                    arg=keywords,
                    timeout=15000,
                )
                url_ok = True
            except Exception:
                url_ok = False
            marker = await self._page_error_marker()
            if not marker:
                break  # страницы ошибки нет — успех либо адрес просто не обновился
            logger.warning(
                f"hh.ru показал страницу ошибки после запуска поиска (попытка {attempt}/{attempts})"
            )
            if attempt >= attempts:
                break
            await asyncio.sleep(self.SEARCH_PAGE_RETRY_BASE_DELAY * attempt)
            try:
                response = await self.page.goto(
                    f"https://hh.ru/search/vacancy?text={urllib.parse.quote(keywords)}",
                    timeout=60000,
                    wait_until="domcontentloaded",
                )
            except Exception as e:
                logger.warning(f"Сбой повторного перехода с ключевыми словами: {e}")
                continue
            if response is not None and response.status >= 500:
                logger.warning(
                    f"hh.ru вернул статус {response.status} при запуске поиска "
                    f"(попытка {attempt + 1}/{attempts})"
                )
                continue
        # После цикла: страница ошибки — явная ошибка, не успех
        marker = await self._page_error_marker()
        if marker:
            raise SearchPageError(
                f"hh.ru вернул страницу ошибки после запуска поиска: «{marker}»"
            )
        if url_ok:
            logger.info(f"Поиск запущен по ключевым словам: '{keywords}'")
        else:
            actual = (await self.page.locator(selector).first.input_value() or "").strip()
            if actual != keywords:
                raise SearchSetupError(
                    f"Ключевые слова не применились: ожидалось '{keywords}', в поле '{actual}'"
                )
            logger.warning(
                "Адрес выдачи не обновился с ключевыми словами, значение в поле совпадает — продолжаю"
            )

    async def _set_search_field(self) -> None:
        """
        Область поиска: доводит чекбоксы search-filter-search_field-value-* до нужного
        состояния (явные false выключаются, true включаются) и проверяет результат.
        """
        logger.debug("Задаем настройки области поиска")
        search_field = self.search_params.get("search_field")
        if not isinstance(search_field, dict) or not search_field:
            return
        for key in ("name", "company_name", "description"):
            if key not in search_field:
                continue
            desired = bool(search_field.get(key))
            selector = f"[data-qa='search-filter-search_field-value-{key}']"
            locator = self.page.locator(selector)
            if await locator.count() == 0:
                raise SearchSetupError(f"Не найден элемент области поиска '{key}'")
            try:
                current = await locator.first.is_checked()
            except Exception:
                current = None
            if current is not None and current != desired:
                if not await self._robust_click(locator, what=f"область поиска '{key}'", timeout=5000):
                    raise SearchSetupError(f"Не удалось переключить область поиска '{key}'")
                await self.pause_async(0.3, 0.7)
        # Проверка фактического состояния
        for key in ("name", "company_name", "description"):
            if key not in search_field:
                continue
            desired = bool(search_field.get(key))
            current = await self.page.locator(
                f"[data-qa='search-filter-search_field-value-{key}']"
            ).first.is_checked()
            if current != desired:
                raise SearchSetupError(
                    f"Область поиска '{key}' не применилась: состояние {current}, ожидалось {desired}"
                )

    async def _read_option_id(self, element, text: str = "") -> Optional[int]:
        """Читает числовой id выбранной опции: data-qa `magritte-select-option-<id>`
        либо атрибут data-magritte-select-option (оба встречаются на живом hh.ru).
        Если элемент — контейнер списка (magritte-select-option-list), ищет id
        у вложенной опции с точно таким же текстом; неоднозначность — None
        (неверный id опаснее отсутствия id)."""
        try:
            handle = await element.first.element_handle()
        except Exception:
            return None
        if handle is None:
            return None
        try:
            return await handle.evaluate(
                """(el, text) => {
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const fromEl = (n) => {
                        if (!n || !n.getAttribute) return null;
                        const qa = n.getAttribute('data-qa') || '';
                        const m = qa.match(/magritte-select-option-(\\d+)/);
                        if (m) return parseInt(m[1], 10);
                        const raw = n.getAttribute('data-magritte-select-option') || '';
                        if (/^\\d+$/.test(raw)) return parseInt(raw, 10);
                        return null;
                    };
                    const id = fromEl(el);
                    if (id !== null) return id;
                    const wanted = norm(text);
                    if (!wanted) return null;
                    const inner = el.querySelectorAll(
                        "[data-qa^='magritte-select-option-'], [data-magritte-select-option]"
                    );
                    let found = null;
                    for (const o of inner) {
                        const oid = fromEl(o);
                        if (oid === null) continue;
                        if (norm(o.textContent) === wanted) {
                            if (found === null) found = oid;
                            else return null; // несколько опций с таким текстом
                        }
                    }
                    return found;
                }""",
                text,
            )
        except Exception:
            return None

    async def _pick_from_chips_block(self, block_selector: str, value: str) -> bool:
        """
        В chips-блоке (регионы, исключаемые слова и т.п.) выбирает значение по подсказкам:
        вводит текст, ждёт список подсказок, кликает по ближайшему подходящему варианту
        и проверяет, что значение появилось в блоке.

        При успехе записывает числовой id выбранного варианта (data-qa
        magritte-select-option-<id>) в self._last_picked_option_id — для регионов
        используется в проверке применённых условий.
        """
        self._last_picked_option_id = None
        block = self.page.locator(block_selector)
        if await block.count() == 0:
            return False
        input_selector = f"{block_selector} [data-qa='chips-trigger-input']"
        input_locator = self.page.locator(input_selector)
        if await input_locator.count() == 0:
            return False
        # Фокус на поле (обычный клик может перехватываться оверлеем панели)
        try:
            await input_locator.first.click(timeout=3000)
        except Exception:
            try:
                await input_locator.first.evaluate("el => el.focus()")
            except Exception:
                return False
        await self.pause_async(0.3, 0.7)
        if not await safe_fill(self.page, input_selector, value, timeout=10000):
            return False

        options = self.page.locator("[data-qa^='magritte-select-option-']")
        # Ждём, пока подсказки обновятся под введённый текст. После предыдущего
        # выбора в DOM могут оставаться старые видимые опции (например «Астана»),
        # и простой wait_for(state="visible") мгновенно срабатывает на них.
        desired_q = value.strip().lower()
        try:
            await self.page.wait_for_function(
                """(desired) => {
                    const opts = document.querySelectorAll("[data-qa^='magritte-select-option-']");
                    for (const el of opts) {
                        // контейнер списка — не опция (содержит весь текст опций)
                        if (el.getAttribute && el.getAttribute('data-qa') === 'magritte-select-option-list') continue;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        const t = (el.textContent || '').trim().toLowerCase();
                        if (t.startsWith(desired)) return true;
                    }
                    return false;
                }""",
                arg=desired_q,
                timeout=10000,
            )
        except Exception:
            logger.warning(
                f"Подсказки под «{value}» не появились за 10 секунд — проверю, что есть в списке"
            )

        items: List[Tuple[int, str]] = []
        for i in range(await options.count()):
            option = options.nth(i)
            try:
                if not await option.is_visible():
                    continue
            except Exception:
                continue
            # Контейнер списка опций (data-qa magritte-select-option-list) тоже
            # попадает в префикс-селектор и содержит весь текст опций —
            # это не выбираемый элемент, его пропускаем
            try:
                if (await option.get_attribute("data-qa")) == "magritte-select-option-list":
                    continue
            except Exception:
                pass
            text = re.sub(r"\s+", " ", (await option.text_content() or "")).strip()
            if text:
                items.append((i, text))
        if not items:
            return False

        desired = value.strip().lower()
        best_idx, best_text = None, None
        for i, text in items:
            if text.lower().startswith(desired):
                best_idx, best_text = i, text
                break
        if best_idx is None:
            best_idx, best_text = min(items, key=lambda item: distance(desired, item[1].lower()))

        # Не берём явно нерелевантную подсказку
        if distance(desired, best_text.lower()) > max(3, len(desired) // 2):
            logger.warning(
                f"Среди подсказок нет подходящего варианта для '{value}' (ближайший: '{best_text}')"
            )
            return False

        clicked = options.nth(best_idx)
        # id выбранного варианта (для регионов: сверка с адресом выдачи).
        # Читается ДО клика: Locator заново ищет элемент при каждом обращении,
        # а после клика список подсказок может очищаться/перерисовываться,
        # и тот же индекс укажет на другой элемент (например, опцию размера
        # страницы). Живой data-qa может иметь суффикс после id — якорь не ставим;
        # резервный источник id — атрибут data-magritte-select-option.
        try:
            self._last_picked_option_id = await self._read_option_id(clicked, best_text)
        except Exception:
            self._last_picked_option_id = None

        if not await self._robust_click(clicked, what=f"подсказка '{best_text}'", timeout=5000):
            return False
        await self.pause_async(0.5, 1)

        block_text = re.sub(r"\s+", " ", (await block.first.text_content()) or "").lower()
        if desired not in block_text:
            logger.warning(f"Выбранное значение '{best_text}' не появилось в блоке {block_selector}")
            return False
        logger.info(f"Применено: '{value}' -> '{best_text}'")
        return True

    async def _set_words_to_exclude(self) -> None:
        """Задает слова для исключения."""
        logger.debug("Задаем слова для исключения")
        words = str(self.search_params.get("words_to_exclude") or "").strip()
        if not words:
            return
        block_selector = "[data-qa='filter-select-excluded_text']"
        if not await self._pick_from_chips_block(block_selector, words):
            raise SearchSetupError(f"Не удалось применить исключённые слова: '{words}'")

    async def _set_tree_selector_single(self, trigger_qa: str, label: str, value: str) -> None:
        """
        Выбирает значение в модальном окне tree-selector (специализация/отрасль):
        открывает модалку по триггеру панели, вводит значение, выбирает ближайшее
        совпадение из подсказок и подтверждает.
        """
        value = str(value or "").strip()
        if not value:
            return

        trigger = self.page.locator(f"[data-qa='{trigger_qa}']")
        if await trigger.count() == 0:
            raise SearchSetupError(f"Не найден триггер фильтра «{label}» ({trigger_qa})")
        if not await self._robust_click(trigger, what=f"триггер «{label}»", timeout=8000):
            raise SearchSetupError(f"Не удалось открыть окно «{label}»")
        await self.pause_async(0.7, 1.2)

        search_input_xpath = "//*[@data-qa='tree-selector-search-input' or @data-qa='bloko-tree-selector-popup-search']"
        if not await safe_fill(self.page, f"xpath={search_input_xpath}", value, timeout=10000):
            raise SearchSetupError(f"Не удалось ввести значение в окне «{label}»")
        await self.pause_async(1, 2)

        # Подсказки внутри модалки
        suggestion_xpath = (
            "//*[starts-with(@data-qa, 'tree-selector-item') "
            "or starts-with(@data-qa, 'bloko-tree-selector-item-text') "
            "or @data-qa='suggest-item-cell']"
        )
        if not await self._click_best_suggestion(value, f"xpath={suggestion_xpath}"):
            # Закрываем окно, чтобы панель не осталась с незавершённым выбором
            await safe_click(
                self.page,
                "xpath=//*[@data-qa='composite-selection-tree-selector-modal-cancel' or @data-qa='bloko-tree-selector-popup-cancel']",
                timeout=3000,
            )
            raise SearchSetupError(f"Не найдена подсказка для «{label}»: '{value}'")
        await self.pause_async(0.5, 1)
        if not await safe_click(
            self.page,
            "xpath=//*[@data-qa='composite-selection-tree-selector-modal-submit' or @data-qa='bloko-tree-selector-popup-submit']",
            timeout=10000,
        ):
            raise SearchSetupError(f"Не удалось подтвердить выбор в окне «{label}»")
        await self.pause_async(0.5, 1)

    async def _set_professional_role(self) -> None:
        """Задает профессиональную роль."""
        logger.debug("Задаем профессиональную роль")
        await self._set_tree_selector_single(
            "search-filter-professional-role-trigger",
            "Указать специализации",
            str(self.search_params.get("professional_role") or ""),
        )

    async def _set_industry(self) -> None:
        """Задает отрасль."""
        logger.debug("Задаем отрасль")
        await self._set_tree_selector_single(
            "search-filter-industry-trigger",
            "Указать отрасль компании",
            str(self.search_params.get("industry") or ""),
        )

    async def _set_area(self) -> None:
        """Выбирает регионы из настроек в поле «Регион» панели фильтров."""
        logger.debug("Задаем регион")
        values = self._split_multi(self.search_params.get("area"))
        if not values:
            return
        block_selector = "[data-qa='filter-select-area']"
        if await self.page.locator(block_selector).count() == 0:
            raise SearchSetupError("Не найден блок выбора регионов (filter-select-area)")
        for region in values:
            if not region:
                continue
            if not await self._pick_from_chips_block(block_selector, region):
                raise SearchSetupError(f"Не удалось применить регион: '{region}'")
            # Id выбранного региона обязателен: без него невозможно подтвердить
            # применение условия после submit (неполный словарь — не успех)
            if self._last_picked_option_id is None:
                raise SearchSetupError(
                    f"Не удалось определить id выбранного региона: '{region}' — "
                    f"применённые условия не могут быть подтверждены"
                )
            self._applied_area_ids[region] = self._last_picked_option_id

    async def _set_districts(self) -> None:
        """Задает районы."""
        logger.debug("Задаем районы")
        values = self._split_multi(self.search_params.get("districts"))
        if not values:
            return
        raise SearchSetupError(
            "Районы (districts) не поддерживаются текущим интерфейсом hh.ru — "
            "уберите districts из search_config.yaml"
        )

    async def _set_salary_and_currency(self) -> None:
        """Задает зарплату и валюту."""
        logger.debug("Задаем зарплату и валюту")
        salary = self.search_params.get("salary")
        if salary is not None and salary != "":
            salary_val = str(int(salary)) if str(salary).strip().isdigit() else str(salary)
            if not await safe_fill(
                self.page, "[data-qa='search-filter-compensation-input']", salary_val, timeout=10000
            ):
                raise SearchSetupError("Не удалось задать зарплату (search-filter-compensation-input)")
            await self.pause_async(0.5, 1)

        currency = self.search_params.get("currency") or {}
        currency_key = self._first_true_key(currency)
        if not currency_key:
            return

        picker = self.page.locator("[data-qa='search-filter-currency-picker']")
        if await picker.count() == 0:
            raise SearchSetupError("Не найден элемент выбора валюты (search-filter-currency-picker)")
        if not await self._robust_click(picker, what="выбор валюты", timeout=5000):
            raise SearchSetupError("Не удалось открыть выбор валюты")
        await self.pause_async(0.5, 1)

        aliases = {
            "RUR": ("rub", "₽", "rur"),
            "USD": ("usd", "$"),
            "EUR": ("eur", "€"),
        }
        options = self.page.locator("[data-qa^='magritte-select-option-']")
        picked = False
        for i in range(await options.count()):
            option = options.nth(i)
            try:
                if not await option.is_visible():
                    continue
            except Exception:
                continue
            text = re.sub(r"\s+", " ", (await option.text_content() or "")).lower()
            if any(alias in text for alias in aliases.get(currency_key, (currency_key.lower(),))):
                if await self._robust_click(options.nth(i), what=f"валюта {currency_key}"):
                    picked = True
                    break
        if not picked:
            raise SearchSetupError(f"Не удалось выбрать валюту: {currency_key}")
        await self.pause_async(0.5, 1)

    async def _set_only_with_salary(self) -> None:
        """
        Задает фильтр «только с зарплатой» в требуемое состояние
        (явный false выключает, true включает) и проверяет результат.
        """
        logger.debug("Задаем фильтр только с зарплатой")
        only = self.search_params.get("only_with_salary")
        if only is None:
            return
        desired = bool(only)
        locator = self.page.locator("[data-qa='search-filter-value-with_salary']")
        if await locator.count() == 0:
            if desired:
                raise SearchSetupError("Не найден фильтр «только с зарплатой»")
            logger.info("Элемент «только с зарплатой» не найден, требуемое состояние false — пропускаю")
            return
        try:
            current = await locator.first.is_checked()
        except Exception:
            current = None
        if current is not None and current != desired:
            if not await self._robust_click(locator, what="«только с зарплатой»", timeout=5000):
                raise SearchSetupError("Не удалось переключить фильтр «только с зарплатой»")
            await self.pause_async(0.3, 0.7)
        if desired:
            if not await locator.first.is_checked():
                raise SearchSetupError("Фильтр «только с зарплатой» не применился")

    async def _set_filter_group(
        self, group: str, states: Dict[str, Any], key_map: Optional[Dict[str, str]] = None
    ) -> None:
        """
        Доводит чекбоксы `search-filter-{group}-value-*` до состояний из настроек:
        явное false выключает (если включено), true включает.
        True без найденного элемента — ошибка.
        """
        if not isinstance(states, dict) or not states:
            return
        for key, raw_value in states.items():
            suffix = (key_map or {}).get(key, key)
            if not suffix:
                continue
            desired = raw_value is True
            locator = self.page.locator(f"[data-qa='search-filter-{group}-value-{suffix}']")
            if await locator.count() == 0:
                if desired:
                    raise SearchSetupError(f"Не найден элемент фильтра '{group}': '{key}'")
                logger.debug(f"Элемент '{group}': '{key}' не найден (значение false), пропускаю")
                continue
            try:
                current = await locator.first.is_checked()
            except Exception:
                current = None
            if current is not None and current != desired:
                if not await self._robust_click(locator, what=f"{group}:{key}", timeout=5000):
                    raise SearchSetupError(f"Не удалось переключить фильтр '{group}': '{key}'")
                await self.pause_async(0.3, 0.7)

    async def _set_education(self) -> None:
        """Задает образование."""
        logger.debug("Задаем образование")
        edu = self.search_params.get("education") or {}
        mapping = {
            "not_needed": "not_required_or_not_specified",
            "middle": "special_secondary",
            "higher": "higher",
        }
        await self._set_filter_group("education", edu, key_map=mapping)

    async def _set_experience(self) -> None:
        """Задает опыт работы."""
        logger.debug("Задаем опыт работы")
        exp = self.search_params.get("experience") or {}
        # Ключи совпадают с UI: noExperience, between1And3, between3And6, moreThan6.
        # doesntMatter в текущем UI нет — при явном true будет понятная ошибка.
        await self._set_filter_group("experience", exp)

    async def _set_employment(self) -> None:
        """Задает тип занятости."""
        logger.debug("Задаем тип занятости")
        employment = self.search_params.get("employment") or {}
        if not employment:
            return
        standard = {
            "FULL",
            "PART",
            "PROJECT",
            "FLY_IN_FLY_OUT",
        }
        await self._set_filter_group(
            "employment_form", {k: v for k, v in employment.items() if k in standard}
        )
        special = {k: v for k, v in employment.items() if k in ("INTERNSHIP", "ACCEPT_TEMPORARY")}
        if special:
            await self._set_filter_group("value", special)

    async def _set_job_format(self) -> None:
        """Задает формат работы."""
        logger.debug("Задаем формат работы")
        job_format = self.search_params.get("job_format") or {}
        await self._set_filter_group("work_format", job_format)

    async def _set_vacancy_label(self) -> None:
        """Задает метки вакансий."""
        logger.debug("Задаем метки вакансий")
        labels = self.search_params.get("vacancy_label") or {}
        await self._set_filter_group("label", labels)

    async def _set_order_by(self) -> None:
        """Задает сортировку."""
        logger.debug("Задаем сортировку")
        order_by = self.search_params.get("order_by") or {}
        key = self._first_true_key(order_by)
        if not key:
            return
        # relevance — поведение по умолчанию, отдельный элемент не требуется
        if key == "relevance":
            return
        trigger = self.page.locator(
            "xpath=//*[self::button or self::span or self::div][normalize-space(text())='Сортировка']"
        )
        if await trigger.count() == 0:
            raise SearchSetupError(
                f"Не найден элемент сортировки для '{key}' в текущем интерфейсе hh.ru"
            )
        if not await self._robust_click(trigger, what=f"сортировка '{key}'", timeout=5000):
            raise SearchSetupError(f"Не удалось задать сортировку '{key}'")
        await self.pause_async(0.5, 1)
        option = self.page.get_by_text(key, exact=True)
        if await option.count() == 0 or not await self._robust_click(
            option, what=f"сортировка '{key}'", timeout=5000
        ):
            raise SearchSetupError(f"Не найден вариант сортировки '{key}'")
        await self.pause_async(0.5, 1)

    async def _set_period(self) -> None:
        """Задает период поиска."""
        logger.debug("Задаем период поиска")
        period = self.search_params.get("period") or {}
        key = self._first_true_key(period)
        if not key:
            return
        label_map = {
            "all_time": "Всё время",
            "month": "1 месяц",
            "week": "1 неделя",
            "three_days": "3 дня",
            "one_day": "1 день",
        }
        label = label_map.get(key)
        if not label:
            return
        menu = self.page.locator("[data-qa='search-period-menu']")
        if await menu.count() == 0:
            raise SearchSetupError("Не найден элемент периода поиска (search-period-menu)")
        menu_text = re.sub(r"\s+", " ", (await menu.first.text_content()) or "")
        if label.lower() in menu_text.lower():
            return  # уже задано
        if not await self._robust_click(menu, what="период поиска", timeout=5000):
            raise SearchSetupError("Не удалось открыть меню периода поиска")
        await self.pause_async(0.5, 1)
        option = self.page.get_by_text(label, exact=True)
        if await option.count() == 0 or not await self._robust_click(
            option, what=f"период '{label}'", timeout=5000
        ):
            raise SearchSetupError(f"Не найден вариант периода '{label}' в меню")
        await self.pause_async(0.5, 1)

    async def _set_show(self) -> None:
        """
        Задает количество вакансий на странице (элемент вне панели).

        Размер выдачи — параметр URL (items_on_page). Основная стратегия — выбор
        через меню; после submit панель страница может перерисовываться и клики
        теряются, поэтому операция повторяется с ожиданием стабильности. Если UI
        не применил значение — гарантированный fallback: навигация по сохранённому
        адресу поиска с параметром items_on_page.
        """
        logger.debug("Задаем количество вакансий на странице")
        show = self.search_params.get("show") or {}
        key = self._first_true_key(show)
        key_mapping = {"show_20": "20", "show_50": "50", "show_100": "100"}
        target = key_mapping.get(key)
        if not target:
            return

        menu = self.page.locator("[data-qa='items_on_page-menu']")
        if await menu.count() == 0:
            raise SearchSetupError("Не найден элемент размера страницы (items_on_page-menu)")

        async def menu_text() -> str:
            return re.sub(r"\s+", " ", (await menu.first.text_content()) or "").strip()

        if re.match(rf"^{target}(\s|$)", await menu_text()):
            logger.info(f"Размер страницы уже {target}")
            self.search_page_url = self.page.url
            return

        options = self.page.locator("[data-qa^='magritte-select-option-']")
        for attempt in range(1, 3):
            await self._settle_search_page()

            if re.match(rf"^{target}(\s|$)", await menu_text()):
                break

            if not await self._robust_click(menu, what="размер страницы", timeout=5000):
                await self._close_page_size_menu()
                continue

            # Ждём, пока popup реально открылся
            popup_open = False
            for _ in range(10):
                try:
                    if await menu.first.get_attribute("aria-expanded") == "true":
                        popup_open = True
                        break
                except Exception:
                    break
                await asyncio.sleep(0.3)
            if not popup_open:
                logger.warning(f"Popup размера страницы не открылся (попытка {attempt})")
                await self._close_page_size_menu()
                continue
            await self.pause_async(0.5, 1)

            picked = False
            for i in range(await options.count()):
                option = options.nth(i)
                try:
                    if not await option.is_visible():
                        continue
                except Exception:
                    continue
                qa = (await option.get_attribute("data-qa")) or ""
                text = re.sub(r"\s+", " ", (await option.text_content() or "")).strip()
                # На живом hh.ru текст опции «20 вакансий», data-qa «magritte-select-option-20»
                if qa == f"magritte-select-option-{target}" or re.match(rf"^{target}(\s|$)", text):
                    picked = await self._robust_click(option, what=f"размер {target}")
                    break
            if not picked:
                exact = self.page.get_by_text(target, exact=True)
                if await exact.count() > 0:
                    picked = await self._robust_click(
                        exact, what=f"размер {target}", timeout=3000
                    )
            if not picked:
                logger.warning(f"Не найдена опция размера {target} (попытка {attempt})")
                await self._close_page_size_menu()
                continue

            applied = False
            for _ in range(self.PAGE_SIZE_VERIFY_POLLS):
                if re.match(rf"^{target}(\s|$)", await menu_text()):
                    applied = True
                    break
                await asyncio.sleep(0.5)
            if applied:
                # Сверяем условия в адресе: при смене размера параметры поиска
                # не должны теряться; адрес сохраняется только после проверки
                await self._validate_search_applied()
                logger.info(f"Размер страницы применён: {target}")
                return
            logger.warning(f"Клик по размеру {target} не применился (попытка {attempt}), повторяю")
            await self._close_page_size_menu()

        # Гарантированный fallback: размер — URL-параметр, навигируем с ним
        logger.warning("Размер не применился через меню, применяю через адрес (items_on_page)")
        base = self.search_page_url or self.page.url
        parsed = urllib.parse.urlparse(base)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        query["items_on_page"] = [target]
        new_url = urllib.parse.urlunparse(
            parsed._replace(query=urllib.parse.urlencode(query, doseq=True))
        )
        await self._goto_search_url(new_url)
        await self._handle_interfering_messages()
        try:
            await self.page.wait_for_selector(
                '[data-qa="vacancy-serp__vacancy"], [data-qa="empty-vacancy-search-block"]',
                timeout=self.SEARCH_RESULTS_WAIT_MS,
            )
        except Exception:
            logger.warning("Выдача не отрисовалась после смены размера страницы")

        final_text = await menu_text()
        if not re.match(rf"^{target}(\s|$)", final_text):
            raise SearchSetupError(
                f"Размер страницы не применён: ожидалось {target}, в меню «{final_text}»"
            )
        # Адрес навигации собрался из сохранённого (с условиями) — проверяем условия
        await self._validate_search_applied()
        logger.info(f"Размер страницы применён через адрес: {target}")

    async def _settle_search_page(self) -> None:
        """Ждёт, пока выдача отрисовалась и страница «успокоилась» (для кликов)."""
        try:
            await self.page.wait_for_selector(
                '[data-qa="vacancy-serp__vacancy"], [data-qa="empty-vacancy-search-block"]',
                timeout=8000,
            )
        except Exception:
            pass
        try:
            await self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        await self.pause_async(0.5, 1)

    async def _close_page_size_menu(self) -> None:
        """Закрывает popup размера страницы (если открыт) и даёт паузу."""
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        await self.pause_async(0.7, 1.2)

    async def get_vacancies_from_page(self, page_num: int = 0) -> List[Dict[str, Any]]:
        """
        Получить вакансии с указанной страницы выдачи.

        Переходит по сохранённому адресу поиска (сохраняя все параметры).
        Возвращает [] только для подтверждённой пустой выдачи; при ошибке hh.ru,
        сбое сети или нераспознанной разметке — SearchPageError (а не []).
        """
        url = self._search_url_for_page(page_num)
        logger.info(f"Переходим на страницу {page_num}: {url}")
        await self._goto_search_url(url)
        await self._handle_interfering_messages()

        # Ждём появления карточек или явного маркера пустой выдачи
        try:
            await self.page.wait_for_selector(
                '[data-qa="vacancy-serp__vacancy"], [data-qa="empty-vacancy-search-block"]',
                timeout=self.SEARCH_RESULTS_WAIT_MS,
            )
        except Exception:
            logger.warning("Карточки вакансий и маркер пустой выдачи не появились вовремя")

        cards = await self.page.locator('[data-qa="vacancy-serp__vacancy"]').all()
        if not cards:
            empty_marker = self.page.locator('[data-qa="empty-vacancy-search-block"]')
            if await empty_marker.count() > 0 and await empty_marker.first.is_visible():
                logger.info("Подтверждённая пустая выдача: вакансий нет")
                return []
            raise SearchPageError(
                "На странице выдачи нет ни карточек вакансий, ни явного признака пустой выдачи — "
                "вероятно, страница не загрузилась или разметка изменилась"
            )

        logger.info(f"Найдено {len(cards)} вакансий на странице {page_num}")
        vacancies: List[Dict[str, Any]] = []
        for card in cards:
            vac = await self._parse_vacancy_card(card)
            if vac:
                vacancies.append(vac)
        if not vacancies:
            raise SearchPageError(
                f"Карточки вакансий есть ({len(cards)}), но ни одну не удалось разобрать — "
                "вероятно, разметка изменилась"
            )
        return vacancies

    async def _parse_vacancy_card(self, card: Locator) -> Optional[Dict[str, Any]]:
        """Парсит карточку вакансии из поисковой выдачи."""
        try:
            # Title element
            title_el = card.locator('[data-qa="serp-item__title"]').first
            if await title_el.count() == 0:
                return None

            title = await get_clean_text(title_el)
            href = await title_el.get_attribute("href")
            if not href:
                return None

            # Full URL
            if not href.startswith("http"):
                full_url = "https://hh.ru" + href
            else:
                full_url = href

            # Vacancy ID
            vacancy_id = None
            # ID is usually in URL path /vacancy/123456
            match = re.search(r"vacancy/(\d+)", full_url)
            if match:
                vacancy_id = match.group(1)

            # Employer
            employer_name = "Unknown"
            employer_id = None
            emp_el = card.locator('[data-qa="vacancy-serp__vacancy-employer"]').first
            if await emp_el.count() > 0:
                employer_name = await get_clean_text(emp_el)
                emp_href = await emp_el.get_attribute("href")
                if emp_href:
                    match_emp = re.search(r"employer/(\d+)", emp_href)
                    if match_emp:
                        employer_id = match_emp.group(1)

            return {
                "name": title,
                "id": vacancy_id,
                "alternate_url": full_url,
                "employer": {
                    "id": employer_id,
                    "name": employer_name,
                },
            }
        except Exception as e:
            logger.warning(f"Failed to parse vacancy card: {e}")
            return None

    async def get_vacancy_full_info(self, vacancy_url: str) -> Dict[str, Any]:
        """Получает полную информацию о вакансии для LLM."""
        try:
            await self.page.goto(vacancy_url, timeout=60000)
            logger.info(f"Переход на страницу: {vacancy_url}")
        except Exception as e:
            logger.warning(f"Ошибка перехода на страницу {vacancy_url}: {e}, пробуем еще раз...")
            try:
                await asyncio.sleep(2)
                await self.page.goto(vacancy_url, timeout=60000)
                logger.info(f"Повторный переход успешен: {vacancy_url}")
            except Exception as e2:
                logger.error(f"Не удалось открыть страницу вакансии {vacancy_url}: {e2}")
                raise

        async def get_text_or_empty(selector: str) -> str:
            el = self.page.locator(selector)
            if await el.count() > 0:
                return await get_clean_text(el.first)
            return ""

        description = await get_text_or_empty('[data-qa="vacancy-description"]')
        title = await get_text_or_empty('[data-qa="vacancy-title"]')

        # Skills
        skills_list = []
        skills_els = self.page.locator('[data-qa="skills-element"]')
        count = await skills_els.count()
        for i in range(count):
            skills_list.append(await get_clean_text(skills_els.nth(i)))
        skills = ", ".join(skills_list)

        # Header details
        experience = await get_text_or_empty('[data-qa="work-experience-text"]')
        employment = await get_text_or_empty('[data-qa="common-employment-text"]')
        hiring_formats = await get_text_or_empty('[data-qa="vacancy-hiring-formats"]')
        schedule = await get_text_or_empty('[data-qa="work-schedule-by-days-text"]')
        working_hours = await get_text_or_empty('[data-qa="working-hours-text"]')
        work_formats = await get_text_or_empty('[data-qa="work-formats-text"]')

        # Salary
        salary = await get_text_or_empty('[data-qa="vacancy-salary"]')
        if not salary:
            # Fallback based on snippet structure (Magritte)
            salary = await get_text_or_empty("xpath=//div[contains(@class, 'vacancy-title')]/span")

        return {
            "title": title,
            "salary": salary,
            "experience": experience,
            "employment": employment,
            "hiring_formats": hiring_formats,
            "schedule": schedule,
            "working_hours": working_hours,
            "work_formats": work_formats,
            "description": description,
            "skills": skills,
        }

    async def _handle_interfering_messages(self) -> None:
        """Обрабатывает мешающие сообщения (куки, уведомления, попапы)."""
        message_was_processed = True

        while message_was_processed:
            await self.pause_async(2, 3)

            # Cookies
            cookies_btn = self.page.locator("xpath=//*[text()='Понятно']")
            if await cookies_btn.count() > 0:
                await cookies_btn.click()
                break
            # Notifications
            close_btn = self.page.locator('[data-qa="notification-close-button"]')
            if await close_btn.count() > 0:
                await close_btn.click()
                break
            # Additional data collector popup
            save_btn = self.page.locator('[data-qa="additional-data-collector__popup-save"]')
            if await save_btn.count() > 0:
                await save_btn.click()
                break

            message_was_processed = False

    async def apply_to_vacancy(
        self, vacancy_url: str, cover_letter: str, gpt_answerer: Any, resume_component: Any
    ) -> Tuple[str, str]:
        """
        Откликается на вакансию. Возвращает (Результат, Сообщение).
        Результат: 'Success', 'Skip', 'Error', 'Limit'
        """
        if self.page.url != vacancy_url:
            await self.page.goto(vacancy_url)
            logger.info(f"Переход на страницу: {vacancy_url}")

        await self.pause_async(1, 2)

        # Click Apply
        apply_btn_top_selector = '[data-qa="vacancy-response-link-top"]'
        apply_btn_bottom_selector = '[data-qa="vacancy-response-link-bottom"]'

        clicked = await safe_click(self.page, apply_btn_top_selector)
        if not clicked:
            clicked = await safe_click(self.page, apply_btn_bottom_selector)

        if not clicked:
            # Check if already applied or other state
            return "Error", "Apply button not found"

        await self.pause_async(1, 2)

        await self._select_resume(resume_component)

        # Wait for modal or navigation
        await self._handle_interfering_messages()

        # Handle Questions
        questions_selector = '[data-qa="task-body"]'
        questions_count = await self.page.locator(questions_selector).count()
        if questions_count > 0:
            logger.info(f"Found {questions_count} questions")
            for i in range(questions_count):
                question_xpath = f"(//*[@data-qa='task-body'])[{i + 1}]"
                question_locator = self.page.locator(question_xpath)
                success, msg = await self._handle_question(
                    question_locator,
                    gpt_answerer,
                    resume_component,
                    question_selector=question_xpath,
                )
                if not success:
                    return "Skip", msg

        # Post-apply Cover Letter
        magritte_cl_form = self.page.locator('[data-qa="vacancy-response-letter-informer"]')
        if await magritte_cl_form.count() > 0:
            logger.info("Found Magritte cover letter form")
            await safe_fill(
                self.page,
                '[data-qa="vacancy-response-letter-informer"] textarea[name="text"]',
                cover_letter,
            )
            await self.pause_async(1, 2)
            # Submit the cover letter
            if await safe_click(
                self.page, '[data-qa="vacancy-response-letter-submit"]', timeout=10000
            ):
                await self.pause_async(2, 3)
                return "Success", "Cover letter sent"

        # Handle Cover Letter
        cl_btn_xpath = "xpath=//*[text()='Добавить' or contains(text(), 'Сопроводительное')]"
        if await self.page.locator(cl_btn_xpath).count() > 0:
            if await self.page.locator(cl_btn_xpath).first.is_visible():
                await safe_click(self.page, cl_btn_xpath, supress_warnings=True)
                await self.pause_async(1, 2)

        cl_input = self.page.locator('[data-qa="vacancy-response-popup-form-letter-input"]')
        if await cl_input.count() > 0:
            await safe_fill(
                self.page,
                '[data-qa="vacancy-response-popup-form-letter-input"]',
                cover_letter,
                supress_warnings=True,
            )
            await self.pause_async(1, 2)

        await self._handle_interfering_messages()

        # Check if Magritte modal with specific submit button is open
        modal_submit_btn = self.page.locator('[data-qa="vacancy-response-submit-popup"]')
        if await modal_submit_btn.count() > 0 and await modal_submit_btn.is_visible():
            logger.info("Found Magritte response modal submit button")
            await safe_click(self.page, '[data-qa="vacancy-response-submit-popup"]')
            await self.pause_async(3, 4)
            return "Success", ""

        # Submit
        submit_btn = self.page.locator("xpath=//*[text()='Откликнуться']")
        if await submit_btn.count() > 0:
            await safe_click(self.page, submit_btn)
            await self.pause_async(3, 4)
            return "Success", ""

        return "Error", "Submit button not found"

    async def _select_resume(self, resume_component: Any) -> None:
        """Выбирает резюме из списка."""
        if not self.page or not resume_component:
            return

        target_title = (getattr(resume_component, "job_title", "") or "").strip()
        if not target_title:
            return

        # Resume selector trigger is present only on some apply flows.
        trigger_selectors = [
            "[data-qa='resume-title']",
            "xpath=//*[@data-qa='resume-title']",
        ]
        trigger_clicked = False
        for selector in trigger_selectors:
            try:
                if await safe_click(self.page, selector, timeout=2000):
                    trigger_clicked = True
                    break
            except Exception:
                continue

        if trigger_clicked:
            await self.pause_async(0.5, 1)

        # Options list appears as magritte select list
        options_locator = self.page.locator("[data-qa^='magritte-select-option-']")
        try:
            await options_locator.first.wait_for(state="visible", timeout=3000)
        except Exception:
            return

        options = await options_locator.all()
        if not options:
            return

        titles: List[str] = []
        for opt in options:
            title_el = opt.locator("[data-qa='resume-title'] [data-qa='cell-text-content']").first
            if await title_el.count() == 0:
                title_el = opt.locator("[data-qa='resume-title']").first
            title_text = (await title_el.text_content()) if await title_el.count() > 0 else ""
            title_text = re.sub(r"\s+", " ", (title_text or "")).strip()
            titles.append(title_text)

        # Pick the closest title
        target_norm = target_title.lower()
        best_idx = None
        best_dist = None
        for idx, title in enumerate(titles):
            if not title:
                continue
            d = distance(target_norm, title.lower())
            if best_dist is None or d < best_dist:
                best_dist = d
                best_idx = idx

        if best_idx is None:
            return

        await safe_click(self.page, "[data-qa^='magritte-select-option-']", element_number=best_idx)

        await self.pause_async(0.5, 1)

        await safe_click(self.page, "[data-qa='vacancy-response-submit-popup']", timeout=10000)

    async def _handle_question(
        self,
        question: Locator,
        gpt_answerer: Any,
        resume_component: Any,
        question_selector: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Обрабатывает одиночный вопрос в анкете."""
        # 1. Extract Question Text
        question_text_el = question.locator('[data-qa="task-question"]').first
        if await question_text_el.count() > 0:
            question_text = await get_clean_text(question_text_el)
        else:
            question_text = await get_clean_text(question)
        logger.info(f"Handling question: {question_text}")

        # 2. Check for Magritte Cells (Radio/Checkbox with labels)
        cells = await question.locator('[data-qa="cell"]').all()
        if cells:
            # Determine type from first cell
            is_radio = await cells[0].locator('[data-qa="radio-container"]').count() > 0
            is_checkbox = await cells[0].locator('[data-qa="checkbox-container"]').count() > 0

            if is_radio or is_checkbox:
                options = []
                for cell in cells:
                    text_el = cell.locator('[data-qa="cell-text-content"]').first
                    text = await get_clean_text(text_el) if await text_el.count() > 0 else ""
                    options.append(text)

                options.append("No info")

                if is_radio:
                    answer = gpt_answerer.select_one_answer_from_options(question_text, options)
                    clicked = False
                    for i, opt in enumerate(options):
                        if opt == answer and opt != "No info":
                            await cells[i].click()
                            clicked = True
                            break
                    if clicked:
                        return True, ""
                    return False, "No suitable answer found"

                elif is_checkbox:
                    answers = gpt_answerer.select_many_answers_from_options(question_text, options)
                    clicked = False
                    for i, opt in enumerate(options):
                        if opt in answers and opt != "No info":
                            await cells[i].click()
                            clicked = True
                    return clicked, "No suitable answer found" if not clicked else ""

        # 3. Radio (fallback)
        radios = await question.locator('[data-qa="radio-container"]').all()
        if radios:
            options = []
            for r in radios:
                options.append(await get_clean_text(r))

            options.append("No info")
            answer = gpt_answerer.select_one_answer_from_options(question_text, options)

            for i, opt in enumerate(options):
                if opt == answer and opt != "No info":
                    if question_selector:
                        # Use safe_click with index relative to the question
                        radio_xpath = f"{question_selector}//*[@data-qa='radio-container']"
                        await safe_click(self.page, radio_xpath, element_number=i)
                    else:
                        # Fallback if no selector provided (should not happen with new call)
                        await radios[i].click()
                    return True, ""
            return False, "No suitable answer found"

        # 4. Checkbox (fallback)
        checkboxes = await question.locator('[data-qa="checkbox-container"]').all()
        if checkboxes:
            options = []
            for c in checkboxes:
                options.append(await get_clean_text(c))

            options.append("No info")
            answers = gpt_answerer.select_many_answers_from_options(question_text, options)

            clicked = False
            for i, opt in enumerate(options):
                if opt in answers and opt != "No info":
                    if question_selector:
                        checkbox_xpath = f"{question_selector}//*[@data-qa='checkbox-container']"
                        await safe_click(self.page, checkbox_xpath, element_number=i)
                    else:
                        await checkboxes[i].click()
                    clicked = True

            return clicked, "No suitable answer found" if not clicked else ""

        # 5. Textarea (Inside)
        textarea = question.locator("textarea")
        if await textarea.count() > 0:
            answer = gpt_answerer.answer_question_textual_wide_range(question_text)
            answer = resume_component.deanonymize_personal_information(answer)
            await textarea.fill(answer)
            return True, ""

        # 6. Textarea (Sibling - Magritte)
        if question_selector:
            sibling_textarea = self.page.locator(
                f"xpath={question_selector}/following-sibling::div[@data-qa='textarea-wrapper'][1]//textarea"
            )
            if await sibling_textarea.count() > 0:
                answer = gpt_answerer.answer_question_textual_wide_range(question_text)
                await sibling_textarea.fill(answer)
                return True, ""

        return False, "Unknown question type"

    async def get_my_resumes_from_browser(self) -> Dict[str, Any]:
        """Получает список резюме пользователя через браузер."""
        await self.ensure_logged_in()
        # Open "Резюме и профиль" page from main menu
        menu_selector = '[data-qa="mainmenu_profileAndResumes"]'
        clicked = await safe_click(self.page, menu_selector, timeout=10000)
        if not clicked:
            await self.page.goto("https://hh.ru")
            logger.info("Переход на страницу: https://hh.ru")
            await self.pause_async(1, 2)
            await safe_click(self.page, menu_selector, timeout=10000)
        # Wait until resume cards are visible on the resumes/profile page
        try:
            await self.page.wait_for_selector(
                '[data-qa="resume"], [data-qa="resume resume-highlighted"]', timeout=15000
            )
        except Exception:
            logger.warning("Resume list not found after opening 'Резюме и профиль' page.")
            return {"items": []}

        def _extract_resume_id_from_href(href: Optional[str]) -> Optional[str]:
            if not href:
                return None
            match = re.search(r"/resume/([a-zA-Z0-9]+)", href)
            if match:
                return match.group(1)
            match = re.search(r"[?&]resume=([a-zA-Z0-9]+)", href)
            if match:
                return match.group(1)
            return None

        resumes: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()

        cards = await self.page.locator(
            '[data-qa="resume"], [data-qa="resume resume-highlighted"]'
        ).all()
        for card in cards:
            title = (await card.get_attribute("data-qa-title")) or ""
            title = title.strip()
            if not title:
                title_el = card.locator('[data-qa="title"]').first
                title = ((await title_el.text_content()) or "").strip()

            link = card.locator('a[href][data-qa^="resume-card-link-"]').first
            if await link.count() == 0:
                link = card.locator('a[href*="/resume/"], a[href*="/profile/resume?resume="]').first

            href = await link.get_attribute("href") if await link.count() > 0 else None
            resume_id = _extract_resume_id_from_href(href)
            if not resume_id:
                continue

            if resume_id in seen_ids:
                continue
            seen_ids.add(resume_id)

            resumes.append({"id": resume_id, "title": title})

        logger.info(f"Found {len(resumes)} resumes")

        return {"items": resumes}

    async def get_resume_content_from_browser(self, resume_id: str) -> Dict[str, Any]:
        """
        Открывает страницу резюме hh.ru и парсит ключевые разделы.

        Сохраняет обратную совместимость, возвращая данные в формате API, когда это возможно,
        и всегда добавляет спарсенные разделы.
        """
        resume = {}

        user_profile_url = "https://hh.ru/profile/me"
        await self.page.goto(user_profile_url)
        logger.info(f"Переход на страницу: {user_profile_url}")
        await self.pause_async(3, 4)

        resume["personal_information"] = {}
        resume["personal_information"]["first_name"] = await self._get_first_name()
        resume["personal_information"]["last_name"] = await self._get_last_name()
        resume["personal_information"]["birthday"] = await self._get_birthday()
        resume["personal_information"]["telegram"] = await self._get_telegram()
        resume["personal_information"]["whatsapp"] = await self._get_whatsapp()
        resume["area"] = await self._get_area()
        resume["driving_license"] = await self._get_driving_license()
        linkedin, habr_career = await self._get_other_links()
        if linkedin:
            resume["personal_information"]["linkedin"] = linkedin
        if habr_career:
            resume["personal_information"]["habr_career"] = habr_career
        await safe_click(
            self.page,
            "xpath=//*[@data-qa='profile-common-card']//*["
            "@data-qa='link' and .//*[@data-qa='link-text' and normalize-space()='Редактировать']"
            "]",
            timeout=10000,
        )
        await self.pause_async(2, 3)
        middle_name = await self._get_middle_name()
        birthday = await self._get_birthday()

        if middle_name:
            resume["personal_information"]["middle_name"] = middle_name
        if birthday:
            resume["personal_information"]["birthday"] = birthday
        (
            sex,
            citizenship,
            legal_auth,
        ) = await self._get_sex_citizenship_and_legal_auth()
        if sex:
            resume["personal_information"]["sex"] = sex
        if citizenship:
            resume["citizenship"] = citizenship
        if legal_auth:
            resume["legal_authorization"] = legal_auth

        resume_url = f"https://hh.ru/resume/{resume_id}"
        await self.page.goto(resume_url)
        logger.info(f"Переход на страницу: {resume_url}")
        await self.pause_async(2, 3)

        resume["personal_information"]["phone"] = await self._get_resume_phone()
        resume["personal_information"]["email"] = await self._get_resume_email()
        resume["job_preferences"] = {}
        resume["job_preferences"]["job_type"] = await self._get_job_type()
        resume["job_preferences"]["job_format"] = await self._get_job_format()
        resume["job_preferences"]["time_to_travel"] = await self._get_time_to_travel()
        resume["job_preferences"][
            "readiness_to_job_trips"
        ] = await self._get_readiness_to_job_trips()
        resume["job_preferences"]["salary"] = await self._get_salary()
        resume["total_experience"] = await self._get_total_experience()
        resume["experience"] = await self._get_experience()
        resume["skills"] = await self._get_skills()
        resume["educations"] = await self._get_educations()
        resume["recommendations"] = await self._get_recommendations()
        resume["additional_education"] = await self._get_additional_education()
        resume["exams"] = await self._get_exams()
        resume["certificates"] = await self._get_certificates()
        await self.raise_resume()

        resume_url = f"https://hh.ru/resume/edit/{resume_id}/about"
        await self.page.goto(resume_url)
        logger.info(f"Переход на страницу: {resume_url}")
        await self.pause_async(2, 3)
        resume["about_me"] = await self._get_about_me()
        return Resume(**resume).model_dump()

    async def raise_resume(self) -> None:
        """Поднимает резюме в поиске."""
        raise_btn_xpath = "xpath=//*[contains(text(), 'Поднять в') and contains(text(), 'поиске')]"
        if await safe_click(self.page, raise_btn_xpath):
            await self.pause_async(2, 3)
            logger.info("Резюме успешно поднято")
        else:
            logger.info("Резюме пока нельзя поднять")

    async def _get_first_name(self) -> str:
        """Получает имя из профиля."""
        first_name_locator = self.page.locator('[data-qa="profile-common-card-firstname"]')
        if await first_name_locator.count() > 0:
            first_name = await first_name_locator.first.text_content()
            return sanitize_text(first_name, lowercase=False)
        return ""

    async def _get_other_links(self) -> Tuple[str, str]:
        """Получает другие ссылки (LinkedIn, Habr Career)."""
        linkedin = ""
        habr_career = ""
        other_links = await self.page.locator(
            '[data-qa*="profile-other-communication-methods-card-row"]'
        ).all()
        for other_link in other_links:
            text = await other_link.text_content()
            text = text.replace("\u2009", "").replace("\xa0", " ")
            if "linkedin.com" in text:
                linkedin = text
            elif "habr.ru" in text:
                habr_career = text
        return linkedin, habr_career

    async def _get_middle_name(self) -> str:
        """Получает отчество из профиля."""
        middle_name_locator = self.page.locator('[data-qa*="profile-common-edit-middleName"]')
        if await middle_name_locator.count() > 0:
            middle_name = await middle_name_locator.first.get_attribute("value")
            return sanitize_text(middle_name, lowercase=False)
        return ""

    async def _get_birthday(self) -> str:
        """Получает дату рождения."""
        birthday_locator = self.page.locator('[data-qa="profile-common-edit-birthday"]')
        if await birthday_locator.count() > 0:
            birthday = await birthday_locator.first.get_attribute("value")
            return sanitize_text(birthday)
        return ""

    async def _get_sex_citizenship_and_legal_auth(self) -> Tuple[str, str, str]:
        """Получает пол, гражданство и разрешение на работу."""
        select_activators = await self.page.locator('[data-qa="magritte-select-activator"]').all()
        sex = ""
        citizenship = ""
        work_permission = ""
        for select_activator in select_activators:
            text = await select_activator.text_content()
            text = text.replace("\u2009", "").replace("\xa0", " ")
            if text.startswith("Пол"):
                sex = text.replace("Пол", "").strip()
            elif text.startswith("Гражданство"):
                citizenship = text.replace("Гражданство", "").strip()
            elif text.startswith("Разрешение на работу"):
                work_permission = text.replace("Разрешение на работу", "").strip()
        return sex, citizenship, work_permission

    async def _get_last_name(self) -> str:
        """Получает фамилию."""
        last_name_locator = self.page.locator('[data-qa="profile-common-card-lastname"]')
        if await last_name_locator.count() > 0:
            last_name = await last_name_locator.first.text_content()
            return sanitize_text(last_name, lowercase=False)
        return ""

    async def _get_telegram(self) -> str:
        """Получает Telegram из контактов."""
        telegram = self.page.locator("xpath=//*[contains(text(), 'Telegram')]")
        if await telegram.count() > 0:
            parent = telegram.first.locator("../../../../../../../..")
            telegram = await parent.text_content()
            telegram = telegram.replace("Telegram", "").strip()
            return telegram
        return ""

    async def _get_whatsapp(self) -> str:
        """Получает WhatsApp из контактов."""
        whatsapp = self.page.locator("xpath=//*[contains(text(), 'Whatsapp')]")
        if await whatsapp.count() > 0:
            parent = whatsapp.first.locator("../../../../../../../..")
            whatsapp = await parent.text_content()
            whatsapp = whatsapp.replace("Whatsapp", "").strip()
            return whatsapp
        return ""

    async def _get_area(self) -> str:
        """Получает местоположение (город)."""
        area = self.page.locator("xpath=//*[contains(text(), 'Где живёте')]")
        if await area.count() > 0:
            parent = area.first.locator("../../../../../../../..")
            area = await parent.text_content()
            area = area.replace("Где живёте", "").strip()
            area = area.split("·")[0].strip()
            return area
        return ""

    async def _get_driving_license(self) -> str:
        """Получает информацию о водительских правах."""
        driving_license = self.page.locator("xpath=//*[contains(text(), 'Опыт вождения')]")
        if await driving_license.count() > 0:
            parent = driving_license.first.locator("../../..")
            driving_license = await parent.text_content()
            driving_license = driving_license.replace("Опыт вождения", "").strip()
            driving_license = sanitize_text(driving_license)
            driving_license = driving_license.split("·")[0].strip()
            return driving_license
        return ""

    async def _get_resume_phone(self) -> str:
        """Получает телефон из резюме."""
        phone = self.page.locator(
            '[data-qa="resume-contact-phone-value-text"], [data-qa="resume-contact-phone-value-preferred-text"]'
        )
        if await phone.count() > 0:
            phone = await phone.first.text_content()
            phone = sanitize_text(phone)
            return phone
        return ""

    async def _get_resume_email(self) -> str:
        """Получает email из резюме."""
        email = self.page.locator(
            '[data-qa="resume-contact-email-value-text"], [data-qa="resume-contact-email-value-preferred-text"]'
        )
        if await email.count() > 0:
            email = await email.first.text_content()
            email = sanitize_text(email, lowercase=False)
            return email
        return ""

    async def _get_salary(self) -> str:
        """Получает зарплату из резюме."""
        salary = self.page.locator('[data-qa="title-description"]')
        if await salary.count() > 0:
            salary = await salary.text_content()
            salary = sanitize_text(salary)
            return salary
        return ""

    async def _get_job_type(self) -> str:
        """Получает тип занятости из резюме."""
        job_type = self.page.locator("xpath=//*[contains(text(), 'Тип занятости:')]")
        if await job_type.count() > 0:
            parent = job_type.first.locator("..")
            job_type = await parent.text_content()
            job_type = sanitize_text(job_type)
            job_type = job_type.split(":")[1].strip()
            return job_type
        return ""

    async def _get_job_format(self) -> str:
        """Получает формат работы из резюме."""
        job_format = self.page.locator("xpath=//*[contains(text(), 'Формат работы:')]")
        if await job_format.count() > 0:
            parent = job_format.first.locator("..")
            job_format = await parent.text_content()
            job_format = sanitize_text(job_format)
            job_format = job_format.split(":")[1].strip()
            return job_format
        return ""

    async def _get_time_to_travel(self) -> str:
        """Получает желательное время в пути до работы."""
        time_to_travel = self.page.locator("xpath=//*[contains(text(), 'Желательное время')]")
        if await time_to_travel.count() > 0:
            parent = time_to_travel.first.locator("..")
            time_to_travel = await parent.text_content()
            time_to_travel = sanitize_text(time_to_travel)
            time_to_travel = time_to_travel.split(":")[1].strip()
            return time_to_travel
        return ""

    async def _get_readiness_to_job_trips(self) -> str:
        """Получает готовность к командировкам."""
        ready_to_job_trip = self.page.locator("xpath=//*[contains(text(), 'Командировки:')]")
        if await ready_to_job_trip.count() > 0:
            parent = ready_to_job_trip.first.locator("..")
            ready_to_job_trip = await parent.text_content()
            ready_to_job_trip = sanitize_text(ready_to_job_trip)
            ready_to_job_trip = ready_to_job_trip.split(":")[1].strip()
            return ready_to_job_trip
        return ""

    async def _get_total_experience(self) -> str:
        """Получает общий опыт работы."""
        total_experience = self.page.locator("xpath=//*[contains(text(), 'Опыт работы:')]")
        if await total_experience.count() > 0:
            parent = total_experience.first.locator("..")
            total_experience = await parent.text_content()
            total_experience = sanitize_text(total_experience)
            total_experience = total_experience.split(":")[1].strip()
            return total_experience
        return ""

    async def _get_experience(self) -> str:
        """Получает опыт работы (описание)."""
        experience = self.page.locator('[data-qa="resume-list-card-experience"]')
        group_locators = experience.locator('[class^="group--"]')
        experience_texts = await group_locators.all_text_contents()
        experience_texts = [
            text.replace("\u2009", "").replace("\xa0", " ") for text in experience_texts
        ]
        experience = "\n".join(experience_texts)
        return experience

    async def _get_skills(self) -> str:
        """Получает навыки."""
        skill_card = self.page.locator("[data-qa='skills-card']")
        skills = skill_card.locator('[class^="magritte-tag__label"]')
        skills = await skills.all_text_contents()
        skills = "\n".join(skills)
        return skills

    async def _get_educations(self) -> str:
        """Получает образование."""
        education_card = self.page.locator("[data-qa='resume-list-card-education']")
        educations = education_card.locator('[data-qa="cell-text-content"]')
        educations = await educations.all_text_contents()
        educations = "\n".join(educations)
        return educations

    async def _get_about_me(self) -> str:
        """Получает информацию 'Обо мне'."""
        about_me = self.page.locator("[data-qa='resume-editor-about']")
        about_me = await about_me.all_text_contents()
        about_me = "\n".join(about_me)
        return about_me

    async def _get_recommendations(self) -> str:
        """Получает рекомендации."""
        recommendations = self.page.locator("[data-qa='resume-list-card-recommendation']")
        recommendations_locator = recommendations.locator('[data-qa="cell-text-content"]')
        recommendations = await recommendations_locator.all_text_contents()
        recommendations = "\n".join(recommendations)
        return recommendations

    async def _get_additional_education(self) -> str:
        """Получает дополнительное образование."""
        additional_education = self.page.locator("[data-qa='resume-list-card-additionalEducation']")
        additional_education_locator = additional_education.locator('[data-qa="cell-text-content"]')
        additional_education = await additional_education_locator.all_text_contents()
        additional_education = "\n".join(additional_education)
        return additional_education

    async def _get_exams(self) -> str:
        """Получает информацию об экзаменах/тестах."""
        exams = self.page.locator("[data-qa='resume-list-card-certificate']")
        exams_locator = exams.locator('[data-qa="cell-text-content"]')
        exams = await exams_locator.all_text_contents()
        exams = [
            r
            for r in exams
            if not (r == "Профориентация" or r.startswith("Тест поможет определить ваши"))
        ]
        exams = "\n".join(exams)
        return exams

    async def _get_certificates(self) -> str:
        """Получает сертификаты."""
        certificates = self.page.locator("[data-qa='resume-list-card-certificate']")
        certificates_locator = certificates.locator('[data-qa="cell-text-content"]')
        certificates = await certificates_locator.all_text_contents()
        certificates = "\n".join(certificates)
        return certificates

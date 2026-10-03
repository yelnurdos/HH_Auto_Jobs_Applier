"""
Ограниченная проверка поиска: открыть поиск hh.ru с настройками пользователя
и оценить максимум N реальных вакансий.

Гарантии процесса проверки:
- отклики не отправляются (send_repsonse/apply_job не вызываются);
- резюме не поднимается (raise_resume явно заблокирован на время проверки);
- Telegram-уведомления (обработчик ошибок и капча) заблокированы;
- резервные модели LLM отключаются только в памяти, чтобы проверить именно
  основную модель; фактическая модель подтверждается отдельно для каждой
  оценённой вакансии из её ответа LLM (ответы сбора резюме не используются).

Контракт блокировок: run() сам блокировки НЕ устанавливает и НЕ снимает.
Область блокировки (block/restore) управляет вызывающий на весь жизненный
цикл проверки: до первого обращения к браузеру/авторизации и до сохранения
отчёта, итогового логирования и закрытия браузера (scripts/verify_search.py).
"""
import time
import traceback
import types
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from src.job_manager.playwright_manager import (
    PlaywrightJobManager,
    SearchPageError,
    SearchSetupError,
)
from src.logger_config import logger
from src.telegram.telegram_error_handler import AsyncTelegramSink


class SearchProbeAborted(Exception):
    """Проверка остановлена: требуется ручное действие (вход/капча)."""


class SearchProbe:
    """Пробный запуск: поиск + оценка максимум MAX_VACANCIES вакансий."""

    MAX_VACANCIES = 3
    WALL_CLOCK_LIMIT_SEC = 30 * 60

    def __init__(
        self,
        manager: PlaywrightJobManager,
        resume_component: Any,
        search_component: Any,
        apply_component: Any,
        gpt_answerer: Any,
    ):
        self.manager = manager
        self.resume_component = resume_component
        self.search_component = search_component
        self.apply_component = apply_component
        self.gpt_answerer = gpt_answerer

        self._orig_raise_resume = None
        self._orig_fallback_models = None
        self._orig_adapter_invoke = None
        self._orig_chain_entry = None
        self._orig_sink_call = None
        self._orig_process_captcha = None
        # Журнал LLM-вызовов: (успех, модель из метаданных ответа)
        self._llm_records: List[Tuple[bool, Optional[str]]] = []
        # Явный сигнал: цепочка job_is_interesting завершилась успехом
        # (вызов LLM + разбор структурированного ответа)
        self._last_chain_ok = False

    # -----------------------------
    # Блокировка внешних эффектов
    # -----------------------------

    def _block_external_side_effects(self) -> None:
        """
        Явно блокирует подъём резюме, отправку в Telegram (ошибки/капча)
        и в памяти отключает резервные модели LLM.
        """

        async def _raise_resume_blocked() -> None:
            logger.info("Подъём резюме заблокирован на время проверки (raise_resume не выполнен)")

        self._orig_raise_resume = self.manager.raise_resume
        self.manager.raise_resume = _raise_resume_blocked

        # Резервные модели — только в памяти: проверяем именно основную модель
        model_obj = getattr(getattr(self.gpt_answerer, "ai_adapter", None), "model", None)
        if model_obj is not None and hasattr(model_obj, "fallback_models"):
            self._orig_fallback_models = model_obj.fallback_models
            model_obj.fallback_models = []

        # Telegram-обработчик ошибок — без сообщений во время проверки.
        # sink вызывается по экземпляру: AsyncTelegramSink.__call__(<self>, message)
        self._orig_sink_call = AsyncTelegramSink.__call__

        def _sink_blocked(_self: Any, *args: Any, **kwargs: Any) -> None:
            message = args[0] if args else kwargs
            logger.debug(
                f"Telegram-уведомление заблокировано на время проверки: {str(message)[:120]}"
            )

        AsyncTelegramSink.__call__ = _sink_blocked  # type: ignore[assignment]

        # Капча — понятная остановка вместо отправки в Telegram
        from src.job_manager import playwright_manager as _pw

        async def _captcha_blocked(*args: Any, **kwargs: Any) -> str:
            raise SearchProbeAborted(
                "На hh.ru требуется ручное действие (вход/капча) — проверочный запуск остановлен. "
                "Выполните действие в браузере и запустите проверку повторно."
            )

        self._orig_process_captcha = _pw.process_captcha
        _pw.process_captcha = _captcha_blocked

    def _capture_llm_model(self) -> None:
        """
        Перехватывает LLM-вызовы:
        - ai_adapter.invoke: успех/сбой вызова и фактическая модель из
          метаданных ответа (журнал _llm_records);
        - цепочка job_is_interesting: явный сигнал успеха «вызов + разбор»
          (_last_chain_ok) — не зависит от текста сообщения об ошибке.
        """
        adapter = self.gpt_answerer.ai_adapter
        self._orig_adapter_invoke = adapter.invoke

        def _capturing_invoke(prompt: Any) -> Any:
            try:
                reply = self._orig_adapter_invoke(prompt)
            except Exception:
                self._llm_records.append((False, None))
                raise
            metadata = getattr(reply, "response_metadata", None) or {}
            model_name = metadata.get("model_name") or metadata.get("model")
            self._llm_records.append(
                (True, model_name if isinstance(model_name, str) else None)
            )
            return reply

        adapter.invoke = _capturing_invoke

        # Сам RunnableSequence (pydantic) атрибуты не принимает, поэтому цепочку
        # заменяем в словаре chains на прокси: job_is_interesting ищет её по
        # словарю при каждом вызове
        original_entry = self.gpt_answerer.chains["job_is_interesting"]
        inner_chain, parser = original_entry
        self._orig_chain_entry = original_entry

        def _capturing_chain_invoke(*args: Any, **kwargs: Any) -> Any:
            self._last_chain_ok = False
            try:
                result = inner_chain.invoke(*args, **kwargs)
            except Exception:
                self._last_chain_ok = False
                raise
            self._last_chain_ok = True
            return result

        proxy = types.SimpleNamespace()
        proxy.invoke = _capturing_chain_invoke
        self.gpt_answerer.chains["job_is_interesting"] = (proxy, parser)

    def _restore_external_side_effects(self) -> None:
        """Возвращает блокировки (идемпотентно)."""
        if self._orig_raise_resume is not None:
            self.manager.raise_resume = self._orig_raise_resume
            self._orig_raise_resume = None
        model_obj = getattr(getattr(self.gpt_answerer, "ai_adapter", None), "model", None)
        if (
            model_obj is not None
            and hasattr(model_obj, "fallback_models")
            and self._orig_fallback_models is not None
        ):
            model_obj.fallback_models = self._orig_fallback_models
            self._orig_fallback_models = None
        if self._orig_sink_call is not None:
            AsyncTelegramSink.__call__ = self._orig_sink_call
            self._orig_sink_call = None
        if self._orig_adapter_invoke is not None:
            self.gpt_answerer.ai_adapter.invoke = self._orig_adapter_invoke
            self._orig_adapter_invoke = None
        if self._orig_chain_entry is not None:
            self.gpt_answerer.chains["job_is_interesting"] = self._orig_chain_entry
            self._orig_chain_entry = None
        if self._orig_process_captcha is not None:
            from src.job_manager import playwright_manager as _pw

            _pw.process_captcha = self._orig_process_captcha
            self._orig_process_captcha = None

    # -----------------------------
    # Отчёт: запрошенные и подтверждённые фильтры
    # -----------------------------

    def _requested_filters(self) -> Dict[str, Any]:
        """Настройки из search_config.yaml (что запрошено)."""
        params = self.search_component.search_params or {}
        show = params.get("show") or {}
        areas = [
            a.strip()
            for a in str(params.get("area") or "").replace(";", ",").split(",")
            if a.strip()
        ]
        return {
            "keywords": params.get("keywords"),
            "areas": areas,
            "search_field": params.get("search_field"),
            "only_with_salary": params.get("only_with_salary"),
            "show": next((k for k, v in (show or {}).items() if v is True), None),
        }

    def _observed_filters(self) -> Dict[str, Any]:
        """Фильтры, фактически подтверждённые адресом выдачи (что применено)."""
        url = self.manager.search_page_url or ""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        area_ids = sorted({str(v) for v in query.get("area", [])})
        name_by_id = {
            str(area_id): name
            for name, area_id in (getattr(self.manager, "_applied_area_ids", {}) or {}).items()
        }
        return {
            "keywords": (query.get("text") or [None])[0],
            "areas": [name_by_id.get(area_id, area_id) for area_id in area_ids],
            "search_field": query.get("search_field") or [],
            "only_with_salary": (query.get("with_salary") or ["false"])[0].lower()
            in ("true", "1"),
            "show": (query.get("items_on_page") or [None])[0],
        }

    def _expected_model(self) -> Optional[str]:
        """Модель, заданная для проверки (настраиваемая, не из прошлых ответов)."""
        model_obj = getattr(getattr(self.gpt_answerer, "ai_adapter", None), "model", None)
        name = getattr(model_obj, "model_name", None)
        return name if isinstance(name, str) else None

    # -----------------------------
    # Сам запуск
    # -----------------------------

    async def run(self) -> Dict[str, Any]:
        """
        Прогон: поиск с настройками + сборка и оценка максимум MAX_VACANCIES вакансий.
        Возвращает словарь-отчёт (сохраняется в data_folder/output).

        ВАЖНО: run() сам не устанавливает и не снимает блокировки внешних
        эффектов. Область блокировки (block/restore) вызывающий держит на весь
        жизненный цикл проверки — см. модуль scripts/verify_search.py.
        """
        started = time.time()
        report: Dict[str, Any] = {
            "status": "ok",
            "search_url": "",
            "requested_filters": self._requested_filters(),
            "confirmed_filters": None,
            "cards_parsed": 0,
            "vacancies_checked": [],
            "expected_model": self._expected_model(),
            "actual_model": None,
            "responses_sent": 0,
            "resumes_raised": 0,
            "telegram_messages_sent": 0,
            "error": None,
        }
        try:
            # 1) Поиск с настройками пользователя
            await self.search_component.start_search()
            report["search_url"] = self.manager.search_page_url
            report["confirmed_filters"] = self._observed_filters()

            # 2) Первая страница выдачи
            vacancies = await self.manager.get_vacancies_from_page(0)
            report["cards_parsed"] = len(vacancies)

            # 3) Оценка первых MAX_VACANCIES вакансий (без откликов)
            for vacancy in vacancies[: self.MAX_VACANCIES]:
                if time.time() - started > self.WALL_CLOCK_LIMIT_SEC:
                    report["status"] = "timeout"
                    report["error"] = "Превышен лимит времени проверки"
                    break

                job = await self.apply_component.scrape_vacancy(vacancy)
                self.gpt_answerer.set_job(job)

                # Для каждой вакансии отдельно: успех «вызов + разбор» и фактическая
                # модель из её ответа (ответы сбора резюме в расчёт не идут)
                calls_before = len(self._llm_records)
                self._last_chain_ok = False
                evaluation = self.gpt_answerer.job_is_interesting()
                if not self._last_chain_ok:
                    report["status"] = "error"
                    report["error"] = (
                        f"Ошибка вызова/разбора LLM при оценке вакансии "
                        f"«{vacancy['name']}»: штатной оценки нет, проверка не успешна"
                    )
                    break
                models = [
                    model_name
                    for ok, model_name in self._llm_records[calls_before:]
                    if ok and model_name
                ]
                if not models:
                    report["status"] = "error"
                    report["error"] = (
                        f"Для вакансии «{vacancy['name']}» нет подтверждённого ответа "
                        f"LLM с метаданными модели — модель не подтверждена"
                    )
                    break
                model = models[-1]
                expected = self._expected_model()
                if expected is not None and model != expected:
                    report["status"] = "error"
                    report["error"] = (
                        f"Подмена модели при оценке вакансии «{vacancy['name']}»: "
                        f"ожидалась {expected}, получена {model}"
                    )
                    break
                report["actual_model"] = model
                report["vacancies_checked"].append(
                    {
                        "name": vacancy["name"],
                        "link": vacancy["alternate_url"],
                        "score": evaluation.get("score"),
                        "reasoning": evaluation.get("reasoning"),
                    }
                )
                logger.info(
                    f"Проверена вакансия: {vacancy['name']} (score={evaluation.get('score')})"
                )
        except SearchProbeAborted as e:
            report["status"] = "aborted"
            report["error"] = str(e)
        except (SearchPageError, SearchSetupError) as e:
            report["status"] = "error"
            report["error"] = f"{type(e).__name__}: {e}"
        except Exception as e:
            report["status"] = "error"
            report["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        finally:
            report["duration_sec"] = int(time.time() - started)
        return report

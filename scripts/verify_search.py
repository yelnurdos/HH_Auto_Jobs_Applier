"""
Ограниченная проверка поиска hh.ru (задача 001):
- поиск с настройками пользователя (data / Астана, Алматы / все области / show_20);
- оценка максимум трёх реальных вакансий основной моделью (openai/gpt-6-luna);
- без откликов, без поднятия резюме, без Telegram-сообщений.

Отчёт пишется в data_folder/output/search_probe_<дата>.yaml.

Блокировки внешних эффектов (подъём резюме, Telegram, капча, резервные модели)
устанавливаются ДО первого обращения к браузеру и снимаются в едином внешнем
finally — только после сохранения отчёта, итогового логирования и закрытия
браузера. Так итоговый logger.error при ошибке проверки не достаёт до
настоящего Telegram-обработчика.

Запуск:
    .venv/bin/python -m scripts.verify_search
"""
import asyncio
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from src.constants import SEARCH_CONFIG_FILE, SECRETS_FILE
from src.job_manager.job_applier import JobApplier
from src.job_manager.playwright_manager import PlaywrightJobManager
from src.job_manager.resume_scraper import ResumeScraper
from src.job_manager.search_customizer import SearchCustomizer
from src.job_manager.search_probe import SearchProbe
from src.llm.llm_manager import GPTAnswerer
from src.logger_config import logger
from src.utils.utils import save_yaml_file


def _base_report(probe: SearchProbe, manager: PlaywrightJobManager, status: str, error: str) -> Dict[str, Any]:
    return {
        "status": status,
        "search_url": manager.search_page_url,
        "requested_filters": probe._requested_filters(),
        "confirmed_filters": None,
        "cards_parsed": 0,
        "vacancies_checked": [],
        "expected_model": probe._expected_model(),
        "actual_model": None,
        "responses_sent": 0,
        "resumes_raised": 0,
        "telegram_messages_sent": 0,
        "error": error,
    }


async def main() -> int:
    from main import ConfigValidator, FileManager

    data_folder = Path("data_folder")
    FileManager.validate_data_folder(data_folder)

    validator = ConfigValidator()
    secrets = validator.validate_secrets(SECRETS_FILE)
    parameters = validator.validate_search_config(
        SEARCH_CONFIG_FILE, Path("data_folder/output/search_config_tmp.yaml"), secrets
    )
    llm_proxy = secrets["llm_proxy"]

    manager = PlaywrightJobManager(secrets)
    gpt_answerer = GPTAnswerer(secrets, llm_proxy)
    resume_component = ResumeScraper(
        manager, parameters.get("job_title"), parameters.get("resume_id"), gpt_answerer
    )
    search_component = SearchCustomizer(manager)
    apply_component = JobApplier(manager, resume_component, search_component)
    probe = SearchProbe(
        manager, resume_component, search_component, apply_component, gpt_answerer
    )

    # Одна область блокировки на весь жизненный цикла проверки: до первого
    # обращения к браузеру/авторизации и до сохранения отчёта, итогового
    # логирования и закрытия браузера. Снятие — только во внешнем finally ниже.
    probe._block_external_side_effects()
    probe._capture_llm_model()

    report: Dict[str, Any] = {}
    try:
        await manager.initialize()
        try:
            # Резюме по настройкам (существующая сессия браузера)
            resume_id, resume_titles = await resume_component.get_resume_parameters()
            parameters["resume_id"] = resume_id
            parameters["resume_titles"] = resume_titles
            apply_component.set_parameters(parameters)

            # Собираем резюме (raise_resume заблокирован)
            resume_info, resume_readable = await resume_component.get_resume_info()
            gpt_answerer.set_resume(resume_info, resume_readable)
            gpt_answerer.set_search_parameters(parameters)
            search_component.set_search_parameters(parameters)
            search_component.set_resume(resume_id, resume_info)
            apply_component.set_resume(resume_info)
            apply_component.set_gpt_answerer(gpt_answerer)

            report = await asyncio.wait_for(
                probe.run(), timeout=SearchProbe.WALL_CLOCK_LIMIT_SEC
            )
        except asyncio.TimeoutError:
            report = _base_report(
                probe,
                manager,
                "timeout",
                "Превышен общий лимит времени проверки",
            )
    except Exception as e:
        report = _base_report(
            probe, manager, "error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
    finally:
        if not report:
            report = _base_report(probe, manager, "error", "Отчёт проверки не получен")
        report_path = (
            Path("data_folder/output")
            / f"search_probe_{datetime.now().strftime('%Y-%m-%d_%H%M')}.yaml"
        )
        save_yaml_file(report_path, report)

        # Итоговое логирование — ещё внутри области блокировки
        logger.info(f"Отчёт проверки сохранён: {report_path}")
        logger.info(
            f"Статус: {report['status']}, вакансий оценено: {len(report['vacancies_checked'])}, "
            f"откликов: {report['responses_sent']}, модель: {report['actual_model']}"
        )
        if report.get("error"):
            logger.error(f"Ошибка проверки: {report['error']}")

        try:
            await manager.close()
        finally:
            probe._restore_external_side_effects()

    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

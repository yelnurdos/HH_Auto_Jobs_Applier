import os
import random
import textwrap
import time
import traceback
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
import yaml
from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.messages.ai import AIMessage
from langchain_core.output_parsers import StrOutputParser, PydanticOutputParser
from langchain_core.prompt_values import StringPromptValue
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI, HarmBlockThreshold, HarmCategory
from langchain_cohere import ChatCohere
from Levenshtein import distance

import src.llm.prompts as prompts
from src.utils.utils import load_app_config, load_yaml_file
from src.views.llm import ContactInfo, JobIsInteresting, ResumeIsInteresting, BaseModel
from src.constants import PRICE_DICT
from src.logger_config import logger
from src.utils.json_to_readable import transform_search_config_data, transform_vacancy_data

load_dotenv()

# Load config
config = load_app_config()
LLM_MODEL = config.get("LLM_MODEL", "gpt-5-nano")
LLM_MODEL_TYPE = config.get("LLM_MODEL_TYPE", "openai")
TEMPERATURE = config.get("TEMPERATURE", 0.4)
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MAX_HTTP_RETRIES = 3
DEFAULT_TOKEN_PRICES = {"price_per_input_token": 1.5e-7, "price_per_output_token": 6e-7}


def _proxy_list(llm_proxy: Union[str, List[str], None]) -> List[str]:
    if not llm_proxy:
        return [""]
    if isinstance(llm_proxy, str):
        return [llm_proxy] if llm_proxy else [""]
    proxies = [p for p in llm_proxy if p is not None]
    return proxies if proxies else [""]


def _proxy_label(proxy: str) -> str:
    if not proxy:
        return "без прокси"
    return proxy.split("@")[-1]


def _openrouter_fallback_ids(raw: Any) -> List[str]:
    """Преобразовать FALLBACK_MODELS в список slug'ов OpenRouter."""
    ids: List[str] = []
    for item in raw or []:
        if isinstance(item, str) and item.strip():
            ids.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        model = (item.get("model") or "").strip()
        typ = (item.get("type") or "").strip()
        if not model:
            continue
        if "/" in model:
            ids.append(model)
        elif typ == "openai":
            ids.append(f"openai/{model}")
        elif typ == "gemini":
            ids.append(f"google/{model}")
        elif typ == "cohere":
            ids.append(f"cohere/{model}")
        elif typ == "deepseek":
            ids.append(f"deepseek/{model}")
        else:
            ids.append(model)
    return ids


def _prices_for_model(model_name: str) -> Dict[str, float]:
    candidates = [model_name, LLM_MODEL]
    if model_name and "/" in model_name:
        candidates.append(model_name.split("/")[-1])
    if LLM_MODEL and "/" in LLM_MODEL:
        candidates.append(LLM_MODEL.split("/")[-1])
    for name in candidates:
        if name and name in PRICE_DICT:
            return PRICE_DICT[name]
    return DEFAULT_TOKEN_PRICES


class AIModel(ABC):
    @abstractmethod
    def invoke(self, prompt: str) -> str:
        pass


class OpenAIModel(AIModel):
    """Получить доступ к модели OpenAI"""

    def __init__(self, api_key: str, llm_model: str, llm_proxy: Union[str, None] = None) -> None:
        self.llm_proxy = llm_proxy
        self.model_name = llm_model
        self.openai_api_key = api_key

    def invoke(self, prompt: ChatPromptTemplate) -> BaseMessage:
        logger.info("Получен доступ к модели через OpenAI API")
        prompt_messages = [SystemMessage(content=prompts.custom_instructions)] + prompt.messages
        # случайно выбираем одну прокси за другой, пока запрос к LLM не пройдет
        llm_proxies = _proxy_list(self.llm_proxy)
        random.shuffle(llm_proxies)
        errors = []

        for proxy in llm_proxies:
            try:
                if proxy:
                    http_client = httpx.Client(proxy=proxy)
                else:
                    http_client = None
                model = ChatOpenAI(
                    model_name=self.model_name,
                    openai_api_key=self.openai_api_key,
                    http_client=http_client,
                    temperature=1
                    if "o1" in self.model_name or "gpt-5" in self.model_name
                    else TEMPERATURE,
                    presence_penalty=0,
                    frequency_penalty=0,
                    timeout=60,
                )
                response = model.invoke(prompt_messages)
                if response is None:
                    raise RuntimeError("OpenAI вернула пустой ответ")
                return response
            except Exception as e:
                tb_str = traceback.format_exc()
                logger.error(
                    f"Ошибка доступа к LLM с использованием прокси {_proxy_label(proxy)}: \n Traceback: {tb_str}"
                )
                errors.append(str(e))
                time.sleep(3)

        raise RuntimeError(f"OpenAI недоступна ни через один прокси. Ошибки: {errors}")


class GeminiModel(AIModel):
    """Получить доступ к модели Gemini"""

    def __init__(self, api_key: str, llm_model: str, llm_proxy: Union[str, None] = None) -> None:
        self.llm_proxy = llm_proxy
        self.model = llm_model
        self.google_api_key = api_key

    def invoke(self, prompt: ChatPromptTemplate) -> BaseMessage:
        logger.info("Получен доступ к модели через Gemini API")
        prompt_messages = [SystemMessage(content=prompts.custom_instructions)] + prompt.messages
        llm_proxies = _proxy_list(self.llm_proxy)
        random.shuffle(llm_proxies)
        errors = []

        for proxy in llm_proxies:
            try:
                if proxy:
                    os.environ["https_proxy"] = proxy
                model = ChatGoogleGenerativeAI(
                    model=self.model,
                    google_api_key=self.google_api_key,
                    temperature=TEMPERATURE,
                    safety_settings={
                        HarmCategory.HARM_CATEGORY_UNSPECIFIED: HarmBlockThreshold.BLOCK_NONE,
                        HarmCategory.HARM_CATEGORY_DEROGATORY: HarmBlockThreshold.BLOCK_NONE,
                        HarmCategory.HARM_CATEGORY_TOXICITY: HarmBlockThreshold.BLOCK_NONE,
                        HarmCategory.HARM_CATEGORY_VIOLENCE: HarmBlockThreshold.BLOCK_NONE,
                        HarmCategory.HARM_CATEGORY_SEXUAL: HarmBlockThreshold.BLOCK_NONE,
                        HarmCategory.HARM_CATEGORY_MEDICAL: HarmBlockThreshold.BLOCK_NONE,
                        HarmCategory.HARM_CATEGORY_DANGEROUS: HarmBlockThreshold.BLOCK_NONE,
                        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
                    },
                )
                response = model.invoke(prompt_messages)
                if response is None:
                    raise RuntimeError("Gemini вернула пустой ответ")
                return response
            except Exception as e:
                tb_str = traceback.format_exc()
                logger.error(
                    f"Ошибка доступа к LLM с использованием прокси {_proxy_label(proxy)}: \n Traceback: {tb_str}"
                )
                errors.append(str(e))
                time.sleep(3)
            finally:
                os.environ.pop("https_proxy", None)

        raise RuntimeError(f"Gemini недоступна ни через один прокси. Ошибки: {errors}")


class OpenRouterModel(AIModel):
    """OpenAI-совместимый доступ к OpenRouter с серверным fallback по списку моделей."""

    def __init__(
        self,
        api_key: str,
        llm_model: str,
        llm_proxy: Union[str, List[str], None] = None,
        fallback_models: Optional[List[str]] = None,
    ) -> None:
        self.llm_proxy = llm_proxy
        self.model_name = llm_model
        self.api_key = api_key
        self.fallback_models = fallback_models or []

    def invoke(self, prompt: ChatPromptTemplate) -> BaseMessage:
        logger.info(
            f"Запрос через OpenRouter: {self.model_name}"
            + (f", fallback={self.fallback_models}" if self.fallback_models else "")
        )
        prompt_messages = [SystemMessage(content=prompts.custom_instructions)] + prompt.messages
        llm_proxies = _proxy_list(self.llm_proxy)
        random.shuffle(llm_proxies)
        errors = []
        extra_body = {"models": self.fallback_models} if self.fallback_models else None

        for proxy in llm_proxies:
            try:
                http_client = httpx.Client(proxy=proxy, timeout=60.0) if proxy else None
                chat_kwargs = {
                    "model": self.model_name,
                    "openai_api_key": self.api_key,
                    "base_url": OPENROUTER_BASE_URL,
                    "http_client": http_client,
                    "temperature": TEMPERATURE,
                    "timeout": 60,
                    "default_headers": {
                        "HTTP-Referer": "https://github.com/yelnurdo/HH_Auto_Jobs_Applier",
                        "X-Title": "HH Auto Jobs Applier",
                    },
                }
                if extra_body:
                    chat_kwargs["extra_body"] = extra_body
                model = ChatOpenAI(**chat_kwargs)
                response = model.invoke(prompt_messages)
                if response is None:
                    raise RuntimeError("OpenRouter вернул пустой ответ")
                actual = getattr(response, "response_metadata", {}) or {}
                logger.info(
                    f"OpenRouter ответил моделью: {actual.get('model_name') or actual.get('model') or self.model_name}"
                )
                return response
            except Exception as e:
                tb_str = traceback.format_exc()
                logger.error(
                    f"Ошибка OpenRouter с прокси {_proxy_label(proxy)}: \n Traceback: {tb_str}"
                )
                errors.append(str(e))
                time.sleep(3)

        raise RuntimeError(f"OpenRouter недоступен ни через один прокси. Ошибки: {errors}")


class CohereModel(AIModel):
    """Получить доступ к модели Cohere"""

    def __init__(self, api_key: str, llm_model: str) -> None:
        self.model_name = llm_model
        self.cohere_api_key = api_key

    def invoke(self, prompt: ChatPromptTemplate) -> BaseMessage:
        logger.info("Получен доступ к модели через Cohere API")
        prompt_messages = [SystemMessage(content=prompts.custom_instructions)] + prompt.messages
        model = ChatCohere(
            model=self.model_name,
            cohere_api_key=self.cohere_api_key,
            temperature=TEMPERATURE,
        )
        response = model.invoke(prompt_messages)
        return response


# class ClaudeModel(AIModel):
#     """Получить доступ к модели Claude"""
#
#     def __init__(self, api_key: str, llm_model: str) -> None:
#         from langchain_anthropic import ChatAnthropic
#
#         self.model = ChatAnthropic(model=llm_model, api_key=api_key, temperature=TEMPERATURE)
#
#     def invoke(self, prompt: str) -> BaseMessage:
#         response = self.model.invoke(prompt)
#         logger.debug("Успешно получен доступ к модели через Claude API")
#         return response


# class OllamaModel(AIModel):
#     """Получить доступ к модели Ollama"""
#
#     def __init__(self, llm_model: str, llm_api_url: str) -> None:
#         from langchain_ollama import ChatOllama
#
#         if len(llm_api_url) > 0:
#             logger.debug(f"Используем Ollama с API URL: {llm_api_url}")
#             self.model = ChatOllama(model=llm_model, base_url=llm_api_url)
#         else:
#             self.model = ChatOllama(model=llm_model)
#
#     def invoke(self, prompt: str) -> BaseMessage:
#         response = self.model.invoke(prompt)
#         logger.debug("Успешно получен доступ к модели через Ollama API")
#         return response

# class HuggingFaceModel(AIModel):
#     """Получить доступ к модели Hugging Face"""
#
#     def __init__(self, api_key: str, llm_model: str) -> None:
#         from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint
#
#         self.model = HuggingFaceEndpoint(
#             repo_id=llm_model, huggingfacehub_api_token=api_key, temperature=TEMPERATURE
#         )
#         self.chatmodel = ChatHuggingFace(llm=self.model)
#
#     def invoke(self, prompt: str) -> BaseMessage:
#         response = self.chatmodel.invoke(prompt)
#         logger.debug("Успешно получен доступ к модели через Hugging Face API")
#         return response


# class GigaChatModel(AIModel):
#     """Получить доступ к модели GigaChat"""
#
#     def __init__(self, api_key: str, llm_model: str) -> None:
#         from langchain_gigachat import GigaChat
#
#         if "GIGACHAT_CREDENTIALS" not in os.environ:
#             os.environ["GIGACHAT_CREDENTIALS"] = api_key
#         self.model = GigaChat(
#             verify_ssl_certs=False,
#             scope="GIGACHAT_API_PERS",
#             temperature=TEMPERATURE,
#             model=llm_model,
#         )
#
#     def invoke(self, prompt: ChatPromptTemplate) -> BaseMessage:
#         logger.info("Получен доступ к модели через GigaChat API")
#         prompt_messages = [SystemMessage(content=prompts.custom_instructions)] + prompt.messages
#         response = self.model.invoke(prompt_messages)
#         return response


class FallbackLLM(AIModel):
    """
    Обертка для использования нескольких LLM с механизмом переключения (fallback).
    Если основная модель падает, пробуем следующую по списку.
    """
    def __init__(self, models: List[AIModel]):
        self.models = models

    def invoke(self, prompt: ChatPromptTemplate) -> BaseMessage:
        errors = []
        for model in self.models:
            try:
                logger.info(f"Попытка использования модели: {model.__class__.__name__}")
                result = model.invoke(prompt)
                if result is None:
                    raise RuntimeError(f"{model.__class__.__name__} вернула пустой ответ")
                return result
            except Exception as e:
                error_msg = f"Ошибка при использовании модели {model.__class__.__name__}: {e}"
                logger.error(error_msg)
                errors.append(error_msg)
        
        # Если ни одна модель не сработала
        raise Exception(f"Все модели для fallback вышли из строя. Ошибки: {errors}")


class AIAdapter:
    """Класс для получения доступа к LLM моделям разных фирм через API"""

    def __init__(self, secrets: Dict[str, str], llm_proxy: str):
        fallback_configs = config.get("FALLBACK_MODELS", []) or []

        if LLM_MODEL_TYPE == "openrouter":
            self.primary_model = self._create_single_model(
                LLM_MODEL_TYPE,
                LLM_MODEL,
                secrets,
                llm_proxy,
                fallback_models=_openrouter_fallback_ids(fallback_configs),
            )
            self.model = self.primary_model
            return

        self.primary_model = self._create_single_model(
            LLM_MODEL_TYPE,
            LLM_MODEL,
            secrets,
            llm_proxy,
        )

        if fallback_configs:
            models_list = [self.primary_model]
            for fb_conf in fallback_configs:
                if isinstance(fb_conf, str):
                    logger.warning(
                        f"Пропуск fallback '{fb_conf}': для {LLM_MODEL_TYPE} нужен объект {{type, model}}"
                    )
                    continue
                fb_type = fb_conf.get("type")
                fb_model_name = fb_conf.get("model")
                if fb_type and fb_model_name:
                    try:
                        fb_model = self._create_single_model(
                            fb_type, fb_model_name, secrets, llm_proxy
                        )
                        models_list.append(fb_model)
                    except Exception as e:
                        logger.error(
                            f"Не удалось инициализировать fallback модель {fb_type}/{fb_model_name}: {e}"
                        )
            self.model = FallbackLLM(models_list) if len(models_list) > 1 else self.primary_model
        else:
            self.model = self.primary_model

    def _create_single_model(
        self,
        model_type: str,
        model_name: str,
        secrets: Dict[str, str],
        llm_proxy: str,
        fallback_models: Optional[List[str]] = None,
    ) -> AIModel:
        logger.info(f"Инициализация модели: {model_type} ({model_name})")

        if model_type == "openrouter":
            api_key = secrets.get("openrouter_api_key") or secrets.get("llm_api_key")
            if not api_key:
                raise ValueError("No OpenRouter API key found")
            return OpenRouterModel(api_key, model_name, llm_proxy, fallback_models)

        if model_type == "gemini":
            api_key = secrets.get("gemini_api_key") or secrets.get("llm_api_key")
            if not api_key:
                raise ValueError("No Gemini API key found")
            return GeminiModel(api_key, model_name, llm_proxy)

        if model_type == "openai":
            api_key = secrets.get("openai_api_key") or secrets.get("llm_api_key")
            if not api_key:
                raise ValueError("No OpenAI API key found")
            return OpenAIModel(api_key, model_name, llm_proxy)

        if model_type == "cohere":
            api_key = secrets.get("cohere_api_key") or secrets.get("llm_api_key")
            if not api_key:
                raise ValueError("No Cohere API key found")
            return CohereModel(api_key, model_name)

        raise ValueError(f"Неподдерживаемый тип модели: {model_type}")

    def invoke(self, prompt: str) -> str:
        return self.model.invoke(prompt)


class LLMLogger:
    """Класс для логирования всех событий, происходящих при работе с LLM"""

    def __init__(self, llm: Any): # Changed type hint to Any to accept FallbackLLM
        self.llm = llm
        logger.info(f"LLMLogger успешно инициализирован, используем LLM: {llm}")

    @staticmethod
    def log_request(prompts, parsed_reply: Dict[str, Dict]) -> None:
        """Метод для логирования всех операций с LLM"""
        logger.info("Начинается выполнение метода log_request")
        logger.info("Получены промпты")
        logger.info("Получен распарсенный ответ")

        try:
            calls_log = os.path.join(Path("data_folder/output"), "llm_api_calls.yaml")
            logger.debug(f"Определен путь к лог-файлу: {calls_log}")
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Ошибка при определении пути к лог-файлу: {tb_str}")
            raise

        if isinstance(prompts, StringPromptValue):
            logger.debug("Промпты имеют тип StringPromptValue")
            prompts = prompts.text
        elif isinstance(prompts, Dict):
            logger.debug("Промпты имеют тип Dict")
            try:
                prompts = {
                    f"prompt_{i + 1}": prompt.content for i, prompt in enumerate(prompts.messages)
                }
                logger.debug("Промпты преобразованы в словарь")
            except Exception:
                tb_str = traceback.format_exc()
                logger.error(f"Ошибка при преобразовании промптов в словарь: {tb_str}")
                raise
        else:
            logger.debug("Неизвестный тип промптов, попытка преобразования по умолчанию")
            try:
                prompts = {
                    f"prompt_{i + 1}": prompt.content for i, prompt in enumerate(prompts.messages)
                }
                logger.debug("Промпты преобразованы в словарь с использованием метода по умолчанию")
            except Exception:
                tb_str = traceback.format_exc()
                logger.error(
                    f"Ошибка при преобразовании промптов с использованием метода по умолчанию: {tb_str}"
                )
                raise

        try:
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.debug(f"Текущее время: {current_time}")
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Ошибка при получении текущего времени: {tb_str}")
            raise

        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        try:
            token_usage = parsed_reply.get("usage_metadata")
            if token_usage:
                output_tokens = token_usage.get("output_tokens", 0)
                input_tokens = token_usage.get("input_tokens", 0)
                total_tokens = token_usage.get("total_tokens", 0)
                logger.info(
                    f"Использование токенов - Input: {input_tokens}, Output: {output_tokens}, Всего: {total_tokens}"
                )
        except KeyError as e:
            logger.error(f"Ошибка ключа в структуре parsed_reply: {str(e)}")
            raise

        try:
            model_name = parsed_reply["response_metadata"].get("model_name") or LLM_MODEL
            logger.info(f"Название модели: {model_name}")
        except KeyError as e:
            logger.error(f"Ошибка ключа в response_metadata: {str(e)}")
            raise

        try:
            prices = _prices_for_model(model_name)
            price_per_input_token = prices["price_per_input_token"]
            price_per_output_token = prices["price_per_output_token"]
            total_cost = (input_tokens * price_per_input_token) + (
                output_tokens * price_per_output_token
            )
            logger.info(f"Общая стоимость рассчитана: {total_cost}")
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Ошибка при расчете общей стоимости: {tb_str}")
            raise

        # загружаем лог из лог-файла
        try:
            with open(calls_log, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
        except FileNotFoundError:
            pass
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Ошибка при загрузке лога из файла: {tb_str}")
            raise


class LoggerChatModel:
    """
    Класс для взаимодействия с языковой моделью (LLM) и логирования всех операций.
    """



    def __init__(self, llm: AIAdapter):
        self.llm = llm
        logger.info(f"LoggerChatModel успешно инициализирован")

    def __call__(self, messages: List[Dict[str, str]]) -> str:
        """
        Выполняем вызов LLM, обрабатываем ответ и логируем весь процесс.
        """
        retries = 0
        while True:
            try:
                logger.info("Попытка вызова LLM")

                reply = self.llm.invoke(messages)
                logger.debug(f"Ответ от LLM: {reply}")

                if reply is None:
                    raise RuntimeError("LLM вернула пустой ответ")

                parsed_reply = self.parse_llmresult(reply)
                logger.info(f"Успешно распарсили результат работы LLM: {parsed_reply}")

                LLMLogger.log_request(prompts=messages, parsed_reply=parsed_reply)

                return reply

            except httpx.HTTPStatusError as e:
                retries += 1
                logger.error(f"Произошла ошибка HTTPStatusError: {str(e)}")
                if retries >= MAX_HTTP_RETRIES:
                    logger.error(f"Исчерпаны {MAX_HTTP_RETRIES} HTTP-ретрая, пробрасываем ошибку")
                    raise
                if e.response.status_code == 429:
                    retry_after = e.response.headers.get("retry-after")
                    retry_after_ms = e.response.headers.get("retry-after-ms")

                    if retry_after:
                        wait_time = int(retry_after)
                        logger.warning(
                            f"Превышен лимит запросов. Ожидание {wait_time} секунд перед повторной попыткой (из заголовка 'retry-after')..."
                        )
                        time.sleep(wait_time)
                    elif retry_after_ms:
                        wait_time = int(retry_after_ms) / 1000.0
                        logger.warning(
                            f"Превышен лимит запросов. Ожидание {wait_time} секунд перед повторной попыткой (из заголовка 'retry-after-ms')..."
                        )
                        time.sleep(wait_time)
                    else:
                        wait_time = 30
                        logger.warning(
                            f"Заголовок 'retry-after' не найден. Ожидание {wait_time} секунд перед повторной попыткой (по умолчанию)..."
                        )
                        time.sleep(wait_time)
                else:
                    logger.error(
                        f"Произошла ошибка HTTP со статусом: {e.response.status_code}, ожидание 30 секунд перед повторной попыткой"
                    )
                    time.sleep(30)
            except Exception as e:
                logger.error(f"Unexpected error in LoggerChatModel: {e}")
                raise e

    def parse_llmresult(self, llmresult: AIMessage) -> Dict[str, Dict]:
        """Парсим результат работы LLM"""
        logger.info("Парсинг результата LLM")

        try:
            if llmresult is None:
                logger.warning("LLM returned None, returning empty response")
                return {"content": "", "response_metadata": {}, "id": "", "usage_metadata": {}}
            if hasattr(llmresult, "usage_metadata") and llmresult.usage_metadata is not None:
                content = llmresult.content
                response_metadata = llmresult.response_metadata
                id_ = llmresult.id
                usage_metadata = llmresult.usage_metadata

                parsed_result = {
                    "content": content,
                    "response_metadata": {
                        "model_name": response_metadata.get("model_name")
                        or response_metadata.get("model", ""),
                        "system_fingerprint": response_metadata.get("system_fingerprint", ""),
                        "finish_reason": response_metadata.get("finish_reason", ""),
                        "logprobs": response_metadata.get("logprobs", None),
                    },
                    "id": id_,
                    "usage_metadata": {
                        "input_tokens": usage_metadata.get("input_tokens", 0),
                        "output_tokens": usage_metadata.get("output_tokens", 0),
                        "total_tokens": usage_metadata.get("total_tokens", 0),
                    },
                }
            else:
                try:
                    content = llmresult.content
                    response_metadata = llmresult.response_metadata
                    id_ = llmresult.id

                    # Handle the case where token_usage might not be in response_metadata
                    if "token_usage" in response_metadata:
                        token_usage = response_metadata["token_usage"]
                        input_tokens = token_usage.prompt_tokens
                        output_tokens = token_usage.completion_tokens
                        total_tokens = token_usage.total_tokens
                    else:
                        # Default values when token_usage is not available
                        input_tokens = 0
                        output_tokens = 0
                        total_tokens = 0

                    parsed_result = {
                        "content": content,
                        "response_metadata": {
                            "model_name": response_metadata.get("model", ""),
                            "finish_reason": response_metadata.get("finish_reason", ""),
                        },
                        "id": id_,
                        "usage_metadata": {
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "total_tokens": total_tokens,
                        },
                    }
                except Exception:
                    tb_str = traceback.format_exc()
                    logger.error(f"Ошибка при обработке результата без usage_metadata: {tb_str}")
                    # Create a minimal parsed result with defaults
                    parsed_result = {
                        "content": llmresult.content if hasattr(llmresult, "content") else "",
                        "response_metadata": {"model_name": "unknown", "finish_reason": "unknown"},
                        "id": llmresult.id if hasattr(llmresult, "id") else "",
                        "usage_metadata": {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 0,
                        },
                    }
            return parsed_result

        except KeyError as e:
            logger.error(f"Ошибка KeyError при парсинге результата LLM: отсутствует ключ {str(e)}")
            raise

        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Непредвиденная ошибка при парсинге результата LLM: {tb_str}")
            raise


class GPTAnswerer:
    """
    Класс для обработки вопросов по резюме и формированию ответов на них с использованием LLM.
    """

    def __init__(self, secrets: Dict[str, str], llm_proxy: str):
        self.job = None
        self.ai_adapter = AIAdapter(secrets, llm_proxy)
        self.llm_cheap = LoggerChatModel(self.ai_adapter)
        self.chains = {
            "job_is_interesting": self._create_pydantic_chain(
                prompts.job_is_interesting, JobIsInteresting
            ),
            "resume_is_interesting": self._create_pydantic_chain(
                prompts.resume_is_interesting, ResumeIsInteresting
            ),
            "extract_skills_from_vacancy": self._create_chain(
                prompts.extract_skills_from_vacancy_template
            ),
            "text_question": self._create_chain(prompts.text_question_answer_template),
            "one_answer_from_options": self._create_chain(prompts.options_template),
            "many_answers_from_options": self._create_chain(prompts.many_options_template),
            "summarize_job_description": self._create_chain(prompts.summarize_prompt_template),
            "resume_improvement_recommendations": self._create_chain(prompts.resume_improve),
            "parse_contacts": self._create_pydantic_chain(
                prompts.parse_contacts_template, ContactInfo
            ),
        }

    @staticmethod
    def find_best_match(text: str, options: list[str]) -> str:
        """
        Находим наилучшее совпадение строки с одним из вариантов
        и возвращаем лучший вариант из списка.
        """
        logger.info(f"Поиск лучшего совпадения для текста: '{text}' в вариантах: {options}")
        distances = [(option, distance(text.lower(), option.lower())) for option in options]
        best_option = min(distances, key=lambda x: x[1])[0]
        logger.info(f"Лучшее совпадение найдено: {best_option}")
        return best_option

    @staticmethod
    def _remove_placeholders(text: str) -> str:
        """Удаляем все заполнители 'PLACEHOLDER' из текста."""
        logger.debug("Удаление заполнителей из текста")
        return text.replace("PLACEHOLDER", "").strip()

    @staticmethod
    def _preprocess_template_string(template: str) -> str:
        """Преобразуем строку шаблона для использования в промптах."""
        logger.debug("Предобработка строки шаблона")
        return textwrap.dedent(template)

    def set_resume(self, resume: Dict[str, Any], resume_readable: str) -> None:
        """Добавляем резюме для анализа."""
        logger.info(f"Добавляем резюме: {resume}")
        self.resume = resume
        if (
            "salary_expectations" in self.resume
            and "currency" in self.resume["salary_expectations"]
        ):
            if self.resume["salary_expectations"]["currency"] == "RUR":
                self.resume["salary_expectations"]["currency"] = "руб"
        self.resume_readable = resume_readable

    def set_job(self, job) -> None:
        """Добавляем описание вакансии."""
        logger.info(f"Добавляем описание вакансии: {job}")
        self.job_description = job
        self.job_readable = transform_vacancy_data(job)

    def set_search_parameters(self, parameters: dict) -> None:
        """Устанавливаем параметры поиска вакансий."""
        logger.info(f"Устанавливаем параметры поиска вакансий: {parameters}")
        self.search_parameters = transform_search_config_data(parameters)

    def extract_skills_from_vacancy(self, job_description: str) -> list[str]:
        """Извлекаем список навыков из описания вакансии"""
        chain = self.chains["extract_skills_from_vacancy"]
        output = chain.invoke({"job_description": job_description})
        output = output.replace("[", "").replace("]", "")
        output = output.replace("'", "").replace('"', "")
        output = output.split(",")
        output = [skill.strip().lower() for skill in output if skill.strip()]
        logger.info(f"Извлечены навыки из вакансии: {output}")
        return output

    def summarize_job_description(self, text: str) -> str:
        """Создаем краткое описание вакансии"""
        logger.info(f"Создаем краткое описание вакансии: '{text}'")
        chain = self.chains["summarize_job_description"]
        output = chain.invoke({"text": text})
        logger.info(f"Сгенерировано краткое описание: {output}")
        return output

    def _create_chain(self, template: str) -> ChatPromptTemplate:
        """Создаем цепочку обработки для конкретного раздела резюме."""
        # logger.debug(f"Создание цепочки с шаблоном: '{template}'")
        prompt = ChatPromptTemplate.from_template(template)
        return prompt | self.llm_cheap | StrOutputParser()

    def _create_pydantic_chain(
        self, template: str, pydantic_object: BaseModel
    ) -> Tuple[ChatPromptTemplate, PydanticOutputParser]:
        """Создаем цепочку обработки для конкретного раздела резюме с использованием Pydantic модели."""
        parser = PydanticOutputParser(pydantic_object=pydantic_object)
        template = self._preprocess_template_string(template)
        prompt = ChatPromptTemplate.from_template(template)
        return prompt | self.llm_cheap | parser, parser

    def answer_question_textual_wide_range(self, question: str) -> str:
        """Определить тему заданного вопроса и ответить на него"""
        logger.info(f"Отвечаем на текстовый вопрос: '{question}'")
        sex = self.resume["personal_information"].get("sex")
        current_date = datetime.now().date().strftime("%Y-%m-%d")

        chain = self.chains["text_question"]
        output = chain.invoke(
            {
                "resume": self.resume_readable,
                "question": question,
                "sex": sex,
                "current_date": current_date,
            }
        )
        logger.info(f"Ответ на вопрос: {output}")
        return output

    def select_one_answer_from_options(self, question: str, options: list[str]) -> str:
        """
        Спрашиваем у LLM ответ на вопрос с несколькими
        вариантами ответа. Должен вернуть только один.
        """
        logger.info(f"Отвечаем на вопрос c выбором одного ответа: {question}")
        chain = self.chains["one_answer_from_options"]
        output_str = chain.invoke(
            {"resume": self.resume_readable, "question": question, "options": options}
        )
        logger.info(f"Ответ от LLM: {output_str}")
        best_option = self.find_best_match(output_str, options)
        logger.info(f"Лучший вариант ответа найден: {best_option}")
        return best_option

    def select_many_answers_from_options(self, question: str, options: list[str]) -> List[str]:
        """
        Спрашиваем у LLM ответ на вопрос с одним или несколькими
        вариантами ответа. Может вернуть больше одного.
        """
        logger.info(f"Отвечаем на вопрос c выбором одного или нескольких ответа: {question}")
        chain = self.chains["many_answers_from_options"]
        output_str = chain.invoke(
            {"resume": self.resume_readable, "question": question, "options": options}
        )
        logger.info(f"Ответ от LLM: {output_str}")
        # на случай если LLM вернет python-like список
        output_str = output_str.replace("[", "").replace("]", "")
        output_str = output_str.replace("'", "").replace("'", "")
        outputs = output_str.split(";")
        best_options = []
        for output in outputs:
            best_option = self.find_best_match(output, options)
            best_options.append(best_option)
        logger.info(f"Лучшие варианты ответа: {best_options}")
        return best_options

    def job_is_interesting(self) -> Dict[str, Any]:
        """
        Спрашиваем у LLM, может ли быть интересна
        данная вакансия с учетом нашего резюме, навыков и интересов
        """
        logger.info("Проверяем, насколько вакансия может быть интересна.")
        chain, parser = self.chains["job_is_interesting"]
        try:
            output = chain.invoke(
                {
                    "resume": self.resume_readable,
                    "job_description": self.job_readable,
                    "search_parameters": self.search_parameters,
                    "format_instructions": parser.get_format_instructions(),
                }
            )
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Ошибка при вызове LLM\n{tb_str}")
            return {"score": 0, "reasoning": "Ошибка при вызове LLM"}
        logger.info(f"Оценка 'интересности' вакансии: {output.score}")
        logger.info(f"Объяснение оценки 'интересности': '{output.reasoning}'")
        return output.model_dump()

    def resume_is_interesting(self) -> Dict[str, Any]:
        """
        Спрашиваем у LLM, наскольо может быть интересно
        данное резюме с точки зрения его улучшения
        """
        chain, parser = self.chains["resume_is_interesting"]
        try:
            output = chain.invoke(
                {
                    "resume": self.resume_readable,
                    "format_instructions": parser.get_format_instructions(),
                }
            )
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Ошибка при вызове LLM\n{tb_str}")
            return {
                "demand_score": 0,
                "resume_score": 0,
                "solvency_score": 0,
                "reasoning": "Ошибка при вызове LLM",
            }
        logger.info(f"Ответ LLM: '{output}'")
        return output.model_dump()

    def write_cover_letter(self) -> str:
        """
        Создаем сопроводительное письмо на основе резюме и описания вакансии.
        """
        # в зависимости от доступности того или иного контакта задаем его в промпте
        sex = self.resume["personal_information"].get("sex")
        telegram = self.resume["personal_information"].get("telegram", "")
        whatsapp = self.resume["personal_information"].get("whatsapp", "")
        phone = self.resume["personal_information"].get("phone", "")
        email = self.resume["personal_information"].get("email", "")
        preferred_contact = self.resume["personal_information"].get("preferred_contact")
        additional_prompt = "- В качестве контакта укажи "
        invoke_dict = {
            "resume": self.resume_readable,
            "job_description": self.job_readable,
            "sex": sex,
        }
        if telegram:
            additional_prompt += f"Telegram: {telegram}"
            invoke_dict["telegram"] = telegram
        elif whatsapp:
            additional_prompt += f"Whatsapp: {whatsapp}"
            invoke_dict["whatsapp"] = whatsapp
        elif preferred_contact == "phone":
            additional_prompt += f"Телефон: {phone}"
            invoke_dict["phone"] = phone
        elif preferred_contact == "email":
            additional_prompt += f"Email: {email}"
            invoke_dict["email"] = email
        elif phone:
            additional_prompt += f"Телефон: {phone}"
            invoke_dict["phone"] = phone
        else:
            additional_prompt += f"Email: {email}"
            invoke_dict["email"] = email

        chain = self._create_chain(prompts.coverletter_template + additional_prompt)
        output = chain.invoke(invoke_dict)
        logger.info(f"Сопроводительное письмо сгенерировано: '{output}'")
        return output

    def resume_improvement_recommendations(self) -> str:
        """
        Пишем рекомендации по улучшению резюме
        """
        logger.info("Пишем рекомендации по улучшению резюме")
        chain = self.chains["resume_improvement_recommendations"]
        output = chain.invoke(
            {
                "resume": self.resume_readable,
            }
        )
        logger.info(f"Рекомендации по улучшению резюме сгенерированы: '{output}'")
        return output

    def parse_contacts(self, resume_info: str) -> Dict[str, str]:
        """
        Парсим контакты из резюме и возвращаем их в виде словаря.
        """
        logger.info("Парсим контакты из резюме")
        chain, parser = self.chains["parse_contacts"]
        output = chain.invoke(
            {
                "resume": resume_info,
                "format_instructions": parser.get_format_instructions(),
            }
        )
        logger.info(f"Ответ LLM: '{output}'")
        return output.model_dump()


if __name__ == "__main__":
    from langchain_core.prompts import ChatPromptTemplate
    from src.constants import SECRETS_FILE

    secrets = load_yaml_file(SECRETS_FILE)
    llm_api_key = secrets["llm_api_key"]
    llm_proxy = secrets["llm_proxy"]

    try:
        openai_model = OpenAIModel(api_key=llm_api_key, llm_model=LLM_MODEL, llm_proxy=llm_proxy)

        # Create a simple test prompt
        template = ChatPromptTemplate.from_messages(
            [("user", "Hello! Respond with 'Test passed' if you receive this.")]
        )

        # Invoke generates a PromptValue
        prompt_value = template.invoke({})

        print("Invoking OpenAIModel...")
        response = openai_model.invoke(prompt_value)

        print(f"Response content: {response.content}")

    except Exception as e:
        print(f"Test failed with error: {e}")
        traceback.print_exc()

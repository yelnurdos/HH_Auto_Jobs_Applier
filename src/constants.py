# Личные данные-пустышки для анонимизации (мужской пол)
DUMMY_PERSONAL_INFO_MALE = {
    "first_name": "Yelnur",
    "first_name_2": "Елнур",
    "first_name_3": "Елнұр",
    "middle_name": "Мейрбекұлы",
    "last_name": "Dossymov",
    "last_name_2": "Досымов",
    "birthday": "14.12.2002",
    "phone": "+7 (707) 477 9686",
    "email": "yelnurdos@gmail.com",
    "telegram": "https://t.me/yelnurdos",
    # "whatsapp": "https://wa.me/aristaniy93",
    # "other_site": "https://www.aristaniy93.ru",
    # "habr_career": "https://career.habr.ru/aristaniy93",
    # "linkedin": "https://linkedin.com/in/aristaniy-zvyagoltsev-f3e57c712",
    "github": "https://github.com/yelnurdo",
    # "telegram_2": "@aristaniy93",
    # "telegram_3": "aristaniy93",
}

# Пути к файлам логов и настроек
SECRETS_FILE = "data_folder/secrets/secrets.yaml"
APP_CONFIG_FILE = "data_folder/app_config/app_config.yaml"
SEARCH_CONFIG_FILE = "data_folder/search_config/search_config.yaml"
SEARCH_CONFIG_FILE_TMP = "data_folder/output/search_config_tmp.yaml"
LAST_RUN_FILE = "data_folder/output/last_run.yaml"
LOGS_DIR = "logs"
BROWSER_STORAGE_STATE = "data_folder/browser_session/hh_state.json"

# Словарь для подсчета стоимости запроса к модели
PRICE_DICT = {
    "gpt-4o": {
        "price_per_input_token": 2.5e-6,
        "price_per_output_token": 1e-5,
    },
    "openai/gpt-4o": {
        "price_per_input_token": 2.5e-6,
        "price_per_output_token": 1e-5,
    },
    "gpt-4o-mini": {
        "price_per_input_token": 1.5e-7,
        "price_per_output_token": 6e-7,
    },
    "gpt-5-nano": {
        "price_per_input_token": 5e-8,
        "price_per_output_token": 4e-7,
    },
    "gpt-5-mini": {
        "price_per_input_token": 2.5e-7,
        "price_per_output_token": 2e-6,
    },
    "gpt-5": {
        "price_per_input_token": 1.25e-6,
        "price_per_output_token": 1e-5,
    },
    "gemini-2.0-flash": {
        "price_per_input_token": 1e-7,
        "price_per_output_token": 2e-7,
    },
    "GigaChat": {  # цены рассчитаны из учета курс 100 руб за $
        "price_per_input_token": 2e-6,
        "price_per_output_token": 2e-6,
    },
    "GigaChat-Pro": {
        "price_per_input_token": 1.5e-5,
        "price_per_output_token": 1.5e-5,
    },
    "GigaChat-Max": {
        "price_per_input_token": 1.95e-5,
        "price_per_output_token": 1.95e-5,
    },
}

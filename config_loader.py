import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import PhotoTrapConfig

logger = logging.getLogger("config_loader")

# Значения по умолчанию (используются если нет ни в БД, ни в файле)
DEFAULTS = {
    "NeedCalibration": "false",
    "DownloadPath": "./downloads",
    "ConnectionStrings": json.dumps({
        "DefaultConnection": "Host=localhost;Database=phototrapdb;Username=postgres;Password=12345678"
    }),
    "CamerasCount": "1",
    "WifiPassword": "12345678",
    "BleScanTimeout": "10",
    "BleCommandTimeout": "10",
    "WifiWaitAfterOpen": "25",
    "WifiConnectTimeout": "90",
    "WifiMaxRetries": "5",
    "CloseWaitSeconds": "25",
    "RetryDelay": "15",
    "MaxRetriesPerCamera": "3",
    "MaxScanRetries": "10",
    "CameraCooldown": "20",
    "CompressQuality": "12",
    "WifiDownloadRetries": "3",
    "DeleteAfterDownload": "true",
    "CompressAfterDownload": "true",
}


async def load_config(db_session: AsyncSession, config_path: str = "appsettings.json") -> dict:
    """
    Загрузка конфигурации с цепочкой fallback:
    1. БД (PhotoTrapConfig)
    2. Файл appsettings.json
    3. Значения по умолчанию (DEFAULTS)
    """
    # Шаг 1: пробуем загрузить из БД
    db_config = await _load_from_db(db_session)

    # Шаг 2: загружаем из файла
    file_config = _load_from_file(config_path)

    # Шаг 3: собираем результат — БД > файл > default
    result = {}
    for key, default_val in DEFAULTS.items():
        if key in db_config:
            result[key] = db_config[key]
        elif key in file_config:
            result[key] = file_config[key]
        else:
            result[key] = default_val

    # ConnectionStrings — специальный случай, в БД хранится JSON-строка
    if "ConnectionStrings" in result:
        try:
            result["ConnectionStrings"] = json.loads(result["ConnectionStrings"])
        except (json.JSONDecodeError, TypeError):
            pass

    # Парсим числовые значения
    int_keys = [
        "CamerasCount", "WifiWaitAfterOpen", "WifiConnectTimeout",
        "CloseWaitSeconds", "RetryDelay", "MaxRetriesPerCamera",
        "MaxScanRetries", "CameraCooldown", "CompressQuality", "WifiDownloadRetries", "WifiMaxRetries",
    ]
    float_keys = ["BleScanTimeout", "BleCommandTimeout"]

    for key in int_keys:
        try:
            result[key] = int(result[key])
        except (ValueError, TypeError):
            pass

    for key in float_keys:
        try:
            result[key] = float(result[key])
        except (ValueError, TypeError):
            pass

    # NeedCalibration — булево
    val = result.get("NeedCalibration", "false")
    result["NeedCalibration"] = str(val).lower() in ("true", "1", "yes")

    source = "БД" if db_config else ("файл" if file_config else "default")
    logger.debug("Конфигурация загружена из %s (%d параметров)", source, len(result))
    return result


async def _load_from_db(db_session: AsyncSession) -> dict:
    """Загрузка всех параметров из таблицы PhotoTrapConfig."""
    try:
        result = await db_session.execute(select(PhotoTrapConfig))
        rows = result.scalars().all()
        if not rows:
            logger.info("Таблица PhotoTrapConfig пуста")
            return {}
        config = {row.Key: row.Value for row in rows}
        logger.debug("Из БД загружено %d параметров", len(config))
        return config
    except Exception as e:
        logger.warning("Не удалось загрузить конфигурацию из БД: %s", e)
        return {}


def _load_from_file(config_path: str) -> dict:
    """Загрузка параметров из appsettings.json."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Преобразуем все значения в строки для единообразия
        flat = {}
        for k, v in data.items():
            if isinstance(v, dict):
                flat[k] = json.dumps(v)
            else:
                flat[k] = str(v)
        logger.debug("Из файла загружено %d параметров", len(flat))
        return flat
    except FileNotFoundError:
        logger.info("Файл %s не найден", config_path)
        return {}
    except Exception as e:
        logger.warning("Ошибка чтения %s: %s", config_path, e)
        return {}


async def save_config_to_db(db_session: AsyncSession, config: dict, descriptions: dict = None):
    """
    Сохранение параметров в таблицу PhotoTrapConfig.
    Если параметр существует — обновляет, если нет — создаёт.
    """
    descriptions = descriptions or {}
    saved = 0
    for key, value in config.items():
        str_value = str(value) if not isinstance(value, str) else value
        result = await db_session.execute(
            select(PhotoTrapConfig).where(PhotoTrapConfig.Key == key)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.Value = str_value
            if key in descriptions:
                existing.Description = descriptions[key]
        else:
            db_session.add(PhotoTrapConfig(
                Key=key,
                Value=str_value,
                Description=descriptions.get(key),
            ))
        saved += 1
    await db_session.commit()
    logger.info("В БД сохранено %d параметров", saved)

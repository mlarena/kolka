"""
Скрипт сжатия изображений через ffmpeg.

Использование:
    python compress_images.py <путь_к_папке>
    python compress_images.py "E:\TestFoto\images\1\20260716_144738"
    python compress_images.py "/opt/bluetooth_scanner/downloads/1/20260716_144738"

Параметры сжатия берутся из appsettings.json (CompressQuality).
"""

import sys
import os
import json
import subprocess
import logging
from pathlib import Path
from datetime import datetime

# Настройка логирования
logger = logging.getLogger(__name__)


def load_config():
    """Загрузка конфигурации"""
    config_path = Path(__file__).parent / "appsettings.json"
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def compress_images(folder_path: str, quality: int = 12):
    """
    Сжатие JPG изображений в указанной папке.
    
    Args:
        folder_path: Путь к папке с изображениями
        quality: Качество сжатия ffmpeg -q:v (1-31, чем меньше тем лучше)
    
    Returns:
        tuple: (сжато, ошибок)
    """
    folder = Path(folder_path)
    
    if not folder.exists():
        logger.error(f"Папка не существует: {folder}")
        return 0, 0
    
    # Ищем все JPG файлы (пропускаем уже сжатые)
    # Используем set чтобы исключить дубликаты (Windows регистронезависимая FS)
    all_jpg = set()
    for f in folder.iterdir():
        if f.is_file() and f.suffix.lower() == '.jpg' and '_cmp_' not in f.name:
            all_jpg.add(f)
    jpg_files = sorted(all_jpg)
    
    if not jpg_files:
        logger.info(f"JPG файлы для сжатия не найдены в {folder}")
        return 0, 0
    
    logger.info(f"Найдено {len(jpg_files)} JPG файлов для сжатия")
    logger.info(f"Качество сжатия: -q:v {quality}")
    
    compressed = 0
    errors = 0
    
    for jpg_file in jpg_files:
        # Формируем имя выходного файла
        stem = jpg_file.stem
        suffix = jpg_file.suffix
        output_name = f"{stem}_cmp_{quality}{suffix}"
        output_path = folder / output_name
        
        # Команда ffmpeg
        cmd = [
            "ffmpeg",
            "-i", str(jpg_file),
            "-q:v", str(quality),
            "-y",  # Перезапись без вопросов
            str(output_path)
        ]
        
        try:
            logger.info(f"Сжатие: {jpg_file.name} -> {output_name}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            
            if result.returncode == 0 and output_path.exists():
                # Удаляем оригинальный файл
                original_size = jpg_file.stat().st_size
                compressed_size = output_path.stat().st_size
                ratio = (1 - compressed_size / original_size) * 100 if original_size > 0 else 0
                
                jpg_file.unlink()  # Удаление оригинала
                logger.info(f"OK: {jpg_file.name} ({original_size} байт) -> {output_name} "
                           f"({compressed_size} байт, -{ratio:.1f}%)")
                compressed += 1
            else:
                logger.error(f"Ошибка ffmpeg для {jpg_file.name}: {result.stderr[:200]}")
                errors += 1
                # Удаляем битый выходной файл если есть
                if output_path.exists():
                    output_path.unlink()
                    
        except subprocess.TimeoutExpired:
            logger.error(f"Таймаут ffmpeg для {jpg_file.name}")
            errors += 1
        except Exception as e:
            logger.error(f"Ошибка при сжатии {jpg_file.name}: {e}")
            errors += 1
    
    return compressed, errors


def main():
    """Главная функция"""
    if len(sys.argv) < 2:
        print("Использование: python compress_images.py <путь_к_папке>")
        print("Пример Windows: python compress_images.py \"E:\\TestFoto\\images\\1\\20260716_144738\"")
        print("Пример Linux: python compress_images.py /opt/bluetooth_scanner/downloads/1/20260716_144738")
        sys.exit(1)
    
    folder_path = sys.argv[1]
    
    # Загружаем конфигурацию
    config = load_config()
    quality = int(config.get('CompressQuality', 12))
    
    logger.info(f"Сжатие изображений в папке: {folder_path}")
    logger.info(f"Качество: {quality}")
    
    compressed, errors = compress_images(folder_path, quality)
    
    logger.info(f"Результат: сжато={compressed}, ошибок={errors}")
    
    if errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    main()

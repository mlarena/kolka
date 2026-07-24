# Сервисы Kolka — Инструкция

## Сервисы

| Сервис | Тип | Назначение | Интервал |
|--------|-----|-----------|----------|
| `kolka_take_photo` | Долгоживущий сервис | Снимки по расписанию | Каждые N мин (из конфига) |
| `kolka_download` | Timer + oneshot | Скачивание фото | Каждый час |

## Установка

```bash
cd /scanner/scripts

# Калибровка (ОБЯЗАТЕЛЬНО перед первым запуском)
cd /scanner && source venv/bin/activate
python3 calibration.py

# Установка сервисов
sudo bash scripts/install_take_photo.sh
sudo bash scripts/install_download.sh
```

## Управление kolka_take_photo (сервис)

```bash
# Статус
sudo systemctl status kolka_take_photo

# Запуск / Остановка / Перезапуск
sudo systemctl start kolka_take_photo
sudo systemctl stop kolka_take_photo
sudo systemctl restart kolka_take_photo

# Автозапуск
sudo systemctl enable kolka_take_photo
sudo systemctl disable kolka_take_photo
```

## Управление kolka_download (timer)

```bash
# Статус таймера
sudo systemctl status kolka_download.timer
sudo systemctl list-timers kolka_download.timer

# Ручной запуск скачивания
sudo systemctl start kolka_download

# Остановка таймера
sudo systemctl stop kolka_download.timer
sudo systemctl disable kolka_download.timer

# Включение таймера
sudo systemctl enable kolka_download.timer
sudo systemctl start kolka_download.timer
```

## Логи

### Через systemd journal

```bash
# Take Photo — последние 100 строк
sudo journalctl -u kolka_take_photo -n 100

# Take Photo — в реальном времени
sudo journalctl -u kolka_take_photo -f

# Download — последние запуски
sudo journalctl -u kolka_download --since today

# Только ошибки
sudo journalctl -u kolka_take_photo -p err
```

### Через файлы

```bash
# Take Photo
tail -f /opt/kolka_service_take_photo/logs/take_photo_log_*.log

# Download
tail -f /opt/kolka_service_download/logs/download_log_*.log
```

### Ротация логов

Логи ротируются автоматически через logrotate:
- Частота: ежедневно
- Хранение: 14 дней
- Сжатие: gzip (с задержкой 1 день)

## Удаление

```bash
cd /scanner/scripts

sudo bash uninstall_take_photo.sh
sudo bash uninstall_download.sh
```

## Конфигурация

Конфигурация загружается с цепочкой приоритетов:
1. **БД** (таблица `PhotoTrapConfig`) — наивысший приоритет
2. **Файл** `appsettings.json` — средний
3. **Дефолты** в коде — наименьший

Изменить конфиг можно через БД:
```sql
-- Интервал снимков (15 мин)
INSERT INTO "PhotoTrapConfig" ("Key", "Value", "Description")
VALUES ('SnapshotIntervalMinutes', '15', 'Интервал снимков в минутах')
ON CONFLICT ("Key") DO UPDATE SET "Value" = EXCLUDED."Value";

-- Количество камер
INSERT INTO "PhotoTrapConfig" ("Key", "Value", "Description")
VALUES ('CamerasCount', '2', 'Количество фотоловушек')
ON CONFLICT ("Key") DO UPDATE SET "Value" = EXCLUDED."Value";
```

## Калибровка

Калибровка выполняется **вручную** перед первым запуском:

```bash
cd /scanner && source venv/bin/activate
python3 calibration.py
```

Проверка что камеры настроены:
```sql
SELECT "Id", "Name", "MacAddress", "WifiSSID" FROM "PhotoTrap";
SELECT "Value" FROM "PhotoTrapConfig" WHERE "Key" = 'CamerasCount';
```

## Требования

- Python 3.10+
- PostgreSQL (asyncpg)
- Bluetooth (bluez, libbluetooth-dev)
- NetworkManager (nmcli)
- ffmpeg (для сжатия изображений)

## Структура файлов

```
/opt/kolka_service_take_photo/
├── kolka_take_photo.py    # Основной скрипт
├── models.py              # Модели БД
├── config_loader.py       # Загрузка конфига
├── compress_images.py     # Сжатие изображений
├── appsettings.json       # Конфигурация
├── requirements.txt       # Зависимости
├── venv/                  # Виртуальное окружение
└── logs/                  # Лог-файлы

/opt/kolka_service_download/
├── kolka_download.py      # Основной скрипт
├── models.py              # Модели БД
├── config_loader.py       # Загрузка конфига
├── compress_images.py     # Сжатие изображений
├── appsettings.json       # Конфигурация
├── requirements.txt       # Зависимости
├── service.lock           # Файл-блокировка (создаётся при запуске)
├── venv/                  # Виртуальное окружение
└── logs/                  # Лог-файлы
```

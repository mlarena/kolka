# Сервисы Kolka — Инструкция

## Сервисы

| Сервис | Назначение | Рабочая папка |
|--------|-----------|---------------|
| `kolka_take_photo` | Снимки по расписанию | `/opt/kolka_service_take_photo` |
| `kolka_download` | Скачивание фото | `/opt/kolka_service_download` |

## Установка

```bash
cd /scanner/scripts

# Установка обоих сервисов
sudo bash install_take_photo.sh
sudo bash install_download.sh

# Или по отдельности
sudo bash install_take_photo.sh
sudo bash install_download.sh
```

## Управление

### Запуск

```bash
sudo systemctl start kolka_take_photo
sudo systemctl start kolka_download
```

### Остановка

```bash
sudo systemctl stop kolka_take_photo
sudo systemctl stop kolka_download
```

### Перезапуск

```bash
sudo systemctl restart kolka_take_photo
sudo systemctl restart kolka_download
```

### Автозапуск при загрузке

```bash
sudo systemctl enable kolka_take_photo
sudo systemctl enable kolka_download
```

### Отключение автозапуска

```bash
sudo systemctl disable kolka_take_photo
sudo systemctl disable kolka_download
```

## Просмотр статуса

```bash
sudo systemctl status kolka_take_photo
sudo systemctl status kolka_download
```

## Логи

### Через systemd journal (рекомендуется)

```bash
# Последние 100 строк
sudo journalctl -u kolka_take_photo -n 100

# В реальном времени
sudo journalctl -u kolka_take_photo -f

# За сегодня
sudo journalctl -u kolka_take_photo --since today

# За конкретный период
sudo journalctl -u kolka_take_photo --since "2026-07-24 05:00" --until "2026-07-24 06:00"

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

Настройки: `/etc/logrotate.d/kolka_take_photo`, `/etc/logrotate.d/kolka_download`

## Удаление

```bash
cd /scanner/scripts

sudo bash uninstall_take_photo.sh
sudo bash uninstall_download.sh
```

## Структура файлов сервиса

```
/opt/kolka_service_take_photo/
├── kolka_take_photo.py    # Основной скрипт
├── models.py              # Модели БД
├── config_loader.py       # Загрузка конфига
├── calibration.py         # Калибровка
├── compress_images.py     # Сжатие изображений
├── appsettings.json       # Конфигурация
├── requirements.txt       # Зависимости
├── venv/                  # Виртуальное окружение
└── logs/                  # Лог-файлы
    └── take_photo_log_*.log
```

## Конфигурация

Конфигурация загружается с цепочкой приоритетов:
1. **БД** (таблица `PhotoTrapConfig`) — наивысший приоритет
2. **Файл** `appsettings.json` — средний
3. **Дефолты** в коде — наименьший

Изменить конфиг можно через БД:
```sql
INSERT INTO "PhotoTrapConfig" ("Key", "Value", "Description")
VALUES ('SnapshotIntervalMinutes', '15', 'Интервал снимков в минутах')
ON CONFLICT ("Key") DO UPDATE SET "Value" = EXCLUDED."Value";
```

## Требования

- Python 3.10+
- PostgreSQL (asyncpg)
- Bluetooth (bluez, libbluetooth-dev)
- NetworkManager (nmcli)
- ffmpeg (для сжатия изображений)

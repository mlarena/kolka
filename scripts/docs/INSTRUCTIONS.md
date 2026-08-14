# Kolka Snapshot Download — Установка и управление

## Установка

### 1. Калибровка (ОБЯЗАТЕЛЬНО перед первым запуском)

```bash
cd /scanner && source venv/bin/activate
python3 calibration.py
```

Калибровка:
- Сканирует BLE-эфир и находит фотоловушки (GCBT40)
- Записывает Name + MacAddress в таблицу `PhotoTrap`
- Привязывает Wi-Fi SSID (`CAM_*`) к каждой камере
- Проверяет, не настроены ли уже все камеры (если да — пропускает)

### 2. Установка сервиса

```bash
sudo bash /scanner/scripts/install_snapshot_download.sh
```

Скрипт:
- Копирует файлы в `/opt/kolka_service_snapshot_download/`
- Создаёт виртуальное окружение и устанавливает зависимости
- Создаёт systemd service (oneshot) и timer
- Настраивает ротацию логов (14 дней, gzip)
- Активирует и запускает таймер

### 3. Проверка установки

```bash
# Статус таймера
sudo systemctl status kolka_snapshot_download.timer

# Список таймеров
sudo systemctl list-timers kolka_snapshot_download.timer

# Проверка камер в БД
psql -U postgres -d phototrapdb -c 'SELECT "Id", "Name", "MacAddress", "WifiSSID" FROM "PhotoTrap";'
```

## Управление

### Таймер

```bash
# Статус
sudo systemctl status kolka_snapshot_download.timer
sudo systemctl list-timers kolka_snapshot_download.timer

# Ручной запуск (не дожидаясь таймера)
sudo systemctl start kolka_snapshot_download

# Остановка таймера
sudo systemctl stop kolka_snapshot_download.timer
sudo systemctl disable kolka_snapshot_download.timer

# Включение таймера
sudo systemctl enable kolka_snapshot_download.timer
sudo systemctl start kolka_snapshot_download.timer
```

### Изменение интервала запуска

Расписание задаётся в файле `/etc/systemd/system/kolka_snapshot_download.timer`.

**Текущее расписание** — каждый час:
```
OnCalendar=*-*-* *:00
```

**Примеры других расписаний:**

```bash
# Каждые 2 часа
OnCalendar=*-*-* 00,02,04,06,08,10,12,14,16,18,20,22:00

# Только с 08:00 до 15:00 каждый час
OnCalendar=*-*-* 08,09,10,11,12,13,14,15:00

# Каждые 30 минут
OnCalendar=*-*-* *:00,30

# Каждые 15 минут
OnCalendar=*-*-* *:00,15,30,45
```

После изменения расписания:
```bash
sudo systemctl daemon-reload
sudo systemctl restart kolka_snapshot_download.timer
# Проверить:
sudo systemctl list-timers kolka_snapshot_download.timer
```

## Логи

### Через systemd journal

```bash
# Последние 100 строк
sudo journalctl -u kolka_snapshot_download -n 100

# В реальном времени
sudo journalctl -u kolka_snapshot_download -f

# Только ошибки
sudo journalctl -u kolka_snapshot_download -p err

# За сегодня
sudo journalctl -u kolka_snapshot_download --since today
```

### Через файлы

```bash
tail -f /opt/kolka_service_snapshot_download/logs/snapshot_download_log_*.log
```

### Ротация логов

Логи ротируются автоматически через logrotate:
- Частота: ежедневно
- Хранение: 14 дней
- Сжатие: gzip (с задержкой 1 день)

## Удаление

```bash
sudo bash /scanner/scripts/uninstall_snapshot_download.sh
```

Скрипт:
- Останавливает сервис и таймер
- Удаляет systemd-файлы
- Удаляет конфигурацию logrotate
- Удаляет рабочую директорию `/opt/kolka_service_snapshot_download/`

## Конфигурация

Конфигурация загружается с цепочкой приоритетов:
1. **БД** (таблица `PhotoTrapConfig`) — наивысший приоритет
2. **Файл** `appsettings.json` — средний
3. **Дефолты** в коде — наименьший

### Ключевые параметры

| Параметр | Значение по умолчанию | Описание |
|----------|----------------------|----------|
| `DownloadPath` | `/outgoing/cameratrap` | Куда сохранять загруженные фото |
| `CamerasCount` | `1` | Ожидаемое количество камер |
| `WifiPassword` | `12345678` | Пароль Wi-Fi камер |
| `WifiConnectTimeout` | `90` | Таймаут подключения к Wi-Fi (сек) |
| `WifiMaxRetries` | `5` | Попыток подключения к Wi-Fi |
| `WifiWaitAfterOpen` | `25` | Ожидание после BLE open перед Wi-Fi (сек) |
| `MaxRetriesPerCamera` | `3` | Попыток BLE open + Wi-Fi connect |
| `BleScanTimeout` | `10` | Таймаут BLE-сканирования (сек) |
| `BleCommandTimeout` | `10` | Таймаут BLE-подключения (сек) |
| `CameraCooldown` | `20` | Пауза между камерами (сек) |
| `CompressAfterDownload` | `true` | Сжимать JPG после загрузки |
| `CompressQuality` | `12` | Качество сжатия ffmpeg (-q:v, 1–31) |

Изменить параметр через БД:
```sql
INSERT INTO "PhotoTrapConfig" ("Key", "Value", "Description")
VALUES ('CamerasCount', '2', 'Количество фотоловушек')
ON CONFLICT ("Key") DO UPDATE SET "Value" = EXCLUDED."Value";
```

## Структура файлов

### Исходники (/scanner/)

```
/scanner/
├── calibration.py                     # Калибровка камер
├── kolka_snapshot_and_download.py     # Основной скрипт (снимок + загрузка)
├── config_loader.py                   # Загрузка конфигурации
├── compress_images.py                 # Сжатие изображений (ffmpeg)
├── models.py                          # Модели БД (SQLAlchemy)
├── appsettings.json                   # Конфигурация
├── requirements.txt                   # Зависимости Python
└── scripts/
    ├── install_snapshot_download.sh   # Установка сервиса
    ├── uninstall_snapshot_download.sh # Удаление сервиса
    ├── INSTRUCTIONS.md                # Этот файл
    ├── WORK.md                        # Принцип работы
    └── TECHNICAL_TASK.md              # Техническое задание
```

### Сервис (/opt/kolka_service_snapshot_download/)

```
/opt/kolka_service_snapshot_download/
├── kolka_snapshot_and_download.py
├── models.py
├── config_loader.py
├── compress_images.py
├── appsettings.json
├── requirements.txt
├── service.lock         # Файл-блокировка (создаётся при запуске)
├── venv/                # Виртуальное окружение
└── logs/                # Лог-файлы
```

## Требования

- Python 3.10+
- PostgreSQL (asyncpg)
- Bluetooth (bluez, libbluetooth-dev)
- NetworkManager (nmcli)
- ffmpeg (для сжатия изображений)


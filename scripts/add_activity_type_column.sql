-- Добавление поля ActivityType в таблицу SnapshotLog
-- Значения: 'photo' (снимки), 'download' (скачивание файлов)

-- 1. Добавляем колонку (nullable, чтобы сначала создать)
ALTER TABLE "SnapshotLog"
ADD COLUMN "ActivityType" varchar(20) NULL;

-- 2. Заполняем существующие записи на основе LogMessage
--    загрузка: LogMessage содержит "Wi-Fi reconnect" (этого нет в снимках)
UPDATE "SnapshotLog"
SET "ActivityType" = 'download'
WHERE "LogMessage" LIKE '%Wi-Fi reconnect%'
  AND "ActivityType" IS NULL;

--    фото: всё остальное (оставшиеся NULL)
UPDATE "SnapshotLog"
SET "ActivityType" = 'photo'
WHERE "ActivityType" IS NULL;

-- 4. Делаем колонку NOT NULL с дефолтом
ALTER TABLE "SnapshotLog"
ALTER COLUMN "ActivityType" SET NOT NULL,
ALTER COLUMN "ActivityType" SET DEFAULT 'photo';

-- 5. Индекс для фильтрации по типу активности
CREATE INDEX IF NOT EXISTS "idx_snaplog_activitytype" ON "SnapshotLog" ("ActivityType");

-- Проверка
SELECT "ActivityType", COUNT(*) AS cnt
FROM "SnapshotLog"
GROUP BY "ActivityType";

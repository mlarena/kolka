from sqlalchemy import Column, Integer, String, Text, DECIMAL, TIMESTAMP, BigInteger, Boolean, ForeignKey, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
from datetime import datetime

Base = declarative_base()

class PhotoTrap(Base):
    __tablename__ = 'PhotoTrap'

    Id = Column(Integer, primary_key=True, autoincrement=True)
    Name = Column(String(255), nullable=False)
    MacAddress = Column(String(50), unique=True)
    WifiSSID = Column(String(100))
    Description = Column(Text)
    Latitude = Column(DECIMAL(10, 8))
    Longitude = Column(DECIMAL(11, 8))
    IsActive = Column(Boolean, default=True, nullable=False)
    CreatedAt = Column(TIMESTAMP, server_default=func.now())
    UpdatedAt = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())


class CalibrationLog(Base):
    __tablename__ = 'CalibrationLog'

    Id = Column(Integer, primary_key=True, autoincrement=True)
    StartTime = Column(TIMESTAMP, nullable=False)
    EndTime = Column(TIMESTAMP)
    CamerasFound = Column(Integer, default=0)
    SsidsBound = Column(Integer, default=0)
    LogMessage = Column(Text)
    ErrorMessage = Column(Text)
    CreatedAt = Column(TIMESTAMP, server_default=func.now())

Index('idx_callog_starttime', CalibrationLog.StartTime)
Index('idx_callog_createdat', CalibrationLog.CreatedAt)


class SnapshotLog(Base):
    __tablename__ = 'SnapshotLog'

    Id = Column(Integer, primary_key=True, autoincrement=True)
    PhotoTrapId = Column(Integer, ForeignKey('PhotoTrap.Id', ondelete='CASCADE'), nullable=False)
    CycleNumber = Column(Integer)
    StartTime = Column(TIMESTAMP, nullable=False)
    EndTime = Column(TIMESTAMP)
    FileName = Column(String(255))
    Status = Column(String(20), default='PENDING')
    LogMessage = Column(Text)
    ErrorMessage = Column(Text)
    CreatedAt = Column(TIMESTAMP, server_default=func.now())

Index('idx_snaplog_phototrapid', SnapshotLog.PhotoTrapId)
Index('idx_snaplogstarttime', SnapshotLog.StartTime)
Index('idx_snaplog_status', SnapshotLog.Status)
Index('idx_snaplog_createdat', SnapshotLog.CreatedAt)


class DownloadLog(Base):
    __tablename__ = 'DownloadLog'

    Id = Column(Integer, primary_key=True, autoincrement=True)
    PhotoTrapId = Column(Integer, ForeignKey('PhotoTrap.Id', ondelete='CASCADE'), nullable=False)
    FileName = Column(String(255))
    FilePath = Column(String(500))
    FileSize = Column(BigInteger)
    TimeCode = Column(BigInteger)
    FileTime = Column(TIMESTAMP)
    IsSuccess = Column(Boolean, default=False)
    IsDeleted = Column(Boolean, default=False)
    IsSent = Column(Boolean, default=False)
    ErrorMessage = Column(Text)
    LocalPath = Column(String(500))
    DownloadedAt = Column(TIMESTAMP, server_default=func.now())

Index('idx_dllog_phototrapid', DownloadLog.PhotoTrapId)
Index('idx_dllog_downloadedat', DownloadLog.DownloadedAt)
Index('idx_dllog_issuccess', DownloadLog.IsSuccess)
Index('idx_dllog_filename', DownloadLog.FileName)
Index('idx_dllog_filetime', DownloadLog.FileTime)


class PhotoTrapConfig(Base):
    __tablename__ = 'PhotoTrapConfig'

    Id = Column(Integer, primary_key=True, autoincrement=True)
    Key = Column(String(100), unique=True, nullable=False)
    Value = Column(Text, nullable=False)
    Description = Column(Text)
    CreatedAt = Column(TIMESTAMP, server_default=func.now())
    UpdatedAt = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

Index('idx_config_key', PhotoTrapConfig.Key)

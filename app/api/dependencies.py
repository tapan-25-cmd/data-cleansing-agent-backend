from fastapi import Request

from app.repositories.mongo import MongoRepositories
from app.services.export_service import ExportService
from app.services.processor import JobProcessor
from app.storage.local import LocalFileStorage


def repositories(request: Request) -> MongoRepositories:
    return request.app.state.repositories


def storage(request: Request) -> LocalFileStorage:
    return request.app.state.storage


def processor(request: Request) -> JobProcessor:
    return request.app.state.processor


def exporter(request: Request) -> ExportService:
    return request.app.state.exporter

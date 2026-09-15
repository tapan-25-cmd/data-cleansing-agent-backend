from pathlib import Path
from shutil import copyfileobj
from typing import BinaryIO


class UploadTooLargeError(ValueError):
    pass


class LocalFileStorage:
    def __init__(self, root: Path, max_upload_bytes: int):
        self.root = root.resolve()
        self.max_upload_bytes = max_upload_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def _job_dir(self, job_id: str) -> Path:
        if not job_id or any(char not in "0123456789abcdef-" for char in job_id.lower()):
            raise ValueError("invalid job id")
        path = (self.root / job_id).resolve()
        if self.root not in path.parents:
            raise ValueError("unsafe job path")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_input(self, job_id: str, source: BinaryIO) -> str:
        destination = self._job_dir(job_id) / "input.xlsx"
        total = 0
        with destination.open("wb") as target:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > self.max_upload_bytes:
                    target.close()
                    destination.unlink(missing_ok=True)
                    raise UploadTooLargeError("upload exceeds configured size limit")
                target.write(chunk)
        return f"jobs/{job_id}/input.xlsx"

    def get_input_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "input.xlsx"

    def save_output(self, job_id: str, source_path: Path) -> str:
        destination = self._job_dir(job_id) / "output.xlsx"
        with source_path.open("rb") as source, destination.open("wb") as target:
            copyfileobj(source, target)
        return f"jobs/{job_id}/output.xlsx"

    def get_output_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "output.xlsx"

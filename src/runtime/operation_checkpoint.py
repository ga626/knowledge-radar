"""可恢复阶段快照；身份变化时作废，不保存账号凭据。"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import os
import time
import uuid
from runtime.file_write_lock import file_write_lock


class OperationCheckpoint:
    def __init__(self, root, request, identity):
        from generic_web.url_policy import public_evidence_payload
        self.key = hashlib.sha256(json.dumps(public_evidence_payload(request), sort_keys=True,
            ensure_ascii=False, default=str).encode()).hexdigest()
        self.identity = public_evidence_payload(identity)
        self.path = Path(root) / (self.key + ".json")
        self._revision = self._load().get("revision", 0)

    def _load(self):
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or type(data.get("revision", 0)) is not int or data.get("revision", 0) < 0:
                raise ValueError("invalid_checkpoint")
            return data
        except (ValueError, OSError):
            return {}

    def read(self):
        with file_write_lock(self.path):
            data = self._load()
            self._revision = data.get("revision", 0)
            if (data.get("identity") != self.identity or not isinstance(data.get("at"), (int, float))
                    or time.time() - data["at"] > 86400 or not isinstance(data.get("data"), dict)):
                return {}
            return data.get("data") or {}

    def write(self, data):
        from runtime.execution_context import check_execution
        check_execution()
        from generic_web.url_policy import public_evidence_payload
        with file_write_lock(self.path):
            if self._load().get("revision", 0) != self._revision:
                raise ValueError("checkpoint_revision_conflict")
            revision = self._revision + 1
            temporary = self.path.with_suffix("." + uuid.uuid4().hex + ".pending")
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump({"identity": self.identity, "at": time.time(), "revision": revision,
                        "data": public_evidence_payload(data)}, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                check_execution()
                temporary.replace(self.path)
                self._revision = revision
            finally:
                temporary.unlink(missing_ok=True)

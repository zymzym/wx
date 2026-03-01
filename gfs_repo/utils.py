from datetime import datetime, timezone
from pathlib import Path
from typing import Union
from .models import PartitionSpec

class GFSUtils:
    @staticmethod
    def get_partition_path(root_dir: Union[str, Path], spec: PartitionSpec, dataset: str = "raw") -> Path:
        """
        生成分区的绝对路径。
        Format: {root}/{dataset}/{model}/{grid}/{step}/{source}/init={init_time}
        """
        root = Path(root_dir)
        # R02: 唯一分区键 init_time
        return root / dataset / spec.signature.get_rel_path() / f"init={spec.init_time}"

    @staticmethod
    def get_utc_now() -> datetime:
        """获取当前 UTC 时间"""
        return datetime.now(timezone.utc)

    @staticmethod
    def get_utc_today() -> datetime:
        """获取当前 UTC 日期 (时间部分为 00:00:00)"""
        now = GFSUtils.get_utc_now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

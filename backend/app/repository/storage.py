import threading
from pathlib import Path
from typing import Dict, Any, List, Optional
from app.core.config import settings
from app.core.jsonio import read_json, write_json, write_text
from app.core.logger import logger
from app.model.domain import SelectionTask

class TaskRepository:
    """
    选品任务持久化仓储，保存至本地存储目录
    """

    def __init__(self, storage_dir: Optional[Path] = None):
        self._lock = threading.RLock()
        self.storage_dir = storage_dir or settings.STORAGE_DIR
        self.tasks_file = self.storage_dir / "tasks.json"
        self.reports_dir = self.storage_dir / "reports"
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        """
        初始化存储目录与初始文件
        """
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        if not self.tasks_file.exists():
            # 主文件不在、只剩 .bak：上次写入在两次 rename 之间被打断（断电 / 被杀进程）。
            # 这里必须先按 read_json 的规则恢复，绝不能直接写 {}——那样紧接着的第一次保存
            # 就会把唯一的完好备份覆盖成空，整份任务历史就真没了。
            recovered = read_json(self.tasks_file, default={})
            self._write_all_tasks(recovered if isinstance(recovered, dict) else {})

    def _read_all_tasks(self) -> Dict[str, Dict[str, Any]]:
        # strict：读不出来就报错，绝不能拿空字典回写、把全部历史覆盖掉
        data = read_json(self.tasks_file, default={}, strict=True)
        return data if isinstance(data, dict) else {}

    def _write_all_tasks(self, data: Dict[str, Dict[str, Any]]) -> None:
        try:
            write_json(self.tasks_file, data)
        except Exception as exc:
            # 不吞：吞掉的话接口照样返回成功，前端随后轮询 404，用户只看到任务凭空消失
            logger.error("写入任务文件异常: %s", str(exc))
            raise RuntimeError(f"保存任务失败（磁盘已满或目录不可写？）：{exc}") from exc

    def save_task(self, task: SelectionTask) -> None:
        with self._lock:
            tasks = self._read_all_tasks()
            tasks[task.task_id] = task.model_dump(mode="json")
            self._write_all_tasks(tasks)
        logger.info("已持久化选品任务: %s", task.task_id)

    def update_task(self, task_id: str, **fields: Any) -> Optional[SelectionTask]:
        """局部更新：后台运行过程每推进一步就调用，前端轮询即可看到实时进度。"""
        with self._lock:
            tasks = self._read_all_tasks()
            data = tasks.get(task_id)
            if not data:
                return None
            data.update(fields)
            task = SelectionTask(**data)
            tasks[task_id] = task.model_dump(mode="json")
            self._write_all_tasks(tasks)
            return task

    def get_task(self, task_id: str) -> Optional[SelectionTask]:
        data = self._read_all_tasks().get(task_id)
        if not data:
            return None
        try:
            return SelectionTask(**data)
        except Exception as exc:
            logger.error("任务数据损坏 %s: %s", task_id, exc)
            return None

    def list_tasks(self, limit: int = 50) -> List[SelectionTask]:
        task_list = []
        for v in self._read_all_tasks().values():
            try:
                task_list.append(SelectionTask(**v))
            except Exception:
                continue
        task_list.sort(key=lambda x: x.created_at, reverse=True)
        return task_list[:limit]

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            tasks = self._read_all_tasks()
            if task_id not in tasks:
                return False
            tasks.pop(task_id)
            self._write_all_tasks(tasks)
        return True

    def save_report_markdown(self, task_id: str, markdown_content: str) -> str:
        report_path = self.reports_dir / f"report_{task_id}.md"
        write_text(report_path, markdown_content)
        logger.info("已落盘选品报告: %s", report_path)
        return str(report_path)

task_repository = TaskRepository()

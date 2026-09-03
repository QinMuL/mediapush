"""统一媒体流水线包（方案二整合，替代原四段服务）。

service.PipelineService：目录A → TMDB 重命名 → B 资源库 → ed2k 哈希
→ 频道卡片 → CD2 串行上传 115 → 删源。单服务单轮询完成全部阶段，
仅保留 A 侧一处稳定判定；原 B→C 目录搬运与 pusher offset 状态机已消灭。
"""

from app.pipeline.service import PipelineReport, PipelineService

__all__ = ["PipelineReport", "PipelineService"]

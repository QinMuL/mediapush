"""流水线：扫描 → 解析 → TMDB 匹配 → 入库。"""
from app.pipeline.context import PipelineContext
from app.pipeline.pipeline import Pipeline

__all__ = ["PipelineContext", "Pipeline"]

"""CloudDrive2 gRPC 客户端（官方 clouddrive.proto 生成）。

clouddrive.proto import 了 google/protobuf/{timestamp,empty,descriptor}.proto。
protobuf 纯 Python 实现的默认描述符池不内置这些 WKT（upb 实现则内置），
必须先 import 对应 *_pb2 把它们注册进默认池，再加载生成的描述符，
否则报 "Depends on file 'google/protobuf/timestamp.proto', but it has not been loaded"。
"""

from google.protobuf import (  # noqa: F401  (副作用 import：注册 WKT 描述符)
    descriptor_pb2,
    empty_pb2,
    timestamp_pb2,
)

from . import clouddrive_pb2, clouddrive_pb2_grpc

__all__ = ["clouddrive_pb2", "clouddrive_pb2_grpc"]
